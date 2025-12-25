#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ARTIFACTS_DIR=$(cd "${SCRIPT_DIR}/../artifacts" && pwd)

zip_lambda() {
  local name="$1"
  local src_dir="${SCRIPT_DIR}/${name}"
  local out="${ARTIFACTS_DIR}/nemo-space-sync-${name}-lambda-function.zip"
  rm -f "$out"
  (cd "$src_dir" && zip -r "$out" .)
  echo "Created $(basename "$out")"
}

zip_lambda prepare
zip_lambda delete_apps
zip_lambda check_apps
zip_lambda delete_space
zip_lambda check_space
zip_lambda delete_profile
zip_lambda check_profile
zip_lambda create_profile
zip_lambda create_space
