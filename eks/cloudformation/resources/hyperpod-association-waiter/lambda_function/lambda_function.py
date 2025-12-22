import base64
import json
import logging
import os
import ssl
import time
import urllib.parse
import urllib.request

import boto3
import cfnresponse
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    CloudFormation custom resource to wait until a HyperPod cluster is
    associated with the expected EKS cluster ARN.
    """
    logger.info("Received event: %s", json.dumps(event))

    request_type = event.get("RequestType", "")
    physical_id = event.get("PhysicalResourceId") or "HyperPodAssociationWaiter"

    if request_type == "Delete":
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
        return

    props = event.get("ResourceProperties", {})
    cluster_name = props.get("HyperPodClusterName")
    expected_eks_arn = props.get("EksClusterArn")
    min_node_count = _parse_int(props.get("MinNodeCount"), default=0)
    min_ready_eks_nodes = _parse_int(props.get("MinReadyEksNodes"), default=0)
    eks_cluster_name = props.get("EksClusterName") or _cluster_name_from_arn(expected_eks_arn)
    eks_label_selector = (props.get("EksNodeLabelSelector") or "").strip()

    if not cluster_name or not expected_eks_arn:
        reason = "HyperPodClusterName and EksClusterArn are required"
        logger.error(reason)
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Reason": reason}, physical_id)
        return

    wait_seconds = int(props.get("WaitTimeoutSeconds", os.environ.get("WAIT_TIMEOUT_SECONDS", "900")))
    poll_interval = int(props.get("PollIntervalSeconds", os.environ.get("POLL_INTERVAL_SECONDS", "15")))

    sagemaker = boto3.client("sagemaker")
    deadline = time.time() + wait_seconds

    last_status = None
    last_eks_arn = None
    last_error = None
    last_running_nodes = 0
    last_ready_eks_nodes = 0

    while time.time() < deadline:
        try:
            resp = sagemaker.describe_cluster(ClusterName=cluster_name)
            last_status = resp.get("ClusterStatus")
            last_eks_arn = resp.get("Orchestrator", {}).get("Eks", {}).get("ClusterArn")

            logger.info(
                "ClusterStatus=%s, EksClusterArn=%s (expected=%s)",
                last_status,
                last_eks_arn,
                expected_eks_arn,
            )

            if last_status == "InService" and last_eks_arn == expected_eks_arn:
                if min_node_count > 0:
                    last_running_nodes = _count_running_nodes(sagemaker, cluster_name, min_node_count)
                    logger.info(
                        "RunningNodes=%s (min=%s)",
                        last_running_nodes,
                        min_node_count,
                    )
                    if last_running_nodes < min_node_count:
                        last_error = (
                            f"ClusterStatus={last_status}, EksClusterArn={last_eks_arn}, "
                            f"RunningNodes={last_running_nodes}"
                        )
                        time.sleep(poll_interval)
                        continue

                if min_ready_eks_nodes > 0:
                    if not eks_cluster_name:
                        last_error = "EksClusterName is required when MinReadyEksNodes > 0"
                        time.sleep(poll_interval)
                        continue
                    try:
                        last_ready_eks_nodes = _count_ready_eks_nodes(
                            eks_cluster_name,
                            eks_label_selector,
                        )
                        logger.info(
                            "ReadyEksNodes=%s (min=%s, selector=%s)",
                            last_ready_eks_nodes,
                            min_ready_eks_nodes,
                            eks_label_selector or "<all>",
                        )
                    except Exception as exc:
                        last_error = f"Failed to query EKS nodes: {exc}"
                        logger.warning(last_error)
                        time.sleep(poll_interval)
                        continue
                    if last_ready_eks_nodes < min_ready_eks_nodes:
                        last_error = (
                            f"ClusterStatus={last_status}, EksClusterArn={last_eks_arn}, "
                            f"RunningNodes={last_running_nodes}, ReadyEksNodes={last_ready_eks_nodes}"
                        )
                        time.sleep(poll_interval)
                        continue

                data = {
                    "ClusterStatus": last_status,
                    "EksClusterArn": last_eks_arn,
                    "RunningNodeCount": last_running_nodes,
                    "ReadyEksNodeCount": last_ready_eks_nodes,
                }
                cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physical_id)
                return

            last_error = f"ClusterStatus={last_status}, EksClusterArn={last_eks_arn}, RunningNodes={last_running_nodes}"
        except ClientError as err:
            last_error = str(err)
            logger.warning("DescribeCluster failed: %s", last_error)

        time.sleep(poll_interval)

    if min_node_count > 0:
        reason = (
            f"Timed out waiting for HyperPod↔EKS association and >= {min_node_count} running nodes. "
            f"Last seen: {last_error}"
        )
    else:
        reason = f"Timed out waiting for HyperPod↔EKS association. Last seen: {last_error}"
    logger.error(reason)
    cfnresponse.send(
        event,
        context,
        cfnresponse.FAILED,
        {
            "Reason": reason,
            "ClusterStatus": last_status,
            "EksClusterArn": last_eks_arn,
            "RunningNodeCount": last_running_nodes,
            "ReadyEksNodeCount": last_ready_eks_nodes,
        },
        physical_id,
    )


def _count_running_nodes(sagemaker, cluster_name, min_node_count):
    running = 0
    next_token = None
    while True:
        kwargs = {"ClusterName": cluster_name, "MaxResults": 50}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = sagemaker.list_cluster_nodes(**kwargs)
        for node in resp.get("ClusterNodeSummaries", []):
            status = node.get("InstanceStatus", {}).get("Status")
            if status == "Running":
                running += 1
                if running >= min_node_count:
                    return running
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return running


def _parse_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _cluster_name_from_arn(arn):
    if not arn:
        return None
    parts = arn.split("/", 1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return None


def _get_eks_cluster_info(eks_cluster_name):
    eks = boto3.client("eks")
    cluster = eks.describe_cluster(name=eks_cluster_name)["cluster"]
    return cluster["endpoint"], cluster["certificateAuthority"]["data"]


def _get_eks_token(cluster_name):
    session = boto3.Session(region_name=os.environ.get("AWS_REGION"))
    sts = session.client("sts")

    def retrieve_k8s_aws_id(params, context, **_kwargs):
        if "x-k8s-aws-id" in params:
            context["x-k8s-aws-id"] = params.pop("x-k8s-aws-id")

    def inject_k8s_aws_id_header(request, **_kwargs):
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


def _k8s_request(endpoint, ca_data, token, method, path):
    url = f"{endpoint}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=base64.b64decode(ca_data).decode())
    req = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return resp.status, resp.read().decode()


def _node_ready(node):
    for condition in node.get("status", {}).get("conditions", []):
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return True
    return False


def _count_ready_eks_nodes(eks_cluster_name, label_selector):
    endpoint, ca_data = _get_eks_cluster_info(eks_cluster_name)
    token = _get_eks_token(eks_cluster_name)
    path = "/api/v1/nodes"
    if label_selector:
        path += "?labelSelector=" + urllib.parse.quote(label_selector, safe="")
    status, body = _k8s_request(endpoint, ca_data, token, "GET", path)
    if status != 200:
        raise Exception(f"EKS API returned {status}: {body}")
    data = json.loads(body)
    count = 0
    for node in data.get("items", []):
        if _node_ready(node):
            count += 1
    return count
