#!/bin/bash

# Build script for NeMo space sync Lambda layer
# This layer is intentionally minimal (no external binaries required)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAYER_DIR="${SCRIPT_DIR}/nemo-space-sync-lambda-layer"
PYTHON_DIR="${LAYER_DIR}/python"


echo "Building NeMo space sync Lambda layer..."

# Clean up previous builds
rm -rf "${LAYER_DIR}"
mkdir -p "${PYTHON_DIR}"

# Keep an empty file so the python/ directory is preserved in the zip
: > "${PYTHON_DIR}/.keep"

# Create the layer zip
cd "${LAYER_DIR}"
echo "Creating layer zip file..."
zip -r "${SCRIPT_DIR}/nemo-space-sync-lambda-layer.zip" .

echo "Layer build complete: ${SCRIPT_DIR}/nemo-space-sync-lambda-layer.zip"
