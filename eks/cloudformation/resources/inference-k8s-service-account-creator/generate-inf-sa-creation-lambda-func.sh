#!/bin/bash
# generate-inf-sa-creation-lambda-func.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(dirname "$SCRIPT_DIR")/artifacts"
cd "$SCRIPT_DIR"

python3 -m venv venv
source venv/bin/activate

pip install -r lambda_function/requirements.txt -t package/
cp lambda_function/lambda_function.py package/

# Remove old zip
rm -f "$ARTIFACTS_DIR/inf-sa-creation-lambda-function.zip"

cd package
# Remove files not needed for Lambda (python3.12 on Linux x86_64)
find . -name "*cpython-311*" -delete
find . -name "*darwin*" -delete
zip -r "$ARTIFACTS_DIR/inf-sa-creation-lambda-function.zip" .
cd ..

rm -rf package venv
echo "Created inf-sa-creation-lambda-function.zip"
