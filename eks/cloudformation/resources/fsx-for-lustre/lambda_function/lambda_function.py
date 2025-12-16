import boto3
import json
import os
import re
import subprocess
import time
import uuid
import cfnresponse
from botocore.exceptions import ClientError
import yaml

FSX_BOOTSTRAP_IMAGE = os.environ.get("FSX_BOOTSTRAP_IMAGE", "public.ecr.aws/docker/library/busybox:1.36")
KUBECTL_REQUEST_TIMEOUT = os.environ.get("KUBECTL_REQUEST_TIMEOUT", "20s")


def _kubectl(args, *, input_text=None, check=True, timeout_seconds=60):
    cmd = ["kubectl"] + list(args)
    if "--request-timeout" not in cmd:
        cmd.append(f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}")
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout_seconds,
    )


def _sanitize_k8s_name(value: str, max_length: int = 63) -> str:
    """
    Kubernetes object names must be DNS-1123 labels: lower-case alphanumerics and '-'.
    """
    s = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "fsx-bootstrap"
    return s[:max_length].rstrip("-")


def _wait_pvc_bound(namespace: str, pvc_name: str, timeout_seconds: int = 300) -> None:
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            phase = _kubectl(
                ["get", "pvc", pvc_name, "-n", namespace, "-ojsonpath={.status.phase}"],
                timeout_seconds=20,
            ).stdout.strip()
            if phase == "Bound":
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for PVC {namespace}/{pvc_name} to reach Bound")


def _wait_for_kubectl_access(timeout_seconds: int = 180) -> None:
    """
    Wait until kubectl can successfully talk to the cluster.

    This helps when the IAM AccessEntry exists but hasn't fully propagated yet.
    """
    start = time.time()
    last_error = None
    while time.time() - start < timeout_seconds:
        proc = _kubectl(["get", "ns"], check=False, timeout_seconds=20)
        if proc.returncode == 0:
            return
        last_error = (proc.stderr or proc.stdout or "").strip()
        if last_error:
            print(f"Waiting for kubectl access: {last_error}")
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for kubectl access: {last_error or 'unknown error'}")


def _node_is_ready(node_obj: dict) -> bool:
    for cond in (node_obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready" and cond.get("status") == "True":
            return True
    return False


def _node_is_fargate(node_obj: dict) -> bool:
    labels = (node_obj.get("metadata") or {}).get("labels") or {}
    if labels.get("eks.amazonaws.com/compute-type") == "fargate":
        return True
    for taint in (node_obj.get("spec") or {}).get("taints") or []:
        if taint.get("key") == "eks.amazonaws.com/compute-type" and taint.get("value") == "fargate":
            return True
    return False


def _wait_for_ready_ec2_node(node_selector: dict | None = None, timeout_seconds: int = 900) -> None:
    """
    Wait for at least one Ready, non-Fargate node matching the selector.
    """
    label_selector = ""
    if node_selector:
        label_selector = ",".join([f"{k}={v}" for k, v in node_selector.items() if v is not None and v != ""])
    args = ["get", "nodes"]
    if label_selector:
        args += ["-l", label_selector]
    args += ["-o", "json"]

    start = time.time()
    while time.time() - start < timeout_seconds:
        proc = _kubectl(args, check=False, timeout_seconds=20)
        if proc.returncode != 0:
            time.sleep(10)
            continue
        try:
            nodes_obj = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            time.sleep(10)
            continue

        nodes = nodes_obj.get("items") or []
        ready_ec2 = [n for n in nodes if _node_is_ready(n) and not _node_is_fargate(n)]
        if ready_ec2:
            print(
                f"Found {len(ready_ec2)} Ready EC2 node(s)"
                + (f" for selector '{label_selector}'." if label_selector else ".")
            )
            return
        time.sleep(10)

    raise TimeoutError(
        "Timed out waiting for a Ready EC2 node"
        + (f" matching selector '{label_selector}'." if label_selector else ".")
    )


def _bootstrap_fsx_writable_subdir(fsx_file_system_id: str, *, node_selector: dict | None = None) -> str:
    """
    Create a stable, writable directory in the FSx root so SageMaker Spaces can mount it.

    Returns the file system path (e.g., '/fs-0123...') that should be used as FileSystemPath.
    """
    fs_id = (fsx_file_system_id or "").strip()
    if not fs_id:
        return ""

    user_namespaces = [ns.strip() for ns in os.environ.get("USER_NAMESPACES", "default").split(",") if ns.strip()]
    if not user_namespaces:
        user_namespaces = ["default"]

    namespace = user_namespaces[0]
    pvc_name = "fsx-claim"
    target_subdir = fs_id
    target_path = f"/{target_subdir}"

    print(f"Bootstrapping writable FSx directory {target_path} using PVC {namespace}/{pvc_name}...")
    _wait_pvc_bound(namespace, pvc_name, timeout_seconds=300)

    suffix = uuid.uuid4().hex[:8]
    pod_name = _sanitize_k8s_name(f"fsx-bootstrap-{fs_id}-{suffix}", max_length=63)

    node_selector_yaml = ""
    if node_selector:
        node_selector_yaml = "  nodeSelector:\n" + "\n".join([f"    {k}: {v}" for k, v in node_selector.items()]) + "\n"

    manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {namespace}
spec:
  restartPolicy: Never
{node_selector_yaml}  terminationGracePeriodSeconds: 0
  containers:
    - name: bootstrap
      image: {FSX_BOOTSTRAP_IMAGE}
      securityContext:
        runAsUser: 0
      command: ["sh", "-lc", "set -eux; mkdir -p /fsx/{target_subdir}; chmod 1777 /fsx/{target_subdir}; ls -ld /fsx /fsx/{target_subdir}"]
      volumeMounts:
        - name: fsx
          mountPath: /fsx
  volumes:
    - name: fsx
      persistentVolumeClaim:
        claimName: {pvc_name}
"""

    # Ensure we don't leak pods if the custom resource is retried.
    try:
        _kubectl(["delete", "pod", pod_name, "-n", namespace, "--ignore-not-found=true"], check=False, timeout_seconds=30)
    except Exception:
        pass
    _kubectl(["apply", "-f", "-"], input_text=manifest, timeout_seconds=120)

    # Wait for completion.
    start = time.time()
    phase = ""
    bad_reason_first_seen = {}
    fatal_wait_reasons = {
        "ErrImagePull",
        "ImagePullBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }

    try:
        while time.time() - start < 300:
            try:
                pod_raw = _kubectl(["get", "pod", pod_name, "-n", namespace, "-o", "json"], timeout_seconds=20).stdout
                pod_obj = json.loads(pod_raw)
                status = pod_obj.get("status") or {}
                phase = (status.get("phase") or "").strip()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
                time.sleep(5)
                continue

            if phase in {"Succeeded", "Failed"}:
                break

            # Fail fast if we're stuck on an image pull or similar fatal wait reason.
            for cs in status.get("containerStatuses") or []:
                waiting = (cs.get("state") or {}).get("waiting") or {}
                reason = (waiting.get("reason") or "").strip()
                if reason in fatal_wait_reasons:
                    if reason not in bad_reason_first_seen:
                        bad_reason_first_seen[reason] = time.time()
                        msg = (waiting.get("message") or "").strip()
                        print(f"Bootstrap pod waiting reason={reason}: {msg}")
                    elif time.time() - bad_reason_first_seen[reason] > 60:
                        msg = (waiting.get("message") or "").strip()
                        raise RuntimeError(f"Bootstrap pod stuck in {reason}: {msg}")

            time.sleep(5)

        logs = _kubectl(["logs", pod_name, "-n", namespace], check=False, timeout_seconds=60).stdout
        if logs:
            print(f"FSx bootstrap pod logs ({namespace}/{pod_name}):\n{logs}")

        if phase != "Succeeded":
            desc = _kubectl(["describe", "pod", pod_name, "-n", namespace], check=False, timeout_seconds=60).stdout
            if desc:
                print(f"FSx bootstrap pod describe ({namespace}/{pod_name}):\n{desc}")
            raise RuntimeError(f"FSx bootstrap pod {namespace}/{pod_name} did not succeed (phase={phase or 'Unknown'})")

        return target_path
    finally:
        try:
            _kubectl(["delete", "pod", pod_name, "-n", namespace, "--ignore-not-found=true"], check=False, timeout_seconds=30)
        except Exception:
            pass


def _get_action(event: dict) -> str:
    props = (event or {}).get("ResourceProperties") or {}
    return str(props.get("Action") or props.get("action") or "").strip()


def _get_bootstrap_node_role(event: dict) -> str:
    props = (event or {}).get("ResourceProperties") or {}
    node_role = str(props.get("NodeRole") or props.get("nodeRole") or os.environ.get("FSX_BOOTSTRAP_NODE_ROLE", "system")).strip()
    return node_role or "system"


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing FSx for Lustre file systems
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
    # Initialize EKS client
    eks = boto3.client('eks', region_name=region)
    
    try:
        # Get cluster info
        cluster = eks.describe_cluster(name=cluster_name)['cluster']
        
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


def find_subnet_in_az(az_id, subnet_ids):
    """
    Find a subnet ID that is in the specified availability zone ID
    
    Args:
        az_id: The availability zone ID to search for
        subnet_ids: List of subnet IDs to search through
        
    Returns:
        The subnet ID that is in the specified AZ ID, or None if not found
    """
    if not az_id or not subnet_ids:
        return None
        
    try:
        ec2 = boto3.client('ec2', region_name=os.environ['AWS_REGION'])
        
        # Split comma-separated subnet IDs if provided as a string
        if isinstance(subnet_ids, str):
            subnet_list = [s.strip() for s in subnet_ids.split(',')]
        else:
            subnet_list = subnet_ids
            
        # Describe all subnets in the list
        response = ec2.describe_subnets(SubnetIds=subnet_list)
        
        # Find the subnet in the specified AZ ID
        for subnet in response['Subnets']:
            if subnet['AvailabilityZoneId'] == az_id:
                print(f"Found subnet {subnet['SubnetId']} in availability zone ID {az_id}")
                return subnet['SubnetId']
                
        print(f"No subnet found in availability zone ID {az_id}")
        return None
        
    except Exception as e:
        print(f"Error finding subnet in AZ ID {az_id}: {str(e)}")
        return None


def get_fsx_network_config(fsx_file_system_id, aws_region):
    """
    Get subnet ID and security group IDs from an existing FSx file system
    
    Args:
        fsx_file_system_id: The FSx file system ID
        aws_region: AWS region
        
    Returns:
        Tuple of (subnet_id, security_group_ids)
    """
    try:
        # Get FSx file system details using boto3
        fsx_client = boto3.client('fsx', region_name=aws_region)
        fsx_response = fsx_client.describe_file_systems(FileSystemIds=[fsx_file_system_id])
        
        if not fsx_response['FileSystems']:
            raise Exception(f"FSx file system {fsx_file_system_id} not found")
            
        fsx_details = fsx_response['FileSystems'][0]
        
        # Get network information
        subnet_id = fsx_details['SubnetIds'][0]  # Use first subnet if multiple
        security_group_ids = ','.join(fsx_details['NetworkInterfaceIds'])
        
        return subnet_id, security_group_ids
        
    except Exception as e:
        print(f"Error getting FSx network configuration: {str(e)}")
        raise


def create_dynamic_fsx_resources(response_data):
    """
    Create Kubernetes resources for dynamic FSx provisioning
    """
    try:
        print("FSX_FILE_SYSTEM_ID is empty. Proceeding with dynamic provisioning...")
        
        # Dynamic Provisioning 
        print("Creating FSx for Lustre StorageClass...")
        
        # Determine the subnet ID to use based on FSX_SUBNETID or find in FSX_AVAILABILITY_ZONE
        subnet_id = ""
        
        # First check if FSX_SUBNETID is provided and not empty
        fsx_subnet_id = os.environ.get('FSX_SUBNETID', '').strip()
        fsx_az = os.environ.get('FSX_AVAILABILITY_ZONE', '').strip()
        private_subnets = os.environ.get('PRIVATE_SUBNET_IDS', '').strip()
        
        if fsx_subnet_id:
            # Use explicitly provided subnet ID
            subnet_id = fsx_subnet_id
            print(f"Using provided FSX_SUBNETID: {subnet_id}")
        elif fsx_az and private_subnets:
            # Find a subnet in the provided availability zone ID
            subnet_id = find_subnet_in_az(fsx_az, private_subnets)
            if subnet_id:
                print(f"Found subnet {subnet_id} in FSX_AVAILABILITY_ZONE ID {fsx_az}")
            else:
                print(f"Warning: No subnet found in FSX_AVAILABILITY_ZONE ID {fsx_az}. StorageClass creation may fail.")
        else:
            print("Warning: Neither FSX_SUBNETID nor both FSX_AVAILABILITY_ZONE and PRIVATE_SUBNET_IDS provided or they are empty. StorageClass creation may fail.")
        
        # Create StorageClass YAML content
        storage_class_content = f"""apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fsx-sc
provisioner: fsx.csi.aws.com
parameters:
  subnetId: {subnet_id}
  securityGroupIds: {os.environ['SECURITY_GROUP_ID']}
  deploymentType: {os.environ['DEPLOYMENT_TYPE']}
  automaticBackupRetentionDays: "0"
  copyTagsToBackups: "true"
  perUnitStorageThroughput: "{os.environ['PER_UNIT_STORAGE_THROUGHPUT']}"
  dataCompressionType: "{os.environ['DATA_COMPRESSION_TYPE']}"
  fileSystemTypeVersion: "{os.environ['FILE_SYSTEM_TYPE_VERSION']}"
mountOptions:
  - flock
"""
        
        # Write StorageClass YAML to a temporary file
        storage_class_path = '/tmp/storageclass.yaml'
        with open(storage_class_path, 'w') as f:
            f.write(storage_class_content)
            
        # Apply the StorageClass using kubectl
        print("Applying StorageClass to the cluster...")
        _kubectl(["apply", "-f", storage_class_path], timeout_seconds=120)
        
        # Verify StorageClass creation
        print("Verifying StorageClass creation...")
        result = _kubectl(["get", "storageclass", "fsx-sc", "-o", "yaml"], timeout_seconds=60).stdout
        print(f"StorageClass verification:\n{result}")
        
        # Add StorageClass name to response data
        response_data["StorageClassName"] = "fsx-sc"
        
        # Create a sample PersistentVolumeClaim using the StorageClass
        print("Creating a sample PersistentVolumeClaim...")
        
        # Get storage capacity and ensure it's properly formatted
        storage_capacity = os.environ['STORAGE_CAPACITY']
        
        pvc_content = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-claim
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: fsx-sc
  resources:
    requests:
      storage: {storage_capacity}Gi
"""
        
        # Write PVC YAML to a temporary file
        pvc_path = '/tmp/pvc.yaml'
        with open(pvc_path, 'w') as f:
            f.write(pvc_content)
            
        # Apply the PVC using kubectl
        print("Applying PersistentVolumeClaim to the cluster...")
        _kubectl(["apply", "-f", pvc_path], timeout_seconds=120)
        
        # Add PVC information to response data
        response_data["PersistentVolumeClaimName"] = "fsx-claim"
        response_data["PVCNamespace"] = "default"
        
        print("This PVC will kick off the dynamic provisioning of an FSx for Lustre file system based on the specifications provided in the storage class.")
        
        # View the status of the PVC
        print("\nChecking PVC status:")
        pvc_status = _kubectl(["describe", "pvc", "fsx-claim"], timeout_seconds=60).stdout
        print(pvc_status)
        
        # Check if the PVC is in Pending or Bound state
        print("\nChecking PVC phase:")
        try:
            pvc_phase = _kubectl(
                ["get", "pvc", "fsx-claim", "-n", "default", "-ojsonpath={.status.phase}"],
                timeout_seconds=60,
            ).stdout
            print(f"PVC Status: {pvc_phase}")
            response_data["PVCStatus"] = pvc_phase
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Failed to get PVC phase: {e}")
            response_data["PVCStatus"] = "Unknown"
        
        # Try to get volume info if PVC is bound (this might fail initially as provisioning takes time)
        if response_data.get("PVCStatus") == "Bound":
            try:
                # Get the PV name first
                pv_name = _kubectl(
                    ["get", "pvc", "fsx-claim", "-n", "default", "-ojsonpath={.spec.volumeName}"],
                    timeout_seconds=60,
                ).stdout.strip()
                
                # Get the FSx volume ID
                volume_id = _kubectl(
                    ["get", "pv", pv_name, "-ojsonpath={.spec.csi.volumeHandle}"],
                    timeout_seconds=60,
                ).stdout.strip()
                
                print(f"\nFSx Volume ID: {volume_id}")
                response_data["FSxVolumeId"] = volume_id
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                print(f"Note: FSx volume ID not yet available. Provisioning may still be in progress: {e}")
                response_data["FSxVolumeId"] = "Provisioning"
        else:
            print("\nNote: FSx provisioning may take up to 10 minutes. Check status later with:")
            print("  kubectl describe pvc fsx-claim")
            print("  kubectl get pvc fsx-claim -n default -ojsonpath={.status.phase}")
            print("\nOnce bound, retrieve volume ID with:")
            print("  kubectl get pv $(kubectl get pvc fsx-claim -n default -ojsonpath={.spec.volumeName}) -ojsonpath={.spec.csi.volumeHandle}")
            
    except Exception as e:
        print(f"Error creating Kubernetes resources for dynamic FSx provisioning: {str(e)}")
        raise


def create_existing_fsx_resources(response_data):
    """
    Create Kubernetes resources for existing FSx file system
    """
    try:
        fsx_file_system_id = os.environ['FSX_FILE_SYSTEM_ID']
        aws_region = os.environ['AWS_REGION']
        
        # Get subnet ID and security group IDs from the existing FSx file system
        subnet_id, security_group_ids = get_fsx_network_config(fsx_file_system_id, aws_region)
        # Create unique resource names to avoid conflicts
        pvc_name = "fsx-claim"
        pv_name = "fsx-pv"
        sc_name = "fsx-sc"
        
        # Get FSx file system details using boto3
        fsx_client = boto3.client('fsx', region_name=aws_region)
        try:
            fsx_response = fsx_client.describe_file_systems(FileSystemIds=[fsx_file_system_id])
        except ClientError as e:
            raise Exception(f"Failed to describe FSx file system {fsx_file_system_id}: {str(e)}")
        
        if not fsx_response['FileSystems']:
            raise Exception(f"FSx file system {fsx_file_system_id} not found")
            
        fsx_details = fsx_response['FileSystems'][0]
        
        # Verify it's a Lustre file system
        if fsx_details['FileSystemType'] != 'LUSTRE':
            raise Exception(f"File system {fsx_file_system_id} is not a Lustre file system. Type: {fsx_details['FileSystemType']}")
            
        # Check file system state
        if fsx_details['Lifecycle'] != 'AVAILABLE':
            raise Exception(f"FSx file system {fsx_file_system_id} is not available. Current state: {fsx_details['Lifecycle']}")
            
        # Get storage capacity directly from FSx API instead of environment variable
        storage_capacity = str(fsx_details['StorageCapacity'])
            
        dns_name = fsx_details['DNSName']
        mount_name = fsx_details['LustreConfiguration']['MountName']
        
        print(f"Found FSx file system: {fsx_file_system_id}")
        print(f"DNS Name: {dns_name}")
        print(f"Mount Name: {mount_name}")
        
        # 1. Create StorageClass for existing FSx
        print("Creating StorageClass for existing FSx file system...")
        storage_class_content = f"""apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {sc_name}
provisioner: fsx.csi.aws.com
parameters:
  fileSystemId: {fsx_file_system_id}
  subnetId: {subnet_id}
  securityGroupIds: {security_group_ids}
"""
        
        storage_class_path = '/tmp/storageclass.yaml'
        with open(storage_class_path, 'w') as f:
            f.write(storage_class_content)
            
        _kubectl(["apply", "-f", storage_class_path], timeout_seconds=120)
        print("StorageClass created successfully")
        
        # 2. Create PersistentVolume and PersistentVolumeClaim in each user namespace
        # Note: PV is cluster-scoped but can only bind to ONE PVC. For multiple namespaces,
        # we create separate PVs (with unique names) pointing to the same FSx filesystem.
        user_namespaces = os.environ.get('USER_NAMESPACES', 'default').split(',')
        user_namespaces = [ns.strip() for ns in user_namespaces if ns.strip()]
        if not user_namespaces:
            user_namespaces = ['default']
        
        created_pvcs = []
        for namespace in user_namespaces:
            ns_pv_name = f"fsx-pv-{namespace}"
            ns_pvc_name = pvc_name
            
            print(f"\nCreating PV and PVC for namespace {namespace}...")
            
            # Ensure namespace exists
            try:
                _kubectl(["create", "namespace", namespace, "--dry-run=client", "-o", "yaml"], timeout_seconds=60)
                ns_yaml = f'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {namespace}\n'
                _kubectl(["apply", "-f", "-"], input_text=ns_yaml, timeout_seconds=60)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass  # Namespace may already exist
            
            # Create namespace-specific PV pointing to the same FSx filesystem
            ns_pv_content = f"""apiVersion: v1
kind: PersistentVolume
metadata:
  name: {ns_pv_name}
spec:
  capacity:
    storage: {storage_capacity}Gi
  volumeMode: Filesystem
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef:
    namespace: {namespace}
    name: {ns_pvc_name}
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: {fsx_file_system_id}
    volumeAttributes:
      dnsname: {dns_name}
      mountname: {mount_name}
"""
            
            ns_pv_path = f'/tmp/pv-{namespace}.yaml'
            with open(ns_pv_path, 'w') as f:
                f.write(ns_pv_content)
            
            try:
                _kubectl(["apply", "-f", ns_pv_path], timeout_seconds=120)
                print(f"PersistentVolume {ns_pv_name} created successfully")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to create PV for {namespace}: {e}")
                continue
            
            # Create PVC in the namespace
            ns_pvc_content = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {ns_pvc_name}
  namespace: {namespace}
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  volumeName: {ns_pv_name}
  resources:
    requests:
      storage: {storage_capacity}Gi
"""
            
            ns_pvc_path = f'/tmp/pvc-{namespace}.yaml'
            with open(ns_pvc_path, 'w') as f:
                f.write(ns_pvc_content)
                
            try:
                _kubectl(["apply", "-f", ns_pvc_path], timeout_seconds=120)
                print(f"PersistentVolumeClaim {ns_pvc_name} created successfully in {namespace}")
                created_pvcs.append(f"{namespace}/{ns_pvc_name}")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to create PVC in {namespace}: {e}")
        
        # Verify resources were created
        print("\nVerifying created resources...")
        
        # Check StorageClass
        try:
            result = _kubectl(["get", "storageclass", sc_name], timeout_seconds=60).stdout
            print(f"StorageClass status:\n{result}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Failed to verify StorageClass: {e}")
        
        # Check PVs and PVCs
        for namespace in user_namespaces:
            ns_pv_name = f"fsx-pv-{namespace}"
            try:
                result = _kubectl(["get", "pv", ns_pv_name], timeout_seconds=60).stdout
                print(f"PV {ns_pv_name} status:\n{result}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                print(f"Warning: Failed to verify PV {ns_pv_name}: {e}")
            
            try:
                result = _kubectl(["get", "pvc", pvc_name, "-n", namespace], timeout_seconds=60).stdout
                print(f"PVC {pvc_name} in {namespace} status:\n{result}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                print(f"Warning: Failed to verify PVC in {namespace}: {e}")
        
        # Update response data
        response_data.update({
            "StorageClassName": sc_name,
            "PersistentVolumeName": pv_name,  # Keep for backward compatibility
            "PersistentVolumeClaimName": pvc_name,
            "PVCNamespace": ",".join(user_namespaces),
            "FSxDNSName": dns_name,
            "FSxMountName": mount_name,
            "CreatedPVCs": ",".join(created_pvcs)
        })
        
        print("\nKubernetes resources for existing FSx file system created successfully!")
        print(f"PVCs created in namespaces: {', '.join(user_namespaces)}")
        
    except Exception as e:
        print(f"Error creating Kubernetes resources for existing FSx: {str(e)}")
        raise


def on_create(event):
    """
    Handle Set Up an FSx for Lustre File System
    """
    try:
        # Initialize response data
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx is set up successfully"
        }

        resourceId = event['LogicalResourceId']
        action = _get_action(event)

        if action == "BootstrapWritableSubdir":
            # Minimal environment requirements for the bootstrap-only action.
            for var in ["CLUSTER_NAME", "FSX_FILE_SYSTEM_ID", "AWS_REGION"]:
                if var not in os.environ or not str(os.environ.get(var, "")).strip():
                    raise ValueError(f"Missing required environment variable: {var} for bootstrap")

            write_kubeconfig(os.environ["CLUSTER_NAME"], os.environ["AWS_REGION"])
            _wait_for_kubectl_access(timeout_seconds=180)

            node_role = _get_bootstrap_node_role(event)
            node_selector = {"node-role": node_role} if node_role else None
            _wait_for_ready_ec2_node(node_selector=node_selector, timeout_seconds=900)

            response_data["SageMakerFileSystemPath"] = _bootstrap_fsx_writable_subdir(
                os.environ["FSX_FILE_SYSTEM_ID"], node_selector=node_selector
            )
            return response_data

        # Ensure required environment variables are set for the original Step1/Step2 behavior.
        required_env_vars = [
            'CLUSTER_NAME',
            'PER_UNIT_STORAGE_THROUGHPUT',
            'DATA_COMPRESSION_TYPE',
            'FILE_SYSTEM_TYPE_VERSION',
            'FSX_FILE_SYSTEM_ID',
            'PATH',
            'GIT_EXEC_PATH',
            'KUBECONFIG',
            'LD_LIBRARY_PATH'
        ]

        # STORAGE_CAPACITY is only required for dynamic provisioning
        if os.environ['FSX_FILE_SYSTEM_ID'] == '' and 'STORAGE_CAPACITY' not in os.environ:
            raise ValueError("Missing required environment variable: STORAGE_CAPACITY for dynamic provisioning")

        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        if resourceId == 'FsxCustomResourceStep1':
            # Associate IAM OIDC provider with the cluster
            subprocess.run(['eksctl', 'utils', 'associate-iam-oidc-provider', '--cluster', os.environ['CLUSTER_NAME'], '--approve'], check=True)

            # Create IAM service account for FSx CSI controller
            subprocess.run(['eksctl', 'create', 'iamserviceaccount',
                            '--name', 'fsx-csi-controller-sa',
                            '--namespace', 'kube-system',
                            '--cluster', os.environ['CLUSTER_NAME'],
                            '--attach-policy-arn', 'arn:aws:iam::aws:policy/AmazonFSxFullAccess',
                            '--approve',
                            '--role-name', f"FSXLCSI-{os.environ['CLUSTER_NAME']}",
                            '--region', os.environ['AWS_REGION']], check=True)

            # Verify proper annotation of the service account with the IAM role ARN
            try:
                result = _kubectl(
                    ["get", "sa", "fsx-csi-controller-sa", "-n", "kube-system", "-oyaml"],
                    timeout_seconds=60,
                ).stdout
                print(f"Service account verification:\n{result}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                print(f"Warning: Failed to verify service account: {e}")
                
            # Verify installation of the FSx for Lustre CSI driver
            try:
                result = _kubectl(
                    ["get", "pods", "-n", "kube-system", "-l", "app.kubernetes.io/name=aws-fsx-csi-driver"],
                    timeout_seconds=60,
                ).stdout
                print(f"FSx for Lustre CSI driver verification:\n{result}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                print(f"Warning: Failed to verify FSx for Lustre CSI driver installation: {e}")
        elif resourceId == 'FsxCustomResourceStep2':
            # Choose between dynamic provisioning or existing FSx
            if os.environ['FSX_FILE_SYSTEM_ID'] == '':
                # Create Kubernetes resources for dynamic FSx provisioning
                create_dynamic_fsx_resources(response_data)
            else:
                print(f"Using existing FSx for Lustre file system with ID: {os.environ['FSX_FILE_SYSTEM_ID']}")
                response_data["FSxVolumeId"] = os.environ['FSX_FILE_SYSTEM_ID']
                
                # Create Kubernetes resources for existing FSx file system
                create_existing_fsx_resources(response_data)
                response_data["SageMakerFileSystemPath"] = f"/{os.environ['FSX_FILE_SYSTEM_ID']}"
        
        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to install Helm chart: {str(e)}")


def on_update(event):
    """
    Handle Update request to upgrade the AWS FSx CSI driver and update StorageClass
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx CSI driver updated successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION',
            'PER_UNIT_STORAGE_THROUGHPUT',
            'DATA_COMPRESSION_TYPE',
            'FILE_SYSTEM_TYPE_VERSION'
        ]
        
        # STORAGE_CAPACITY is only required for dynamic provisioning
        if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] == '' and 'STORAGE_CAPACITY' not in os.environ:
            raise ValueError("Missing required environment variable: STORAGE_CAPACITY for dynamic provisioning")
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
        

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Associate IAM OIDC provider with the cluster (if not already done)
        try:
            subprocess.run(['eksctl', 'utils', 'associate-iam-oidc-provider', '--cluster', os.environ['CLUSTER_NAME'], '--approve'], check=True)
        except subprocess.CalledProcessError as e:
            # This might fail if already exists, which is fine
            print(f"Note: OIDC provider association: {e}")

        # Create or update IAM service account for FSx CSI controller
        subprocess.run(['eksctl', 'create', 'iamserviceaccount',
                        '--name', 'fsx-csi-controller-sa',
                        '--namespace', 'kube-system',
                        '--cluster', os.environ['CLUSTER_NAME'],
                        '--attach-policy-arn', 'arn:aws:iam::aws:policy/AmazonFSxFullAccess',
                        '--approve',
                        '--role-name', f"FSXLCSI-{os.environ['CLUSTER_NAME']}-{os.environ['AWS_REGION']}",
                        '--region', os.environ['AWS_REGION']], check=True)

        # Verify proper annotation of the service account with the IAM role ARN
        try:
            result = _kubectl(
                ["get", "sa", "fsx-csi-controller-sa", "-n", "kube-system", "-oyaml"],
                timeout_seconds=60,
            ).stdout
            print(f"Service account verification:\n{result}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Failed to verify service account: {e}")
            
        # Verify installation of the FSx for Lustre CSI driver
        try:
            result = _kubectl(
                ["get", "pods", "-n", "kube-system", "-l", "app.kubernetes.io/name=aws-fsx-csi-driver"],
                timeout_seconds=60,
            ).stdout
            print(f"FSx for Lustre CSI driver verification:\n{result}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Failed to verify FSx for Lustre CSI driver installation: {e}")
            
        # Choose between dynamic provisioning or existing FSx for updates
        if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] == '':
            # Update StorageClass for dynamic provisioning
            create_dynamic_fsx_resources(response_data)
        else:
            print(f"Using existing FSx for Lustre file system with ID: {os.environ.get('FSX_FILE_SYSTEM_ID', 'Not provided')}")
            if 'FSX_FILE_SYSTEM_ID' in os.environ:
                response_data["FSxVolumeId"] = os.environ['FSX_FILE_SYSTEM_ID']
                # Update Kubernetes resources for existing FSx file system
                create_existing_fsx_resources(response_data)
            
        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to update AWS FSx CSI driver: {str(e)}")


def on_delete(event):
    """
    Handle Delete request to uninstall the AWS FSx CSI driver
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx CSI driver uninstalled successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Delete Kubernetes resources (both for dynamic and existing FSx)
        pvc_name = "fsx-claim"
        pv_name = "fsx-pv"
        sc_name = "fsx-sc"
        
        # Get user namespaces for cleanup
        user_namespaces = os.environ.get('USER_NAMESPACES', 'default').split(',')
        user_namespaces = [ns.strip() for ns in user_namespaces if ns.strip()]
        if not user_namespaces:
            user_namespaces = ['default']
        
        # Delete PVCs and PVs from all namespaces
        for namespace in user_namespaces:
            ns_pv_name = f"fsx-pv-{namespace}"
            
            try:
                print(f"Deleting PersistentVolumeClaim {pvc_name} from namespace {namespace}...")
                _kubectl(
                    ["delete", "pvc", pvc_name, "-n", namespace, "--ignore-not-found=true"],
                    timeout_seconds=60,
                )
                print(f"Successfully deleted PVC from {namespace}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                print(f"Warning: Failed to delete PVC from {namespace}: {e}")
            
            # Delete namespace-specific PV for existing FSx
            if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] != '':
                try:
                    print(f"Deleting PersistentVolume {ns_pv_name}...")
                    _kubectl(["delete", "pv", ns_pv_name, "--ignore-not-found=true"], timeout_seconds=60)
                    print(f"Successfully deleted PV {ns_pv_name}")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    print(f"Warning: Failed to delete PV {ns_pv_name}: {e}")
        
        # Also try to delete the legacy single PV (for backward compatibility)
        if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] != '':
            try:
                print(f"Deleting legacy PersistentVolume {pv_name}...")
                _kubectl(["delete", "pv", pv_name, "--ignore-not-found=true"], timeout_seconds=60)
                print("Successfully deleted legacy PV")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                print(f"Warning: Failed to delete legacy PV: {e}")
                
        try:
            print(f"Deleting StorageClass {sc_name}...")
            _kubectl(["delete", "storageclass", sc_name, "--ignore-not-found=true"], timeout_seconds=60)
            print("Successfully deleted StorageClass")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Failed to delete StorageClass: {e}")
            
        # Delete the IAM service account
        try:
            print("Deleting IAM service account...")
            subprocess.run(['eksctl', 'delete', 'iamserviceaccount',
                          '--name', 'fsx-csi-controller-sa',
                          '--namespace', 'kube-system',
                          '--cluster', os.environ['CLUSTER_NAME'],
                          '--region', os.environ['AWS_REGION']], check=True)
            print("Successfully deleted IAM service account")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to delete service account: {e}")

        return response_data

    except Exception as e:
        print(f"Error during deletion: {str(e)}")
        # Return SUCCESS anyway to allow stack deletion to proceed
        return {
            "Status": "SUCCESS",
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
