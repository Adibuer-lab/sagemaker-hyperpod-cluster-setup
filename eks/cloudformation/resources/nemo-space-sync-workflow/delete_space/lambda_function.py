import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")

def handler(event, context):
    domain_id = event["domain_id"]
    space_name = event["space_name"]
    try:
        sagemaker.delete_space(DomainId=domain_id, SpaceName=space_name)
        event["space_delete_requested"] = True
        event["space_delete_retry"] = False
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code")
        message = (error.get("Message") or "").lower()
        if code == "ResourceNotFound":
            event["space_delete_retry"] = False
        elif code == "ResourceInUse" or (
            code == "ValidationException" and ("app" in message or "in use" in message)
        ):
            event["space_delete_retry"] = True
        else:
            raise
    return event
