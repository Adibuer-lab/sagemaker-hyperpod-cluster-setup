import boto3
from botocore.exceptions import ClientError

sagemaker = boto3.client("sagemaker")


def _should_stop(status):
    status = (status or "").lower()
    return status not in {"deleted", "deleting", "stopped", "stopping"}


def handler(event, context):
    domain_id = event["domain_id"]
    space_name = event["space_name"]
    token = None

    while True:
        args = {"DomainIdEquals": domain_id, "SpaceNameEquals": space_name}
        if token:
            args["NextToken"] = token
        resp = sagemaker.list_apps(**args)
        for app in resp.get("Apps", []):
            if not _should_stop(app.get("Status")):
                continue
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

    event["apps_stop_requested"] = True
    return event
