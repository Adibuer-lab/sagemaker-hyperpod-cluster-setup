#!/bin/bash
# generate-hpto-addon-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-hpto-addon-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-hpto-addon-lambda-func.sh
