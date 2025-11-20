#!/bin/bash
# generate-tiered-cache-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-tiered-cache-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-tiered-cache-lambda-func.sh
