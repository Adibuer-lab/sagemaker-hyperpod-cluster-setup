#!/bin/bash
# generate-cluster-policy-lambda-func.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(dirname "$SCRIPT_DIR")/artifacts"
cd "$SCRIPT_DIR"

mkdir -p package/
python3 -m venv venv
source venv/bin/activate

pip install -r lambda_function/requirements.txt -t package/
cp lambda_function/lambda_function.py package/

mkdir -p "$ARTIFACTS_DIR"
cd package
zip -r "$ARTIFACTS_DIR/task-governance-cluster-policy-lambda.zip" .
cd ..

rm -rf package venv
echo "Created task-governance-cluster-policy-lambda.zip"
