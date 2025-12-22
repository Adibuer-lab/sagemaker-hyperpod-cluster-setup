import boto3
import cfnresponse
import hashlib
import json
import os
import re
import subprocess
import time
import yaml

CLUSTER_NAME_ENV = "EKS_CLUSTER_NAME"
AWS_REGION_ENV = "AWS_REGION"


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


def _bool_env(value):
    return str(value).strip().lower() in ("1", "true", "yes")


def _sanitize_name(name, max_len=63):
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        name = "kueue"
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


def _get_kueue_api_version():
    raw = _run(["kubectl", "get", "--raw", "/apis/kueue.x-k8s.io"], timeout=30)
    data = json.loads(raw)
    version = None
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
    if not version:
        raise Exception("Unable to determine Kueue API version from /apis/kueue.x-k8s.io")
    return f"kueue.x-k8s.io/{version}"


def _wait_for_kueue_webhook(max_attempts=30, delay_seconds=10):
    """Wait for Kueue webhook to be ready (5 minutes max)."""
    for attempt in range(1, max_attempts + 1):
        try:
            # Check pod is Running and Ready
            result = _run([
                "kubectl", "get", "pods", "-n", "kueue-system",
                "-l", "control-plane=controller-manager",
                "-o", "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}"
            ], timeout=30)
            if result.strip() == "True":
                print(f"Kueue controller pod is Ready (attempt {attempt})")
                return True
            print(f"Kueue controller not ready: {result.strip()} (attempt {attempt}/{max_attempts})")
        except Exception as exc:
            print(f"Error checking Kueue readiness (attempt {attempt}/{max_attempts}): {exc}")
        time.sleep(delay_seconds)
    raise Exception("Kueue webhook not ready after 5 minutes")


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


def _resolve_fsx_az_name(fsx_subnet_id, fsx_az_id, region):
    ec2 = boto3.client("ec2", region_name=region)
    if fsx_subnet_id:
        resp = ec2.describe_subnets(SubnetIds=[fsx_subnet_id])
        subnets = resp.get("Subnets", [])
        if subnets:
            return subnets[0].get("AvailabilityZone")
    if fsx_az_id:
        resp = ec2.describe_availability_zones(ZoneIds=[fsx_az_id])
        zones = resp.get("AvailabilityZones", [])
        if zones:
            return zones[0].get("ZoneName")
    return None


def _apply_resource(obj):
    _run(["kubectl", "apply", "-f", "-"], input_text=json.dumps(obj))


def _delete_resource(kind, name, namespace=None):
    cmd = ["kubectl", "delete", kind, name, "--ignore-not-found=true"]
    if namespace:
        cmd.extend(["-n", namespace])
    try:
        _run(cmd, timeout=60)
    except subprocess.CalledProcessError as exc:
        print(f"Warning: failed to delete {kind} {name}: {exc.stderr}")


def _ensure_namespace(namespace):
    ns_obj = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace},
    }
    _apply_resource(ns_obj)


def _build_resource_flavor(api_version, name, az_name):
    return {
        "apiVersion": api_version,
        "kind": "ResourceFlavor",
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/managed-by": "nemo-kueue-az-placement"},
        },
        "spec": {"nodeLabels": {"topology.kubernetes.io/zone": az_name}},
    }


def _build_cluster_queue(api_version, name, ordered_flavors):
    resources = [
        {"name": "cpu", "nominalQuota": "100000"},
        {"name": "memory", "nominalQuota": "100000Gi"},
        {"name": "nvidia.com/gpu", "nominalQuota": "100000"},
        {"name": "pods", "nominalQuota": "100000"},
    ]
    return {
        "apiVersion": api_version,
        "kind": "ClusterQueue",
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/managed-by": "nemo-kueue-az-placement"},
        },
        "spec": {
            "namespaceSelector": {},
            "resourceGroups": [
                {
                    "coveredResources": ["cpu", "memory", "nvidia.com/gpu", "pods"],
                    "flavors": [
                        {"name": flavor, "resources": resources}
                        for flavor in ordered_flavors
                    ],
                }
            ],
        },
    }


def _build_local_queue(api_version, name, namespace, cluster_queue):
    return {
        "apiVersion": api_version,
        "kind": "LocalQueue",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "nemo-kueue-az-placement"},
        },
        "spec": {"clusterQueue": cluster_queue},
    }


def _create_resources():
    cluster_name = os.environ[CLUSTER_NAME_ENV]
    region = os.environ.get(AWS_REGION_ENV, "us-east-1")
    az_list = _parse_csv(os.environ.get("EKS_AZ_NAMES", ""))
    namespaces = _parse_csv(os.environ.get("USER_NAMESPACES", ""))
    if not namespaces:
        namespaces = ["default"]
    if not az_list:
        raise Exception("EKS_AZ_NAMES is empty; cannot create Kueue flavors")

    fsx_subnet_id = os.environ.get("FSX_SUBNET_ID", "").strip()
    fsx_az_id = os.environ.get("FSX_AVAILABILITY_ZONE_ID", "").strip()

    flavor_prefix = os.environ.get("KUEUE_FLAVOR_PREFIX", "nemo-az").strip() or "nemo-az"
    cluster_queue_name = _sanitize_name(
        os.environ.get("KUEUE_CLUSTER_QUEUE_NAME", "nemo-az-clusterqueue").strip()
        or "nemo-az-clusterqueue"
    )
    local_queue_name = _sanitize_name(
        os.environ.get("KUEUE_LOCAL_QUEUE_NAME", "nemo-az-localqueue").strip()
        or "nemo-az-localqueue"
    )
    enable_per_az = _bool_env(os.environ.get("ENABLE_PER_AZ_QUEUES", "false"))

    _setup_kubeconfig(cluster_name, region)
    api_version = _get_kueue_api_version_with_retry()
    _wait_for_kueue_webhook()

    fsx_az_name = _resolve_fsx_az_name(fsx_subnet_id, fsx_az_id, region)
    ordered_azs = list(az_list)
    if fsx_az_name and fsx_az_name in ordered_azs:
        ordered_azs = [fsx_az_name] + [az for az in ordered_azs if az != fsx_az_name]

    flavor_names = {}
    seen = set()
    for az in ordered_azs:
        base = f"{flavor_prefix}-{az}"
        name = _sanitize_name(base)
        if name in seen:
            name = _sanitize_name(f"{base}-{az}")
        flavor_names[az] = name
        seen.add(name)

    for az in ordered_azs:
        _apply_resource(_build_resource_flavor(api_version, flavor_names[az], az))

    ordered_flavors = [flavor_names[az] for az in ordered_azs]
    _apply_resource(_build_cluster_queue(api_version, cluster_queue_name, ordered_flavors))

    per_az_clusterqueues = {}
    if enable_per_az:
        seen_cqs = {cluster_queue_name}
        for az in ordered_azs:
            base = f"{cluster_queue_name}-{az}"
            name = _sanitize_name(base)
            if name in seen_cqs:
                name = _sanitize_name(f"{base}-{az}")
            per_az_clusterqueues[az] = name
            seen_cqs.add(name)
            _apply_resource(_build_cluster_queue(api_version, name, [flavor_names[az]]))

    preferred_az = ordered_azs[0]
    preferred_clusterqueue = cluster_queue_name
    if enable_per_az and preferred_az in per_az_clusterqueues:
        preferred_clusterqueue = per_az_clusterqueues[preferred_az]

    for namespace in namespaces:
        _ensure_namespace(namespace)
        _apply_resource(
            _build_local_queue(
                api_version,
                local_queue_name,
                namespace,
                preferred_clusterqueue,
            )
        )
        if enable_per_az:
            seen_lqs = {local_queue_name}
            for az in ordered_azs:
                base = f"{local_queue_name}-{az}"
                name = _sanitize_name(base)
                if name in seen_lqs:
                    name = _sanitize_name(f"{base}-{az}")
                seen_lqs.add(name)
                _apply_resource(
                    _build_local_queue(
                        api_version,
                        name,
                        namespace,
                        per_az_clusterqueues[az],
                    )
                )

    return {
        "ApiVersion": api_version,
        "Flavors": ordered_flavors,
        "ClusterQueue": cluster_queue_name,
        "LocalQueue": local_queue_name,
        "PreferredFsxAz": fsx_az_name or "",
        "PerAzQueuesEnabled": str(enable_per_az).lower(),
        "PerAzClusterQueues": ",".join(per_az_clusterqueues.values()),
        "Namespaces": ",".join(namespaces),
    }


def _delete_resources():
    cluster_name = os.environ[CLUSTER_NAME_ENV]
    region = os.environ.get(AWS_REGION_ENV, "us-east-1")
    az_list = _parse_csv(os.environ.get("EKS_AZ_NAMES", ""))
    namespaces = _parse_csv(os.environ.get("USER_NAMESPACES", ""))
    if not namespaces:
        namespaces = ["default"]

    flavor_prefix = os.environ.get("KUEUE_FLAVOR_PREFIX", "nemo-az").strip() or "nemo-az"
    cluster_queue_name = _sanitize_name(
        os.environ.get("KUEUE_CLUSTER_QUEUE_NAME", "nemo-az-clusterqueue").strip()
        or "nemo-az-clusterqueue"
    )
    local_queue_name = _sanitize_name(
        os.environ.get("KUEUE_LOCAL_QUEUE_NAME", "nemo-az-localqueue").strip()
        or "nemo-az-localqueue"
    )

    _setup_kubeconfig(cluster_name, region)

    for namespace in namespaces:
        _delete_resource("localqueue", local_queue_name, namespace=namespace)

    seen_lqs = {local_queue_name}
    for az in az_list:
        base = f"{local_queue_name}-{az}"
        name = _sanitize_name(base)
        if name in seen_lqs:
            name = _sanitize_name(f"{base}-{az}")
        seen_lqs.add(name)
        for namespace in namespaces:
            _delete_resource("localqueue", name, namespace=namespace)

    _delete_resource("clusterqueue", cluster_queue_name)
    seen_cqs = {cluster_queue_name}
    for az in az_list:
        base = f"{cluster_queue_name}-{az}"
        name = _sanitize_name(base)
        if name in seen_cqs:
            name = _sanitize_name(f"{base}-{az}")
        seen_cqs.add(name)
        _delete_resource("clusterqueue", name)

    for az in az_list:
        name = _sanitize_name(f"{flavor_prefix}-{az}")
        _delete_resource("resourceflavor", name)


def handler(event, context):
    try:
        request_type = event.get("RequestType")
        if request_type == "Delete":
            _delete_resources()
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Status": "DELETED"})
            return

        data = _create_resources()
        cfnresponse.send(event, context, cfnresponse.SUCCESS, data)
    except Exception as exc:
        print(f"Error: {exc}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": str(exc)})
