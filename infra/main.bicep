// qq - Azure infrastructure
//
// Subscription-scope deployment: creates the resource group and everything in it.
// The entire server side of qq is one Azure AI Foundry account, one
// model-router deployment and one Foundry project on the account. There is
// deliberately no API, no gateway, no database and no Key Vault: the CLI talks
// to Foundry directly, through the project endpoint.
//
//   az deployment sub create \
//     --location eastus2 \
//     --template-file infra/main.bicep \
//     --parameters infra/main.bicepparam

targetScope = 'subscription'

@description('Short name prefix for all resources. Lowercase letters and digits.')
@minLength(2)
@maxLength(12)
param namePrefix string = 'qq'

@description('Environment suffix, used in resource names and tags.')
@minLength(1)
@maxLength(10)
param environment string = 'dev'

@description('Azure region. Must support model-router; see README for the supported list.')
param location string = 'eastus2'

@description('Resource group name. Defaults to rg-<namePrefix>-<environment>.')
param resourceGroupName string = 'rg-${namePrefix}-${environment}'

@description('Name of the model-router deployment. This is the value qq sends as the model id.')
param routerDeploymentName string = 'qq-router'

@description('''Name of the Foundry project on the account. qq calls the project endpoint,
because model-router only accepts the Responses API there; the bare account endpoint
speaks Chat Completions only. Defaults to <namePrefix>-<environment>.''')
param projectName string = '${namePrefix}-${environment}'

@description('Model-router routing mode. balanced trades cost against quality; cost and quality bias to one end.')
@allowed([
  'balanced'
  'cost'
  'quality'
])
param routingMode string = 'balanced'

@description('Model-router version. 2025-11-18 is updated in place by Microsoft as new models are added.')
param routerModelVersion string = '2025-11-18'

@description('Deployment capacity in thousands of tokens per minute. 10 = ~10,000 TPM.')
@minValue(1)
@maxValue(1000)
param routerCapacity int = 10

@description('''Models the router may choose among. Each entry is { format, name, version }.
Supply at least two: a single-model subset disables automatic failover.

COST WARNING: leaving this empty lets the router use every model its version supports,
which includes Anthropic, xAI, DeepSeek and Meta models. Those are billed separately
rather than against Azure consumption. The default below is OpenAI only.''')
param routerModels array = [
  {
    format: 'OpenAI'
    name: 'gpt-5.6-luna'
    version: '2026-07-09'
  }
  {
    format: 'OpenAI'
    name: 'gpt-5.6-terra'
    version: '2026-07-09'
  }
  {
    format: 'OpenAI'
    name: 'gpt-5.6-sol'
    version: '2026-07-09'
  }
]

@description('Object ID of a user or service principal to grant inference access (Foundry User). Empty skips the role assignment.')
param principalId string = ''

@description('Principal type for the role assignment above.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param principalType string = 'User'

@description('Set true to refuse API keys and require Microsoft Entra ID for every call.')
param disableLocalAuth bool = false

@description('''Resource id of an existing Log Analytics workspace to receive request logs
and metrics. Empty disables diagnostics, which is the default because a workspace is a
billable resource and qq costs nothing when idle.''')
param logAnalyticsWorkspaceId string = ''

@description('Extra tags merged into the default tag set.')
param tags object = {}

var defaultTags = union(
  {
    application: 'qq'
    environment: environment
    'managed-by': 'bicep'
    repo: 'github.com/CrankingAI/qq-router'
  },
  tags
)

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: defaultTags
}

module ai 'modules/ai.bicep' = {
  name: 'qq-ai'
  scope: rg
  params: {
    namePrefix: namePrefix
    environment: environment
    location: location
    routerDeploymentName: routerDeploymentName
    projectName: projectName
    routingMode: routingMode
    routerModelVersion: routerModelVersion
    routerCapacity: routerCapacity
    routerModels: routerModels
    principalId: principalId
    principalType: principalType
    disableLocalAuth: disableLocalAuth
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
    tags: defaultTags
  }
}

@description('Foundry project endpoint. Set this as QQ_ENDPOINT; qq appends /openai/v1.')
output projectEndpoint string = ai.outputs.projectEndpoint

@description('Name of the Foundry project.')
output projectName string = ai.outputs.projectName

@description('Foundry account endpoint. Also works as QQ_ENDPOINT, but the router speaks Chat Completions only there, so --search is unavailable.')
output endpoint string = ai.outputs.endpoint

@description('Name of the Foundry (Cognitive Services) account.')
output accountName string = ai.outputs.accountName

@description('Model-router deployment name. Set this as QQ_DEPLOYMENT.')
output routerDeploymentName string = ai.outputs.routerDeploymentName

@description('Resource group the account lives in.')
output resourceGroupName string = rg.name

@description('Region the account was deployed to.')
output location string = location

@description('Routing mode the router was configured with.')
output routingMode string = routingMode

@description('Models the router may select among.')
output routerModels array = [for m in routerModels: '${m.name}:${m.version}']
