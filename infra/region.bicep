targetScope = 'resourceGroup'

@allowed([
  'koreacentral'
  'southeastasia'
  'westus'
])
param location string

@minLength(3)
@maxLength(20)
param appName string
param image string
param acrServer string
param pullIdentityId string

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 90
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// BOOTSTRAP ONLY. Redeploying this resource after a release is NOT safe.
resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Multiple'
      maxInactiveRevisions: 20
      registries: [
        {
          server: acrServer
          identity: pullIdentityId
        }
      ]
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        stickySessions: {
          affinity: 'none'
        }
        traffic: [
          {
            latestRevision: false
            revisionName: '${appName}--bootstrap'
            weight: 100
          }
        ]
      }
    }
    template: {
      revisionSuffix: 'bootstrap'
      containers: [
        {
          name: 'web'
          image: image
          env: [
            {
              name: 'RELEASE_ID'
              value: 'bootstrap'
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          probes: loadJsonContent('./health-probes.json')
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '25'
              }
            }
          }
        ]
      }
    }
  }
}

output appId string = app.id
output fqdn string = app.properties.configuration.ingress.fqdn
output workspaceId string = workspace.id
output workspaceCustomerId string = workspace.properties.customerId
