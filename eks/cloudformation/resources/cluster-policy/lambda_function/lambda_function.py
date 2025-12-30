import json
import logging
import os
import subprocess
import time

import boto3
from botocore.exceptions import ClientError
import yaml

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KUEUE_NAMESPACE = "kueue-system"
KUEUE_WEBHOOK_SERVICE = "kueue-webhook-service"
KUEUE_CONTROLLER_DEPLOYMENT = "kueue-controller-manager"
KUEUE_WEBHOOK_PORT = 9443
TASK_GOV_ADDON_NAME = "amazon-sagemaker-hyperpod-taskgovernance"


def _is_truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _run(cmd, input_text=None, timeout=120):
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    logger.info("Exit code: %s", result.returncode)
    if result.stdout:
        logger.info("stdout: %s", result.stdout)
    if result.stderr:
        logger.info("stderr: %s", result.stderr)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout


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
            logger.info("Kueue API not ready (attempt %s/%s): %s", attempt, max_attempts, exc)
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
    subsets = data.get("subsets", []) or []
    ready_addresses = 0
    ports_ok = False
    ports_seen = False
    for subset in subsets:
        ports = subset.get("ports", []) or []
        if ports:
            ports_seen = True
            for port in ports:
                port_num = port.get("port")
                name = (port.get("name") or "").lower()
                if port_num == KUEUE_WEBHOOK_PORT or name in ("https", "webhook", "kueue-webhook"):
                    ports_ok = True
        addresses = subset.get("addresses", []) or []
        ready_addresses += len(addresses)
    if not ports_seen:
        ports_ok = True
    ok = ready_addresses > 0 and ports_ok
    detail = f"ready_addresses={ready_addresses} ports_ok={ports_ok}"
    return ok, ready_addresses, ports_ok, detail


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


def _list_workload_priority_classes():
    raw = _run(["kubectl", "get", "workloadpriorityclass", "-A", "-o", "json"], timeout=30)
    data = json.loads(raw)
    items = data.get("items", []) or []
    names = []
    for item in items:
        name = (item.get("metadata") or {}).get("name")
        if name:
            names.append(name)
    return names


def _normalize_scheduler_config(config):
    if "PriorityClasses" not in config:
        return config
    for priority_class in config["PriorityClasses"]:
        if "Weight" in priority_class and isinstance(priority_class["Weight"], str):
            priority_class["Weight"] = int(priority_class["Weight"])
    return config


def _list_scheduler_configs(sm):
    configs = []
    token = None
    while True:
        kwargs = {}
        if token:
            kwargs["NextToken"] = token
        resp = sm.list_cluster_scheduler_configs(**kwargs)
        configs.extend(resp.get("ClusterSchedulerConfigSummaries", []) or [])
        token = resp.get("NextToken")
        if not token:
            break
    return configs


def _find_scheduler_config_by_name(sm, cluster_arn, name):
    for item in _list_scheduler_configs(sm):
        if item.get("Name") != name:
            continue
        if cluster_arn and item.get("ClusterArn") != cluster_arn:
            continue
        return item
    return None


def _wait_for_scheduler_config_ready(sm, config_id, timeout_seconds=600, poll_seconds=10):
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        desc = sm.describe_cluster_scheduler_config(ClusterSchedulerConfigId=config_id)
        status = (desc.get("Status") or "").upper()
        failure_reason = desc.get("FailureReason") or ""
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            return desc, status, failure_reason
        if status and status not in ("CREATING", "UPDATING", "DELETING"):
            return desc, status, ""
        last_status = status
        time.sleep(poll_seconds)
    return None, last_status or "TIMEOUT", "Timed out waiting for scheduler config to become ready"


def _wait_for_scheduler_config_deleted(sm, config_id, timeout_seconds=300, poll_seconds=10):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            sm.describe_cluster_scheduler_config(ClusterSchedulerConfigId=config_id)
        except sm.exceptions.ResourceNotFound:
            return True
        time.sleep(poll_seconds)
    return False


def _get_scheduler_config_version(sm, config_id):
    desc = sm.describe_cluster_scheduler_config(ClusterSchedulerConfigId=config_id)
    version = desc.get("ClusterSchedulerConfigVersion")
    if version is None:
        raise Exception(f"Missing ClusterSchedulerConfigVersion for scheduler config {config_id}")
    return version


def _addon_active(eks_cluster_name, region):
    try:
        eks = boto3.client("eks", region_name=region)
        addon = eks.describe_addon(clusterName=eks_cluster_name, addonName=TASK_GOV_ADDON_NAME)
        status = (addon.get("addon", {}).get("status") or "").upper()
        if status == "ACTIVE":
            return True, "Task governance add-on is ACTIVE"
        return False, f"Task governance add-on status is {status or 'UNKNOWN'}"
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "ResourceNotFound"):
            return False, "Task governance add-on not found yet"
        raise


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
    logger.info("Received event: %s", json.dumps(event))
    request_type = event.get("RequestType") or "Create"
    attempt = int(event.get("Attempt") or 0)
    max_attempts = int(event.get("MaxAttempts") or os.environ.get("MAX_ATTEMPTS", "45"))
    delay_seconds = int(event.get("DelaySeconds") or os.environ.get("DELAY_SECONDS", "60"))
    next_attempt = attempt + 1

    props = event.get("ResourceProperties") or {}
    cluster_arn = props.get("ClusterArn") or ""
    scheduler_config = props.get("SchedulerConfig") or {}
    config_name = props.get("Name") or ""
    description = props.get("Description") or "HyperPod cluster scheduler configuration"
    eks_cluster_name = props.get("EKSClusterName") or os.environ.get("EKS_CLUSTER_NAME", "")
    region = os.environ.get("AWS_REGION", "us-east-1")
    verify_only = _is_truthy(event.get("VerifyOnly") or props.get("VerifyOnly") or False)

    if not cluster_arn or not config_name:
        return _build_response(
            "FAILED",
            "ClusterArn and Name are required to manage scheduler config",
            {},
            next_attempt,
            max_attempts,
            delay_seconds,
        )

    sagemaker = boto3.client("sagemaker", region_name=region)

    if request_type == "Delete":
        try:
            existing = _find_scheduler_config_by_name(sagemaker, cluster_arn, config_name)
            if existing:
                config_id = existing.get("ClusterSchedulerConfigId")
                if config_id:
                    logger.info("Deleting scheduler config %s", config_id)
                    sagemaker.delete_cluster_scheduler_config(ClusterSchedulerConfigId=config_id)
            return _build_response(
                "SUCCESS",
                "Deleted scheduler config (if present)",
                {},
                next_attempt,
                max_attempts,
                delay_seconds,
            )
        except Exception as exc:
            logger.info("Delete failed: %s", exc)
            return _build_response("FAILED", str(exc), {}, next_attempt, max_attempts, delay_seconds)

    if not eks_cluster_name:
        return _build_response(
            "NOT_READY",
            "EKSClusterName not available yet",
            {},
            next_attempt,
            max_attempts,
            delay_seconds,
        )

    addon_ready, addon_reason = _addon_active(eks_cluster_name, region)
    if not addon_ready:
        return _build_response("NOT_READY", addon_reason, {}, next_attempt, max_attempts, delay_seconds)

    ok, msg = _safe_setup_kubeconfig(eks_cluster_name, region)
    if not ok:
        return _build_response("NOT_READY", msg, {}, next_attempt, max_attempts, delay_seconds)

    ready, detail, _ = _kueue_api_ready()
    if not ready:
        return _build_response("NOT_READY", detail, {}, next_attempt, max_attempts, delay_seconds)

    scheduler_config = _normalize_scheduler_config(scheduler_config)

    try:
        existing = _find_scheduler_config_by_name(sagemaker, cluster_arn, config_name)
        if verify_only:
            if not existing:
                return _build_response(
                    "NOT_READY",
                    "Scheduler config not found yet",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
            status = (existing.get("Status") or "").upper()
            failure_reason = existing.get("FailureReason") or ""
            if status in ("CREATE_FAILED", "UPDATE_FAILED"):
                return _build_response(
                    "FAILED",
                    f"Scheduler config {status}: {failure_reason}",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
            if status in ("CREATING", "UPDATING", "DELETING") or not status:
                return _build_response(
                    "NOT_READY",
                    f"Scheduler config status is {status or 'UNKNOWN'}",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
            try:
                wpc_names = _list_workload_priority_classes()
            except Exception as exc:
                return _build_response(
                    "NOT_READY",
                    f"WorkloadPriorityClasses not ready: {exc}",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
            expected_count = len((scheduler_config or {}).get("PriorityClasses") or [])
            if expected_count and len(wpc_names) < expected_count:
                return _build_response(
                    "NOT_READY",
                    f"WorkloadPriorityClasses not ready (expected {expected_count}, found {len(wpc_names)})",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
            if not wpc_names:
                return _build_response(
                    "NOT_READY",
                    "WorkloadPriorityClasses not found yet",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
            config_id = existing.get("ClusterSchedulerConfigId")
            config_arn = existing.get("ClusterSchedulerConfigArn")
            if config_id and not config_arn:
                try:
                    desc = sagemaker.describe_cluster_scheduler_config(
                        ClusterSchedulerConfigId=config_id
                    )
                    config_arn = desc.get("ClusterSchedulerConfigArn") or config_arn
                except Exception as exc:
                    logger.info("Describe scheduler config failed during verify: %s", exc)
            if not config_id or not config_arn:
                return _build_response(
                    "NOT_READY",
                    "Scheduler config identifiers not available yet",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
            data = {
                "ClusterSchedulerConfigArn": config_arn,
                "ClusterSchedulerConfigId": config_id,
                "ClusterSchedulerConfigStatus": status,
                "WorkloadPriorityClassCount": len(wpc_names),
            }
            return _build_response(
                "SUCCESS",
                "Scheduler config verified",
                data,
                next_attempt,
                max_attempts,
                delay_seconds,
            )

        if existing:
            existing_status = (existing.get("Status") or "").upper()
            failure_reason = existing.get("FailureReason") or ""
            config_id = existing.get("ClusterSchedulerConfigId")
            if existing_status in ("CREATE_FAILED", "UPDATE_FAILED"):
                if config_id:
                    logger.info(
                        "Scheduler config %s in %s (%s); deleting and recreating.",
                        config_id,
                        existing_status,
                        failure_reason,
                    )
                    sagemaker.delete_cluster_scheduler_config(
                        ClusterSchedulerConfigId=config_id
                    )
                    deleted = _wait_for_scheduler_config_deleted(sagemaker, config_id)
                    if not deleted:
                        return _build_response(
                            "NOT_READY",
                            "Timed out waiting for scheduler config deletion",
                            {},
                            next_attempt,
                            max_attempts,
                            delay_seconds,
                        )
                existing = None
            elif existing_status in ("CREATING", "UPDATING", "DELETING"):
                return _build_response(
                    "NOT_READY",
                    f"Scheduler config status is {existing_status}; retry later",
                    {},
                    next_attempt,
                    max_attempts,
                    delay_seconds,
                )
        config_id = None
        config_arn = None

        if request_type == "Create" and existing:
            config_id = existing.get("ClusterSchedulerConfigId")
            config_arn = existing.get("ClusterSchedulerConfigArn")
            logger.info("Scheduler config already exists, updating: %s", config_id)
            target_version = _get_scheduler_config_version(sagemaker, config_id)
            sagemaker.update_cluster_scheduler_config(
                ClusterSchedulerConfigId=config_id,
                TargetVersion=target_version,
                SchedulerConfig=scheduler_config,
                Description=description,
            )
        elif request_type == "Update" and existing:
            config_id = existing.get("ClusterSchedulerConfigId")
            config_arn = existing.get("ClusterSchedulerConfigArn")
            logger.info("Updating scheduler config: %s", config_id)
            target_version = _get_scheduler_config_version(sagemaker, config_id)
            sagemaker.update_cluster_scheduler_config(
                ClusterSchedulerConfigId=config_id,
                TargetVersion=target_version,
                SchedulerConfig=scheduler_config,
                Description=description,
            )
        elif request_type in ("Create", "Update"):
            logger.info("Creating scheduler config %s", config_name)
            resp = sagemaker.create_cluster_scheduler_config(
                ClusterArn=cluster_arn,
                Name=config_name,
                SchedulerConfig=scheduler_config,
                Description=description,
            )
            config_id = resp.get("ClusterSchedulerConfigId")
            config_arn = resp.get("ClusterSchedulerConfigArn")
        else:
            return _build_response(
                "FAILED",
                f"Unsupported RequestType: {request_type}",
                {},
                next_attempt,
                max_attempts,
                delay_seconds,
            )

        if not config_id:
            return _build_response(
                "FAILED",
                "Scheduler config ID not available after create/update",
                {},
                next_attempt,
                max_attempts,
                delay_seconds,
            )

        desc, status, failure_reason = _wait_for_scheduler_config_ready(sagemaker, config_id)
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            logger.info("Scheduler config %s failed: %s", config_id, failure_reason)
            try:
                sagemaker.delete_cluster_scheduler_config(ClusterSchedulerConfigId=config_id)
            except Exception as exc:
                logger.info("Delete after failure failed: %s", exc)
            return _build_response(
                "NOT_READY",
                f"Scheduler config {status}: {failure_reason}",
                {},
                next_attempt,
                max_attempts,
                delay_seconds,
            )
        if status == "TIMEOUT":
            return _build_response("NOT_READY", failure_reason, {}, next_attempt, max_attempts, delay_seconds)

        data = {
            "ClusterSchedulerConfigArn": (desc or {}).get("ClusterSchedulerConfigArn", config_arn),
            "ClusterSchedulerConfigId": (desc or {}).get("ClusterSchedulerConfigId", config_id),
            "ClusterSchedulerConfigStatus": (desc or {}).get("Status", status),
        }
        return _build_response(
            "SUCCESS",
            "Scheduler config ready",
            data,
            next_attempt,
            max_attempts,
            delay_seconds,
        )
    except ClientError as exc:
        logger.info("Scheduler config operation failed: %s", exc)
        return _build_response("FAILED", str(exc), {}, next_attempt, max_attempts, delay_seconds)
    except Exception as exc:
        logger.info("Unhandled error: %s", exc)
        return _build_response("FAILED", str(exc), {}, next_attempt, max_attempts, delay_seconds)
