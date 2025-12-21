#!/bin/bash
# generate-hyperpod-association-waiter-lambda-func.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(dirname "$SCRIPT_DIR")/artifacts"
cd "$SCRIPT_DIR"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

pip install -r lambda_function/requirements.txt -t package/
cp lambda_function/lambda_function.py package/

cd package
zip -r "$ARTIFACTS_DIR/hyperpod-association-waiter-lambda-function.zip" .
cd ..

rm -rf package venv
echo "Created hyperpod-association-waiter-lambda-function.zip"
