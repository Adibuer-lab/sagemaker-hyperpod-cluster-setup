#!/bin/bash
# Sync HyperPod templates from git fork to S3 bucket
# Maps: eks/cloudformation/* → templates/, resources/
#       slurm/cloudformation/* → templates-slurm/
# Run via: make sync
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Use env vars exported by Makefile
AWS_REGION="${AWS_REGION:-us-east-1}"
PROFILE_ARG=""
[ -n "$AWS_PROFILE" ] && PROFILE_ARG="--profile $AWS_PROFILE"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text $PROFILE_ARG)
BUCKET_NAME="nemo-hyperpod-templates-${ACCOUNT_ID}-${AWS_REGION}"

# Staging directory for S3 structure
STAGING_DIR="$SCRIPT_DIR/.staging"

echo "=============================================="
echo "HyperPod Templates → S3 Sync"
echo "=============================================="
echo "Account: $ACCOUNT_ID"
echo "Region:  $AWS_REGION"
echo "Bucket:  $BUCKET_NAME"
echo ""

# Build staging directory with S3 structure
echo -e "${YELLOW}[1/3] Building staging directory...${NC}"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR/templates" "$STAGING_DIR/templates-slurm" "$STAGING_DIR/resources"

# Map EKS templates
cp "$REPO_ROOT/eks/cloudformation/"*.yaml "$STAGING_DIR/templates/"
cp -r "$REPO_ROOT/eks/cloudformation/resources/"* "$STAGING_DIR/resources/"

# Map Slurm templates
cp "$REPO_ROOT/slurm/cloudformation/"*.yaml "$STAGING_DIR/templates-slurm/"

# Create VERSION file (use git short hash)
git -C "$REPO_ROOT" rev-parse --short HEAD > "$STAGING_DIR/VERSION"

# Create versioned copy (1/) - must create 1/ first so cp -r creates subdirs
mkdir -p "$STAGING_DIR/1/templates" "$STAGING_DIR/1/templates-slurm" "$STAGING_DIR/1/resources"
cp "$STAGING_DIR/templates/"* "$STAGING_DIR/1/templates/"
cp "$STAGING_DIR/templates-slurm/"* "$STAGING_DIR/1/templates-slurm/"
cp -r "$STAGING_DIR/resources/"* "$STAGING_DIR/1/resources/"

echo -e "${GREEN}✓ Staging ready${NC}"
echo "  templates:       $(ls "$STAGING_DIR/templates/"*.yaml 2>/dev/null | wc -l | tr -d ' ') files"
echo "  templates-slurm: $(ls "$STAGING_DIR/templates-slurm/"*.yaml 2>/dev/null | wc -l | tr -d ' ') files"
echo "  resources:       $(find "$STAGING_DIR/resources" -type f | wc -l | tr -d ' ') files"
echo ""

# Ensure bucket exists
echo -e "${YELLOW}[2/3] Checking S3 bucket...${NC}"
if ! aws s3 ls "s3://$BUCKET_NAME" $PROFILE_ARG 2>/dev/null; then
    echo "  Creating bucket..."
    if [ "$AWS_REGION" = "us-east-1" ]; then
        aws s3 mb "s3://$BUCKET_NAME" --region $AWS_REGION $PROFILE_ARG
    else
        aws s3 mb "s3://$BUCKET_NAME" --region $AWS_REGION $PROFILE_ARG \
            --create-bucket-configuration LocationConstraint=$AWS_REGION
    fi
    
    # Set bucket policy for CloudFormation access
    aws s3api put-bucket-policy --bucket "$BUCKET_NAME" $PROFILE_ARG --policy "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
            \"Sid\": \"AllowCloudFormationAccess\",
            \"Effect\": \"Allow\",
            \"Principal\": {\"Service\": \"cloudformation.amazonaws.com\"},
            \"Action\": \"s3:*\",
            \"Resource\": [\"arn:aws:s3:::${BUCKET_NAME}\", \"arn:aws:s3:::${BUCKET_NAME}/*\"],
            \"Condition\": {\"StringEquals\": {\"aws:SourceAccount\": \"${ACCOUNT_ID}\"}}
        }]
    }"
fi
echo -e "${GREEN}✓ Bucket ready${NC}"
echo ""

# Sync to S3
echo -e "${YELLOW}[3/3] Syncing to S3...${NC}"
aws s3 sync "$STAGING_DIR/" "s3://$BUCKET_NAME/" \
    --region $AWS_REGION $PROFILE_ARG --delete \
    --size-only \
    --exclude "blueprints/*"

echo -e "${GREEN}✓ Synced to s3://$BUCKET_NAME/${NC}"
echo ""

# Cleanup
rm -rf "$STAGING_DIR"

echo "=============================================="
echo -e "${GREEN}Done!${NC}"
echo "=============================================="
echo "Git commit: $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
echo "S3 bucket:  s3://$BUCKET_NAME/"
