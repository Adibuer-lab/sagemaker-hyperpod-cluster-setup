import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    user_id = event["user_id"]
    user_settings = event.get("user_settings") or {}
    profile_tags = event.get("profile_tags") or []
    try:
        sagemaker.create_user_profile(
            DomainId=domain_id,
            UserProfileName=user_id,
            UserSettings=user_settings,
            Tags=profile_tags,
        )
        event["profile_created"] = True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"ResourceInUse", "ValidationException"}:
            event["profile_created"] = False
        else:
            raise
    return event
