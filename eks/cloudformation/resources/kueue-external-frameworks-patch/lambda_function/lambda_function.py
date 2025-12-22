import base64
import boto3
import cfnresponse
import datetime
import json
import os
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import yaml


DEFAULT_NAMESPACE = "kueue-system"
DEFAULT_CONFIGMAP_NAME = "kueue-manager-config"
DEFAULT_DEPLOYMENT_NAME = "kueue-controller-manager"
DEFAULT_CONFIG_KEY = "controller_manager_config.yaml"
DEFAULT_WEBHOOK_SERVICE = "kueue-webhook-service"
DEFAULT_WEBHOOK_PORT = 9443


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
    except urllib.error.URLError as e:
        raise Exception(f"Failed to reach Kubernetes API: {e}")


def _deployment_ready(request, namespace, deployment_name):
    status, resp = request("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}")
    if status == 404:
        return False, "deployment not found"
    if status != 200:
        return False, f"deployment fetch failed: {status}"
    data = json.loads(resp)
    spec = data.get("spec", {})
    status_block = data.get("status", {})
    desired = int(spec.get("replicas", 1))
    available = int(status_block.get("availableReplicas", 0) or 0)
    ready = int(status_block.get("readyReplicas", 0) or 0)
    updated = int(status_block.get("updatedReplicas", 0) or 0)
    ok = available >= 1 and ready >= 1 and updated >= desired
    detail = f"desired={desired} available={available} ready={ready} updated={updated}"
    return ok, detail


def _endpointslice_ready(request, namespace, service_name):
    selector = urllib.parse.quote(f"kubernetes.io/service-name={service_name}")
    status, resp = request(
        "GET",
        f"/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices?labelSelector={selector}",
    )
    if status == 404:
        return False, 0, False, "endpointslice not found"
    if status != 200:
        return False, 0, False, f"endpointslice fetch failed: {status}"
    data = json.loads(resp)
    items = data.get("items", [])
    ready_addresses = 0
    ports_ok = False
    ports_seen = False
    for item in items:
        ports = item.get("ports", []) or []
        if ports:
            ports_seen = True
            for port in ports:
                port_num = port.get("port")
                name = (port.get("name") or "").lower()
                if port_num == DEFAULT_WEBHOOK_PORT or name in ("https", "webhook", "kueue-webhook"):
                    ports_ok = True
        for endpoint in item.get("endpoints", []) or []:
            conditions = endpoint.get("conditions", {}) or {}
            if conditions.get("ready") is True:
                addresses = endpoint.get("addresses", []) or []
                ready_addresses += len(addresses)
    if not ports_seen:
        ports_ok = True
    ok = ready_addresses > 0 and ports_ok
    detail = f"ready_addresses={ready_addresses} ports_ok={ports_ok}"
    return ok, ready_addresses, ports_ok, detail


def _endpoints_ready(request, namespace, service_name):
    status, resp = request("GET", f"/api/v1/namespaces/{namespace}/endpoints/{service_name}")
    if status == 404:
        return False, 0, False, "endpoints not found"
    if status != 200:
        return False, 0, False, f"endpoints fetch failed: {status}"
    data = json.loads(resp)
    ready = 0
    not_ready = 0
    ports_ok = False
    ports_seen = False
    for subset in data.get("subsets", []) or []:
        ready += len(subset.get("addresses", []) or [])
        not_ready += len(subset.get("notReadyAddresses", []) or [])
        ports = subset.get("ports", []) or []
        if ports:
            ports_seen = True
            for port in ports:
                port_num = port.get("port")
                name = (port.get("name") or "").lower()
                if port_num == DEFAULT_WEBHOOK_PORT or name in ("https", "webhook", "kueue-webhook"):
                    ports_ok = True
    if not ports_seen:
        ports_ok = True
    ok = ready > 0 and ports_ok
    detail = f"ready_addresses={ready} not_ready={not_ready} ports_ok={ports_ok}"
    return ok, ready, ports_ok, detail


def _wait_for_kueue_ready(request, namespace, deployment_name, max_attempts=30, delay_seconds=10):
    for attempt in range(1, max_attempts + 1):
        try:
            dep_ready, dep_detail = _deployment_ready(request, namespace, deployment_name)
            slice_ready, _, _, slice_detail = _endpointslice_ready(request, namespace, DEFAULT_WEBHOOK_SERVICE)
            eps_ready, _, _, eps_detail = _endpoints_ready(request, namespace, DEFAULT_WEBHOOK_SERVICE)
            webhook_ready = slice_ready or eps_ready
            if dep_ready and webhook_ready:
                print(f"Kueue ready (attempt {attempt})")
                return True
            print(
                "Kueue not ready "
                f"(deployment_ready={dep_ready} {dep_detail}, "
                f"endpointslice_ready={slice_ready} {slice_detail}, "
                f"endpoints_ready={eps_ready} {eps_detail}) "
                f"(attempt {attempt}/{max_attempts})"
            )
        except Exception as exc:
            print(f"Error checking Kueue readiness (attempt {attempt}/{max_attempts}): {exc}")
        time.sleep(delay_seconds)
    raise Exception("Kueue webhook not ready after waiting")


def _request_with_retry(request, method, path, body=None, content_type="application/yaml", max_attempts=6):
    last_status = None
    last_resp = None
    for attempt in range(1, max_attempts + 1):
        status, resp = request(method, path, body, content_type)
        last_status, last_resp = status, resp
        if status not in (429, 500, 502, 503, 504):
            return status, resp
        delay = min(5 * (2 ** (attempt - 1)), 60)
        delay = delay + random.uniform(0, 3)
        print(f"Transient API error {status} on {method} {path}; retrying in {delay:.1f}s")
        time.sleep(delay)
    return last_status, last_resp


def parse_frameworks(value):
    items = [v.strip() for v in value.split(",") if v.strip()]
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def parse_bool(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    if value == "":
        return None
    return value in ("1", "true", "yes")


def parse_int(value):
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def select_config_key(configmap_data):
    if DEFAULT_CONFIG_KEY in configmap_data:
        return DEFAULT_CONFIG_KEY
    if len(configmap_data) == 1:
        return next(iter(configmap_data.keys()))
    for key in configmap_data.keys():
        if key.endswith(".yaml"):
            return key
    return None


def merge_external_frameworks(config, frameworks):
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
        return False

    integrations["externalFrameworks"] = current
    config["integrations"] = integrations
    return True


def update_wait_for_pods_ready(config, enabled, timeout, backoff_limit):
    changed = False
    wait_config = config.get("waitForPodsReady") or {}

    if enabled is not None and wait_config.get("enable") != enabled:
        wait_config["enable"] = enabled
        changed = True

    if timeout:
        if wait_config.get("timeout") != timeout:
            wait_config["timeout"] = timeout
            changed = True

    if backoff_limit is not None:
        if wait_config.get("requeuingBackoffLimit") != backoff_limit:
            wait_config["requeuingBackoffLimit"] = backoff_limit
            changed = True

    if changed:
        config["waitForPodsReady"] = wait_config
    return changed


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
    wait_enabled = parse_bool(os.environ.get("WAIT_FOR_PODS_READY_ENABLED", ""))
    wait_timeout = os.environ.get("WAIT_FOR_PODS_READY_TIMEOUT", "").strip()
    wait_backoff = parse_int(os.environ.get("WAIT_FOR_PODS_READY_REQUEUE_BACKOFF_LIMIT", ""))

    has_wait_settings = wait_enabled is not None or wait_timeout or wait_backoff is not None
    if not frameworks and not has_wait_settings:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "No config updates requested"})
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

    _wait_for_kueue_ready(request, namespace, deployment_name)

    status, resp = _request_with_retry(
        request, "GET", f"/api/v1/namespaces/{namespace}/configmaps/{configmap_name}"
    )
    if status == 404:
        raise Exception("Kueue configmap not found after wait; Kueue is not ready or not installed")
    if status != 200:
        raise Exception(f"Failed to get configmap: {status} {resp}")

    configmap = json.loads(resp)
    data = configmap.get("data") or {}
    config_key = select_config_key(data)
    if not config_key:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "No config YAML found"})
        return

    config = yaml.safe_load(data.get(config_key, "")) or {}
    changed = False
    if frameworks:
        changed = merge_external_frameworks(config, frameworks) or changed
    if has_wait_settings:
        changed = update_wait_for_pods_ready(config, wait_enabled, wait_timeout, wait_backoff) or changed
    if not changed:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "Kueue config already up to date"})
        return

    updated_yaml = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    configmap["data"][config_key] = updated_yaml
    sanitize_resource(configmap)
    status, resp = _request_with_retry(
        request,
        "PUT",
        f"/api/v1/namespaces/{namespace}/configmaps/{configmap_name}",
        json.dumps(configmap),
        content_type="application/json",
    )
    if status not in [200, 201]:
        raise Exception(f"Failed to update configmap: {status} {resp}")

    # Restart kueue-controller-manager to pick up config changes
    status, resp = _request_with_retry(
        request,
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
        status, resp = _request_with_retry(
            request,
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
        {"Message": "Kueue config updated", "ConfigKey": config_key},
    )
