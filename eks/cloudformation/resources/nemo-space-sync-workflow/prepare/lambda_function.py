import os
import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")

SPACE_SYNC_TAG_KEY = "NeMoSpaceSyncManaged"


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


def normalize_tags(raw_tags):
    normalized = []
    for tag in raw_tags or []:
        key = tag.get("Key") or tag.get("key")
        value = tag.get("Value") or tag.get("value")
        if key is None or value is None:
            continue
        normalized.append({"Key": key, "Value": value})
    return normalized


def merge_required_tags(tags, required):
    merged = {t["Key"]: t["Value"] for t in tags}
    for key, value in required.items():
        merged.setdefault(key, value)
    return [{"Key": k, "Value": v} for k, v in merged.items()]


def _extract_user_id(request_params, tags_map):
    ownership = request_params.get("ownershipSettings") or {}
    user_id = ownership.get("OwnerUserProfileName") or ownership.get(
        "ownerUserProfileName"
    )
    if user_id:
        return user_id
    return tags_map.get("AmazonDataZoneUser")


def _has_required_s3_config(custom_configs, *, s3_uri):
    for cfg in custom_configs or []:
        s3_cfg = cfg.get("S3FileSystemConfig") or {}
        if not s3_cfg:
            continue
        mount_path = (s3_cfg.get("MountPath") or "").strip().strip("/")
        if mount_path != "shared":
            continue
        if s3_cfg.get("S3Uri") == s3_uri:
            return True
    return False


def _has_required_fsx_config(custom_configs, *, fsx_id):
    for cfg in custom_configs or []:
        fsx_cfg = cfg.get("FSxLustreFileSystemConfig") or {}
        if fsx_cfg.get("FileSystemId") == fsx_id:
            return True
    return False


def _merge_fsx_into_defaults(default_settings, *, fsx_id):
    settings = dict(default_settings or {})
    custom = list(settings.get("CustomFileSystemConfigs") or [])
    if not _has_required_fsx_config(custom, fsx_id=fsx_id):
        custom.append(
            {
                "FSxLustreFileSystemConfig": {
                    "FileSystemId": fsx_id,
                    "FileSystemPath": f"/{fsx_id}",
                }
            }
        )
    settings["CustomFileSystemConfigs"] = custom
    return settings


def handler(event, context):
    detail = event.get("detail", {})
    request_params = detail.get("requestParameters", {})
    raw_tags = request_params.get("tags", [])
    normalized_tags = normalize_tags(raw_tags)
    tags_map = {t.get("Key"): t.get("Value") for t in normalized_tags}

    project_id = os.environ["PROJECT_ID"]
    scope_name = os.environ["SCOPE_NAME"]

    if tags_map.get(SPACE_SYNC_TAG_KEY) == "true":
        return {"action": "skip", "reason": "managed_by_space_sync"}
    if tags_map.get("AmazonDataZoneProject") != project_id:
        return {"action": "skip", "reason": "project_mismatch"}
    if tags_map.get("AmazonDataZoneScopeName") != scope_name:
        return {"action": "skip", "reason": "scope_mismatch"}

    user_id = _extract_user_id(request_params, tags_map)
    if not user_id:
        return {"action": "skip", "reason": "missing_user"}

    space_name = request_params.get("spaceName") or request_params.get("SpaceName")
    if not space_name:
        return {"action": "skip", "reason": "missing_space"}

    default_space_name = f"default-{user_id}"
    if space_name != default_space_name:
        return {"action": "skip", "reason": "non_default_space"}

    target_domain = os.environ["TARGET_DOMAIN_ID"]
    fsx_id = os.environ.get("FSX_FILESYSTEM_ID", "").strip()
    if not fsx_id:
        raise RuntimeError("FSX_FILESYSTEM_ID not set")

    s3_uri = s3_shared_uri_from_bucket_arn(os.environ.get("S3_BUCKET_ARN", ""))

    space_tags = merge_required_tags(
        normalized_tags,
        {
            "AmazonDataZoneProject": project_id,
            "AmazonDataZoneDomain": os.environ["DZ_DOMAIN_ID"],
            "AmazonDataZoneScopeName": scope_name,
            "AmazonDataZoneUser": user_id,
        },
    )

    need_profile_recreate = True
    try:
        profile = sagemaker.describe_user_profile(
            DomainId=target_domain, UserProfileName=user_id
        )
        custom_configs = (
            (profile.get("UserSettings") or {}).get("CustomFileSystemConfigs") or []
        )
        need_profile_recreate = not (
            _has_required_s3_config(custom_configs, s3_uri=s3_uri)
            and _has_required_fsx_config(custom_configs, fsx_id=fsx_id)
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFound":
            raise

    default_settings = sagemaker.describe_domain(DomainId=target_domain).get(
        "DefaultUserSettings", {}
    )
    user_settings = _merge_fsx_into_defaults(default_settings, fsx_id=fsx_id)

    space_exists = False
    space_settings = {}
    try:
        space = sagemaker.describe_space(
            DomainId=target_domain, SpaceName=space_name
        )
        space_exists = True
        space_settings = space.get("SpaceSettings") or {}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFound":
            raise

    payload = {
        "domain_id": target_domain,
        "user_id": user_id,
        "space_name": space_name,
        "default_space_name": default_space_name,
        "fsx_id": fsx_id,
        "s3_uri": s3_uri,
        "space_tags": space_tags,
        "space_settings": space_settings,
        "space_exists": space_exists,
        "user_settings": user_settings,
        "profile_tags": space_tags,
    }

    if need_profile_recreate:
        payload["action"] = "recreate_profile"
        payload["next_step"] = "delete_space"
    else:
        payload["action"] = "update_space"
        payload["next_step"] = "update_space"

    if not space_exists and payload["action"] == "update_space":
        payload["action"] = "update_space"
        payload["next_step"] = "update_space"

    return payload
