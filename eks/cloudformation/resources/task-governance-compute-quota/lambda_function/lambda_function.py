import boto3
from botocore.exceptions import ClientError
import hashlib
import json
import os
import re
import subprocess
import time
import yaml

CLUSTER_NAME_ENV = "EKS_CLUSTER_NAME"
HYPERPOD_CLUSTER_ARN_ENV = "HYPERPOD_CLUSTER_ARN"
TEAM_NAMESPACES_ENV = "TEAM_NAMESPACES"
INSTANCE_TYPE_ENV = "COMPUTE_QUOTA_INSTANCE_TYPE"
INSTANCE_COUNT_ENV = "COMPUTE_QUOTA_INSTANCE_COUNT"
FAIR_SHARE_WEIGHT_ENV = "COMPUTE_QUOTA_FAIR_SHARE_WEIGHT"
BORROW_LIMIT_ENV = "COMPUTE_QUOTA_BORROW_LIMIT"
SHARING_STRATEGY_ENV = "COMPUTE_QUOTA_SHARING_STRATEGY"
PREEMPT_TASKS_ENV = "COMPUTE_QUOTA_PREEMPT_TEAM_TASKS"
RESOURCE_NAME_PREFIX_ENV = "RESOURCE_NAME_PREFIX"

KUEUE_NAMESPACE = "kueue-system"
KUEUE_WEBHOOK_SERVICE = "kueue-webhook-service"
KUEUE_CONTROLLER_DEPLOYMENT = "kueue-controller-manager"
KUEUE_WEBHOOK_PORT = 9443
TEAM_NAMESPACE_PREFIX = "hyperpod-ns-"


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


def _sanitize_name(name, max_len=63):
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        name = "tg"
    if len(name) > max_len:
        suffix = hashlib.sha1(name.encode()).hexdigest()[:6]
        trim_len = max_len - len(suffix) - 1
        name = name[:trim_len].rstrip("-") + "-" + suffix
    return name


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


def _get_kueue_api_version():
    raw = _run(["kubectl", "get", "--raw", "/apis/kueue.x-k8s.io"], timeout=30)
    data = json.loads(raw)
    preferred = data.get("preferredVersion") or {}
    if preferred.get("version"):
        version = preferred["version"]
    else:
        versions = [v.get("version") for v in data.get("versions", []) if v.get("version")]
        if "v1beta2" in versions:
            version = "v1beta2"
        elif "v1beta1" in versions:
            version = "v1beta1"
        elif versions:
            version = versions[-1]
        else:
            version = None
    if not version:
        raise Exception("Unable to determine Kueue API version from /apis/kueue.x-k8s.io")
    return f"kueue.x-k8s.io/{version}"


def _get_kueue_api_version_with_retry(max_attempts=12, delay_seconds=5):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _get_kueue_api_version()
        except Exception as exc:
            last_error = exc
            print(f"Kueue API not ready (attempt {attempt}/{max_attempts}): {exc}")
            time.sleep(delay_seconds)
    raise last_error


def _deployment_ready(namespace, name):
    raw = _run(["kubectl", "get", "deployment", "-n", namespace, name, "-o", "json"], timeout=30)
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


def _endpointslice_ready(namespace, service_name):
    try:
        raw = _run(
            [
                "kubectl",
                "get",
                "endpointslice",
                "-n",
                namespace,
                "-l",
                f"kubernetes.io/service-name={service_name}",
                "-o",
                "json",
            ],
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        return False, 0, False, f"endpointslice error: {exc.stderr}"

    data = json.loads(raw)
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
                if port_num == KUEUE_WEBHOOK_PORT or name in ("https", "webhook", "kueue-webhook"):
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


def _endpoints_ready(namespace, service_name):
    raw = _run(["kubectl", "get", "endpoints", "-n", namespace, service_name, "-o", "json"], timeout=30)
    data = json.loads(raw)
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
                if port_num == KUEUE_WEBHOOK_PORT or name in ("https", "webhook", "kueue-webhook"):
                    ports_ok = True
    if not ports_seen:
        ports_ok = True
    ok = ready > 0 and ports_ok
    detail = f"ready={ready} not_ready={not_ready} ports_ok={ports_ok}"
    return ok, ready, ports_ok, detail


def _check_kueue_webhook():
    controller_ready, controller_detail = _deployment_ready(
        KUEUE_NAMESPACE, KUEUE_CONTROLLER_DEPLOYMENT
    )
    endpointslice_ready, _, _, endpointslice_detail = _endpointslice_ready(
        KUEUE_NAMESPACE, KUEUE_WEBHOOK_SERVICE
    )
    endpoints_ready, _, _, endpoints_detail = _endpoints_ready(
        KUEUE_NAMESPACE, KUEUE_WEBHOOK_SERVICE
    )
    webhook_ready = endpointslice_ready or endpoints_ready
    detail = (
        f"controller_ready={controller_ready} {controller_detail}, "
        f"endpointslice_ready={endpointslice_ready} {endpointslice_detail}, "
        f"endpoints_ready={endpoints_ready} {endpoints_detail}"
    )
    return controller_ready and webhook_ready, detail


def _crd_established(name):
    raw = _run(["kubectl", "get", "crd", name, "-o", "json"], timeout=30)
    data = json.loads(raw)
    for cond in data.get("status", {}).get("conditions", []) or []:
        if cond.get("type") == "Established" and cond.get("status") == "True":
            return True
    return False


def _kueue_api_ready():
    api_version = _get_kueue_api_version_with_retry()
    for crd in [
        "clusterqueues.kueue.x-k8s.io",
        "localqueues.kueue.x-k8s.io",
        "workloadpriorityclasses.kueue.x-k8s.io",
    ]:
        if not _crd_established(crd):
            return False, f"CRD not established: {crd}", api_version
    ready, detail = _check_kueue_webhook()
    if not ready:
        return False, f"Kueue webhook not ready: {detail}", api_version
    try:
        _run(["kubectl", "get", "clusterqueue", "-A", "-o", "name"], timeout=30)
        _run(["kubectl", "get", "localqueue", "-A", "-o", "name"], timeout=30)
    except Exception as exc:
        return False, f"Kueue API not responding: {exc}", api_version
    return True, "Kueue API ready", api_version


def _derive_team_names():
    namespaces = _parse_csv(os.environ.get(TEAM_NAMESPACES_ENV, ""))
    teams = []
    for item in namespaces:
        if item.startswith(TEAM_NAMESPACE_PREFIX):
            team = item[len(TEAM_NAMESPACE_PREFIX):]
        else:
            team = item
        team = team.strip()
        if team:
            teams.append(team)
    seen = set()
    ordered = []
    for team in teams:
        if team not in seen:
            seen.add(team)
            ordered.append(team)
    return ordered


def _compute_quota_name(team, prefix, cluster_id):
    base = f"{prefix}-tg-{cluster_id}-{team}" if cluster_id else f"{prefix}-tg-{team}"
    return _sanitize_name(base)


def _cluster_id_from_arn(cluster_arn):
    if not cluster_arn:
        return ""
    parts = cluster_arn.split("/", 1)
    if len(parts) == 2:
        return parts[1]
    return ""


def _namespace_exists(namespace):
    try:
        _run(["kubectl", "get", "namespace", namespace, "-o", "json"], timeout=20)
        return True
    except Exception:
        return False


def _localqueue_exists(namespace, name):
    try:
        _run(["kubectl", "get", "localqueue", name, "-n", namespace, "-o", "json"], timeout=20)
        return True
    except Exception:
        return False


def _clusterqueue_exists(name):
    try:
        raw = _run(["kubectl", "get", "clusterqueue", name, "-o", "json"], timeout=20)
        data = json.loads(raw)
        labels = data.get("metadata", {}).get("labels", {}) or {}
        return labels.get("sagemaker.amazonaws.com/sagemaker-managed-queue") == "true"
    except Exception:
        return False


def _find_quota_by_team(sm, cluster_arn, team_name):
    paginator = sm.get_paginator("list_compute_quotas")
    for page in paginator.paginate(ClusterArn=cluster_arn):
        for summary in page.get("ComputeQuotaSummaries", []):
            quota_id = summary.get("ComputeQuotaId")
            if not quota_id:
                continue
            desc = sm.describe_compute_quota(ComputeQuotaId=quota_id)
            target = desc.get("ComputeQuotaTarget", {}) or {}
            if target.get("TeamName") == team_name:
                return desc
    return None


def _build_quota_config(instance_type, count, borrow_limit, sharing_strategy, preempt_tasks):
    config = {
        "ComputeQuotaResources": [
            {
                "InstanceType": instance_type,
                "Count": count,
            }
        ],
        "PreemptTeamTasks": preempt_tasks,
    }
    if sharing_strategy:
        resource_sharing = {"Strategy": sharing_strategy}
        if borrow_limit is not None:
            resource_sharing["BorrowLimit"] = borrow_limit
        config["ResourceSharingConfig"] = resource_sharing
    return config


def _ensure_compute_quota(sm, cluster_arn, team_name, config, fair_share_weight, name_prefix, cluster_id):
    existing = _find_quota_by_team(sm, cluster_arn, team_name)
    target = {"TeamName": team_name, "FairShareWeight": fair_share_weight}
    if existing:
        quota_id = existing["ComputeQuotaId"]
        print(f"Updating compute quota for team {team_name}: {quota_id}")
        sm.update_compute_quota(
            ComputeQuotaId=quota_id,
            ComputeQuotaConfig=config,
            ComputeQuotaTarget=target,
            ActivationState="Enabled",
        )
        return quota_id

    quota_name = _compute_quota_name(team_name, name_prefix, cluster_id)
    tags = [
        {"Key": "CreatedBy", "Value": "nemo-task-governance-compute-quota"},
        {"Key": "TeamName", "Value": team_name},
    ]
    print(f"Creating compute quota {quota_name} for team {team_name}")
    response = sm.create_compute_quota(
        Name=quota_name,
        ClusterArn=cluster_arn,
        ComputeQuotaTarget=target,
        ComputeQuotaConfig=config,
        ActivationState="Enabled",
        Tags=tags,
    )
    return response["ComputeQuotaId"]


def _delete_compute_quota(sm, team_name, cluster_arn):
    existing = _find_quota_by_team(sm, cluster_arn, team_name)
    if not existing:
        print(f"No compute quota found for team {team_name}")
        return None
    quota_id = existing["ComputeQuotaId"]
    print(f"Deleting compute quota {quota_id} for team {team_name}")
    sm.delete_compute_quota(ComputeQuotaId=quota_id)
    return quota_id


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
    region = os.environ.get("AWS_REGION", "us-east-1")
    hyperpod_cluster_arn = os.environ.get(HYPERPOD_CLUSTER_ARN_ENV, "")

    teams = _derive_team_names()
    if not teams:
        return _build_response(
            "SUCCESS",
            "No team namespaces provided",
            {"Teams": []},
            next_attempt,
            max_attempts,
            delay_seconds,
        )

    if not hyperpod_cluster_arn:
        return _build_response(
            "NOT_READY",
            "HyperPod cluster ARN not available yet",
            {},
            next_attempt,
            max_attempts,
            delay_seconds,
        )

    if request_type == "Delete":
        try:
            sm = boto3.client("sagemaker", region_name=region)
            deleted = []
            for team in teams:
                quota_id = _delete_compute_quota(sm, team, hyperpod_cluster_arn)
                if quota_id:
                    deleted.append(quota_id)
            return _build_response(
                "SUCCESS",
                "Deleted compute quotas",
                {"DeletedQuotas": deleted},
                next_attempt,
                max_attempts,
                delay_seconds,
            )
        except Exception as exc:
            print(f"Delete failed: {exc}")
            return _build_response("FAILED", str(exc), {}, next_attempt, max_attempts, delay_seconds)

    ok, msg = _safe_setup_kubeconfig(cluster_name, region)
    if not ok:
        return _build_response("NOT_READY", msg, {}, next_attempt, max_attempts, delay_seconds)

    ready, detail, _ = _kueue_api_ready()
    if not ready:
        return _build_response("NOT_READY", detail, {}, next_attempt, max_attempts, delay_seconds)

    instance_type = (os.environ.get(INSTANCE_TYPE_ENV) or "").strip()
    count_raw = (os.environ.get(INSTANCE_COUNT_ENV) or "").strip()
    if not instance_type or not count_raw:
        return _build_response(
            "FAILED",
            "Compute quota instance type/count not configured",
            {},
            next_attempt,
            max_attempts,
            delay_seconds,
        )
    try:
        instance_count = int(count_raw)
    except ValueError as exc:
        return _build_response("FAILED", f"Invalid instance count: {exc}", {}, next_attempt, max_attempts, delay_seconds)
    if instance_count <= 0:
        return _build_response("FAILED", "Compute quota instance count must be > 0", {}, next_attempt, max_attempts, delay_seconds)

    fair_share_weight = int(os.environ.get(FAIR_SHARE_WEIGHT_ENV, "50"))
    borrow_limit_raw = (os.environ.get(BORROW_LIMIT_ENV) or "").strip()
    borrow_limit = int(borrow_limit_raw) if borrow_limit_raw else None
    sharing_strategy = (os.environ.get(SHARING_STRATEGY_ENV) or "LendAndBorrow").strip()
    preempt_tasks = (os.environ.get(PREEMPT_TASKS_ENV) or "LowerPriority").strip()
    name_prefix = (os.environ.get(RESOURCE_NAME_PREFIX_ENV) or "tg").strip()
    cluster_id = _cluster_id_from_arn(hyperpod_cluster_arn)

    config = _build_quota_config(instance_type, instance_count, borrow_limit, sharing_strategy, preempt_tasks)

    sm = boto3.client("sagemaker", region_name=region)
    quota_ids = []
    for team in teams:
        try:
            quota_id = _ensure_compute_quota(
                sm, hyperpod_cluster_arn, team, config, fair_share_weight, name_prefix, cluster_id
            )
            quota_ids.append(quota_id)
        except ClientError as exc:
            msg = str(exc)
            return _build_response("FAILED", f"Create/update failed for {team}: {msg}", {}, next_attempt, max_attempts, delay_seconds)

    pending = []
    for quota_id in quota_ids:
        desc = sm.describe_compute_quota(ComputeQuotaId=quota_id)
        status = desc.get("Status")
        if status in ("Creating", "Updating"):
            pending.append(quota_id)
        elif status in ("CreateFailed", "UpdateFailed", "CreateRollbackFailed", "UpdateRollbackFailed"):
            return _build_response(
                "FAILED",
                f"Compute quota {quota_id} failed with status {status}: {desc.get('FailureReason', '')}",
                {},
                next_attempt,
                max_attempts,
                delay_seconds,
            )

    if pending:
        return _build_response(
            "NOT_READY",
            f"Compute quota(s) still pending: {pending}",
            {"Pending": pending},
            next_attempt,
            max_attempts,
            delay_seconds,
        )

    missing = []
    for team in teams:
        ns = f"{TEAM_NAMESPACE_PREFIX}{team}"
        localqueue = f"{ns}-localqueue"
        clusterqueue = f"{ns}-clusterqueue"
        if not _namespace_exists(ns):
            missing.append(f"namespace:{ns}")
            continue
        if not _localqueue_exists(ns, localqueue):
            missing.append(f"localqueue:{ns}/{localqueue}")
            continue
        if not _clusterqueue_exists(clusterqueue):
            missing.append(f"clusterqueue:{clusterqueue}")

    if missing:
        return _build_response(
            "NOT_READY",
            f"Waiting for TG queues/namespaces: {', '.join(missing)}",
            {"Missing": missing},
            next_attempt,
            max_attempts,
            delay_seconds,
        )

    return _build_response(
        "SUCCESS",
        "Compute quota(s) ready",
        {"Teams": teams, "ComputeQuotaIds": quota_ids},
        next_attempt,
        max_attempts,
        delay_seconds,
    )
