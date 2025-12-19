#!/bin/bash
# generate-nemo-space-sync-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-nemo-space-sync-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-nemo-space-sync-lambda-func.sh
