import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    user_id = event["user_id"]
    try:
        sagemaker.delete_user_profile(DomainId=domain_id, UserProfileName=user_id)
        event["profile_delete_requested"] = True
        event["profile_delete_retry"] = False
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFound":
            event["profile_delete_retry"] = False
        elif code == "ResourceInUse":
            event["profile_delete_retry"] = True
        else:
            raise
    return event
