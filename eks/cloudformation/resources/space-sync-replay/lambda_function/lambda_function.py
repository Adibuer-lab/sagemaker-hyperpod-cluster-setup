import json
import os

import boto3


def _bool_env(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _load_json(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def handler(event, context):
    sqs = boto3.client("sqs")
    lambdaclient = boto3.client("lambda")

    queue_url = os.environ.get("DLQ_URL")
    target_function = os.environ.get("TARGET_FUNCTION")
    if not queue_url or not target_function:
        raise ValueError("DLQ_URL and TARGET_FUNCTION are required")

    max_messages = int(os.environ.get("MAX_MESSAGES", "100"))
    batch_size = int(os.environ.get("BATCH_SIZE", "10"))
    delete_on_success = _bool_env(os.environ.get("DELETE_ON_SUCCESS", "true"), True)

    # Allow overrides from event payload
    if isinstance(event, dict):
        max_messages = int(event.get("max_messages", max_messages))
        batch_size = int(event.get("batch_size", batch_size))
        delete_on_success = bool(event.get("delete_on_success", delete_on_success))

    max_messages = max(1, min(max_messages, 1000))
    batch_size = max(1, min(batch_size, 10))

    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0

    while processed < max_messages:
        remaining = max_messages - processed
        receive_count = min(batch_size, remaining)
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=receive_count,
            WaitTimeSeconds=0,
            VisibilityTimeout=30,
        )
        messages = resp.get("Messages", [])
        if not messages:
            break

        for message in messages:
            processed += 1
            body = message.get("Body")
            payload = _load_json(body)
            if payload is None:
                print("Skipping message: invalid JSON body")
                skipped += 1
                continue

            try:
                invoke_resp = lambdaclient.invoke(
                    FunctionName=target_function,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(payload).encode("utf-8"),
                )
                function_error = invoke_resp.get("FunctionError")
                if function_error:
                    failed += 1
                    print(f"Replay failed: {function_error}")
                else:
                    succeeded += 1
                    if delete_on_success:
                        sqs.delete_message(
                            QueueUrl=queue_url,
                            ReceiptHandle=message["ReceiptHandle"],
                        )
            except Exception as exc:
                failed += 1
                print(f"Replay error: {exc}")

        if len(messages) < receive_count:
            break

    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "queue_url": queue_url,
        "target_function": target_function,
    }
