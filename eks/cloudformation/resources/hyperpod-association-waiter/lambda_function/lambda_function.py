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
                data = {"ClusterStatus": last_status, "EksClusterArn": last_eks_arn}
                cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physical_id)
                return

            last_error = f"ClusterStatus={last_status}, EksClusterArn={last_eks_arn}"
        except ClientError as err:
            last_error = str(err)
            logger.warning("DescribeCluster failed: %s", last_error)

        time.sleep(poll_interval)

    reason = f"Timed out waiting for HyperPod↔EKS association. Last seen: {last_error}"
    logger.error(reason)
    cfnresponse.send(
        event,
        context,
        cfnresponse.FAILED,
        {"Reason": reason, "ClusterStatus": last_status, "EksClusterArn": last_eks_arn},
        physical_id,
    )
