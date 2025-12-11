#!/bin/bash
# generate-cert-manager-lambda-func.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(dirname "$SCRIPT_DIR")/artifacts"
cd "$SCRIPT_DIR"

python3 -m venv venv
source venv/bin/activate

pip install -r lambda_function/requirements.txt -t package/
cp lambda_function/lambda_function.py package/

cd package
zip -r "$ARTIFACTS_DIR/cert-manager-lambda-function.zip" .
cd ..

rm -rf package venv
echo "Created cert-manager-lambda-function.zip"
