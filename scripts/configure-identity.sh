#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/configure-identity.sh \
    --repository OWNER/REPOSITORY \
    --environment ENVIRONMENT \
    --subscription-id ID \
    --claims-file FILE \
    [--acr-name NAME]

Regional environments additionally require:
  --resource-group NAME
  --container-app NAME
  --workspace-resource-id ID
  --workspace-customer-id ID

Environments: build, prod-kr, prod-sea, prod-usw,
              prod-rollback-kr, prod-rollback-sea, prod-rollback-usw
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

repository=
environment=
subscription_id=
claims_file=
acr_name=
resource_group=
container_app=
workspace_resource_id=
workspace_customer_id=
while (($#)); do
  case "$1" in
    --repository|--environment|--subscription-id|--claims-file|--acr-name|--resource-group|--container-app|--workspace-resource-id|--workspace-customer-id)
      (($# >= 2)) || { usage >&2; fail "Missing value for $1"; }
      case "$1" in
        --repository) repository=$2 ;;
        --environment) environment=$2 ;;
        --subscription-id) subscription_id=$2 ;;
        --claims-file) claims_file=$2 ;;
        --acr-name) acr_name=$2 ;;
        --resource-group) resource_group=$2 ;;
        --container-app) container_app=$2 ;;
        --workspace-resource-id) workspace_resource_id=$2 ;;
        --workspace-customer-id) workspace_customer_id=$2 ;;
      esac
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "Unknown argument: $1"
      ;;
  esac
done

[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Repository must be OWNER/REPOSITORY"
case "$environment" in
  build|prod-kr|prod-sea|prod-usw|prod-rollback-kr|prod-rollback-sea|prod-rollback-usw) ;;
  *) fail "Unsupported environment: $environment" ;;
esac
[[ -n "$subscription_id" ]] || fail "--subscription-id is required"
[[ -n "$claims_file" ]] || fail "--claims-file is required"
[[ -f "$claims_file" ]] || fail "Claims file does not exist: $claims_file"
for command in az gh jq; do
  command -v "$command" >/dev/null 2>&1 || fail "Required command not found: $command"
done

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
claims_file=$(realpath -- "$claims_file")

az_json() {
  az "$@" --only-show-errors --output json
}

az_json account set --subscription "$subscription_id" >/dev/null
account=$(az_json account show)
tenant_id=$(jq -er '.tenantId' <<<"$account")

claims=$(jq -ce \
  --arg repository "$repository" \
  --arg environment "$environment" '
    select(
      .iss == "https://token.actions.githubusercontent.com" and
      .aud == "api://AzureADTokenExchange" and
      .repository == $repository and
      .ref == "refs/heads/main" and
      .inspected_environment == $environment and
      (.sub | type == "string" and length > 0)
    )
  ' "$claims_file") || fail "Claims do not match the expected issuer/audience/repository/main/environment. Inspect actual GitHub job claims first."
subject=$(jq -er '.sub' <<<"$claims")
claim_environment=$(jq -r '.environment // empty' <<<"$claims")
if [[ -n "$claim_environment" && "$claim_environment" != "$environment" ]]; then
  fail "Token environment does not match requested environment"
fi
if [[ -z "$claim_environment" && "$subject" != *":environment:${environment}" && "$subject" != *":environment:${environment}:"* ]]; then
  fail "No environment trust context in inspected claims; review custom subject template"
fi

if ! repository_metadata=$(gh api "repos/$repository" --jq '{repository_id: (.id|tostring), owner_id: (.owner.id|tostring)}'); then
  fail "Cannot verify current GitHub repository/owner IDs"
fi
jq -e \
  --arg repository_id "$(jq -er '.repository_id' <<<"$claims")" \
  --arg owner_id "$(jq -er '.repository_owner_id' <<<"$claims")" '
    .repository_id == $repository_id and .owner_id == $owner_id
  ' <<<"$repository_metadata" >/dev/null ||
  fail "Claims IDs do not match current GitHub repository/owner; inspect again after rename/transfer"

if [[ "$environment" == build ]]; then
  [[ -n "$acr_name" ]] || fail "Build requires --acr-name"
else
  [[ -n "$resource_group" && -n "$container_app" && -n "$workspace_resource_id" && -n "$workspace_customer_id" ]] ||
    fail "Regional identity requires --resource-group, --container-app, --workspace-resource-id, and --workspace-customer-id"
fi

mkdir -p state
record="state/identity-${environment}.json"
[[ ! -e "$record" ]] || fail "Identity record exists: $record. Reuse it, do not blindly create duplicate principals."
oidc_file="state/oidc-${environment}.json"

application=$(az_json ad app create --display-name "safe-aca-${repository//\//-}-${environment}")
application_id=$(jq -er '.id' <<<"$application")
client_id=$(jq -er '.appId' <<<"$application")
principal=$(az_json ad sp create --id "$client_id")
principal_id=$(jq -er '.id' <<<"$principal")

# Persist identifiers immediately, even if later RBAC provisioning fails.
jq -n \
  --arg app_object_id "$application_id" \
  --arg client_id "$client_id" \
  --arg principal_id "$principal_id" \
  '{appObjectId: $app_object_id, clientId: $client_id, principalId: $principal_id}' > "$record"
jq -n \
  --arg name "github-${environment}" \
  --arg subject "$subject" \
  '{
    name: $name,
    issuer: "https://token.actions.githubusercontent.com",
    subject: $subject,
    audiences: ["api://AzureADTokenExchange"]
  }' > "$oidc_file"
az_json ad app federated-credential create --id "$application_id" --parameters "@$oidc_file" >/dev/null

set_github_variable() {
  local name=$1 value=$2
  if ! gh variable set "$name" --repo "$repository" --env "$environment" --body "$value"; then
    fail "Failed setting $name; create/protect GitHub environment first"
  fi
}

if [[ "$environment" == build ]]; then
  registry=$(az_json acr show --name "$acr_name")
  registry_id=$(jq -er '.id' <<<"$registry")
  az_json role assignment create --assignee-object-id "$principal_id" --assignee-principal-type ServicePrincipal --role AcrPush --scope "$registry_id" >/dev/null
  az_json role assignment create --assignee-object-id "$principal_id" --assignee-principal-type ServicePrincipal --role Reader --scope "$registry_id" >/dev/null
  set_github_variable ACR_NAME "$acr_name"
else
  app_scope="/subscriptions/${subscription_id}/resourceGroups/${resource_group}/providers/Microsoft.App/containerApps/${container_app}"
  group_scope="/subscriptions/${subscription_id}/resourceGroups/${resource_group}"
  az_json role assignment create --assignee-object-id "$principal_id" --assignee-principal-type ServicePrincipal --role 'Container Apps Contributor' --scope "$app_scope" >/dev/null
  az_json role assignment create --assignee-object-id "$principal_id" --assignee-principal-type ServicePrincipal --role Reader --scope "$group_scope" >/dev/null
  az_json role assignment create --assignee-object-id "$principal_id" --assignee-principal-type ServicePrincipal --role 'Log Analytics Reader' --scope "$workspace_resource_id" >/dev/null
  set_github_variable RESOURCE_GROUP "$resource_group"
  set_github_variable CONTAINER_APP "$container_app"
  set_github_variable LOG_ANALYTICS_WORKSPACE_ID "$workspace_customer_id"
fi
set_github_variable AZURE_CLIENT_ID "$client_id"
set_github_variable AZURE_TENANT_ID "$tenant_id"
set_github_variable AZURE_SUBSCRIPTION_ID "$subscription_id"
printf 'Configured distinct %s OIDC identity. No client secret created. Verify reviewers/branch protection in GitHub.\n' "$environment"
