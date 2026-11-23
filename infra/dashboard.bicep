targetScope = 'resourceGroup'

@minLength(3)
@maxLength(15)
param name string
param location string = resourceGroup().location
param managedEnvironmentId string
param storageAccountName string
@description('Internal ACR image pinned by digest. Grant both dedicated identities pull permission first.')
param image string
param registryServer string
param tenantId string = tenant().tenantId
param entraClientId string
param entraSecretUri string
@description('Key Vault secret URI for the private token-store Blob container HTTPS user-delegation SAS URL.')
param tokenStoreSasSecretUri string
param githubRepo string
param githubRepoId string
param githubInstallationId string
param githubAppClientId string
param githubSigningKeyUri string
param githubWebhookSecretUri string
param consoleIdentity { id: string, clientId: string, principalId: string }
param receiverIdentity { id: string, clientId: string, principalId: string }
@description('Registered Container App resource IDs. Grant the console identity Reader on these apps.')
param targets { kr: string, sea: string, usw: string }

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  name: 'default'
  parent: storage
}

resource operations 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'dashboardops'
}

resource inbox 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'dashboardinbox'
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource tokenStore 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'dashboardtokens'
  properties: { publicAccess: 'None' }
}

resource operationsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: operations
  name: guid(operations.id, consoleIdentity.principalId, 'data')
  properties: {
    principalId: consoleIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  }
}

resource inboxReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: inbox
  name: guid(inbox.id, consoleIdentity.principalId, 'read')
  properties: {
    principalId: consoleIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '76199698-9eea-4c19-bc75-cec21354c6b6')
  }
}

resource inboxInsertRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, name, 'inbox-insert')
  properties: {
    roleName: '${name} webhook inbox insert (${uniqueString(resourceGroup().id)})'
    description: 'Insert GitHub delivery records; cannot read, rewrite or delete them.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [
      {
        actions: []
        dataActions: ['Microsoft.Storage/storageAccounts/tableServices/tables/entities/add/action']
      }
    ]
  }
}

resource inboxWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: inbox
  name: guid(inbox.id, receiverIdentity.principalId, 'insert')
  properties: {
    principalId: receiverIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: inboxInsertRole.id
  }
}

var shared = [
  { name: 'DASHBOARD_REPO', value: githubRepo }
  { name: 'DASHBOARD_REPO_ID', value: githubRepoId }
  { name: 'DASHBOARD_INSTALLATION_ID', value: githubInstallationId }
  { name: 'DASHBOARD_TABLE_ENDPOINT', value: 'https://${storage.name}.table.${environment().suffixes.storage}' }
]

var probe = {
  type: 'Readiness'
  httpGet: { path: '/healthz', port: 8080 }
  periodSeconds: 10
}

resource console 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-console'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${consoleIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8080, allowInsecure: false }
      registries: [{ server: registryServer, identity: consoleIdentity.id }]
      secrets: [
        { name: 'entra-client-secret', keyVaultUrl: entraSecretUri, identity: consoleIdentity.id }
        { name: 'token-store-sas', keyVaultUrl: tokenStoreSasSecretUri, identity: consoleIdentity.id }
      ]
    }
    template: {
      scale: { minReplicas: 1, maxReplicas: 3 }
      containers: [
        {
          name: 'console'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          args: ['dashboard.server:app', '--host', '0.0.0.0', '--port', '8080']
          probes: [probe]
          env: concat(shared, [
            { name: 'DASHBOARD_MI_CLIENT_ID', value: consoleIdentity.clientId }
            { name: 'DASHBOARD_TENANT_ID', value: tenantId }
            { name: 'DASHBOARD_ENTRA_CLIENT_ID', value: entraClientId }
            { name: 'DASHBOARD_ORIGIN', value: 'https://${name}-console.${environmentResource.properties.defaultDomain}' }
            { name: 'DASHBOARD_APP_CLIENT_ID', value: githubAppClientId }
            { name: 'DASHBOARD_SIGNING_KEY', value: githubSigningKeyUri }
            { name: 'DASHBOARD_TARGETS', value: string(targets) }
          ])
        }
      ]
    }
  }
}

resource environmentResource 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  scope: resourceGroup(split(managedEnvironmentId, '/')[2], split(managedEnvironmentId, '/')[4])
  name: last(split(managedEnvironmentId, '/'))
}

resource consoleAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: console
  name: 'current'
  dependsOn: [tokenStore]
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          clientSecretSettingName: 'entra-client-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: { allowedAudiences: [entraClientId] }
        login: { loginParameters: ['scope=openid profile offline_access'] }
      }
    }
    login: {
      tokenStore: {
        enabled: true
        azureBlobStorage: { sasUrlSettingName: 'token-store-sas' }
      }
    }
  }
}

resource receiver 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-hooks'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${receiverIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8080, allowInsecure: false }
      registries: [{ server: registryServer, identity: receiverIdentity.id }]
      secrets: [{ name: 'github-hook-secret', keyVaultUrl: githubWebhookSecretUri, identity: receiverIdentity.id }]
    }
    template: {
      scale: { minReplicas: 1, maxReplicas: 2 }
      containers: [
        {
          name: 'receiver'
          image: image
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          args: ['dashboard.webhook:app', '--host', '0.0.0.0', '--port', '8080']
          probes: [probe]
          env: concat(shared, [
            { name: 'DASHBOARD_MI_CLIENT_ID', value: receiverIdentity.clientId }
            { name: 'DASHBOARD_WEBHOOK_SECRET', secretRef: 'github-hook-secret' }
          ])
        }
      ]
    }
  }
}

output consoleUrl string = 'https://${console.properties.configuration.ingress.fqdn}'
output githubCallback string = 'https://${receiver.properties.configuration.ingress.fqdn}/webhooks/github'
