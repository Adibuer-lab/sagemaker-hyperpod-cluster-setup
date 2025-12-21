import boto3
import cfnresponse
import hashlib
import json
import os
import re
import subprocess


CLUSTER_NAME_ENV = "EKS_CLUSTER_NAME"
AWS_REGION_ENV = "AWS_REGION"


def _run(cmd, input_text=None, timeout=120):
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def _parse_csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


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

    os.makedirs("/tmp/.kube", exist_ok=True)
    kubeconfig_path = "/tmp/.kube/config"
    with open(kubeconfig_path, "w") as handle:
        json.dump(kubeconfig, handle)

    os.environ["KUBECONFIG"] = kubeconfig_path


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


def _resolve_fsx_az_name(subnet_id, az_id, region):
    ec2 = boto3.client("ec2", region_name=region)
    if subnet_id:
        resp = ec2.describe_subnets(SubnetIds=[subnet_id])
        subnets = resp.get("Subnets") or []
        if subnets:
            return subnets[0].get("AvailabilityZone")

    if az_id:
        resp = ec2.describe_availability_zones(ZoneIds=[az_id], AllAvailabilityZones=True)
        zones = resp.get("AvailabilityZones") or []
        if zones:
            return zones[0].get("ZoneName")
    return ""


def _build_queue_order(base_local_queue, az_list, fsx_az_name):
    base_local_queue = _sanitize_name(base_local_queue)
    ordered_azs = list(az_list)
    if fsx_az_name and fsx_az_name in ordered_azs:
        ordered_azs = [fsx_az_name] + [az for az in ordered_azs if az != fsx_az_name]

    queue_order = [base_local_queue]
    if not ordered_azs:
        return queue_order

    preferred = ordered_azs[0]
    for az in ordered_azs:
        if az == preferred:
            continue
        queue_order.append(_sanitize_name(f"{base_local_queue}-{az}"))
    return queue_order


def _build_namespace(name):
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}


def _build_service_account(name, namespace, labels):
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
    }


def _build_cluster_role(name, labels):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": name, "labels": labels},
        "rules": [
            {
                "apiGroups": ["kueue.x-k8s.io"],
                "resources": ["workloads"],
                "verbs": ["get", "list", "watch", "patch"],
            },
            {
                "apiGroups": [""],
                "resources": ["events"],
                "verbs": ["create", "patch", "update"],
            },
        ],
    }


def _build_cluster_role_binding(name, role_name, service_account, namespace, labels):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name, "labels": labels},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": role_name,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": service_account,
                "namespace": namespace,
            }
        ],
    }


def _build_deployment(
    name,
    namespace,
    service_account,
    image,
    labels,
    env_vars,
):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": labels["app.kubernetes.io/name"]}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": service_account,
                    "containers": [
                        {
                            "name": "controller",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "env": [{"name": k, "value": str(v)} for k, v in env_vars.items()],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "250m", "memory": "256Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }


def _create_resources():
    cluster_name = os.environ[CLUSTER_NAME_ENV]
    region = os.environ.get(AWS_REGION_ENV, "us-east-1")

    az_list = _parse_csv(os.environ.get("EKS_AZ_NAMES", ""))
    namespaces = _parse_csv(os.environ.get("USER_NAMESPACES", ""))
    if not namespaces:
        namespaces = ["default"]
    if not az_list:
        raise Exception("EKS_AZ_NAMES is empty; cannot create rotation controller")

    base_local_queue = os.environ.get("KUEUE_LOCAL_QUEUE_NAME", "nemo-az-localqueue").strip()
    fsx_subnet_id = os.environ.get("FSX_SUBNET_ID", "").strip()
    fsx_az_id = os.environ.get("FSX_AVAILABILITY_ZONE_ID", "").strip()
    rotation_image = os.environ.get("ROTATION_IMAGE", "").strip()
    rotation_namespace = os.environ.get("ROTATION_NAMESPACE", "kueue-system").strip()
    rotation_sa = os.environ.get("ROTATION_SERVICE_ACCOUNT", "nemo-az-rotation-controller").strip()
    rotation_log_level = os.environ.get("ROTATION_LOG_LEVEL", "INFO").strip()
    rotation_max = os.environ.get("ROTATION_MAX_ROTATIONS", "0").strip()
    rotation_reason = os.environ.get("ROTATION_EVICTION_REASON", "PodsReadyTimeout").strip()

    if not rotation_image:
        raise Exception("ROTATION_IMAGE is empty")

    _setup_kubeconfig(cluster_name, region)

    fsx_az_name = _resolve_fsx_az_name(fsx_subnet_id, fsx_az_id, region)
    base_local_queue = _sanitize_name(base_local_queue)
    queue_order = _build_queue_order(base_local_queue, az_list, fsx_az_name)

    labels = {
        "app.kubernetes.io/name": "nemo-az-rotation-controller",
        "app.kubernetes.io/component": "controller",
        "app.kubernetes.io/managed-by": "nemo-kueue-az-rotation",
    }

    role_name = _sanitize_name(f"{rotation_namespace}-{rotation_sa}-role")
    binding_name = _sanitize_name(f"{rotation_namespace}-{rotation_sa}-binding")
    deploy_name = _sanitize_name(rotation_sa)

    _apply_resource(_build_namespace(rotation_namespace))
    _apply_resource(_build_service_account(rotation_sa, rotation_namespace, labels))
    _apply_resource(_build_cluster_role(role_name, labels))
    _apply_resource(_build_cluster_role_binding(binding_name, role_name, rotation_sa, rotation_namespace, labels))

    env_vars = {
        "USER_NAMESPACES": ",".join(namespaces),
        "QUEUE_ORDER": ",".join(queue_order),
        "EVICTION_REASON": rotation_reason,
        "MAX_ROTATIONS": rotation_max,
        "LOG_LEVEL": rotation_log_level,
    }

    _apply_resource(
        _build_deployment(
            deploy_name,
            rotation_namespace,
            rotation_sa,
            rotation_image,
            labels,
            env_vars,
        )
    )

    return {
        "Namespace": rotation_namespace,
        "ServiceAccount": rotation_sa,
        "Deployment": deploy_name,
        "QueueOrder": ",".join(queue_order),
        "PreferredFsxAz": fsx_az_name or "",
    }


def _delete_resources():
    rotation_namespace = os.environ.get("ROTATION_NAMESPACE", "kueue-system").strip()
    rotation_sa = os.environ.get("ROTATION_SERVICE_ACCOUNT", "nemo-az-rotation-controller").strip()

    role_name = _sanitize_name(f"{rotation_namespace}-{rotation_sa}-role")
    binding_name = _sanitize_name(f"{rotation_namespace}-{rotation_sa}-binding")
    deploy_name = _sanitize_name(rotation_sa)

    _delete_resource("deployment", deploy_name, namespace=rotation_namespace)
    _delete_resource("clusterrolebinding", binding_name)
    _delete_resource("clusterrole", role_name)
    _delete_resource("serviceaccount", rotation_sa, namespace=rotation_namespace)


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
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {"Status": "FAILED", "Reason": str(exc)},
        )
