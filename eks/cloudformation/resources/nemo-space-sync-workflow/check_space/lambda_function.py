import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    space_name = event["default_space_name"]
    try:
        sagemaker.describe_space(DomainId=domain_id, SpaceName=space_name)
        event["space_exists"] = True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFound":
            event["space_exists"] = False
        else:
            raise
    return event
