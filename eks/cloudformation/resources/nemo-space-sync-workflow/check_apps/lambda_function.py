import boto3

sagemaker = boto3.client("sagemaker")


def handler(event, context):
    domain_id = event["domain_id"]
    space_name = event["space_name"]
    token = None
    apps = []
    while True:
        args = {"DomainIdEquals": domain_id, "SpaceNameEquals": space_name}
        if token:
            args["NextToken"] = token
        resp = sagemaker.list_apps(**args)
        apps.extend(resp.get("Apps", []))
        token = resp.get("NextToken")
        if not token:
            break
    active_apps = [
        app for app in apps if (app.get("Status") or "").lower() != "deleted"
    ]
    statuses = [(app.get("Status") or "").lower() for app in apps]
    blocking_statuses = {"deleted", "stopped", "failed"}
    apps_blocking_update = sum(
        1 for status in statuses if status and status not in blocking_statuses
    )
    event["apps_remaining"] = len(active_apps)
    event["apps_statuses"] = sorted(
        {app.get("Status") for app in apps if app.get("Status")}
    )
    event["apps_blocking_update"] = apps_blocking_update
    return event
