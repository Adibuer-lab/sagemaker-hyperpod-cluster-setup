import json
import os

import boto3
import cfnresponse


def _build_input(event, physical_id):
    return {
        "RequestType": event.get("RequestType"),
        "ResponseURL": event.get("ResponseURL"),
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
        "PhysicalResourceId": physical_id,
        "Attempt": 0,
        "MaxAttempts": int(os.environ.get("MAX_ATTEMPTS", "45")),
        "DelaySeconds": int(os.environ.get("DELAY_SECONDS", "60")),
    }


def handler(event, context):
    physical_id = event.get("PhysicalResourceId") or context.log_stream_name
    state_machine_arn = os.environ.get("STATE_MACHINE_ARN", "")
    if not state_machine_arn:
        reason = "STATE_MACHINE_ARN is required"
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Reason": reason}, physical_id)
        return

    try:
        sfn = boto3.client("stepfunctions")
        payload = _build_input(event, physical_id)
        resp = sfn.start_execution(
            stateMachineArn=state_machine_arn,
            input=json.dumps(payload),
        )
        print(f"Started state machine execution: {resp.get('executionArn')}")
        # Intentionally do not send a SUCCESS response here; the state machine
        # will report back to CloudFormation when complete.
        return {"ExecutionArn": resp.get("executionArn")}
    except Exception as exc:
        reason = f"Failed to start state machine: {exc}"
        print(reason)
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Reason": reason}, physical_id)
