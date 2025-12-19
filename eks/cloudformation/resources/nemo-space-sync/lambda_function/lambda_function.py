import json
import os
import time

import boto3

sagemaker = boto3.client("sagemaker")


def wait_for_user_profile(domain_id, user_id, phase):
    print(f"Waiting for UserProfile {user_id} to be InService ({phase}) in domain {domain_id}")
    for _ in range(30):
        try:
            resp = sagemaker.describe_user_profile(
                DomainId=domain_id, UserProfileName=user_id
            )
            status = resp.get("Status")
            print(f"UserProfile status: {status}")
            if status == "InService":
                return True
        except sagemaker.exceptions.ResourceNotFound:
            print("UserProfile not found yet, waiting...")
        time.sleep(2)
    return False


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

    if not fsx_id:
        print("FSX_FILESYSTEM_ID not set")
        return {"statusCode": 500}

    if not wait_for_user_profile(target_domain, user_id, "before update"):
        print("UserProfile not InService before update")
        return {"statusCode": 500}

    print(f"Updating UserProfile {user_id} with FSx in domain {target_domain}")
    sagemaker.update_user_profile(
        DomainId=target_domain,
        UserProfileName=user_id,
        UserSettings={
            "CustomFileSystemConfigs": [
                {
                    "FSxLustreFileSystemConfig": {
                        "FileSystemId": fsx_id,
                        "FileSystemPath": "/lustre",
                    }
                }
            ]
        },
    )

    if not wait_for_user_profile(target_domain, user_id, "after update"):
        print("UserProfile not InService after update")
        return {"statusCode": 500}

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
                }
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
