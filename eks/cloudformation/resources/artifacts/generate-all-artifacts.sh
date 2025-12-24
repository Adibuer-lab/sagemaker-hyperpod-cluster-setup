#!/bin/bash
# generate-all-artifacts.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOURCES_DIR="$(dirname "$SCRIPT_DIR")"

run() {
    local dir="$RESOURCES_DIR/$1"
    local script="$2"
    if [[ -d "$dir" && -x "$dir/$script" ]]; then
        echo "=== $1 ==="
        (cd "$dir" && ./"$script")
    else
        echo "SKIP: $1 (not found)"
    fi
}

run helm-chart-injector generate-helm-lambda-zip.sh
run inference-helm-chart-injector generate-inf-helm-lambda-zip.sh
run inference-k8s-service-account-creator generate-inf-sa-creation-lambda-zip.sh
run data-scientist-setup generate-ds-setup-lambda-zip.sh
run nemo-space-sync generate-nemo-space-sync-lambda-zip.sh
run tiered-cache-config generate-tiered-cache-lambda-zip.sh
run hpto-addon-installer generate-hpto-addon-lambda-zip.sh
run fsx-for-lustre generate-fsx-lambda-zip.sh
run hyperpod-cluster-creator generate-hp-lambda-zip.sh
run private-subnet-tagging generate-lambda-zip.sh
run grafana-lambda-function generate-lambda-zip.sh
run observability-grafana-creator generate-observability-grafana-creator-lambda-zip.sh
run grafana-service-token generate-grafana-service-token-lambda-zip.sh
run observability-stack generate-observability-stack-lambda-zip.sh
run cluster-policy generate-cluster-policy-lambda-zip.sh
run workspace-templates generate-workspace-templates-lambda-func.sh
run karpenter-setup generate-karpenter-setup-lambda-func.sh
run hyperpod-association-waiter generate-hyperpod-association-waiter-lambda-func.sh
run cfn-stepfunction-starter generate-cfn-stepfunction-starter-lambda-func.sh
run cfn-response-sender generate-cfn-response-sender-lambda-func.sh
run kueue-external-frameworks-patch generate-kueue-external-frameworks-patch-lambda-func.sh
run kueue-az-placement generate-kueue-az-placement-lambda-func.sh
run kueue-az-rotation-controller generate-kueue-az-rotation-controller-lambda-func.sh

echo "=== Done ==="
