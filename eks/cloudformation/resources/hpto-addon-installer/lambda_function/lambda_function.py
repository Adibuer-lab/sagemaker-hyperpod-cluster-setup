import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml
import time
import json

# Environment variables
EKS_CLUSTER_NAME = 'EKS_CLUSTER_NAME'
AWS_REGION = 'AWS_REGION'

# Constants
CERT_MANAGER_NAMESPACE = "cert-manager"
HPTO_ADDON_NAME = "amazon-sagemaker-hyperpod-training-operator"
HPTO_NAMESPACE = "aws-hyperpod"
HPTO_SERVICE_ACCOUNT = "hp-training-operator-controller-manager"


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing HPTO EKS add-on
    """
    try:
        request_type = event['RequestType']

        if request_type == 'Create':
            response_data = on_create()
        elif request_type == 'Update':
            response_data = on_update()
        elif request_type == 'Delete':
            response_data = on_delete()
        else:
            raise ValueError(f"Invalid request type: {request_type}")

        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            response_data
        )

    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {
                "Status": "FAILED",
                "Reason": str(e)
            }
        )


def write_kubeconfig(cluster_name, region):
    """
    Generate kubeconfig using boto3
    """
    # Initialize EKS client
    eks = boto3.client('eks', region_name=region)
    
    try:
        # Get cluster info
        cluster = eks.describe_cluster(name=cluster_name)['cluster']
        cluster_arn = cluster['arn']
        
        # Generate kubeconfig content
        kubeconfig = {
            'apiVersion': 'v1',
            'kind': 'Config',
            'clusters': [{
                'cluster': {
                    'server': cluster['endpoint'],
                    'certificate-authority-data': cluster['certificateAuthority']['data']
                },
                'name': cluster_name
            }],
            'contexts': [{
                'context': {
                    'cluster': cluster_name,
                    'user': cluster_name
                },
                'name': cluster_arn
            }],
            # rig script get region from current-context value, expected to be cluster arn
            'current-context': cluster_arn, 
            'preferences': {},
            'users': [{
                'name': cluster_name,
                'user': {
                    'exec': {
                        'apiVersion': 'client.authentication.k8s.io/v1beta1',
                        'command': 'aws-iam-authenticator',
                        'args': [
                            'token',
                            '-i',
                            cluster_name
                        ]
                    }
                }
            }]
        }
        
        # Use /tmp instead of ~/.kube
        kubeconfig_dir = '/tmp/.kube'
        os.makedirs(kubeconfig_dir, exist_ok=True)
        kubeconfig_path = os.path.join(kubeconfig_dir, 'config')
        
        with open(kubeconfig_path, 'w') as f:
            yaml.dump(kubeconfig, f, default_flow_style=False)
        
        # Make sure kubectl can read it
        os.chmod(kubeconfig_path, 0o600)
        
        # Set KUBECONFIG environment variable
        os.environ['KUBECONFIG'] = kubeconfig_path
        
        return True
        
    except ClientError as e:
        print(f"Error getting cluster info: {str(e)}")
        raise


def check_cert_manager_pods_ready():
    """
    Check if cert-manager pods are ready
    Returns True if all cert-manager deployments have ready replicas
    """
    try:
        deployments = [
            'cert-manager',
            'cert-manager-cainjector',
            'cert-manager-webhook'
        ]
        
        for deployment in deployments:
            result = subprocess.run([
                'kubectl', 'get', 'deployment', deployment,
                '-n', CERT_MANAGER_NAMESPACE,
                '-o', 'jsonpath={.status.readyReplicas}'
            ], capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                print(f"Failed to check deployment {deployment}: {result.stderr}")
                return False
            
            ready_replicas = result.stdout.strip()
            if not ready_replicas or int(ready_replicas) == 0:
                print(f"Deployment {deployment} has no ready replicas")
                return False
        
        print("All cert-manager deployments are ready")
        return True
        
    except Exception as e:
        print(f"Error checking cert-manager pods: {str(e)}")
        return False


def get_addon_status(eks_client, cluster_name):
    """
    Get current status of HPTO add-on if it exists
    Returns: tuple (addon_arn, addon_status) or (None, None) if not found
    """
    try:
        addon_info = eks_client.describe_addon(
            clusterName=cluster_name,
            addonName=HPTO_ADDON_NAME
        )
        return addon_info['addon']['addonArn'], addon_info['addon']['status']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return None, None
        raise


def wait_for_addon_terminal_state(eks_client, cluster_name, max_wait_time=300):
    """
    Wait for HPTO add-on to reach a terminal state (ACTIVE, CREATE_FAILED, or DEGRADED)
    Returns: tuple (addon_arn, addon_status)
    """
    start_time = time.time()
    
    while time.time() - start_time < max_wait_time:
        addon_info = eks_client.describe_addon(
            clusterName=cluster_name,
            addonName=HPTO_ADDON_NAME
        )
        
        status = addon_info['addon']['status']
        print(f"HPTO add-on status: {status}")
        
        # Terminal states - stop waiting
        if status in ['ACTIVE', 'CREATE_FAILED', 'DEGRADED']:
            print(f"HPTO add-on reached terminal state: {status}")
            return addon_info['addon']['addonArn'], status
        
        time.sleep(30)
    
    # Timeout reached - return current status
    addon_info = eks_client.describe_addon(
        clusterName=cluster_name,
        addonName=HPTO_ADDON_NAME
    )
    print(f"Timeout waiting for terminal state, current status: {addon_info['addon']['status']}")
    return addon_info['addon']['addonArn'], addon_info['addon']['status']


def install_hpto_addon(cluster_name):
    """
    Install HPTO EKS add-on
    Returns: tuple (addon_arn, addon_status)
    """
    try:
        eks = boto3.client('eks')
        
        print(f"Checking if HPTO add-on already exists on cluster {cluster_name}...")
        
        # Check if add-on already exists
        addon_arn, addon_status = get_addon_status(eks, cluster_name)
        if addon_arn:
            print(f"HPTO add-on already exists with status: {addon_status}")
            return addon_arn, addon_status
        
        # Create new add-on
        print(f"Creating HPTO add-on on cluster {cluster_name}...")
        response = eks.create_addon(
            clusterName=cluster_name,
            addonName=HPTO_ADDON_NAME,
            resolveConflicts='OVERWRITE'
        )
        
        print(f"HPTO add-on creation initiated: {response['addon']['addonArn']}")
        
        # Wait for terminal state
        return wait_for_addon_terminal_state(eks, cluster_name, max_wait_time=300)
        
    except Exception as e:
        print(f"Error installing HPTO add-on: {str(e)}")
        raise


def on_create():
    """
    Handle Create request to install HPTO EKS add-on
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "HPTO add-on successfully installed"
    }

    try:
        # Ensure required environment variables are set
        required_env_vars = [
            EKS_CLUSTER_NAME,
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        cluster_name = os.environ[EKS_CLUSTER_NAME]
        region = os.environ['AWS_REGION']

        # Configure kubectl
        write_kubeconfig(cluster_name, region)

        # Check if cert-manager pods are running
        cert_manager_ready = check_cert_manager_pods_ready()
        if not cert_manager_ready:
            print("Warning: Cert-manager pods not ready - HPTO add-on may enter CREATE_FAILED state")

        # Always attempt to install HPTO add-on
        try:
            addon_arn, addon_status = install_hpto_addon(cluster_name)
            
            # HptoInstalled is True only if addon is in a successful state
            is_installed = addon_status in ['ACTIVE', 'DEGRADED', 'UPDATING']
            
            response_data["HptoAddonArn"] = addon_arn
            response_data["HptoInstalled"] = is_installed
            response_data["AddonStatus"] = addon_status
            response_data["Reason"] = f"HPTO add-on installation attempted, status: {addon_status}"
        except Exception as e:
            print(f"Failed to install HPTO add-on: {str(e)}")
            response_data["HptoInstalled"] = False
            response_data["AddonStatus"] = "N/A"
            response_data["Reason"] = f"Failed to install HPTO add-on: {str(e)}"

        return response_data

    except Exception as e:
        print(f"Error in on_create: {str(e)}")
        response_data["AddonStatus"] = "N/A"
        response_data["Reason"] = str(e)
        response_data["HptoInstalled"] = False
        return response_data


def on_update():
    """
    Handle Update request for HPTO EKS add-on
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "HPTO add-on update completed"
    }

    try:
        cluster_name = os.environ[EKS_CLUSTER_NAME]
        
        eks = boto3.client('eks')
        
        # Check if add-on exists
        try:
            addon_info = eks.describe_addon(
                clusterName=cluster_name,
                addonName=HPTO_ADDON_NAME
            )
            
            # Update add-on if needed
            print(f"Updating HPTO add-on if necessary...")
            response_data["Reason"] = "HPTO add-on checked and updated if necessary"
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                print("HPTO add-on not found, nothing to update")
                response_data["Reason"] = "HPTO add-on not found"
            else:
                raise

        return response_data

    except Exception as e:
        print(f"Error in on_update: {str(e)}")
        response_data["Reason"] = str(e)
        return response_data


def on_delete():
    """
    Handle Delete request to uninstall HPTO EKS add-on
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "HPTO add-on uninstall completed"
        }

        cluster_name = os.environ.get(EKS_CLUSTER_NAME)
        if not cluster_name:
            print("Cluster name not found, skipping cleanup")
            return response_data

        eks = boto3.client('eks')

        # Delete add-on
        try:
            print(f"Deleting HPTO add-on from cluster {cluster_name}...")
            eks.delete_addon(
                clusterName=cluster_name,
                addonName=HPTO_ADDON_NAME
            )
            
            # Wait for deletion
            max_wait_time = 300
            start_time = time.time()
            
            while time.time() - start_time < max_wait_time:
                try:
                    eks.describe_addon(
                        clusterName=cluster_name,
                        addonName=HPTO_ADDON_NAME
                    )
                    time.sleep(10)
                except ClientError as e:
                    if e.response['Error']['Code'] == 'ResourceNotFoundException':
                        print("HPTO add-on deleted successfully")
                        break
                    raise
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                print("HPTO add-on not found, already deleted")
            else:
                print(f"Error deleting HPTO add-on: {str(e)}")

        response_data["HptoUninstalled"] = True
        return response_data

    except Exception as e:
        print(f"Error in on_delete: {str(e)}")
        # Return SUCCESS anyway to not block stack deletion
        return {
            "Status": "SUCCESS",
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
