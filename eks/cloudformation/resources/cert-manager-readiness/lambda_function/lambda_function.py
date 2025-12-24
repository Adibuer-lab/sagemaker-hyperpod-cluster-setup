import boto3
from botocore.exceptions import ClientError
import json
import os
import subprocess
import time
import yaml

CLUSTER_NAME_ENV = "EKS_CLUSTER_NAME"
AWS_REGION_ENV = "AWS_REGION"
DEFAULT_NAMESPACE = "cert-manager"
DEFAULT_DEPLOYMENTS = "cert-manager,cert-manager-webhook,cert-manager-cainjector"
DEFAULT_WEBHOOK_SERVICE = "cert-manager-webhook"
DEFAULT_WEBHOOK_CA_SECRET = "cert-manager-webhook-ca"
DEFAULT_CRDS = [
    "certificates.cert-manager.io",
    "certificaterequests.cert-manager.io",
    "issuers.cert-manager.io",
    "clusterissuers.cert-manager.io",
]


def _run(cmd, input_text=None, timeout=120):
    print(f"Running: {' '.join(cmd)}")
    if input_text:
        print(f"Input: {input_text[:500]}...")
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    print(f"Exit code: {result.returncode}")
    print(f"stdout: {result.stdout}")
    print(f"stderr: {result.stderr}")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout


def _parse_csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _setup_kubeconfig(cluster_name, region):
    eks = boto3.client("eks", region_name=region)
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
    cluster_arn = cluster["arn"]

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "cluster": {
                    "server": cluster["endpoint"],
                    "certificate-authority-data": cluster["certificateAuthority"]["data"],
                },
                "name": cluster_name,
            }
        ],
        "contexts": [
            {
                "context": {"cluster": cluster_name, "user": cluster_name},
                "name": cluster_arn,
            }
        ],
        "current-context": cluster_arn,
        "preferences": {},
        "users": [
            {
                "name": cluster_name,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "aws-iam-authenticator",
                        "args": ["token", "-i", cluster_name],
                    }
                },
            }
        ],
    }

    kubeconfig_dir = "/tmp/.kube"
    os.makedirs(kubeconfig_dir, exist_ok=True)
    kubeconfig_path = os.path.join(kubeconfig_dir, "config")

    with open(kubeconfig_path, "w") as f:
        yaml.safe_dump(kubeconfig, f, default_flow_style=False)

    os.chmod(kubeconfig_path, 0o600)
    os.environ["KUBECONFIG"] = kubeconfig_path


def _safe_setup_kubeconfig(cluster_name, region):
    try:
        _setup_kubeconfig(cluster_name, region)
        return True, ""
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "ResourceNotFound"):
            return False, f"EKS cluster {cluster_name} not found"
        raise


def _namespace_exists(namespace):
    try:
        _run(["kubectl", "get", "namespace", namespace, "-o", "json"], timeout=30)
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr or exc.stdout


def _crd_established(name):
    try:
        raw = _run(["kubectl", "get", "crd", name, "-o", "json"], timeout=30)
    except subprocess.CalledProcessError as exc:
        return False, f"crd {name} not found: {exc.stderr or exc.stdout}"

    data = json.loads(raw)
    conditions = data.get("status", {}).get("conditions", []) or []
    for condition in conditions:
        if condition.get("type") == "Established":
            status = condition.get("status")
            if status == "True":
                return True, "Established"
            return False, f"Established={status}"
    return False, "Established condition missing"


def _secret_exists(namespace, name):
    try:
        _run(["kubectl", "get", "secret", name, "-n", namespace, "-o", "json"], timeout=30)
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr or exc.stdout


def _endpoints_ready(namespace, service_name):
    try:
        raw = _run([
            "kubectl", "get", "endpoints", "-n", namespace, service_name, "-o", "json"
        ], timeout=30)
    except subprocess.CalledProcessError as exc:
        return False, f"endpoints {service_name} not ready: {exc.stderr or exc.stdout}"

    data = json.loads(raw)
    ready = 0
    not_ready = 0
    for subset in data.get("subsets", []) or []:
        ready += len(subset.get("addresses", []) or [])
        not_ready += len(subset.get("notReadyAddresses", []) or [])
    ok = ready > 0
    detail = f"ready_addresses={ready} not_ready={not_ready}"
    return ok, detail


def _deployment_ready(namespace, name):
    try:
        raw = _run([
            "kubectl", "get", "deployment", "-n", namespace, name, "-o", "json"
        ], timeout=30)
    except subprocess.CalledProcessError as exc:
        return False, f"deployment {name} not ready: {exc.stderr or exc.stdout}"

    data = json.loads(raw)
    spec = data.get("spec", {})
    status = data.get("status", {})
    desired = int(spec.get("replicas", 1))
    available = int(status.get("availableReplicas", 0) or 0)
    ready = int(status.get("readyReplicas", 0) or 0)
    updated = int(status.get("updatedReplicas", 0) or 0)
    ok = available >= 1 and ready >= 1 and updated >= desired
    detail = f"desired={desired} available={available} ready={ready} updated={updated}"
    return ok, detail


def _check_cert_manager_ready(namespace, deployments):
    ns_ok, ns_detail = _namespace_exists(namespace)
    if not ns_ok:
        return False, f"namespace {namespace} not ready: {ns_detail}"

    for crd in DEFAULT_CRDS:
        ok, detail = _crd_established(crd)
        if not ok:
            return False, f"crd {crd} not established: {detail}"

    secret_ok, secret_detail = _secret_exists(namespace, DEFAULT_WEBHOOK_CA_SECRET)
    if not secret_ok:
        return False, f"secret {DEFAULT_WEBHOOK_CA_SECRET} not ready: {secret_detail}"

    endpoints_ok, endpoints_detail = _endpoints_ready(namespace, DEFAULT_WEBHOOK_SERVICE)
    if not endpoints_ok:
        return False, f"webhook endpoints not ready: {endpoints_detail}"

    details = []
    for name in deployments:
        ok, detail = _deployment_ready(namespace, name)
        details.append(f"{name}: {detail}")
        if not ok:
            return False, "; ".join(details)

    return True, "; ".join(details)


def _build_response(status, reason, data, attempt, max_attempts, delay_seconds):
    return {
        "Status": status,
        "Reason": reason,
        "Data": data or {},
        "Attempt": attempt,
        "MaxAttempts": max_attempts,
        "DelaySeconds": delay_seconds,
    }


def handler(event, context):
    request_type = event.get("RequestType") or "Create"
    attempt = int(event.get("Attempt") or 0)
    max_attempts = int(event.get("MaxAttempts") or os.environ.get("MAX_ATTEMPTS", "45"))
    delay_seconds = int(event.get("DelaySeconds") or os.environ.get("DELAY_SECONDS", "60"))
    next_attempt = attempt + 1

    cluster_name = os.environ[CLUSTER_NAME_ENV]
    region = os.environ.get(AWS_REGION_ENV, "us-east-1")

    if request_type == "Delete":
        return _build_response(
            "SUCCESS",
            "Cert-manager readiness check skipped on delete",
            {"Status": "DELETED"},
            next_attempt,
            max_attempts,
            delay_seconds,
        )

    ok, msg = _safe_setup_kubeconfig(cluster_name, region)
    if not ok:
        return _build_response("NOT_READY", msg, {}, next_attempt, max_attempts, delay_seconds)

    namespace = os.environ.get("CERT_MANAGER_NAMESPACE", DEFAULT_NAMESPACE).strip() or DEFAULT_NAMESPACE
    deployments = _parse_csv(os.environ.get("CERT_MANAGER_DEPLOYMENTS", DEFAULT_DEPLOYMENTS))
    if not deployments:
        deployments = _parse_csv(DEFAULT_DEPLOYMENTS)

    try:
        ready, detail = _check_cert_manager_ready(namespace, deployments)
        if not ready:
            return _build_response(
                "NOT_READY",
                f"Cert-manager not ready: {detail}",
                {"Namespace": namespace, "Deployments": deployments},
                next_attempt,
                max_attempts,
                delay_seconds,
            )
        return _build_response(
            "SUCCESS",
            "Cert-manager deployments ready",
            {"Namespace": namespace, "Deployments": deployments, "Detail": detail},
            next_attempt,
            max_attempts,
            delay_seconds,
        )
    except Exception as exc:
        print(f"Error checking cert-manager readiness: {exc}")
        return _build_response("FAILED", str(exc), {}, next_attempt, max_attempts, delay_seconds)
