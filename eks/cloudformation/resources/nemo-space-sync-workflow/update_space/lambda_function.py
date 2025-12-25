import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def _normalize_custom_filesystems(custom):
    normalized = []
    for cfg in custom or []:
        if isinstance(cfg, dict):
            normalized.append(cfg)
    return normalized


def _ensure_custom_filesystems(settings, *, fsx_id, s3_uri):
    custom = _normalize_custom_filesystems(settings.get("CustomFileSystems"))

    has_s3 = any("S3FileSystem" in cfg for cfg in custom)
    has_fsx = any(
        cfg.get("FSxLustreFileSystem", {}).get("FileSystemId") == fsx_id
        for cfg in custom
    )

    changed = False
    if not has_s3 and s3_uri:
        custom.append({"S3FileSystem": {"S3Uri": s3_uri}})
        changed = True
    if not has_fsx and fsx_id:
        custom.append({"FSxLustreFileSystem": {"FileSystemId": fsx_id}})
        changed = True

    settings["CustomFileSystems"] = custom
    return settings, changed


def handler(event, context):
    domain_id = event["domain_id"]
    space_name = event["space_name"]
    fsx_id = event.get("fsx_id", "")
    s3_uri = event.get("s3_uri", "")

    event["update_attempts"] = int(event.get("update_attempts", 0)) + 1

    try:
        resp = sagemaker.describe_space(DomainId=domain_id, SpaceName=space_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFound":
            event["update_space_action"] = "wait_space"
            return event
        raise

    space_settings = resp.get("SpaceSettings") or {}
    space_settings, changed = _ensure_custom_filesystems(
        space_settings, fsx_id=fsx_id, s3_uri=s3_uri
    )

    if not changed:
        event["update_space_action"] = "noop"
        return event

    try:
        sagemaker.update_space(
            DomainId=domain_id, SpaceName=space_name, SpaceSettings=space_settings
        )
        event["update_space_action"] = "ok"
        return event
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code")
        message = (error.get("Message") or "").lower()
        if code == "ValidationException" and "inservice app" in message:
            event["update_space_action"] = "retry_apps"
            event["next_step"] = "update_space"
            return event
        if code == "ValidationException" and "doesn't have the custom file system" in message:
            event["update_space_action"] = "retry_profile"
            event["next_step"] = "delete_space"
            return event
        raise
