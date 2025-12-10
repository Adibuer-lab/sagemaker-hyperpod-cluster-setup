import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for restarting CoreDNS
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

        cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data)

    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Status": "FAILED", "Reason": str(e)})


def write_kubeconfig(cluster_name, region):
    """
    Generate kubeconfig using boto3
    """
    eks = boto3.client('eks', region_name=region)

    try:
        cluster = eks.describe_cluster(name=cluster_name)['cluster']

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
                'name': cluster['arn']
            }],
            'current-context': cluster['arn'],
            'preferences': {},
            'users': [{
                'name': cluster_name,
                'user': {
                    'exec': {
                        'apiVersion': 'client.authentication.k8s.io/v1beta1',
                        'command': 'aws-iam-authenticator',
                        'args': ['token', '-i', cluster_name]
                    }
                }
            }]
        }

        kubeconfig_dir = '/tmp/.kube'
        os.makedirs(kubeconfig_dir, exist_ok=True)
        kubeconfig_path = os.path.join(kubeconfig_dir, 'config')

        with open(kubeconfig_path, 'w') as f:
            yaml.dump(kubeconfig, f, default_flow_style=False)

        os.chmod(kubeconfig_path, 0o600)
        os.environ['KUBECONFIG'] = kubeconfig_path

        return True

    except ClientError as e:
        print(f"Error getting cluster info: {str(e)}")
        raise


def patch_coredns_for_fargate():
    """
    Patch CoreDNS deployment with Fargate compute-type annotation
    """
    try:
        print("Patching CoreDNS deployment with Fargate compute-type annotation...")

        result = subprocess.run([
            'kubectl', 'patch', 'deployment', 'coredns', '-n', 'kube-system',
            '-p', '{"spec":{"template":{"metadata":{"annotations":{"eks.amazonaws.com/compute-type":"fargate"}}}}}'
        ], check=True, capture_output=True, text=True)
        print(f"Patch output: {result.stdout}")

        return True

    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to patch CoreDNS: {e.cmd}. Return code: {e.returncode}. Stderr: {e.stderr}")


def wait_for_coredns_rollout():
    """
    Wait for CoreDNS deployment rollout to complete
    """
    try:
        print("Waiting for CoreDNS rollout to complete...")

        result = subprocess.run([
            'kubectl', 'rollout', 'status', 'deployment/coredns', '-n', 'kube-system', '--timeout=540s'
        ], check=True, capture_output=True, text=True)
        print(f"Status output: {result.stdout}")

        print("CoreDNS rollout completed successfully")
        return True

    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed waiting for CoreDNS rollout: {e.cmd}. Return code: {e.returncode}. Stderr: {e.stderr}")


def on_create():
    """
    Handle Create request - patch CoreDNS for Fargate and wait for rollout
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "CoreDNS patched for Fargate successfully"
    }

    cluster_name = os.environ['CLUSTER_NAME']
    region = os.environ['AWS_REGION']

    write_kubeconfig(cluster_name, region)
    patch_coredns_for_fargate()
    wait_for_coredns_rollout()

    response_data["CoreDNSPatched"] = True
    return response_data


def on_update():
    """
    Handle Update request - patch CoreDNS again
    """
    return on_create()


def on_delete():
    """
    Handle Delete request - nothing to do
    """
    return {
        "Status": "SUCCESS",
        "Reason": "Delete completed (no action needed)"
    }
