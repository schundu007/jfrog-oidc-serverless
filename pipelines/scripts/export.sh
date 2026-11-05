#!/usr/bin/env bash
# The portable package: everything committed on HEAD except this environment's own data (.gitattributes
# export-ignore: onboarding workspaces, the ADO mirror). Nothing in it names an account, subscription,
# tenant, ADO organization or JFrog instance; docs/fresh-start-azure.md runs it in a new environment.
#
#   pipelines/scripts/export.sh [out.zip]
set -euo pipefail
cd "$(dirname "$0")/../.."
out=${1:-jfrog-oidc-serverless-$(git rev-parse --short HEAD).zip}
git archive --format=zip --prefix=jfrog-oidc-serverless/ -o "$out" HEAD
echo "$out ($(unzip -l "$out" | tail -1 | awk '{print $2}') files)"
