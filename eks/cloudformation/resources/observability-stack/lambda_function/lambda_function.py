import boto3
import botocore
import cfnresponse
import os
import json
from botocore.exceptions import ClientError
import time

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
            response_data = on_update()
        elif request_type == 'Delete':
            response_data = on_delete()
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
            "Reason": "Observability Stack created successfully"
        }
        props = event.get('ResourceProperties', {})
        stack_name = props.get('ResourceNamePrefix') + "-ObservabilityStack"
        template_url = props.get('StackTemplateUrl')
        props = event.get('ResourceProperties', {})
        subnet_list = props.get('PrivateSubnetIds')
        subnet_string = ",".join(subnet_list)

        cfn = boto3.client('cloudformation')
        print(f"Creating Cloudformation Stack: {stack_name}")
        response = cfn.create_stack(
            StackName=stack_name,
            TemplateURL=template_url,
            OnFailure='DELETE',
            EnableTerminationProtection=False,
            Parameters=[
                {
                'ParameterKey': 'ResourceNamePrefix',
                'ParameterValue': props.get('ResourceNamePrefix')
                },
                {
                'ParameterKey': 'CustomResourceS3Bucket',
                'ParameterValue': props.get('CustomResourceS3Bucket')
                },      
                {
                'ParameterKey': 'GrafanaCreatorfunctionS3Key',
                'ParameterValue': props.get('GrafanaCreatorfunctionS3Key')
                },
                {
                'ParameterKey': 'GrafanaServiceAccountfunctionS3Key',
                'ParameterValue': props.get('GrafanaServiceAccountfunctionS3Key')
                },  
                {
                'ParameterKey': 'FunctionS3Key',
                'ParameterValue': props.get('FunctionS3Key')
                },
                {
                'ParameterKey': 'EKSClusterName',
                'ParameterValue': props.get('EKSClusterName')
                },                
                {
                'ParameterKey': 'TrainingMetricLevel',
                'ParameterValue': props.get('TrainingMetricLevel')
                },
                {
                'ParameterKey': 'TaskGovernanceMetricLevel',
                'ParameterValue': props.get('TaskGovernanceMetricLevel')
                },  
                {
                'ParameterKey': 'ClusterMetricLevel',
                'ParameterValue': props.get('ClusterMetricLevel')
                },
                {
                'ParameterKey': 'NodeMetricLevel',
                'ParameterValue': props.get('NodeMetricLevel')
                },
                {
                'ParameterKey': 'AcceleratedComputeMetricLevel',
                'ParameterValue': props.get('AcceleratedComputeMetricLevel')
                },
                {
                'ParameterKey': 'ScalingMetricLevel',
                'ParameterValue': props.get('ScalingMetricLevel')
                },
                {
                'ParameterKey': 'NetworkMetricLevel',
                'ParameterValue': props.get('NetworkMetricLevel')
                },
                {
                'ParameterKey': 'Logging',
                'ParameterValue': props.get('Logging')
                },
                {
                'ParameterKey': 'VpcId',
                'ParameterValue': props.get('VpcId')
                },
                {
                'ParameterKey': 'SecurityGroupId',
                'ParameterValue': props.get('SecurityGroupId')
                },                
                {
                'ParameterKey': 'PrivateSubnetIds',
                'ParameterValue': subnet_string
                },           
                {
                'ParameterKey': 'GrafanaWorkspaceName',
                'ParameterValue': props.get('GrafanaWorkspaceName')
                }, 
                {
                'ParameterKey': 'GrafanaWorkspaceId',
                'ParameterValue': props.get('GrafanaWorkspaceId')
                },
                {
                'ParameterKey': 'GrafanaWorkspaceArn',
                'ParameterValue': props.get('GrafanaWorkspaceArn')
                },                
                {
                'ParameterKey': 'PrometheusWorkspaceId',
                'ParameterValue': props.get('PrometheusWorkspaceId')
                },           
                {
                'ParameterKey': 'PrometheusWorkspaceArn',
                'ParameterValue': props.get('PrometheusWorkspaceArn')
                },
                {
                'ParameterKey': 'PrometheusWorkspaceEndpoint',
                'ParameterValue': props.get('PrometheusWorkspaceEndpoint')
                },           
                {
                'ParameterKey': 'HyperPodObservabilityRole',
                'ParameterValue': props.get('HyperPodObservabilityRole')
                },
                {
                'ParameterKey': 'GrafanaRole',
                'ParameterValue': props.get('GrafanaRole')
                }, 
                {
                'ParameterKey': 'HyperPodObservabilityRoleType',
                'ParameterValue': props.get('HyperPodObservabilityRoleType')
                },
                {
                'ParameterKey': 'GrafanaRoleType',
                'ParameterValue': props.get('GrafanaRoleType')
                },                
                {
                'ParameterKey': 'PrometheusWorkspaceType',
                'ParameterValue': props.get('PrometheusWorkspaceType')
                },                
                {
                'ParameterKey': 'GrafanaWorkspaceType',
                'ParameterValue': props.get('GrafanaWorkspaceType')
                },
            ],
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND'],
        )
        print(f"Stack creation initiated: {response['StackId']}")
        response_data['StackId'] = response['StackId']
        return response_data
    except Exception as e:
        print(f"Failed to create Cloudformation Workspace: {str(e)}")
        raise
          
def on_update():
    """
    Handle Update request to update an existing Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Observability Stack updated successfully"
        }
        print(f"Request received for Updation of CFN")
        props = event.get('ResourceProperties', {})
        stack_name = props.get('ResourceNamePrefix') + "-ObservabilityStack"
        template_url = props.get('StackTemplateUrl')
        props = event.get('ResourceProperties', {})
        subnet_list = props.get('PrivateSubnetIds')
        subnet_string = ",".join(subnet_list)

        cfn = boto3.client('cloudformation')
        print(f"Updating Cloudformation Stack: {stack_name}")
        response = cfn.update_stack(
            StackName=stack_name,
            TemplateURL=template_url,
            Parameters=[
                {
                'ParameterKey': 'ResourceNamePrefix',
                'ParameterValue': props.get('ResourceNamePrefix')
                },
                {
                'ParameterKey': 'CustomResourceS3Bucket',
                'ParameterValue': props.get('CustomResourceS3Bucket')
                },      
                {
                'ParameterKey': 'GrafanaCreatorfunctionS3Key',
                'ParameterValue': props.get('GrafanaCreatorfunctionS3Key')
                },
                {
                'ParameterKey': 'GrafanaServiceAccountfunctionS3Key',
                'ParameterValue': props.get('GrafanaServiceAccountfunctionS3Key')
                },  
                {
                'ParameterKey': 'FunctionS3Key',
                'ParameterValue': props.get('FunctionS3Key')
                },
                {
                'ParameterKey': 'EKSClusterName',
                'ParameterValue': props.get('EKSClusterName')
                },                
                {
                'ParameterKey': 'TrainingMetricLevel',
                'ParameterValue': props.get('TrainingMetricLevel')
                },
                {
                'ParameterKey': 'TaskGovernanceMetricLevel',
                'ParameterValue': props.get('TaskGovernanceMetricLevel')
                },  
                {
                'ParameterKey': 'ClusterMetricLevel',
                'ParameterValue': props.get('ClusterMetricLevel')
                },
                {
                'ParameterKey': 'NodeMetricLevel',
                'ParameterValue': props.get('NodeMetricLevel')
                },
                {
                'ParameterKey': 'AcceleratedComputeMetricLevel',
                'ParameterValue': props.get('AcceleratedComputeMetricLevel')
                },
                {
                'ParameterKey': 'ScalingMetricLevel',
                'ParameterValue': props.get('ScalingMetricLevel')
                },
                {
                'ParameterKey': 'NetworkMetricLevel',
                'ParameterValue': props.get('NetworkMetricLevel')
                },
                {
                'ParameterKey': 'Logging',
                'ParameterValue': props.get('Logging')
                },
                {
                'ParameterKey': 'VpcId',
                'ParameterValue': props.get('VpcId')
                },
                {
                'ParameterKey': 'SecurityGroupId',
                'ParameterValue': props.get('SecurityGroupId')
                },                
                {
                'ParameterKey': 'PrivateSubnetIds',
                'ParameterValue': subnet_string
                },           
                {
                'ParameterKey': 'GrafanaWorkspaceName',
                'ParameterValue': props.get('GrafanaWorkspaceName')
                }, 
                {
                'ParameterKey': 'GrafanaWorkspaceId',
                'ParameterValue': props.get('GrafanaWorkspaceId')
                },
                {
                'ParameterKey': 'GrafanaWorkspaceArn',
                'ParameterValue': props.get('GrafanaWorkspaceArn')
                },                
                {
                'ParameterKey': 'PrometheusWorkspaceId',
                'ParameterValue': props.get('PrometheusWorkspaceId')
                },           
                {
                'ParameterKey': 'PrometheusWorkspaceArn',
                'ParameterValue': props.get('PrometheusWorkspaceArn')
                },
                {
                'ParameterKey': 'PrometheusWorkspaceEndpoint',
                'ParameterValue': props.get('PrometheusWorkspaceEndpoint')
                },           
                {
                'ParameterKey': 'HyperPodObservabilityRole',
                'ParameterValue': props.get('HyperPodObservabilityRole')
                },
                {
                'ParameterKey': 'GrafanaRole',
                'ParameterValue': props.get('GrafanaRole')
                }, 
                {
                'ParameterKey': 'HyperPodObservabilityRoleType',
                'ParameterValue': props.get('HyperPodObservabilityRoleType')
                },
                {
                'ParameterKey': 'GrafanaRoleType',
                'ParameterValue': props.get('GrafanaRoleType')
                },                
                {
                'ParameterKey': 'PrometheusWorkspaceType',
                'ParameterValue': props.get('PrometheusWorkspaceType')
                },                
                {
                'ParameterKey': 'GrafanaWorkspaceType',
                'ParameterValue': props.get('GrafanaWorkspaceType')
                },
            ],
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND'],
        )
        print(f"Stack creation initiated: {response['StackId']}")
        response_data['StackId'] = response['StackId']
        return response_data
    except Exception as e:
        print(f"Failed to create Cloudformation Workspace: {str(e)}")
        raise

def on_delete():
    """
    Handle Delete request to delete a Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Observability Stack deleted successfully"
        }
        print(f"Request received for Deletion of CFN")
        return response_data

    except Exception as e:
        print(f"Failed to delete Grafana Workspace: {str(e)}")
        raise