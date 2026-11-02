#!/usr/bin/env bash
# Build the React site, bundle it into the API Lambda, and deploy the stack.
# Usage: scripts/deploy.sh            (uses samconfig.toml answers saved by `sam deploy --guided`)
#        scripts/deploy.sh --guided   (first time, or to change answers)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Building the frontend"
(cd frontend && npm install --no-audit --no-fund && npm run build)

echo "==> Bundling the site into the Lambda package"
rm -rf backend/src/clippulse/static
cp -R frontend/dist backend/src/clippulse/static

echo "==> sam build"
sam build

echo "==> sam deploy"
sam deploy "$@"

echo
aws cloudformation describe-stacks --stack-name "${STACK_NAME:-clippulse}" \
  --query "Stacks[0].Outputs[?OutputKey=='SiteUrl'||OutputKey=='PollerFunctionName'].[OutputKey,OutputValue]" --output text
