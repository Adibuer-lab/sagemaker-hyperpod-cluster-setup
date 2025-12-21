import base64
import json
import os
import ssl
import urllib.request

import boto3
import cfnresponse


def get_eks_token(cluster_name):
    session = boto3.Session(region_name=os.environ["AWS_REGION"])
    sts = session.client("sts")

    def retrieve_k8s_aws_id(params, context, **kwargs):
        if "x-k8s-aws-id" in params:
            context["x-k8s-aws-id"] = params.pop("x-k8s-aws-id")

    def inject_k8s_aws_id_header(request, **kwargs):
        if "x-k8s-aws-id" in request.context:
            request.headers["x-k8s-aws-id"] = request.context["x-k8s-aws-id"]

    sts.meta.events.register("provide-client-params.sts.GetCallerIdentity", retrieve_k8s_aws_id)
    sts.meta.events.register("before-sign.sts.GetCallerIdentity", inject_k8s_aws_id_header)
    url = sts.generate_presigned_url(
        "get_caller_identity",
        Params={"x-k8s-aws-id": cluster_name},
        ExpiresIn=60,
        HttpMethod="GET",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def k8s_request(endpoint, ca_data, token, method, path, body=None):
    url = f"{endpoint}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=base64.b64decode(ca_data).decode())
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def build_deployment(namespace, name, image, cpu_request, mem_request, node_selector=None):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": name},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "bootstrap",
                            "image": image,
                            "resources": {
                                "requests": {"cpu": cpu_request, "memory": mem_request}
                            },
                        }
                    ]
                },
            },
        },
    }


def handler(event, context):
    request_type = event.get("RequestType", "")
    props = event.get("ResourceProperties", {})

    cluster_name = props.get("EksClusterName")
    namespace = props.get("Namespace", "default")
    deployment_name = props.get("DeploymentName", "hyperpod-bootstrap")
    image = props.get("Image", "public.ecr.aws/eks-distro/kubernetes/pause:3.9")
    cpu_request = props.get("CpuRequest", "100m")
    mem_request = props.get("MemoryRequest", "128Mi")
    cleanup_only = str(props.get("CleanupOnly", "false")).lower() == "true"
    node_selector_key = props.get("NodeSelectorKey", "").strip()
    node_selector_value = props.get("NodeSelectorValue", "").strip()

    if not cluster_name:
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {"Reason": "EksClusterName is required"},
        )
        return

    eks = boto3.client("eks")
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
    endpoint, ca_data = cluster["endpoint"], cluster["certificateAuthority"]["data"]
    token = get_eks_token(cluster_name)

    if request_type == "Delete":
        if cleanup_only:
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return
        _delete_deployment(endpoint, ca_data, token, namespace, deployment_name)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
        return

    if cleanup_only:
        _delete_deployment(endpoint, ca_data, token, namespace, deployment_name)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Cleanup": "Deleted"})
        return

    node_selector = None
    if node_selector_key and node_selector_value:
        node_selector = {node_selector_key: node_selector_value}

    deployment = build_deployment(namespace, deployment_name, image, cpu_request, mem_request, node_selector=node_selector)
    if node_selector:
        deployment["spec"]["template"]["spec"]["nodeSelector"] = node_selector
    status, resp = k8s_request(
        endpoint,
        ca_data,
        token,
        "POST",
        f"/apis/apps/v1/namespaces/{namespace}/deployments",
        deployment,
    )
    if status not in (200, 201, 409):
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {"Reason": f"Failed to create deployment: {status} {resp}"},
        )
        return

    cfnresponse.send(
        event,
        context,
        cfnresponse.SUCCESS,
        {"Deployment": deployment_name, "Namespace": namespace},
    )


def _delete_deployment(endpoint, ca_data, token, namespace, name):
    status, resp = k8s_request(
        endpoint,
        ca_data,
        token,
        "DELETE",
        f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
        None,
    )
    if status in (200, 202, 404):
        return
    raise Exception(f"Failed to delete deployment: {status} {resp}")
