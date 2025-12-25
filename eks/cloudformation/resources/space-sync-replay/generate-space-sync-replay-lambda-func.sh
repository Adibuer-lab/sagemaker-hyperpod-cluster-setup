#!/bin/bash
# generate-space-sync-replay-lambda-func.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="$(dirname "$SCRIPT_DIR")/artifacts"
cd "$SCRIPT_DIR"

mkdir -p package
cp lambda_function/lambda_function.py package/

cd package
zip -r "$ARTIFACTS_DIR/space-sync-replay-lambda-function.zip" .
cd ..

rm -rf package
echo "Created space-sync-replay-lambda-function.zip"
