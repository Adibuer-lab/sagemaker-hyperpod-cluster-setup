import os
import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    user_id = event["user_id"]
    space_name = event["space_name"]
    fsx_id = event["fsx_id"]
    s3_uri = event["s3_uri"]
    space_tags = event.get("space_tags") or []

    region = os.environ.get("AWS_REGION", "")
    image_arn = f"arn:aws:sagemaker:{region}:885854791233:image/sagemaker-distribution-cpu"

    try:
        sagemaker.create_space(
            DomainId=domain_id,
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
                    {"FSxLustreFileSystem": {"FileSystemId": fsx_id}},
                    {"S3FileSystem": {"S3Uri": s3_uri}},
                ],
            },
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
