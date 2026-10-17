# Provisioning helper; intentionally NOT called by deployment workflows.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repository,
    [Parameter(Mandatory)][ValidateSet('build','prod-kr','prod-de','prod-br','prod-rollback-kr','prod-rollback-de','prod-rollback-br')][string]$Environment,
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$ClaimsFile,
    [string]$AcrName,
    [string]$ResourceGroup,
    [string]$ContainerApp,
    [string]$WorkspaceResourceId,
    [string]$WorkspaceCustomerId
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
function Invoke-Az {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    $result = & az @Arguments --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw 'Azure identity provisioning failed' }
    if ($result) { $result | ConvertFrom-Json }
}
function Set-GitHubVariable([string]$Name, [string]$Value) {
    & gh variable set $Name --repo $Repository --env $Environment --body $Value
    if ($LASTEXITCODE -ne 0) { throw "Failed setting $Name; create/protect GitHub environment first" }
}
Invoke-Az account set --subscription $SubscriptionId
$account = Invoke-Az account show
$claims = Get-Content $ClaimsFile -Raw | ConvertFrom-Json
if ($claims.iss -ne 'https://token.actions.githubusercontent.com' -or
    $claims.aud -ne 'api://AzureADTokenExchange' -or
    $claims.repository -cne $Repository -or
    $claims.ref -ne 'refs/heads/main' -or
    $claims.inspected_environment -ne $Environment -or
    -not $claims.sub) {
    throw 'Claims do not match the expected issuer/audience/repository/main/environment. Inspect actual GitHub job claims first.'
}
if ($claims.environment -and $claims.environment -ne $Environment) {
    throw 'Token environment does not match requested environment'
}
if (-not $claims.environment -and
    $claims.sub -notlike "*:environment:$Environment" -and
    $claims.sub -notlike "*:environment:${Environment}:*") {
    throw 'No environment trust context in inspected claims; review custom subject template'
}
$repositoryMetadata = & gh api "repos/$Repository" --jq '{repository_id: (.id|tostring), owner_id: (.owner.id|tostring)}'
if ($LASTEXITCODE -ne 0) { throw 'Cannot verify current GitHub repository/owner IDs' }
$repositoryMetadata = $repositoryMetadata | ConvertFrom-Json
if ($claims.repository_id -ne $repositoryMetadata.repository_id -or
    $claims.repository_owner_id -ne $repositoryMetadata.owner_id) {
    throw 'Claims IDs do not match current GitHub repository/owner; inspect again after rename/transfer'
}
New-Item -ItemType Directory -Path state -Force | Out-Null
$record = "state\identity-$Environment.json"
if (Test-Path $record) { throw "Identity record exists: $record. Reuse it, do not blindly create duplicate principals." }
if ($Environment -eq 'build') {
    if (-not $AcrName) { throw 'Build requires AcrName' }
} elseif (-not ($ResourceGroup -and $ContainerApp -and $WorkspaceResourceId -and $WorkspaceCustomerId)) {
    throw 'Regional identity requires ResourceGroup, ContainerApp, WorkspaceResourceId, WorkspaceCustomerId'
}
$application = Invoke-Az ad app create --display-name "safe-aca-$($Repository.Replace('/','-'))-$Environment"
$principal = Invoke-Az ad sp create --id $application.appId
# Preserve identifiers immediately, even if subsequent RBAC provisioning fails.
@{ appObjectId = $application.id; clientId = $application.appId; principalId = $principal.id } |
    ConvertTo-Json | Set-Content $record -Encoding utf8
@{
    name = "github-$Environment"
    issuer = 'https://token.actions.githubusercontent.com'
    subject = $claims.sub
    audiences = @('api://AzureADTokenExchange')
} | ConvertTo-Json | Set-Content "state\oidc-$Environment.json" -Encoding utf8
Invoke-Az ad app federated-credential create --id $application.id --parameters "@state\oidc-$Environment.json" | Out-Null
if ($Environment -eq 'build') {
    $registry = Invoke-Az acr show --name $AcrName
    Invoke-Az role assignment create --assignee-object-id $principal.id --assignee-principal-type ServicePrincipal --role AcrPush --scope $registry.id | Out-Null
    Invoke-Az role assignment create --assignee-object-id $principal.id --assignee-principal-type ServicePrincipal --role Reader --scope $registry.id | Out-Null
    Set-GitHubVariable ACR_NAME $AcrName
} else {
    $appScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.App/containerApps/$ContainerApp"
    $groupScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
    Invoke-Az role assignment create --assignee-object-id $principal.id --assignee-principal-type ServicePrincipal --role 'Container Apps Contributor' --scope $appScope | Out-Null
    Invoke-Az role assignment create --assignee-object-id $principal.id --assignee-principal-type ServicePrincipal --role Reader --scope $groupScope | Out-Null
    Invoke-Az role assignment create --assignee-object-id $principal.id --assignee-principal-type ServicePrincipal --role 'Log Analytics Reader' --scope $WorkspaceResourceId | Out-Null
    Set-GitHubVariable RESOURCE_GROUP $ResourceGroup
    Set-GitHubVariable CONTAINER_APP $ContainerApp
    Set-GitHubVariable LOG_ANALYTICS_WORKSPACE_ID $WorkspaceCustomerId
}
Set-GitHubVariable AZURE_CLIENT_ID $application.appId
Set-GitHubVariable AZURE_TENANT_ID $account.tenantId
Set-GitHubVariable AZURE_SUBSCRIPTION_ID $SubscriptionId
Write-Host "Configured distinct $Environment OIDC identity. No client secret created. Verify reviewers/branch protection in GitHub."
