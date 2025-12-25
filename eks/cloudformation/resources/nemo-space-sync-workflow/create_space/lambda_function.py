import os
import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")

SPACE_SYNC_TAG_KEY = "NeMoSpaceSyncManaged"


def _ensure_custom_filesystems(settings, *, fsx_id, s3_uri):
    custom = []
    for cfg in settings.get("CustomFileSystems", []) or []:
        if isinstance(cfg, dict):
            custom.append(cfg)
    has_s3 = any("S3FileSystem" in cfg for cfg in custom)
    has_fsx = any(
        cfg.get("FSxLustreFileSystem", {}).get("FileSystemId") == fsx_id
        for cfg in custom
        if isinstance(cfg, dict)
    )
    if not has_s3 and s3_uri:
        custom.append({"S3FileSystem": {"S3Uri": s3_uri}})
    if not has_fsx and fsx_id:
        custom.append({"FSxLustreFileSystem": {"FileSystemId": fsx_id}})
    settings["CustomFileSystems"] = custom
    return settings


def handler(event, context):
    domain_id = event["domain_id"]
    user_id = event["user_id"]
    space_name = event["space_name"]
    fsx_id = event["fsx_id"]
    s3_uri = event["s3_uri"]
    space_tags = event.get("space_tags") or []
    space_settings = event.get("space_settings") or {}

    region = os.environ.get("AWS_REGION", "")
    image_arn = f"arn:aws:sagemaker:{region}:885854791233:image/sagemaker-distribution-cpu"

    if not space_settings:
        space_settings = {
            "AppType": "JupyterLab",
            "SpaceStorageSettings": {"EbsStorageSettings": {"EbsVolumeSizeInGb": 16}},
            "SpaceManagedResources": "ENABLED",
            "RemoteAccess": "DISABLED",
            "JupyterLabAppSettings": {
                "DefaultResourceSpec": {
                    "SageMakerImageArn": image_arn,
                    "SageMakerImageVersionAlias": "2.11",
                    "InstanceType": "ml.t3.medium",
                }
            },
        }
    if "AppType" not in space_settings:
        space_settings["AppType"] = "JupyterLab"
    _ensure_custom_filesystems(space_settings, fsx_id=fsx_id, s3_uri=s3_uri)

    if SPACE_SYNC_TAG_KEY not in {t.get("Key") for t in space_tags}:
        space_tags.append({"Key": SPACE_SYNC_TAG_KEY, "Value": "true"})

    try:
        sagemaker.create_space(
            DomainId=domain_id,
            SpaceName=space_name,
            OwnershipSettings={"OwnerUserProfileName": user_id},
            SpaceSharingSettings={"SharingType": "Private"},
            SpaceSettings=space_settings,
            Tags=space_tags,
        )
        event["space_created"] = True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"ResourceInUse", "ValidationException"}:
            event["space_created"] = False
        else:
            raise
    return event
