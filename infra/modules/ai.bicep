// The Azure AI Foundry account and its model-router deployment.
//
// Cost note: neither resource has a fixed or hourly charge. The S0 SKU on a
// Cognitive Services account is a pay-as-you-go tier, not a reservation, and a
// GlobalStandard model deployment bills per token rather than per hour. An idle
// qq deployment costs nothing. Only fine-tuned model deployments carry hosting
// charges, and qq does not create one.

@description('Short name prefix for all resources.')
param namePrefix string

@description('Environment suffix.')
param environment string

@description('Azure region.')
param location string

@description('Name of the model-router deployment.')
param routerDeploymentName string

@allowed([
  'balanced'
  'cost'
  'quality'
])
@description('Model-router routing mode.')
param routingMode string

@description('Model-router version.')
param routerModelVersion string

@description('Capacity in thousands of TPM.')
param routerCapacity int

@description('Models the router may route among. Empty means all models the router version supports.')
param routerModels array

@description('Principal to grant the Foundry User role. Empty skips the assignment.')
param principalId string = ''

@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param principalType string = 'User'

@description('Refuse API keys and require Entra ID.')
param disableLocalAuth bool = false

@description('Resource id of an existing Log Analytics workspace for request logs and metrics. Empty disables diagnostics.')
param logAnalyticsWorkspaceId string = ''

param tags object = {}

// Account names must be globally unique, 2-64 chars, lowercase alphanumeric and
// hyphens. uniqueString is deterministic per resource group, so redeploying is
// idempotent rather than name-churning.
var accountName = toLower('${namePrefix}-${environment}-${uniqueString(resourceGroup().id)}')

// Foundry User. Microsoft renamed this role from "Azure AI User" and advises
// referencing the GUID rather than the display name.
// https://learn.microsoft.com/en-us/azure/foundry/concepts/rbac-foundry
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Required in practice for Entra ID / keyless auth against the data plane.
    customSubDomainName: accountName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: disableLocalAuth
  }
}

resource router 'Microsoft.CognitiveServices/accounts/deployments@2026-05-01' = {
  parent: account
  name: routerDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: routerCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'model-router'
      version: routerModelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    routing: empty(routerModels)
      ? {
          mode: routingMode
        }
      : {
          mode: routingMode
          models: routerModels
        }
  }
}

// An AIServices account publishes several endpoints. The OpenAI v1 inference
// route lives on the *.openai.azure.com host, not on the generic account
// endpoint, so pick that one out of the map and fall back to the deterministic
// form (customSubDomainName is set to the account name above).
var endpointMap = account.properties.endpoints
var openAiEndpoint = endpointMap[?'OpenAI Language Model Instance API'] ?? 'https://${accountName}.openai.azure.com/'

resource inferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(account.id, principalId, foundryUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: principalId
    principalType: principalType
  }
}

// Server-side observability. Off unless a workspace is supplied, because a
// workspace is a billable resource and qq should cost nothing when idle. When
// enabled, the RequestResponse category carries per-request latency and token
// counts, including which model the router actually selected.
resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'qq-diagnostics'
  scope: account
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output endpoint string = openAiEndpoint
output accountEndpoint string = account.properties.endpoint
output accountName string = account.name
output routerDeploymentName string = router.name
output accountId string = account.id
