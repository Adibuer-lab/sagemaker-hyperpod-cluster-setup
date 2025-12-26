import os
import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def s3_shared_uri_from_bucket_arn(bucket_arn: str) -> str:
    if not bucket_arn:
        raise RuntimeError("S3_BUCKET_ARN not set")
    value = bucket_arn.strip()
    if value.startswith("arn:aws:s3:::"):
        value = value[len("arn:aws:s3:::") :]
    elif value.startswith("s3://"):
        value = value[len("s3://") :]
    value = value.strip("/")
    parts = value.split("/")
    base = "/".join(parts[:-1]) if len(parts) > 1 else value
    return f"s3://{base}/shared"


def _tags_to_map(tags):
    return {t.get("Key"): t.get("Value") for t in tags or []}


def _get_request_param(request_params, *names):
    for name in names:
        value = request_params.get(name)
        if value:
            return value
    return None


def handler(event, context):
    detail = event.get("detail", {})
    request_params = detail.get("requestParameters", {})

    domain_id = _get_request_param(request_params, "DomainId", "domainId")
    space_name = _get_request_param(request_params, "SpaceName", "spaceName")

    if not domain_id or not space_name:
        return {"action": "skip", "reason": "missing_domain_or_space"}

    sync_role_arn = os.environ.get("SPACE_SYNC_ROLE_ARN")
    issuer_arn = (
        detail.get("userIdentity", {})
        .get("sessionContext", {})
        .get("sessionIssuer", {})
        .get("arn")
    )
    if sync_role_arn and issuer_arn == sync_role_arn:
        return {"action": "skip", "reason": "automation_update"}

    try:
        space = sagemaker.describe_space(DomainId=domain_id, SpaceName=space_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFound":
            return {"action": "skip", "reason": "space_not_found"}
        raise

    owner = (space.get("OwnershipSettings") or {}).get("OwnerUserProfileName")
    if not owner:
        return {"action": "skip", "reason": "missing_owner"}

    if space_name != f"default-{owner}":
        return {"action": "skip", "reason": "non_default_space"}

    tags = sagemaker.list_tags(ResourceArn=space["SpaceArn"]).get("Tags", [])
    tags_map = _tags_to_map(tags)
    if tags_map.get("AmazonDataZoneProject") != os.environ.get("PROJECT_ID"):
        return {"action": "skip", "reason": "project_mismatch"}
    if tags_map.get("AmazonDataZoneScopeName") != os.environ.get("SCOPE_NAME"):
        return {"action": "skip", "reason": "scope_mismatch"}

    fsx_id = os.environ.get("FSX_FILESYSTEM_ID", "").strip()
    if not fsx_id:
        raise RuntimeError("FSX_FILESYSTEM_ID not set")

    s3_uri = s3_shared_uri_from_bucket_arn(os.environ.get("S3_BUCKET_ARN", ""))

    return {
        "action": "update_space",
        "domain_id": domain_id,
        "space_name": space_name,
        "user_id": owner,
        "fsx_id": fsx_id,
        "s3_uri": s3_uri,
    }
