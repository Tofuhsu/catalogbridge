#!/usr/bin/env bash
# Build the frontend and deploy the stack with your local AWS credentials.
#   CB_ALERT_EMAIL=you@example.com scripts/deploy.sh [extra cdk args, e.g. -c workerBatchSize=1]
set -euo pipefail
cd "$(dirname "$0")/.."
: "${CB_ALERT_EMAIL:?set CB_ALERT_EMAIL (alarm + budget notifications)}"
export AWS_REGION="${AWS_REGION:-us-west-2}"
export CDK_DEFAULT_REGION="$AWS_REGION"
export CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
(cd frontend && npm ci --silent && npm run build --silent)
(cd infra && npm ci --silent && npx cdk deploy CatalogBridge --require-approval never \
  -c alertEmail="$CB_ALERT_EMAIL" "$@" --outputs-file ../cdk-outputs.json)
