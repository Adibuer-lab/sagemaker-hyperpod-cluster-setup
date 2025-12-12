import boto3
import subprocess
import json
import os
import cfnresponse
import tempfile

def run_kubectl(cmd):
    """Execute kubectl command and return result"""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(f"Command: {cmd}")
    print(f"stdout: {result.stdout}")
    print(f"stderr: {result.stderr}")
    return result

def write_kubeconfig(cluster_name):
    """Generate kubeconfig for EKS cluster"""
    region = os.environ.get('AWS_REGION', 'us-east-1')
    result = run_kubectl(f"aws eks update-kubeconfig --name {cluster_name} --kubeconfig /tmp/.kube/config --region {region}")
    if result.returncode != 0:
        raise Exception(f"Failed to update kubeconfig: {result.stderr}")

def get_cpu_template():
    """Return CPU WorkspaceTemplate manifest"""
    return '''
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
'''

def get_gpu_template():
    """Return GPU WorkspaceTemplate manifest"""
    return '''
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
'''

def apply_template(name, manifest):
    """Apply a WorkspaceTemplate manifest"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(manifest)
        f.flush()
        result = run_kubectl(f"kubectl apply -f {f.name}")
        os.unlink(f.name)
        if result.returncode != 0:
            raise Exception(f"Failed to apply {name} template: {result.stderr}")
    return True

def delete_template(name):
    """Delete a WorkspaceTemplate"""
    result = run_kubectl(f"kubectl delete workspacetemplate {name} -n jupyter-k8s-system --ignore-not-found")
    return result.returncode == 0

def lambda_handler(event, context):
    """Handle CloudFormation custom resource requests"""
    print(f"Event: {json.dumps(event)}")
    
    try:
        cluster_name = os.environ['CLUSTER_NAME']
        write_kubeconfig(cluster_name)
        
        if event['RequestType'] == 'Delete':
            delete_template('jupyter-cpu-template')
            delete_template('jupyter-gpu-template')
            delete_template('code-editor-cpu-template')
            delete_template('code-editor-gpu-template')
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return
        
        # Create or Update - JupyterLab and Code Editor templates
        apply_template('jupyter-cpu', get_cpu_template())
        apply_template('jupyter-gpu', get_gpu_template())
        apply_template('code-editor-cpu', get_code_editor_cpu_template())
        apply_template('code-editor-gpu', get_code_editor_gpu_template())
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'JupyterCPUTemplate': 'jupyter-cpu-template',
            'JupyterGPUTemplate': 'jupyter-gpu-template',
            'CodeEditorCPUTemplate': 'code-editor-cpu-template',
            'CodeEditorGPUTemplate': 'code-editor-gpu-template'
        })
    except Exception as e:
        print(f"Error: {e}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
