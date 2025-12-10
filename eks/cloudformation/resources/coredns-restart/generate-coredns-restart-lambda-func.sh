#!/bin/bash
# generate-coredns-restart-lambda-func.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create and activate a temporary virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies from requirements.txt
pip install -r lambda_function/requirements.txt -t package/

# Copy function code to package directory
cp lambda_function/lambda_function.py package/

# Create ZIP file
cd package
zip -r ../../artifacts/coredns-restart-lambda-function.zip .
cd ..

# Clean up
rm -rf package venv

echo "Created coredns-restart-lambda-function.zip in artifacts/"
