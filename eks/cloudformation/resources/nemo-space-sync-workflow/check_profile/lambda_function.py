import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    user_id = event["user_id"]
    try:
        resp = sagemaker.describe_user_profile(
            DomainId=domain_id, UserProfileName=user_id
        )
        event["profile_exists"] = True
        event["profile_status"] = resp.get("Status")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFound":
            event["profile_exists"] = False
            event["profile_status"] = None
        else:
            raise
    return event
