import os
import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")
events = boto3.client("events")


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


def merge_custom_file_system_configs(existing, *, fsx_id, fsx_path, s3_uri):
    merged = []
    for cfg in existing or []:
        if "FSxLustreFileSystemConfig" in cfg or "S3FileSystemConfig" in cfg:
            continue
        merged.append(cfg)
    merged.append(
        {
            "FSxLustreFileSystemConfig": {
                "FileSystemId": fsx_id,
                "FileSystemPath": fsx_path,
            }
        }
    )
    merged.append(
        {
            "S3FileSystemConfig": {
                "S3Uri": s3_uri,
                "MountPath": "shared",
            }
        }
    )
    return merged


def list_spaces_for_owner(domain_id, user_id):
    spaces = []
    token = None
    while True:
        args = {"DomainIdEquals": domain_id}
        if token:
            args["NextToken"] = token
        resp = sagemaker.list_spaces(**args)
        for space in resp.get("Spaces", []):
            owner = (space.get("OwnershipSettingsSummary") or {}).get(
                "OwnerUserProfileName"
            )
            if owner == user_id:
                spaces.append(space.get("SpaceName"))
        token = resp.get("NextToken")
        if not token:
            break
    return spaces


def get_profile_tags(user_profile_arn):
    try:
        return sagemaker.list_tags(ResourceArn=user_profile_arn).get("Tags", [])
    except ClientError:
        return []


def handler(event, context):
    detail = event.get("detail", {})
    request_params = detail.get("requestParameters", {})
    raw_tags = request_params.get("tags", [])
    tags_map = {t.get("key"): t.get("value") for t in raw_tags if isinstance(t, dict)}

    project_id = os.environ["PROJECT_ID"]
    scope_name = os.environ["SCOPE_NAME"]
    rule_name = os.environ.get("SPACE_SYNC_RULE_NAME") or f"nemo-space-sync-{project_id}"

    try:
        events.disable_rule(Name=rule_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        print(f"Failed to disable rule {rule_name} ({code}); continuing")

    if tags_map.get("AmazonDataZoneProject") != project_id:
        return {"action": "skip", "reason": "project_mismatch"}
    if tags_map.get("AmazonDataZoneScopeName") != scope_name:
        return {"action": "skip", "reason": "scope_mismatch"}

    user_id = request_params.get("userProfileName")
    if not user_id:
        return {"action": "skip", "reason": "missing_user"}

    target_domain = os.environ["TARGET_DOMAIN_ID"]
    fsx_id = os.environ.get("FSX_FILESYSTEM_ID", "").strip()
    if not fsx_id:
        raise RuntimeError("FSX_FILESYSTEM_ID not set")

    s3_uri = s3_shared_uri_from_bucket_arn(os.environ.get("S3_BUCKET_ARN", ""))

    space_tags = merge_required_tags(
        normalize_tags(raw_tags),
        {
            "AmazonDataZoneProject": project_id,
            "AmazonDataZoneDomain": os.environ["DZ_DOMAIN_ID"],
            "AmazonDataZoneScopeName": scope_name,
            "AmazonDataZoneUser": user_id,
        },
    )

    space_name = f"nemo-{user_id}"
    default_space_name = f"default-{user_id}"
    owned_spaces = list_spaces_for_owner(target_domain, user_id)

    if space_name in owned_spaces:
        return {
            "action": "skip",
            "reason": "space_exists",
            "domain_id": target_domain,
            "user_id": user_id,
            "space_name": space_name,
        }

    non_default_spaces = [
        name for name in owned_spaces if name not in {default_space_name, space_name}
    ]
    action = "create_space_only" if non_default_spaces else "recreate_profile"

    payload = {
        "action": action,
        "domain_id": target_domain,
        "user_id": user_id,
        "space_name": space_name,
        "default_space_name": default_space_name,
        "fsx_id": fsx_id,
        "s3_uri": s3_uri,
        "space_tags": space_tags,
        "owned_spaces": owned_spaces,
    }

    if action == "recreate_profile":
        current = sagemaker.describe_user_profile(
            DomainId=target_domain, UserProfileName=user_id
        )
        current_settings = current.get("UserSettings") or {}
        current_custom = current_settings.get("CustomFileSystemConfigs") or []
        current_settings["CustomFileSystemConfigs"] = merge_custom_file_system_configs(
            current_custom, fsx_id=fsx_id, fsx_path=f"/{fsx_id}", s3_uri=s3_uri
        )
        profile_tags = get_profile_tags(current.get("UserProfileArn"))
        if profile_tags:
            profile_tags = merge_required_tags(
                normalize_tags(profile_tags),
                {
                    "AmazonDataZoneProject": project_id,
                    "AmazonDataZoneDomain": os.environ["DZ_DOMAIN_ID"],
                    "AmazonDataZoneScopeName": scope_name,
                    "AmazonDataZoneUser": user_id,
                },
            )
        else:
            profile_tags = space_tags
        payload["user_settings"] = current_settings
        payload["profile_tags"] = profile_tags

    return payload
