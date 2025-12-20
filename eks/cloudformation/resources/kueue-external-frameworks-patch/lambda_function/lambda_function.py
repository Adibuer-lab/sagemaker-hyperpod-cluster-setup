import base64
import boto3
import cfnresponse
import datetime
import json
import os
import ssl
import urllib.request
import yaml


DEFAULT_NAMESPACE = "kueue-system"
DEFAULT_CONFIGMAP_NAME = "kueue-manager-config"
DEFAULT_DEPLOYMENT_NAME = "kueue-controller-manager"
DEFAULT_CONFIG_KEY = "controller_manager_config.yaml"


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


def k8s_request(endpoint, ca_data, token, method, path, body=None, content_type="application/yaml"):
    url = f"{endpoint}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = content_type
        data = body.encode()
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=base64.b64decode(ca_data).decode())
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def parse_frameworks(value):
    items = [v.strip() for v in value.split(",") if v.strip()]
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def select_config_key(configmap_data):
    if DEFAULT_CONFIG_KEY in configmap_data:
        return DEFAULT_CONFIG_KEY
    if len(configmap_data) == 1:
        return next(iter(configmap_data.keys()))
    for key in configmap_data.keys():
        if key.endswith(".yaml"):
            return key
    return None


def update_external_frameworks(config_yaml, frameworks):
    config = yaml.safe_load(config_yaml) or {}
    integrations = config.get("integrations") or {}
    current = integrations.get("externalFrameworks") or []
    if not isinstance(current, list):
        current = [str(current)]

    changed = False
    for framework in frameworks:
        if framework not in current:
            current.append(framework)
            changed = True

    if not changed:
        return config_yaml, False

    integrations["externalFrameworks"] = current
    config["integrations"] = integrations
    updated_yaml = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    return updated_yaml, True


def sanitize_resource(resource):
    metadata = resource.get("metadata") or {}
    metadata.pop("managedFields", None)
    resource["metadata"] = metadata
    resource.pop("status", None)
    return resource


def handler(event, context):
    if event["RequestType"] == "Delete":
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
        return

    cluster_name = os.environ["EKS_CLUSTER_NAME"]
    namespace = os.environ.get("KUEUE_NAMESPACE", DEFAULT_NAMESPACE)
    configmap_name = os.environ.get("KUEUE_CONFIGMAP_NAME", DEFAULT_CONFIGMAP_NAME)
    deployment_name = os.environ.get("KUEUE_DEPLOYMENT_NAME", DEFAULT_DEPLOYMENT_NAME)
    frameworks = parse_frameworks(os.environ.get("EXTERNAL_FRAMEWORKS", ""))

    if not frameworks:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "No frameworks specified"})
        return

    eks = boto3.client("eks")
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
    endpoint = cluster["endpoint"]
    ca_data = cluster["certificateAuthority"]["data"]

    token = get_eks_token(cluster_name)

    def request(method, path, body=None, content_type="application/yaml"):
        nonlocal token
        for attempt in range(5):
            status, resp = k8s_request(endpoint, ca_data, token, method, path, body, content_type)
            if status != 401 or attempt == 4:
                return status, resp
            token = get_eks_token(cluster_name)
        return status, resp

    status, resp = request("GET", f"/api/v1/namespaces/{namespace}/configmaps/{configmap_name}")
    if status == 404:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "Kueue configmap not found"})
        return
    if status != 200:
        raise Exception(f"Failed to get configmap: {status} {resp}")

    configmap = json.loads(resp)
    data = configmap.get("data") or {}
    config_key = select_config_key(data)
    if not config_key:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "No config YAML found"})
        return

    updated_yaml, changed = update_external_frameworks(data.get(config_key, ""), frameworks)
    if not changed:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "externalFrameworks already set"})
        return

    configmap["data"][config_key] = updated_yaml
    sanitize_resource(configmap)
    status, resp = request(
        "PUT",
        f"/api/v1/namespaces/{namespace}/configmaps/{configmap_name}",
        json.dumps(configmap),
        content_type="application/json",
    )
    if status not in [200, 201]:
        raise Exception(f"Failed to update configmap: {status} {resp}")

    # Restart kueue-controller-manager to pick up config changes
    status, resp = request(
        "GET",
        f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
    )
    if status == 200:
        deployment = json.loads(resp)
        sanitize_resource(deployment)
        annotations = (
            deployment.get("spec", {})
            .get("template", {})
            .get("metadata", {})
            .get("annotations")
        )
        if annotations is None:
            annotations = {}
        annotations["kubectl.kubernetes.io/restartedAt"] = datetime.datetime.utcnow().isoformat() + "Z"
        deployment.setdefault("spec", {}).setdefault("template", {}).setdefault("metadata", {})[
            "annotations"
        ] = annotations
        status, resp = request(
            "PUT",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
            json.dumps(deployment),
            content_type="application/json",
        )
        if status not in [200, 201]:
            raise Exception(f"Failed to restart deployment: {status} {resp}")

    cfnresponse.send(
        event,
        context,
        cfnresponse.SUCCESS,
        {"Message": "externalFrameworks updated", "ConfigKey": config_key},
    )
