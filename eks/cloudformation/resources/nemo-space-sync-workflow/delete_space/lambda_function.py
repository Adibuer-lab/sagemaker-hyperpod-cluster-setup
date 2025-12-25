import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    space_name = event["default_space_name"]
    try:
        sagemaker.delete_space(DomainId=domain_id, SpaceName=space_name)
        event["space_delete_requested"] = True
        event["space_delete_retry"] = False
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFound":
            event["space_delete_retry"] = False
        elif code == "ResourceInUse":
            event["space_delete_retry"] = True
        else:
            raise
    return event
