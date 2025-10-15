import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml
import time

# Environment variables
HYPERPOD_CLI_GITHUB_REPO_URL = 'HYPERPOD_CLI_GITHUB_REPO_URL'
HYPERPOD_CLI_GITHUB_REPO_REVISION = 'HYPERPOD_CLI_GITHUB_REPO_REVISION'
EKS_CLUSTER_NAME = 'EKS_CLUSTER_NAME'
AWS_REGION = 'AWS_REGION'
CHART_PATH = 'helm_chart/HyperPodHelmChart'
CHART_LOCAL_PATH = '/tmp/hyperpod-helm-charts'

# Namespace for cert-manager
CERT_MANAGER_NAMESPACE = "cert-manager"
RELEASE_NAME = 'cert-manager'


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing cert-manager via HyperPod Helm Chart
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


def check_cert_manager_exists():
    """
    Cert-manager existence detection based on deployments using labels
    """
    try:
        result = subprocess.run([
            'kubectl', 'get', 'deployments', '-n', 'cert-manager',
            '-l', 'app.kubernetes.io/name=cert-manager',
            '-o', 'jsonpath={.items[*].status.readyReplicas}'
        ], capture_output=True, text=True, timeout=5)
        
        if result.returncode == 0 and result.stdout.strip():
            ready_replicas = result.stdout.strip().split()
            total_ready = sum(int(r) for r in ready_replicas if r.isdigit())
            if total_ready > 0:
                print(f"cert-manager found with {total_ready} ready replicas")
                return True
        
        return False
        
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"Error checking cert-manager existence: {e}")
        return False


def install_cert_manager():
    """
    Install cert-manager using the HyperPod Helm Chart with only cert-manager enabled
    """
    try:
        print("Installing cert-manager via HyperPod Helm Chart...")
        
        # Ensure required environment variables are set
        required_env_vars = [
            HYPERPOD_CLI_GITHUB_REPO_URL,
            HYPERPOD_CLI_GITHUB_REPO_REVISION,
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
        
        # Clone the GitHub repository
        clone_cmd = ['git', 'clone', os.environ[HYPERPOD_CLI_GITHUB_REPO_URL], CHART_LOCAL_PATH]
        subprocess.run(clone_cmd, check=True)

        # Checkout specific revision
        subprocess.run(['git', '-C', CHART_LOCAL_PATH, 'checkout', os.environ[HYPERPOD_CLI_GITHUB_REPO_REVISION]], check=True)

        # Update dependencies to download cert-manager chart
        subprocess.run(['helm', 'dependency', 'update', f"{CHART_LOCAL_PATH}/{CHART_PATH}"], check=True)

        # Create cert-manager namespace
        create_namespace(CERT_MANAGER_NAMESPACE)

        # Install only cert-manager from the HyperPod Helm Chart
        # We disable all other components and only enable cert-manager
        install_cmd = [
            'helm', 'install',
            RELEASE_NAME,
            f'{CHART_LOCAL_PATH}/{CHART_PATH}',
            '--namespace', CERT_MANAGER_NAMESPACE,
            '--set', 'cert-manager.enabled=true',
            # Disable all other components
            '--set', 'trainingOperators.enabled=false',
            '--set', 'mlflow.enabled=false',
            '--set', 'nvidia-device-plugin.devicePlugin.enabled=false',
            '--set', 'aws-efa-k8s-device-plugin.devicePlugin.enabled=false',
            '--set', 'neuron-device-plugin.devicePlugin.enabled=false',
            '--set', 'storage.enabled=false',
            '--set', 'health-monitoring-agent.enabled=false',
            '--set', 'mpi-operator.enabled=false',
            '--set', 'deep-health-check.enabled=false',
            '--set', 'job-auto-restart.enabled=false',
            '--set', 'cluster-role-and-bindings.enabled=false',
            '--set', 'namespaced-role-and-bindings.enabled=false',
            '--set', 'team-role-and-bindings.enabled=false',
            '--set', 'inferenceOperators.enabled=false',
            '--set', 'hyperpod-patching.enabled=false'
        ]

        # Execute the Helm install
        subprocess.run(install_cmd, check=True)

        # Wait for cert-manager to be ready
        wait_for_cert_manager_ready()

        # Clean up cloned repository
        subprocess.run(['rm', '-rf', CHART_LOCAL_PATH], check=True)
        
        print("cert-manager installed successfully via HyperPod Helm Chart")
        return True
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to install cert-manager via HyperPod Helm Chart: {e.cmd}. Return code: {e.returncode}")


def wait_for_cert_manager_ready():
    """
    Wait for cert-manager deployments to be ready
    """
    try:
        print("Waiting for cert-manager deployments to be ready...")
        
        deployments = [
            RELEASE_NAME,
            f'{RELEASE_NAME}-cainjector',
            f'{RELEASE_NAME}-webhook'
        ]
        
        for deployment in deployments:
            wait_cmd = [
                'kubectl', 'wait', '--for=condition=available',
                f'deployment/{deployment}',
                '-n', CERT_MANAGER_NAMESPACE,
                '--timeout=300s'
            ]
            subprocess.run(wait_cmd, check=True)
            print(f"Deployment {deployment} is ready")
        
        print("All cert-manager deployments are ready")
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"cert-manager deployments failed to become ready: {e}")


def create_namespace(namespace):
    """
    Create a Kubernetes namespace if it doesn't exist
    """
    try:
        subprocess.run(
            ["kubectl", "create", "namespace", namespace],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        print(f"Namespace '{namespace}' created.")
    except subprocess.CalledProcessError as e:
        if "AlreadyExists" in e.stderr:
            print(f"Namespace '{namespace}' already exists. Skipping.")
        else:
            print(f"Failed to create namespace {namespace}: {str(e)}")
            raise


def on_create():
    """
    Handle Create request to install cert-manager via HyperPod Helm Chart
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "cert-manager installation completed successfully"
    }

    try:
        # Ensure required environment variables are set
        required_env_vars = [
            'EKS_CLUSTER_NAME',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Set HELM_CACHE_HOME and HELM_CONFIG_HOME
        os.environ['HELM_CACHE_HOME'] = '/tmp/.helm/cache'
        os.environ['HELM_CONFIG_HOME'] = '/tmp/.helm/config'
        
        # Create directories
        os.makedirs('/tmp/.helm/cache', exist_ok=True)
        os.makedirs('/tmp/.helm/config', exist_ok=True)

        # Configure kubectl using boto3
        write_kubeconfig(os.environ[EKS_CLUSTER_NAME], os.environ['AWS_REGION'])

        # Check if cert-manager already exists
        if check_cert_manager_exists():
            print("cert-manager already exists, skipping installation")
            response_data["CertManagerInstalled"] = False
            response_data["CertManagerExists"] = True
            response_data["Reason"] = "cert-manager already exists, skipped installation"
        else:
            # Install cert-manager via HyperPod Helm Chart
            install_cert_manager()
            response_data["CertManagerInstalled"] = True
            response_data["CertManagerExists"] = False
            response_data["Reason"] = "cert-manager installed successfully via HyperPod Helm Chart"

        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to handle cert-manager: {str(e)}")


def update_cert_manager():
    """
    Update cert-manager using the HyperPod Helm Chart with only cert-manager enabled
    """
    try:
        print("Updating cert-manager via HyperPod Helm Chart...")
        
        # Ensure required environment variables are set
        required_env_vars = [
            HYPERPOD_CLI_GITHUB_REPO_URL,
            HYPERPOD_CLI_GITHUB_REPO_REVISION,
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
        
        # Clone the updated GitHub repository
        clone_cmd = ['git', 'clone', os.environ[HYPERPOD_CLI_GITHUB_REPO_URL], CHART_LOCAL_PATH]
        subprocess.run(clone_cmd, check=True)

        # Checkout specific revision
        subprocess.run(['git', '-C', CHART_LOCAL_PATH, 'checkout', os.environ[HYPERPOD_CLI_GITHUB_REPO_REVISION]], check=True)

        # Update dependencies to download updated cert-manager chart
        subprocess.run(['helm', 'dependency', 'update', f"{CHART_LOCAL_PATH}/{CHART_PATH}"], check=True)

        # Upgrade cert-manager using helm upgrade --install
        # This will update existing installation or install if not present
        upgrade_cmd = [
            'helm', 'upgrade', '--install',
            RELEASE_NAME,
            f'{CHART_LOCAL_PATH}/{CHART_PATH}',
            '--namespace', CERT_MANAGER_NAMESPACE,
            '--set', 'cert-manager.enabled=true',
            # Disable all other components
            '--set', 'trainingOperators.enabled=false',
            '--set', 'mlflow.enabled=false',
            '--set', 'nvidia-device-plugin.devicePlugin.enabled=false',
            '--set', 'aws-efa-k8s-device-plugin.devicePlugin.enabled=false',
            '--set', 'neuron-device-plugin.devicePlugin.enabled=false',
            '--set', 'storage.enabled=false',
            '--set', 'health-monitoring-agent.enabled=false',
            '--set', 'mpi-operator.enabled=false',
            '--set', 'deep-health-check.enabled=false',
            '--set', 'job-auto-restart.enabled=false',
            '--set', 'cluster-role-and-bindings.enabled=false',
            '--set', 'namespaced-role-and-bindings.enabled=false',
            '--set', 'team-role-and-bindings.enabled=false',
            '--set', 'inferenceOperators.enabled=false',
            '--set', 'hyperpod-patching.enabled=false'
        ]

        # Execute the Helm upgrade
        subprocess.run(upgrade_cmd, check=True)

        # Wait for cert-manager to be ready after update
        wait_for_cert_manager_ready()

        # Clean up cloned repository
        subprocess.run(['rm', '-rf', CHART_LOCAL_PATH], check=True)
        
        print("cert-manager updated successfully via HyperPod Helm Chart")
        return True
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to update cert-manager via HyperPod Helm Chart: {e.cmd}. Return code: {e.returncode}")


def on_update():
    """
    Handle Update request to upgrade existing cert-manager installation
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "cert-manager update completed successfully"
    }

    try:
        # Ensure required environment variables are set
        required_env_vars = [
            'EKS_CLUSTER_NAME',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Set HELM_CACHE_HOME and HELM_CONFIG_HOME
        os.environ['HELM_CACHE_HOME'] = '/tmp/.helm/cache'
        os.environ['HELM_CONFIG_HOME'] = '/tmp/.helm/config'
        
        # Create directories
        os.makedirs('/tmp/.helm/cache', exist_ok=True)
        os.makedirs('/tmp/.helm/config', exist_ok=True)

        # Configure kubectl using boto3
        write_kubeconfig(os.environ[EKS_CLUSTER_NAME], os.environ['AWS_REGION'])

        # Check if cert-manager exists before updating
        if check_cert_manager_exists():
            # Update cert-manager via HyperPod Helm Chart
            update_cert_manager()
            response_data["CertManagerUpdated"] = True
            response_data["Reason"] = "cert-manager updated successfully via HyperPod Helm Chart"
        else:
            # If cert-manager doesn't exist, install it (upgrade --install handles this)
            update_cert_manager()
            response_data["CertManagerUpdated"] = True
            response_data["Reason"] = "cert-manager installed successfully via HyperPod Helm Chart (was not present)"

        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to update cert-manager: {str(e)}")


def on_delete():
    """
    Handle Delete request to uninstall cert-manager helm release
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "cert-manager uninstall completed successfully"
        }

        # Ensure required environment variables are set
        required_env_vars = [
            'EKS_CLUSTER_NAME',
            'AWS_REGION'
        ]

        for var in required_env_vars:
            if var not in os.environ:
                print(f"Warning: Missing environment variable {var}, skipping cleanup")
                return response_data

        try:
            # Set HELM_CACHE_HOME and HELM_CONFIG_HOME
            os.environ['HELM_CACHE_HOME'] = '/tmp/.helm/cache'
            os.environ['HELM_CONFIG_HOME'] = '/tmp/.helm/config'
            
            # Create directories
            os.makedirs('/tmp/.helm/cache', exist_ok=True)
            os.makedirs('/tmp/.helm/config', exist_ok=True)

            # Configure kubectl using boto3
            write_kubeconfig(os.environ[EKS_CLUSTER_NAME], os.environ['AWS_REGION'])
        except Exception as e:
            print(f"Warning: Failed to configure kubectl/helm, cluster may already be deleted: {str(e)}")
            return response_data

        # Check if our Helm release exists and uninstall it
        try:
            print(f"Checking for Helm release: {RELEASE_NAME}")
            
            # Check if the release exists
            list_cmd = ['helm', 'list', '-n', CERT_MANAGER_NAMESPACE, '-q']
            result = subprocess.run(list_cmd, check=True, capture_output=True, text=True)
            releases = result.stdout.strip().split('\n') if result.stdout.strip() else []
            
            if RELEASE_NAME in releases:
                # Uninstall the Helm release
                uninstall_cmd = [
                    'helm', 'uninstall', RELEASE_NAME,
                    '--namespace', CERT_MANAGER_NAMESPACE,
                    '--wait'
                ]
                subprocess.run(uninstall_cmd, check=True, capture_output=True, text=True)
                print(f"Successfully uninstalled Helm release: {RELEASE_NAME}")

                # Sleep time to allow for resources to be deleted before checking namespace
                time.sleep(10)
                
                # Check if namespace is empty and delete it
                check_cmd = ['kubectl', 'get', 'all', '-n', CERT_MANAGER_NAMESPACE, '--no-headers']
                result = subprocess.run(check_cmd, capture_output=True, text=True)
                
                if result.returncode == 0 and not result.stdout.strip():
                    delete_ns_cmd = ['kubectl', 'delete', 'namespace', CERT_MANAGER_NAMESPACE, '--ignore-not-found=true']
                    subprocess.run(delete_ns_cmd, check=True, capture_output=True, text=True)
                    print(f"Successfully deleted namespace: {CERT_MANAGER_NAMESPACE}")
                else:
                    print(f"Namespace {CERT_MANAGER_NAMESPACE} contains resources, skipping deletion")
            else:
                print(f"Helm release {RELEASE_NAME} not found, skipping uninstall")
                
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to uninstall Helm release {RELEASE_NAME}: {e}")

        # Clean up any temporary files
        try:
            if os.path.exists(CHART_LOCAL_PATH):
                subprocess.run(['rm', '-rf', CHART_LOCAL_PATH], check=True)
                print("Cleaned up temporary chart files")
        except Exception as e:
            print(f"Warning: Failed to clean up temporary files: {str(e)}")

        response_data["CertManagerUninstalled"] = True
        return response_data

    except Exception as e:
        # For delete operations, we generally want to succeed even if cleanup fails
        # to avoid blocking stack deletion
        print(f"Warning: Error during cert-manager uninstallation: {str(e)}")
        return {
            "Status": "SUCCESS", 
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
