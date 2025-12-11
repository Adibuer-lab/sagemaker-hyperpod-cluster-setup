#!/bin/bash
# generate-cluster-policy-lambda-func.sh

# Create and activate a temporary virtual environment
python3 -m venv venv
source venv/bin/activate

# Create package directory if it doesn't exist
mkdir -p package/

# Install dependencies from requirements.txt
pip install -r lambda_function/requirements.txt -t package/

# Copy function code to package directory
cp lambda_function/lambda_function.py package/

# Create artifacts directory if it doesn't exist
mkdir -p ../../artifacts/

# Create ZIP file
cd package
zip -r ../../artifacts/task-governance-cluster-policy-lambda.zip .
cd ..

# Clean up
rm -rf package venv