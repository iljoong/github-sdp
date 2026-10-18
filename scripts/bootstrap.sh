#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh --subscription-id ID --acr-name NAME [--prefix PREFIX]
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

progress() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2
}

subscription_id=
acr_name=
prefix=safeaca
while (($#)); do
  case "$1" in
    --subscription-id)
      (($# >= 2)) || { usage >&2; fail "Missing value for $1"; }
      subscription_id=$2
      shift 2
      ;;
    --acr-name)
      (($# >= 2)) || { usage >&2; fail "Missing value for $1"; }
      acr_name=$2
      shift 2
      ;;
    --prefix)
      (($# >= 2)) || { usage >&2; fail "Missing value for $1"; }
      prefix=$2
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

[[ -n "$subscription_id" ]] || { usage >&2; fail "--subscription-id is required"; }
[[ "$acr_name" =~ ^[a-z0-9]{5,50}$ ]] || fail "ACR name must contain 5-50 lowercase letters or digits"
[[ "$prefix" =~ ^[a-z][a-z0-9]{2,10}$ ]] || fail "Prefix must start with a lowercase letter and contain 3-11 lowercase letters or digits"

progress "Checking required commands and Linux Azure CLI."
for command in az docker jq python3; do
  command -v "$command" >/dev/null 2>&1 || fail "Required command not found: $command"
done

az_path=$(command -v az)
if [[ "$az_path" == /mnt/* ]]; then
  fail "az resolves to a Windows installation ($az_path). 'az acr login' cannot reach the Linux Docker engine from it. Install Azure CLI inside Linux (https://learn.microsoft.com/cli/azure/install-azure-cli-linux) and ensure it comes first in PATH."
fi

progress "Checking Docker daemon connectivity."
if ! docker info >/dev/null; then
  fail "Docker client is installed but cannot reach a running Docker daemon. Start the Docker engine, verify 'docker info' succeeds, then rerun bootstrap."
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

az_json() {
  az "$@" --only-show-errors --output json
}

progress "Starting bootstrap for prefix '$prefix'. Selecting Azure subscription."
az_json account set --subscription "$subscription_id" >/dev/null
for provider in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.ManagedIdentity; do
  progress "Registering resource provider $provider (waiting for registration)."
  az_json provider register --namespace "$provider" --wait >/dev/null
done

shared_group="${prefix}-shared"
progress "Creating shared resource group $shared_group in koreacentral."
az_json group create --name "$shared_group" --location koreacentral >/dev/null
progress "Deploying shared ACR and image-pull identity in $shared_group. This may take several minutes."
shared=$(az_json deployment group create \
  --name shared \
  --resource-group "$shared_group" \
  --template-file infra/shared.bicep \
  --parameters "acrName=$acr_name")
server=$(jq -er '.properties.outputs.acrServer.value' <<<"$shared")
pull_id=$(jq -er '.properties.outputs.pullIdentityId.value' <<<"$shared")
progress "Shared infrastructure ready. Registry: $server."

codes=(kr sea usw)
locations=(koreacentral southeastasia westus)
for code in "${codes[@]}"; do
  group="${prefix}-${code}"
  progress "Checking $group for existing container apps before bootstrap."
  exists=$(az_json group exists --name "$group")
  if [[ "$exists" == true ]]; then
    apps=$(az_json resource list --resource-group "$group" --resource-type Microsoft.App/containerApps)
    if [[ "$(jq 'length' <<<"$apps")" -gt 0 ]]; then
      fail "$group already contains apps. Bootstrap is not a release/recovery command."
    fi
  fi
done

progress "Logging in to ACR $acr_name."
az acr login --name "$acr_name" --only-show-errors
tag="safe-demo:bootstrap-$(date -u +%s)"
progress "Building linux/amd64 bootstrap image: ${server}/${tag}."
docker build --platform linux/amd64 -t "${server}/${tag}" app
progress "Pushing bootstrap image: ${server}/${tag}."
docker push "${server}/${tag}"
progress "Resolving immutable image digest."
manifest=$(az_json acr repository show --name "$acr_name" --image "$tag")
digest=$(jq -er '.digest' <<<"$manifest")
image="${server}/safe-demo@${digest}"
[[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || fail "Immutable digest not resolved"

progress "Image ready. Waiting 60 seconds for initial AcrPull RBAC propagation (not a guarantee)."
sleep 60
outputs='{}'
for index in "${!codes[@]}"; do
  code=${codes[$index]}
  location=${locations[$index]}
  group="${prefix}-${code}"
  app="${prefix}-${code}-app"
  progress "Creating regional resource group $group in $location."
  az_json group create --name "$group" --location "$location" >/dev/null
  progress "Deploying ACA environment, workspace and app $app in $location. This may take several minutes."
  deployment=$(az_json deployment group create \
    --name bootstrap \
    --resource-group "$group" \
    --template-file infra/region.bicep \
    --parameters \
      "location=$location" \
      "appName=$app" \
      "image=$image" \
      "acrServer=$server" \
      "pullIdentityId=$pull_id")
  outputs=$(jq \
    --arg code "$code" \
    --arg group "$group" \
    --arg app "$app" \
    --arg workspace_customer_id "$(jq -er '.properties.outputs.workspaceCustomerId.value' <<<"$deployment")" \
    --arg workspace_resource_id "$(jq -er '.properties.outputs.workspaceId.value' <<<"$deployment")" \
    --arg app_resource_id "$(jq -er '.properties.outputs.appId.value' <<<"$deployment")" \
    --arg fqdn "$(jq -er '.properties.outputs.fqdn.value' <<<"$deployment")" \
    '. + {($code): {
      RESOURCE_GROUP: $group,
      CONTAINER_APP: $app,
      LOG_ANALYTICS_WORKSPACE_ID: $workspace_customer_id,
      WORKSPACE_RESOURCE_ID: $workspace_resource_id,
      APP_RESOURCE_ID: $app_resource_id,
      FQDN: $fqdn
    }}' <<<"$outputs")
  progress "Region $code deployed. App FQDN: $(jq -er '.properties.outputs.fqdn.value' <<<"$deployment")."
done

progress "Saving bootstrap outputs to state/bootstrap-outputs.json."
mkdir -p state
printf '%s\n' "$outputs" | jq . > state/bootstrap-outputs.json
progress "Bootstrap complete. Outputs: state/bootstrap-outputs.json (do not commit). Configure environments and OIDC next."
