import json
import os
import time

import boto3

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
    if len(parts) > 1:
        base = "/".join(parts[:-1])
    else:
        base = value
    return f"s3://{base}/shared"


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
    if pre_status == "Update_Failed":
        print("UserProfile is Update_Failed; retrying update")

    print(f"Updating UserProfile {user_id} with FSx in domain {target_domain}")
    sagemaker.update_user_profile(
        DomainId=target_domain,
        UserProfileName=user_id,
        UserSettings={
            "CustomFileSystemConfigs": [
                {
                    "FSxLustreFileSystemConfig": {
                        "FileSystemId": fsx_id,
                        "FileSystemPath": f"/{fsx_id}",
                    }
                },
                {
                    "S3FileSystemConfig": {
                        "S3Uri": s3_shared_uri,
                        "MountPath": "shared",
                    }
                }
            ]
        },
    )

    post_status = wait_for_user_profile(target_domain, user_id, "after update")
    if post_status != "InService":
        failure_reason = None
        try:
            resp = sagemaker.describe_user_profile(
                DomainId=target_domain, UserProfileName=user_id
            )
            failure_reason = resp.get("FailureReason")
        except Exception:
            pass
        print("UserProfile not InService after update")
        raise RuntimeError(
            f"UserProfile not InService after update (status={post_status}, "
            f"failure_reason={failure_reason})"
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
        Tags=[
            {"Key": "AmazonDataZoneProject", "Value": os.environ["PROJECT_ID"]},
            {"Key": "AmazonDataZoneDomain", "Value": os.environ["DZ_DOMAIN_ID"]},
            {"Key": "AmazonDataZoneScopeName", "Value": os.environ["SCOPE_NAME"]},
            {"Key": "AmazonDataZoneUser", "Value": user_id},
        ],
    )
    print(f"Created Space: {space_name}")
    return {"statusCode": 200}
