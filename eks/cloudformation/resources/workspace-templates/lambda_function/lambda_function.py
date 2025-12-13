import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing WorkspaceTemplates
    """
    try:
        request_type = event['RequestType']

        if request_type == 'Create':
            response_data = on_create(event)
        elif request_type == 'Update':
            response_data = on_update(event)
        elif request_type == 'Delete':
            response_data = on_delete(event)
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
                'name': cluster_name
            }],
            'current-context': cluster_name,
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


def run_kubectl(args):
    """Execute kubectl command"""
    cmd = ['kubectl'] + args
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"stdout: {result.stdout}")
    print(f"stderr: {result.stderr}")
    if result.returncode != 0:
        raise Exception(f"kubectl failed: {result.stderr}")
    return result.stdout


def apply_manifest(manifest_yaml):
    """Apply a Kubernetes manifest"""
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(manifest_yaml)
        f.flush()
        try:
            return run_kubectl(['apply', '-f', f.name])
        finally:
            os.unlink(f.name)


def delete_manifest(name, namespace='jupyter-k8s-system'):
    """Delete a WorkspaceTemplate"""
    try:
        run_kubectl(['delete', 'workspacetemplate', name, '-n', namespace, '--ignore-not-found'])
    except Exception as e:
        print(f"Warning deleting {name}: {e}")


def get_templates():
    """Return all WorkspaceTemplate manifests"""
    return {
        'jupyter-cpu-template': '''
apiVersion: workspace.jupyter.org/v1alpha1
kind: WorkspaceTemplate
metadata:
  name: jupyter-cpu-template
  namespace: jupyter-k8s-system
spec:
  displayName: "JupyterLab (CPU)"
  description: "CPU workspace for code development and data exploration"
  appType: jupyterlab
  defaultImage: public.ecr.aws/sagemaker/sagemaker-distribution:latest-cpu
  allowedImages:
    - public.ecr.aws/sagemaker/sagemaker-distribution:latest-cpu
  allowCustomImages: false
  defaultResources:
    requests:
      cpu: "2"
      memory: 8Gi
    limits:
      cpu: "4"
      memory: 16Gi
  resourceBounds:
    resources:
      cpu:
        min: "1"
        max: "8"
      memory:
        min: 4Gi
        max: 32Gi
  defaultNodeSelector:
    node-role: workspace-cpu
  primaryStorage:
    defaultSize: 10Gi
    minSize: 5Gi
    maxSize: 50Gi
    defaultStorageClassName: sagemaker-spaces-default-storage-class
  allowSecondaryStorages: true
  defaultAccessType: Public
  defaultOwnershipType: Public
  defaultPodSecurityContext:
    fsGroup: 1000
''',
        'jupyter-gpu-template': '''
apiVersion: workspace.jupyter.org/v1alpha1
kind: WorkspaceTemplate
metadata:
  name: jupyter-gpu-template
  namespace: jupyter-k8s-system
spec:
  displayName: "JupyterLab (GPU)"
  description: "GPU workspace for ML development and debugging"
  appType: jupyterlab
  defaultImage: public.ecr.aws/sagemaker/sagemaker-distribution:latest-gpu
  allowedImages:
    - public.ecr.aws/sagemaker/sagemaker-distribution:latest-gpu
  allowCustomImages: false
  defaultResources:
    requests:
      cpu: "4"
      memory: 16Gi
      nvidia.com/gpu: "1"
    limits:
      cpu: "8"
      memory: 32Gi
      nvidia.com/gpu: "1"
  resourceBounds:
    resources:
      cpu:
        min: "2"
        max: "16"
      memory:
        min: 8Gi
        max: 64Gi
  defaultNodeSelector:
    node-role: workspace-gpu
  primaryStorage:
    defaultSize: 20Gi
    minSize: 10Gi
    maxSize: 100Gi
    defaultStorageClassName: sagemaker-spaces-default-storage-class
  allowSecondaryStorages: true
  defaultAccessType: Public
  defaultOwnershipType: Public
  defaultPodSecurityContext:
    fsGroup: 1000
''',
        'code-editor-cpu-template': '''
apiVersion: workspace.jupyter.org/v1alpha1
kind: WorkspaceTemplate
metadata:
  name: code-editor-cpu-template
  namespace: jupyter-k8s-system
spec:
  displayName: "Code Editor (CPU)"
  description: "VS Code-based CPU workspace for development"
  appType: code-editor
  defaultImage: public.ecr.aws/sagemaker/sagemaker-distribution:latest-cpu
  allowedImages:
    - public.ecr.aws/sagemaker/sagemaker-distribution:latest-cpu
  allowCustomImages: false
  defaultResources:
    requests:
      cpu: "2"
      memory: 8Gi
    limits:
      cpu: "4"
      memory: 16Gi
  resourceBounds:
    resources:
      cpu:
        min: "1"
        max: "8"
      memory:
        min: 4Gi
        max: 32Gi
  defaultNodeSelector:
    node-role: workspace-cpu
  primaryStorage:
    defaultSize: 10Gi
    minSize: 5Gi
    maxSize: 50Gi
    defaultStorageClassName: sagemaker-spaces-default-storage-class
  allowSecondaryStorages: true
  defaultAccessType: Public
  defaultOwnershipType: Public
  defaultPodSecurityContext:
    fsGroup: 1000
''',
        'code-editor-gpu-template': '''
apiVersion: workspace.jupyter.org/v1alpha1
kind: WorkspaceTemplate
metadata:
  name: code-editor-gpu-template
  namespace: jupyter-k8s-system
spec:
  displayName: "Code Editor (GPU)"
  description: "VS Code-based GPU workspace for ML development"
  appType: code-editor
  defaultImage: public.ecr.aws/sagemaker/sagemaker-distribution:latest-gpu
  allowedImages:
    - public.ecr.aws/sagemaker/sagemaker-distribution:latest-gpu
  allowCustomImages: false
  defaultResources:
    requests:
      cpu: "4"
      memory: 16Gi
      nvidia.com/gpu: "1"
    limits:
      cpu: "8"
      memory: 32Gi
      nvidia.com/gpu: "1"
  resourceBounds:
    resources:
      cpu:
        min: "2"
        max: "16"
      memory:
        min: 8Gi
        max: 64Gi
  defaultNodeSelector:
    node-role: workspace-gpu
  primaryStorage:
    defaultSize: 20Gi
    minSize: 10Gi
    maxSize: 100Gi
    defaultStorageClassName: sagemaker-spaces-default-storage-class
  allowSecondaryStorages: true
  defaultAccessType: Public
  defaultOwnershipType: Public
  defaultPodSecurityContext:
    fsGroup: 1000
'''
    }


def on_create(event):
    """Handle Create request"""
    cluster_name = os.environ['CLUSTER_NAME']
    region = os.environ.get('AWS_REGION', 'us-east-1')

    write_kubeconfig(cluster_name, region)

    templates = get_templates()
    for name, manifest in templates.items():
        print(f"Creating template: {name}")
        apply_manifest(manifest)

    return {
        'JupyterCPUTemplate': 'jupyter-cpu-template',
        'JupyterGPUTemplate': 'jupyter-gpu-template',
        'CodeEditorCPUTemplate': 'code-editor-cpu-template',
        'CodeEditorGPUTemplate': 'code-editor-gpu-template'
    }


def on_update(event):
    """Handle Update request"""
    return on_create(event)


def on_delete(event):
    """Handle Delete request"""
    cluster_name = os.environ['CLUSTER_NAME']
    region = os.environ.get('AWS_REGION', 'us-east-1')

    try:
        write_kubeconfig(cluster_name, region)
        for name in get_templates().keys():
            print(f"Deleting template: {name}")
            delete_manifest(name)
    except Exception as e:
        print(f"Warning during delete: {e}")

    return {}
