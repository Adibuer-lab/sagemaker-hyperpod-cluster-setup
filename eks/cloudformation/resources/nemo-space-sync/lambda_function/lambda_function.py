import json
import os
import time

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
    if len(parts) > 1:
        base = "/".join(parts[:-1])
    else:
        base = value
    return f"s3://{base}/shared"


def normalize_tags(raw_tags: list | None) -> list:
    normalized = []
    for tag in raw_tags or []:
        key = tag.get("Key") or tag.get("key")
        value = tag.get("Value") or tag.get("value")
        if key is None or value is None:
            continue
        normalized.append({"Key": key, "Value": value})
    return normalized


def merge_required_tags(tags: list, required: dict) -> list:
    merged = {t["Key"]: t["Value"] for t in tags}
    for key, value in required.items():
        merged.setdefault(key, value)
    return [{"Key": k, "Value": v} for k, v in merged.items()]


def merge_custom_file_system_configs(
    existing: list | None, *, fsx_id: str, fsx_path: str, s3_uri: str
) -> list:
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


def has_custom_fs(configs: list | None, key: str) -> bool:
    return any(key in (cfg or {}) for cfg in (configs or []))


def wait_for_user_profile_deleted(domain_id, user_id):
    for _ in range(60):
        try:
            sagemaker.describe_user_profile(
                DomainId=domain_id, UserProfileName=user_id
            )
        except sagemaker.exceptions.ResourceNotFound:
            return True
        time.sleep(5)
    return False


def wait_for_space_deleted(domain_id, space_name):
    for _ in range(60):
        try:
            sagemaker.describe_space(DomainId=domain_id, SpaceName=space_name)
        except sagemaker.exceptions.ResourceNotFound:
            return True
        time.sleep(5)
    return False


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


def delete_apps_for_space(domain_id, space_name):
    token = None
    while True:
        args = {"DomainIdEquals": domain_id, "SpaceNameEquals": space_name}
        if token:
            args["NextToken"] = token
        resp = sagemaker.list_apps(**args)
        for app in resp.get("Apps", []):
            try:
                sagemaker.delete_app(
                    DomainId=domain_id,
                    SpaceName=space_name,
                    AppType=app.get("AppType"),
                    AppName=app.get("AppName"),
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ValidationException":
                    raise
        token = resp.get("NextToken")
        if not token:
            break


def wait_for_apps_deleted(domain_id, space_name, *, max_attempts=60, sleep_seconds=5):
    for _ in range(max_attempts):
        token = None
        apps = []
        while True:
            args = {"DomainIdEquals": domain_id, "SpaceNameEquals": space_name}
            if token:
                args["NextToken"] = token
            resp = sagemaker.list_apps(**args)
            apps.extend(resp.get("Apps", []))
            token = resp.get("NextToken")
            if not token:
                break
        if not apps:
            return True
        delete_apps_for_space(domain_id, space_name)
        time.sleep(sleep_seconds)
    return False


def disable_rule(rule_name: str):
    events.disable_rule(Name=rule_name)


def get_profile_tags(user_profile_arn):
    try:
        return sagemaker.list_tags(ResourceArn=user_profile_arn).get("Tags", [])
    except ClientError:
        return []


def wait_for_user_profile(domain_id, user_id, phase, *, allow_update_failed=False):
    print(f"Waiting for UserProfile {user_id} to be InService ({phase}) in domain {domain_id}")
    for _ in range(30):
        try:
            resp = sagemaker.describe_user_profile(
                DomainId=domain_id, UserProfileName=user_id
            )
            status = resp.get("Status")
            print(f"UserProfile status: {status}")
            if status == "InService":
                return status
            if allow_update_failed and status == "Update_Failed":
                return status
            if status in {"Failed", "Delete_Failed"}:
                return status
        except sagemaker.exceptions.ResourceNotFound:
            print("UserProfile not found yet, waiting...")
        time.sleep(2)
    return None


def handler(event, context):
    print(json.dumps(event))
    detail = event.get("detail", {})
    request_params = detail.get("requestParameters", {})
    tags = {t.get("key"): t.get("value") for t in request_params.get("tags", [])}
    space_tags = normalize_tags(request_params.get("tags", []))

    if tags.get("AmazonDataZoneProject") != os.environ["PROJECT_ID"]:
        print("Skipping - different project")
        return
    if tags.get("AmazonDataZoneScopeName") != os.environ["SCOPE_NAME"]:
        print("Skipping - different scope")
        return

    user_id = request_params.get("userProfileName")
    target_domain = os.environ["TARGET_DOMAIN_ID"]
    fsx_id = os.environ.get("FSX_FILESYSTEM_ID")
    s3_bucket_arn = os.environ.get("S3_BUCKET_ARN", "")
    s3_shared_uri = s3_shared_uri_from_bucket_arn(s3_bucket_arn)
    rule_name = os.environ.get("SPACE_SYNC_RULE_NAME") or (
        f"nemo-space-sync-{os.environ['PROJECT_ID']}"
    )
        try:
            disable_rule(rule_name)
            print(f"Disabled rule {rule_name} (one-time run)")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            print(f"Failed to disable rule {rule_name} ({code}); continuing")
    space_tags = merge_required_tags(
        space_tags,
        {
            "AmazonDataZoneProject": os.environ["PROJECT_ID"],
            "AmazonDataZoneDomain": os.environ["DZ_DOMAIN_ID"],
            "AmazonDataZoneScopeName": os.environ["SCOPE_NAME"],
            "AmazonDataZoneUser": user_id,
        },
    )

    if not fsx_id:
        print("FSX_FILESYSTEM_ID not set")
        raise RuntimeError("FSX_FILESYSTEM_ID not set")

    pre_status = wait_for_user_profile(
        target_domain, user_id, "before update", allow_update_failed=True
    )
    if pre_status is None:
        print("UserProfile not InService before update (timeout)")
        raise RuntimeError("UserProfile not InService before update (timeout)")
    if pre_status in {"Failed", "Delete_Failed"}:
        print(f"UserProfile in terminal state before update: {pre_status}")
        raise RuntimeError(f"UserProfile in terminal state before update: {pre_status}")

    space_name = f"nemo-{user_id}"
    default_space_name = f"default-{user_id}"
    owned_spaces = list_spaces_for_owner(target_domain, user_id)

    if space_name in owned_spaces:
        print(f"Space {space_name} already exists; skipping create and profile changes")
        return {"statusCode": 200}

    non_default_spaces = [
        name for name in owned_spaces if name not in {default_space_name, space_name}
    ]
    can_recreate_profile = owned_spaces == [default_space_name]

    if non_default_spaces:
        print(
            "Found non-default owned spaces; leaving user profile unchanged and "
            "creating nemo space only"
        )
    elif can_recreate_profile:
        print(
            "Only default space exists; deleting it to recreate user profile with "
            "FSx and S3"
        )
        current = sagemaker.describe_user_profile(
            DomainId=target_domain, UserProfileName=user_id
        )
        current_settings = current.get("UserSettings") or {}
        current_custom = current_settings.get("CustomFileSystemConfigs") or []
        merged_custom = merge_custom_file_system_configs(
            current_custom, fsx_id=fsx_id, fsx_path=f"/{fsx_id}", s3_uri=s3_shared_uri
        )
        current_settings["CustomFileSystemConfigs"] = merged_custom

        profile_tags = get_profile_tags(current.get("UserProfileArn"))
        if not profile_tags:
            profile_tags = space_tags
        else:
            profile_tags = merge_required_tags(
                normalize_tags(profile_tags),
                {
                    "AmazonDataZoneProject": os.environ["PROJECT_ID"],
                    "AmazonDataZoneDomain": os.environ["DZ_DOMAIN_ID"],
                    "AmazonDataZoneScopeName": os.environ["SCOPE_NAME"],
                    "AmazonDataZoneUser": user_id,
                },
            )

        delete_apps_for_space(target_domain, default_space_name)
        if not wait_for_apps_deleted(target_domain, default_space_name):
            raise RuntimeError(f"Timed out deleting apps in space {default_space_name}")
        try:
            sagemaker.delete_space(
                DomainId=target_domain, SpaceName=default_space_name
            )
        except sagemaker.exceptions.ResourceNotFound:
            pass
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceInUse":
                if not wait_for_apps_deleted(target_domain, default_space_name):
                    raise RuntimeError(
                        f"Timed out deleting apps in space {default_space_name}"
                    )
                sagemaker.delete_space(
                    DomainId=target_domain, SpaceName=default_space_name
                )
            else:
                raise
        if not wait_for_space_deleted(target_domain, default_space_name):
            raise RuntimeError(f"Timed out deleting space {default_space_name}")

        sagemaker.delete_user_profile(
            DomainId=target_domain, UserProfileName=user_id
        )
        if not wait_for_user_profile_deleted(target_domain, user_id):
            raise RuntimeError("Timed out deleting user profile")

        sagemaker.create_user_profile(
            DomainId=target_domain,
            UserProfileName=user_id,
            UserSettings=current_settings,
            Tags=profile_tags,
        )
        post_status = wait_for_user_profile(
            target_domain, user_id, "after recreate"
        )
        if post_status != "InService":
            raise RuntimeError(
                f"UserProfile not InService after recreate (status={post_status})"
            )
    else:
        print(
            "No owned spaces found; leaving user profile unchanged and creating nemo "
            "space only"
        )

    region = os.environ.get("AWS_REGION", "")
    image_arn = (
        f"arn:aws:sagemaker:{region}:885854791233:image/sagemaker-distribution-cpu"
    )
    space_name = f"nemo-{user_id}"

    print(f"Creating Space {space_name} in domain {target_domain}")
    sagemaker.create_space(
        DomainId=target_domain,
        SpaceName=space_name,
        OwnershipSettings={"OwnerUserProfileName": user_id},
        SpaceSharingSettings={"SharingType": "Private"},
        SpaceSettings={
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
            "CustomFileSystems": [
                {
                    "FSxLustreFileSystem": {
                        "FileSystemId": fsx_id,
                    }
                },
                {
                    "S3FileSystem": {
                        "S3Uri": s3_shared_uri,
                    }
                },
            ],
        },
        Tags=space_tags,
    )
    print(f"Created Space: {space_name}")
    return {"statusCode": 200}
