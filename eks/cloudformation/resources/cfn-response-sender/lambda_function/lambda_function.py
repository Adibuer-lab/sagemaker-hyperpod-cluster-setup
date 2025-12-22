import json
import urllib.error
import urllib.request


def _send_response(
    *,
    response_url,
    status,
    reason,
    data,
    physical_resource_id,
    stack_id,
    request_id,
    logical_resource_id,
    no_echo=False,
):
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason,
            "PhysicalResourceId": physical_resource_id,
            "StackId": stack_id,
            "RequestId": request_id,
            "LogicalResourceId": logical_resource_id,
            "NoEcho": no_echo,
            "Data": data or {},
        }
    )
    req = urllib.request.Request(
        response_url,
        data=body.encode(),
        method="PUT",
        headers={
            "content-type": "",
            "content-length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"CloudFormation response status: {resp.status}")
    except urllib.error.HTTPError as exc:
        print(f"Failed to send CloudFormation response: HTTP {exc.code} {exc.reason}")
        print(exc.read().decode() if exc.fp else "")
    except Exception as exc:
        print(f"Failed to send CloudFormation response: {exc}")


def handler(event, context):
    response_url = event.get("ResponseURL")
    if not response_url:
        raise ValueError("ResponseURL is required")

    status = event.get("Status", "FAILED")
    physical_id = event.get("PhysicalResourceId") or context.log_stream_name
    reason = event.get("Reason") or f"See CloudWatch Log Stream: {context.log_stream_name}"

    _send_response(
        response_url=response_url,
        status=status,
        reason=reason,
        data=event.get("Data") or {},
        physical_resource_id=physical_id,
        stack_id=event.get("StackId"),
        request_id=event.get("RequestId"),
        logical_resource_id=event.get("LogicalResourceId"),
        no_echo=bool(event.get("NoEcho", False)),
    )

    return {"Status": status, "Reason": reason, "PhysicalResourceId": physical_id}
