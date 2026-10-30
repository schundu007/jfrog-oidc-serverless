#!/usr/bin/env bash
# One-time, secretless read access for Argo CD to the Azure DevOps repo that holds the KCP GitOps files.
#
# Creates an app registration in the ADO organisation's Entra tenant, federates it to Argo CD's
# repo-server service account on the hub cluster (no client secret), and adds it to the ADO
# organisation with Basic access and Reader on the project. Prints only the app (client) ID.
#
#   AZURE_CONFIG_DIR=~/.azure-personal bash pipelines/scripts/kcp-argocd-ado-reader.sh \
#     <ado-tenant-id> <ado-org> <ado-project> <hub-oidc-issuer>
set -euo pipefail

TENANT="$1" ORG="$2" PROJECT="$3" ISSUER="$4"
NAME="${KCP_ARGOCD_APP_NAME:-kcp-argocd-repo-reader}"
SUBJECT="system:serviceaccount:argocd:argocd-repo-server"
ORG_URL="https://dev.azure.com/$ORG"

az account set --subscription "$TENANT" >/dev/null

APP_ID=$(az ad app list --display-name "$NAME" --query "[0].appId" -o tsv)
if [ -z "$APP_ID" ]; then
  APP_ID=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
fi
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || az ad sp create --id "$APP_ID" --query id -o tsv)

if ! az ad app federated-credential list --id "$APP_ID" --query "[?subject=='$SUBJECT']" -o tsv | grep -q .; then
  az ad app federated-credential create --id "$APP_ID" --parameters "$(printf '{"name":"argocd-repo-server","issuer":"%s","subject":"%s","audiences":["api://AzureADTokenExchange"]}' "$ISSUER" "$SUBJECT")" >/dev/null
fi

PROJECT_ID=$(az devops project show --org "$ORG_URL" -p "$PROJECT" --query id -o tsv)
BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT
printf '{"accessLevel":{"accountLicenseType":"express"},"projectEntitlements":[{"group":{"groupType":"projectReader"},"projectRef":{"id":"%s"}}],"servicePrincipal":{"origin":"aad","originId":"%s","subjectKind":"servicePrincipal"}}' \
  "$PROJECT_ID" "$SP_ID" > "$BODY"
az devops invoke --org "$ORG_URL" --area MemberEntitlementManagement --resource ServicePrincipalEntitlements \
  --http-method POST --api-version 7.1-preview --in-file "$BODY" --query "operationResult.isSuccess" -o tsv

echo "app (client) id: $APP_ID"
