#!/bin/bash
# generate-ds-setup-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-ds-setup-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-ds-setup-lambda-func.sh