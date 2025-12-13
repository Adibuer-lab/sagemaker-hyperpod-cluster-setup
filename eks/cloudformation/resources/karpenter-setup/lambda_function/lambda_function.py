import boto3
import cfnresponse
import json
import base64
import os
import urllib.request
import ssl
import time
import yaml


def get_eks_token(cluster_name):
    session = boto3.Session(region_name=os.environ['AWS_REGION'])
    sts = session.client('sts')
    
    def retrieve_k8s_aws_id(params, context, **kwargs):
        if 'x-k8s-aws-id' in params:
            context['x-k8s-aws-id'] = params.pop('x-k8s-aws-id')
    
    def inject_k8s_aws_id_header(request, **kwargs):
        if 'x-k8s-aws-id' in request.context:
            request.headers['x-k8s-aws-id'] = request.context['x-k8s-aws-id']
    
    sts.meta.events.register('provide-client-params.sts.GetCallerIdentity', retrieve_k8s_aws_id)
    sts.meta.events.register('before-sign.sts.GetCallerIdentity', inject_k8s_aws_id_header)
    url = sts.generate_presigned_url('get_caller_identity', Params={'x-k8s-aws-id': cluster_name}, ExpiresIn=60, HttpMethod='GET')
    return 'k8s-aws-v1.' + base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')


def k8s_request(endpoint, ca_data, token, method, path, body=None):
    url = f"{endpoint}{path}"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/yaml'}
    data = body.encode() if body else None
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=base64.b64decode(ca_data).decode())
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def build_nodeclass(nodeclass_name, instance_groups):
    return {
        'apiVersion': 'karpenter.sagemaker.amazonaws.com/v1',
        'kind': 'HyperpodNodeClass',
        'metadata': {'name': nodeclass_name},
        'spec': {'instanceGroups': [ig.strip() for ig in instance_groups]}
    }


def build_nodepool(pool_name, nodeclass_name, instance_types, role=None, is_default=False):
    requirements = []
    
    if instance_types:
        requirements.append({
            'key': 'node.kubernetes.io/instance-type',
            'operator': 'In',
            'values': sorted(list(instance_types))
        })
    else:
        requirements.append({
            'key': 'node.kubernetes.io/instance-type',
            'operator': 'Exists'
        })
    
    if is_default:
        requirements.append({'key': 'node-role', 'operator': 'DoesNotExist'})
    elif role:
        requirements.append({'key': 'node-role', 'operator': 'In', 'values': [role]})
    
    nodepool = {
        'apiVersion': 'karpenter.sh/v1',
        'kind': 'NodePool',
        'metadata': {'name': pool_name},
        'spec': {
            'template': {
                'spec': {
                    'nodeClassRef': {
                        'group': 'karpenter.sagemaker.amazonaws.com',
                        'kind': 'HyperpodNodeClass',
                        'name': nodeclass_name
                    },
                    'expireAfter': 'Never',
                    'requirements': requirements
                }
            },
            'disruption': {
                'consolidationPolicy': 'WhenEmptyOrUnderutilized',
                'consolidateAfter': '30m'
            }
        }
    }
    
    if is_default:
        nodepool['spec']['weight'] = 1
    
    return nodepool


def handler(event, context):
    if event['RequestType'] == 'Delete':
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
        return
    
    try:
        cluster_name = os.environ['EKS_CLUSTER_NAME']
        hyperpod_cluster_name = os.environ['HYPERPOD_CLUSTER_NAME']
        nodeclass_name = os.environ['NODECLASS_NAME']
        nodepool_prefix = os.environ['NODEPOOL_NAME']
        
        # Get HyperPod instance groups
        sagemaker = boto3.client('sagemaker')
        hyperpod_cluster = sagemaker.describe_cluster(ClusterName=hyperpod_cluster_name)
        instance_groups = [ig['InstanceGroupName'] for ig in hyperpod_cluster['InstanceGroups']]
        print(f"Found {len(instance_groups)} instance groups: {instance_groups}")
        
        # Get EKS cluster info
        eks = boto3.client('eks')
        cluster = eks.describe_cluster(name=cluster_name)['cluster']
        endpoint, ca_data = cluster['endpoint'], cluster['certificateAuthority']['data']
        token = get_eks_token(cluster_name)
        
        # Create HyperpodNodeClass
        nodeclass = build_nodeclass(nodeclass_name, instance_groups)
        nodeclass_yaml = yaml.dump(nodeclass, default_flow_style=False, sort_keys=False)
        print(f"Creating NodeClass:\n{nodeclass_yaml}")
        
        for attempt in range(5):
            status, resp = k8s_request(endpoint, ca_data, token, 'POST', '/apis/karpenter.sagemaker.amazonaws.com/v1/hyperpodnodeclasses', nodeclass_yaml)
            if status in [200, 201, 409]:
                break
            if status == 401 and attempt < 4:
                print(f"Got 401, waiting for AccessEntry propagation (attempt {attempt + 1}/5)...")
                time.sleep(10)
                token = get_eks_token(cluster_name)
                continue
            raise Exception(f"Failed to create NodeClass: {status} {resp}")
        print(f"NodeClass response: {status}")
        
        # Wait for HyperpodNodeClass status and build role-to-types mapping
        role_to_types = {}
        default_types = set()
        
        for attempt in range(12):
            time.sleep(5)
            status, resp = k8s_request(endpoint, ca_data, token, 'GET', f'/apis/karpenter.sagemaker.amazonaws.com/v1/hyperpodnodeclasses/{nodeclass_name}', None)
            if status == 200:
                nc_status = json.loads(resp).get('status', {})
                instance_groups_in_status = nc_status.get('instanceGroups', [])
                if instance_groups_in_status:
                    for ig in instance_groups_in_status:
                        types = ig.get('instanceTypes', [])
                        role = next((l.get('value') for l in (ig.get('desiredLabels') or []) if l.get('key') == 'node-role'), None)
                        if role:
                            role_to_types.setdefault(role, set()).update(types)
                        else:
                            default_types.update(types)
                    print(f"Found role->types mapping: { {r: list(t) for r, t in role_to_types.items()} }")
                    break
            print(f"Waiting for NodeClass status (attempt {attempt + 1}/12)...")
        
        # Create NodePools for each role
        created_pools = []
        
        for role, types in role_to_types.items():
            pool_name = f"{nodepool_prefix}-{role}"
            nodepool = build_nodepool(pool_name, nodeclass_name, types, role=role)
            nodepool_yaml = yaml.dump(nodepool, default_flow_style=False, sort_keys=False)
            print(f"Creating NodePool {pool_name} with types={list(types)}")
            
            status, resp = k8s_request(endpoint, ca_data, token, 'POST', '/apis/karpenter.sh/v1/nodepools', nodepool_yaml)
            if status not in [200, 201, 409]:
                raise Exception(f"Failed to create NodePool {pool_name}: {status} {resp}")
            created_pools.append(pool_name)
        
        # Create default NodePool for instance groups without node-role
        default_pool_name = f"{nodepool_prefix}-default"
        default_nodepool = build_nodepool(default_pool_name, nodeclass_name, default_types, is_default=True)
        default_nodepool_yaml = yaml.dump(default_nodepool, default_flow_style=False, sort_keys=False)
        print(f"Creating default NodePool with types={list(default_types)}")
        
        status, resp = k8s_request(endpoint, ca_data, token, 'POST', '/apis/karpenter.sh/v1/nodepools', default_nodepool_yaml)
        if status not in [200, 201, 409]:
            raise Exception(f"Failed to create default NodePool: {status} {resp}")
        created_pools.append(default_pool_name)
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {'NodeClassName': nodeclass_name, 'NodePools': ','.join(created_pools)})
    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
