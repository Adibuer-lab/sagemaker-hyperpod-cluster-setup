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

run cert-manager-installer generate-cert-manager-lambda-zip.sh
run cert-manager-readiness generate-cert-manager-readiness-lambda-func.sh
run cfn-response-sender generate-cfn-response-sender-lambda-func.sh
run cfn-stepfunction-starter generate-cfn-stepfunction-starter-lambda-func.sh
run cluster-policy generate-cluster-policy-lambda-zip.sh
run data-scientist-setup generate-ds-setup-lambda-zip.sh
run eks-subnet-tagging generate-lambda-zip.sh
run fsx-for-lustre generate-fsx-lambda-zip.sh
run grafana-lambda-function generate-lambda-zip.sh
run grafana-service-token generate-grafana-service-token-lambda-zip.sh
run helm-chart-injector generate-helm-lambda-zip.sh
run hyperpod-association-waiter generate-hyperpod-association-waiter-lambda-func.sh
run hyperpod-cluster-creator generate-hp-lambda-zip.sh
run hpto-addon-installer generate-hpto-addon-lambda-zip.sh
run inference-helm-chart-injector generate-inf-helm-lambda-zip.sh
run inference-k8s-service-account-creator generate-inf-sa-creation-lambda-zip.sh
run karpenter-setup generate-karpenter-setup-lambda-func.sh
run kueue-external-frameworks-patch generate-kueue-external-frameworks-patch-lambda-func.sh
run task-governance-compute-quota generate-task-governance-compute-quota-lambda-func.sh
run nemo-space-sync-workflow generate-nemo-space-sync-workflow-zips.sh
run observability-grafana-creator generate-observability-grafana-creator-lambda-zip.sh
run observability-stack generate-observability-stack-lambda-zip.sh
run private-subnet-tagging generate-lambda-zip.sh
run space-sync-replay generate-space-sync-replay-lambda-func.sh
run tiered-cache-config generate-tiered-cache-lambda-zip.sh
run workspace-templates generate-workspace-templates-lambda-func.sh

echo "=== Done ==="
