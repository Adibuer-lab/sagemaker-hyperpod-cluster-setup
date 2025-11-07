import json
import boto3
import cfnresponse
import logging
import traceback

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    """
    Lambda function handler for the Custom::ClusterSchedulerConfig custom resource.
    Creates, updates, or deletes a HyperPod cluster scheduler configuration using the task governance addon.
    """
    try:
        logger.info(f"Received event: {json.dumps(event)}")

        # Extract relevant information from the event
        request_type = event['RequestType']
        physical_id = event.get('PhysicalResourceId', None)
        resource_properties = event['ResourceProperties']

        cluster_arn = resource_properties['ClusterArn']
        scheduler_config = resource_properties['SchedulerConfig']
        
        # Convert Weight values from strings to integers in PriorityClasses
        if 'PriorityClasses' in scheduler_config:
            for priority_class in scheduler_config['PriorityClasses']:
                if 'Weight' in priority_class and isinstance(priority_class['Weight'], str):
                    priority_class['Weight'] = int(priority_class['Weight'])
        
        config_name = resource_properties['Name']
        description = resource_properties.get('Description', 'HyperPod cluster scheduler configuration')

        # Initialize Sagemaker client
        sagemaker = boto3.client('sagemaker')

        if request_type == 'Create':
            # Create cluster scheduler config
            try:
                response = sagemaker.create_cluster_scheduler_config(
                    ClusterArn=cluster_arn,
                    Name=config_name,
                    SchedulerConfig=scheduler_config,
                    Description=description
                )
                physical_id = response['ClusterSchedulerConfigId']
                response_data = {
                    'ClusterSchedulerConfigArn': response['ClusterSchedulerConfigArn'],
                    'ClusterSchedulerConfigId': physical_id
                }
                cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data, physical_id)

            except Exception as create_error:
                logger.error(f"Create error: {str(create_error)}")
                cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_id)

        elif request_type == 'Update':
            if physical_id:
                try:
                    response = sagemaker.update_cluster_scheduler_config(
                        ClusterSchedulerConfigId=physical_id,
                        SchedulerConfig=scheduler_config,
                        Description=description
                    )
                    response_data = {
                        'ClusterSchedulerConfigArn': response['ClusterSchedulerConfigArn'],
                        'ClusterSchedulerConfigId': physical_id
                    }
                    cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data, physical_id)
                except Exception as update_error:
                    logger.error(f"Update error: {str(update_error)}")
                    cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_id)
            else:
                logger.error("No physical resource ID provided for update")
                cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_id)

        elif request_type == 'Delete':
            if physical_id:
                # Delete the cluster scheduler config
                try:
                    sagemaker.delete_cluster_scheduler_config(
                        ClusterSchedulerConfigId=physical_id
                    )
                except Exception as e:
                    if 'ResourceNotFound' in str(e) or 'ValidationException' in str(e):
                        # Config already deleted or invalid ID format
                        logger.info(f"Cluster scheduler config already deleted or not found: {str(e)}")
                    else:
                        logger.error(f"Delete error: {str(e)}")
                        # Don't fail on delete errors to avoid stack deletion issues
                        pass

            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        logger.error(traceback.format_exc())
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_id)