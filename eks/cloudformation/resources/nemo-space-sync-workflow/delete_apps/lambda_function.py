import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    space_name = event["default_space_name"]
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
    event["apps_delete_requested"] = True
    return event
