import boto3
import botocore
import cfnresponse
import os
import json
import re
from botocore.exceptions import ClientError


def _stack_suffix(event):
    stack_id = event.get("StackId", "")
    suffix = stack_id.rsplit("/", 1)[-1]
    suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix)
    return (suffix[-8:] or "stack")


def _get_service_account_id(grafana, workspace_id, name):
    token = None
    while True:
        kwargs = {"workspaceId": workspace_id}
        if token:
            kwargs["nextToken"] = token
        resp = grafana.list_workspace_service_accounts(**kwargs)
        for acct in resp.get("serviceAccounts", []):
            if acct.get("name") == name:
                return acct.get("id")
        token = resp.get("nextToken")
        if not token:
            break
    return None


def _delete_token_if_exists(grafana, workspace_id, service_account_id, token_name):
    token = None
    while True:
        kwargs = {"workspaceId": workspace_id, "serviceAccountId": service_account_id}
        if token:
            kwargs["nextToken"] = token
        resp = grafana.list_workspace_service_account_tokens(**kwargs)
        for tok in resp.get("serviceAccountTokens", []):
            if tok.get("name") == token_name:
                grafana.delete_workspace_service_account_token(
                    workspaceId=workspace_id,
                    serviceAccountId=service_account_id,
                    tokenId=tok.get("id"),
                )
                return True
        token = resp.get("nextToken")
        if not token:
            break
    return False


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing SageMaker HyperPod Observability
    """
    try: 
        print(f'boto3 version: {boto3.__version__}')
        print(f'botocore version: {botocore.__version__}')
        request_type = event['RequestType']
 
        if request_type == 'Create':
            response_data = on_create(event)
        elif request_type == 'Update':
            response_data = on_update(event)
        elif request_type == 'Delete':
            response_data = on_delete(event)
        else:
            raise ValueError(f"Invalid request type: {request_type}")
 
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            response_data
        )

    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {
                "Status": "FAILED",
                "Reason": str(e)
            }
        )
 
 
def on_create(event):
    """
    Handle Create request to create a new HyperPod cluster
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Grafana Workspace Service Token created successfully"
        }
        workspace_id = os.environ['GRAFANA_WORKSPACE_ID']
        service_account_name = os.environ['SERVICE_ACCOUNT_NAME']
        grafana = boto3.client('grafana')
        try:
            service_account_response = grafana.create_workspace_service_account(
                workspaceId=workspace_id,
                name=service_account_name,
                grafanaRole='ADMIN',
            )
            response_data['ServiceAccountId'] = service_account_response['id']
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code not in ("ConflictException", "ResourceAlreadyExistsException"):
                raise
            response_data['ServiceAccountId'] = _get_service_account_id(
                grafana, workspace_id, service_account_name
            )
            if not response_data['ServiceAccountId']:
                raise

        token_suffix = _stack_suffix(event)
        token_name = f"{service_account_name}-token-{token_suffix}"
        _delete_token_if_exists(
            grafana, workspace_id, response_data['ServiceAccountId'], token_name
        )

        try:
            service_account_token_response = grafana.create_workspace_service_account_token(
                workspaceId=workspace_id,
                serviceAccountId=response_data['ServiceAccountId'],
                name=token_name,
                secondsToLive=1500
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code != "ConflictException":
                raise
            _delete_token_if_exists(
                grafana, workspace_id, response_data['ServiceAccountId'], token_name
            )
            service_account_token_response = grafana.create_workspace_service_account_token(
                workspaceId=workspace_id,
                serviceAccountId=response_data['ServiceAccountId'],
                name=token_name,
                secondsToLive=1500
            )
        response_data['ServiceAccountTokenId'] = service_account_token_response['serviceAccountToken']['id']
        response_data['ServiceAccountTokenKey'] = service_account_token_response['serviceAccountToken']['key']
        response_data['ServiceAccountTokenName'] = token_name

        return response_data
         
    except Exception as e:
        print(f"Failed to create Grafana Workspace: {str(e)}")
        raise
 

def on_update(event):
    """
    Handle Update request to update an existing Grafana Workspace
    """
    return on_create(event)

 
def on_delete(event):
    """
    Handle Delete request to delete a Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Grafana Workspace Service Token deletion skipped successfully"
        }
        workspace_id = os.environ['GRAFANA_WORKSPACE_ID']
        service_account_name = os.environ['SERVICE_ACCOUNT_NAME']
        print(f"Request received for Deletion of workspace: {workspace_id}")
        try:
            grafana = boto3.client('grafana')
            service_account_id = _get_service_account_id(
                grafana, workspace_id, service_account_name
            )
            if service_account_id:
                token_name = f"{service_account_name}-token-{_stack_suffix(event)}"
                if _delete_token_if_exists(
                    grafana, workspace_id, service_account_id, token_name
                ):
                    response_data["Reason"] = (
                        "Grafana Workspace Service Token deleted successfully"
                    )
        except Exception as delete_error:
            print(f"Failed to delete Grafana Service token: {str(delete_error)}")
        return response_data

    except Exception as e:
        print(f"Failed to delete Grafana Service token: {str(e)}")
        raise
