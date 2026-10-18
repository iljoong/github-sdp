# Requires PowerShell 7, Azure CLI, Docker (Linux engine), Python 3.13.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{5,50}$')][string]$AcrName,
    [ValidatePattern('^[a-z][a-z0-9]{2,10}$')][string]$Prefix = 'safeaca'
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
function Write-ProgressMessage {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $Message"
}
function Invoke-Az {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    $result = & az @Arguments --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw "az $($Arguments[0..1] -join ' ') failed" }
    if ($result) { return ($result | ConvertFrom-Json) }
}

Write-ProgressMessage "Starting bootstrap for prefix '$Prefix'. Selecting Azure subscription."
Invoke-Az account set --subscription $SubscriptionId
foreach ($provider in @('Microsoft.App', 'Microsoft.OperationalInsights', 'Microsoft.ContainerRegistry', 'Microsoft.ManagedIdentity')) {
    Write-ProgressMessage "Registering resource provider $provider (waiting for registration)."
    Invoke-Az provider register --namespace $provider --wait | Out-Null
}
$sharedGroup = "$Prefix-shared"
Write-ProgressMessage "Creating shared resource group $sharedGroup in koreacentral."
Invoke-Az group create --name $sharedGroup --location koreacentral | Out-Null
Write-ProgressMessage "Deploying shared ACR and image-pull identity in $sharedGroup. This may take several minutes."
$shared = Invoke-Az deployment group create --name shared --resource-group $sharedGroup --template-file infra\shared.bicep --parameters "acrName=$AcrName"
$server = $shared.properties.outputs.acrServer.value
$pullId = $shared.properties.outputs.pullIdentityId.value
Write-ProgressMessage "Shared infrastructure ready. Registry: $server."

# Fail closed on existing apps: rerunning bootstrap must not reset a live release.
$regions = @(
    @{ Code = 'kr'; Location = 'koreacentral' },
    @{ Code = 'sea'; Location = 'southeastasia' },
    @{ Code = 'usw'; Location = 'westus' }
)
foreach ($region in $regions) {
    $group = "$Prefix-$($region.Code)"
    Write-ProgressMessage "Checking $group for existing container apps before bootstrap."
    $exists = Invoke-Az group exists --name $group
    if ($exists) {
        $apps = @(Invoke-Az resource list --resource-group $group --resource-type Microsoft.App/containerApps)
        if ($apps.Count -gt 0) { throw "$group already contains apps. Bootstrap is not a release/recovery command." }
    }
}

# Operator must already have AcrPush (or equivalent); no admin registry password.
Write-ProgressMessage "Logging in to ACR $AcrName."
& az acr login --name $AcrName --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'ACR login failed' }
$tag = "safe-demo:bootstrap-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
Write-ProgressMessage "Building linux/amd64 bootstrap image: $server/$tag."
& docker build --platform linux/amd64 -t "$server/$tag" app
if ($LASTEXITCODE -ne 0) { throw 'Docker build failed' }
Write-ProgressMessage "Pushing bootstrap image: $server/$tag."
& docker push "$server/$tag"
if ($LASTEXITCODE -ne 0) { throw 'Docker push failed' }
Write-ProgressMessage 'Resolving immutable image digest.'
$manifest = Invoke-Az acr repository show --name $AcrName --image $tag
$image = "$server/safe-demo@$($manifest.digest)"
if ($image -notmatch '@sha256:[a-f0-9]{64}$') { throw 'Immutable digest not resolved' }
Write-ProgressMessage 'Image ready. Waiting 60 seconds for initial AcrPull RBAC propagation (not a guarantee).'
Start-Sleep -Seconds 60 # Initial AcrPull RBAC propagation; not a guarantee.
$outputs = @{}
foreach ($region in $regions) {
    $group = "$Prefix-$($region.Code)"
    $app = "$Prefix-$($region.Code)-app"
    Write-ProgressMessage "Creating regional resource group $group in $($region.Location)."
    Invoke-Az group create --name $group --location $region.Location | Out-Null
    Write-ProgressMessage "Deploying ACA environment, workspace and app $app in $($region.Location). This may take several minutes."
    $deployment = Invoke-Az deployment group create --name bootstrap --resource-group $group --template-file infra\region.bicep --parameters "location=$($region.Location)" "appName=$app" "image=$image" "acrServer=$server" "pullIdentityId=$pullId"
    $outputs[$region.Code] = @{
        RESOURCE_GROUP = $group
        CONTAINER_APP = $app
        LOG_ANALYTICS_WORKSPACE_ID = $deployment.properties.outputs.workspaceCustomerId.value
        WORKSPACE_RESOURCE_ID = $deployment.properties.outputs.workspaceId.value
        APP_RESOURCE_ID = $deployment.properties.outputs.appId.value
        FQDN = $deployment.properties.outputs.fqdn.value
    }
    Write-ProgressMessage "Region $($region.Code) deployed. App FQDN: $($outputs[$region.Code].FQDN)."
}
Write-ProgressMessage 'Saving bootstrap outputs to state\bootstrap-outputs.json.'
New-Item -ItemType Directory -Path state -Force | Out-Null
$outputs | ConvertTo-Json -Depth 6 | Set-Content state\bootstrap-outputs.json -Encoding utf8
Write-ProgressMessage 'Bootstrap complete. Outputs: state\bootstrap-outputs.json (do not commit). Configure environments and OIDC next.'
