import json
import logging
import os
import time

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
                    if last_running_nodes >= min_node_count:
                        data = {
                            "ClusterStatus": last_status,
                            "EksClusterArn": last_eks_arn,
                            "RunningNodeCount": last_running_nodes,
                        }
                        cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physical_id)
                        return
                else:
                    data = {"ClusterStatus": last_status, "EksClusterArn": last_eks_arn}
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
