#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# deploy.sh — Provision the Foundry account, model-router and project for qq
#
# Usage:
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --env dev --location eastus2
#   ./scripts/deploy.sh --dry-run
#
# Lifecycle: routine — safe to re-run; use --dry-run to preview.
#
# Deploys infra/main.bicep at subscription scope, which creates the resource
# group and everything in it. Re-running converges the same resources.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_NAME="dev"
LOCATION="eastus2"
NAME_PREFIX="qq"
ROUTER_NAME="qq-router"
PROJECT_NAME=""
ROUTING_MODE="balanced"
ROUTER_CAPACITY="10"
SUBSCRIPTION=""
DRY_RUN=false
GRANT_SELF=true

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Provision the Foundry account, model-router deployment and project that qq talks to.
Lifecycle: routine — safe to re-run; use --dry-run to preview.

Options:
  --env <name>            Environment suffix used in names and tags (default: $ENV_NAME)
  --location <region>     Azure region (default: $LOCATION)
  --name-prefix <prefix>  Resource name prefix (default: $NAME_PREFIX)
  --deployment <name>     Model-router deployment name (default: $ROUTER_NAME)
  --project <name>        Foundry project name (default: <prefix>-<env>)
  --routing-mode <mode>   balanced | cost | quality (default: $ROUTING_MODE)
  --capacity <n>          Capacity in thousands of TPM (default: $ROUTER_CAPACITY)
  --subscription <id>     Azure subscription (default: the current az subscription)
  --no-grant              Skip granting yourself the Foundry User role
  --dry-run               Show what would change (az deployment sub what-if)
  -h, --help              Show this help

Environment variables:
  AZURE_SUBSCRIPTION_ID   Used when --subscription is not given and no CLI default is set

Examples:
  $(basename "$0")
  $(basename "$0") --env prod --location swedencentral
  $(basename "$0") --dry-run
EOF
  exit 0
}

case "${1:-}" in
  -h|--help|help) usage ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help)  usage ;;
    --env)           ENV_NAME="${2:?--env needs a value}"; shift 2 ;;
    --location)      LOCATION="${2:?--location needs a value}"; shift 2 ;;
    --name-prefix)   NAME_PREFIX="${2:?--name-prefix needs a value}"; shift 2 ;;
    --deployment)    ROUTER_NAME="${2:?--deployment needs a value}"; shift 2 ;;
    --project)       PROJECT_NAME="${2:?--project needs a value}"; shift 2 ;;
    --routing-mode)  ROUTING_MODE="${2:?--routing-mode needs a value}"; shift 2 ;;
    --capacity)      ROUTER_CAPACITY="${2:?--capacity needs a value}"; shift 2 ;;
    --subscription)  SUBSCRIPTION="${2:?--subscription needs a value}"; shift 2 ;;
    --no-grant)      GRANT_SELF=false; shift ;;
    --dry-run)       DRY_RUN=true; shift ;;
    *)               echo "Unknown option: $1" >&2; usage ;;
  esac
done

need() {
  command -v "$1" >/dev/null 2>&1 && return 0
  echo "Error: '$1' is not installed." >&2
  echo "  $2" >&2
  exit 1
}
need az "brew install azure-cli"
need jq "brew install jq"
need openssl "brew install openssl"

case "$ROUTING_MODE" in
  balanced|cost|quality) ;;
  *) echo "Error: invalid routing mode '$ROUTING_MODE'. Must be balanced, cost, or quality." >&2; exit 1 ;;
esac

if ! [[ "$ROUTER_CAPACITY" =~ ^[0-9]+$ ]] || [[ "$ROUTER_CAPACITY" -lt 1 ]]; then
  echo "Error: --capacity must be a positive integer (thousands of TPM)." >&2
  exit 1
fi

# Resolve the subscription once, then pass it explicitly to every az call.
# Never 'az account set': that mutates state shared with every other shell.
if [[ -z "$SUBSCRIPTION" ]]; then
  SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-}"
fi
if [[ -z "$SUBSCRIPTION" ]]; then
  SUBSCRIPTION="$(az account show --query id -o tsv 2>/dev/null || true)"
fi
if [[ -z "$SUBSCRIPTION" ]]; then
  echo "Error: no Azure subscription selected and none could be inferred." >&2
  echo "  az login   # then re-run, or pass --subscription <id>" >&2
  exit 1
fi

if ! az account show --subscription "$SUBSCRIPTION" --output none 2>/dev/null; then
  echo "Error: not logged in, or no access to subscription '$SUBSCRIPTION'." >&2
  echo "  az login" >&2
  exit 1
fi

SUB_NAME="$(az account show --subscription "$SUBSCRIPTION" --query name -o tsv)"
RESOURCE_GROUP="rg-${NAME_PREFIX}-${ENV_NAME}"

echo "==> Target"
echo "    Subscription:   $SUB_NAME ($SUBSCRIPTION)"
echo "    Resource group: $RESOURCE_GROUP"
echo "    Location:       $LOCATION"
echo "    Router:         $ROUTER_NAME (mode=$ROUTING_MODE capacity=$ROUTER_CAPACITY)"
echo "    Project:        ${PROJECT_NAME:-${NAME_PREFIX}-${ENV_NAME}}"

echo "==> Checking model-router availability in $LOCATION..."
MODELS_JSON="$(az cognitiveservices model list \
  --subscription "$SUBSCRIPTION" \
  --location "$LOCATION" \
  --only-show-errors \
  -o json)"

ROUTER_VERSION="$(echo "$MODELS_JSON" | jq -r '
  [ .[]
    | select(.kind == "AIServices")
    | select(.model.name == "model-router")
    | select([.model.skus[].name] | index("GlobalStandard"))
    | .model.version ]
  | sort | last // empty')"

if [[ -z "$ROUTER_VERSION" ]]; then
  echo "Error: model-router (GlobalStandard) is not available in '$LOCATION'." >&2
  echo "       Supported regions change over time. Pick one that lists it:" >&2
  echo "       az cognitiveservices model list --subscription $SUBSCRIPTION --location <region> \\" >&2
  echo "         --query \"[?model.name=='model-router'].model.version\" -o tsv" >&2
  echo "       eastus2 and swedencentral are usually safe choices." >&2
  exit 1
fi
echo "    model-router version $ROUTER_VERSION is available"

# The router can only route to models that exist in this region. Report the
# gpt-5.6 subset the Bicep asks for so a region problem surfaces here rather
# than as an opaque InvalidResourceProperties during deployment.
echo "==> Checking the requested routing subset..."
MISSING=""
for want in gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol; do
  hit="$(echo "$MODELS_JSON" | jq -r --arg n "$want" '
    [ .[] | select(.kind=="AIServices") | select(.model.name == $n) | .model.version ] | sort | last // empty')"
  if [[ -z "$hit" ]]; then
    MISSING="$MISSING $want"
  else
    echo "    $want $hit"
  fi
done
if [[ -n "$MISSING" ]]; then
  echo "Error: these models are not available in '$LOCATION':$MISSING" >&2
  echo "       Choose another region, or edit routerModels in infra/main.bicep." >&2
  exit 1
fi

# Resolve the caller's object id *in the target subscription's tenant*.
#
# 'az ad signed-in-user show' answers for whatever tenant the Azure CLI happens
# to have selected globally, which is shared mutable state. If that differs from
# the tenant owning this subscription, the role assignment fails with
# PrincipalNotFound. Reading the 'oid' claim out of a Graph token issued for
# this subscription always gives the right answer.
principal_id_for_subscription() {
  local sub="$1" token payload
  token="$(az account get-access-token \
    --subscription "$sub" \
    --resource https://graph.microsoft.com \
    --query accessToken -o tsv 2>/dev/null || true)"
  [[ -n "$token" ]] || return 0
  payload="$(printf '%s' "$token" | cut -d. -f2 | tr '_-' '/+')"
  while [[ $(( ${#payload} % 4 )) -ne 0 ]]; do
    payload="${payload}="
  done
  printf '%s' "$payload" | openssl base64 -d -A 2>/dev/null | jq -r '.oid // empty'
}

PRINCIPAL_ID=""
if [[ "$GRANT_SELF" == true ]]; then
  PRINCIPAL_ID="$(principal_id_for_subscription "$SUBSCRIPTION")"
  if [[ -z "$PRINCIPAL_ID" ]]; then
    echo "    note: could not read your object id; skipping the role assignment."
    echo "          Entra sign-in may need a role granted by hand (see README)."
  else
    echo "    Granting Foundry User to principal $PRINCIPAL_ID"
  fi
fi

echo "==> Validating Bicep..."
az bicep build --file "$REPO_ROOT/infra/main.bicep" --stdout >/dev/null
echo "    infra/main.bicep builds cleanly"

DEPLOYMENT_NAME="qq-$(date +%Y%m%d%H%M%S)"
PARAMS=(
  "namePrefix=$NAME_PREFIX"
  "environment=$ENV_NAME"
  "location=$LOCATION"
  "routerDeploymentName=$ROUTER_NAME"
  "routingMode=$ROUTING_MODE"
  "routerCapacity=$ROUTER_CAPACITY"
)
if [[ -n "$PRINCIPAL_ID" ]]; then
  PARAMS+=("principalId=$PRINCIPAL_ID")
fi
if [[ -n "$PROJECT_NAME" ]]; then
  PARAMS+=("projectName=$PROJECT_NAME")
fi

if [[ "$DRY_RUN" == true ]]; then
  echo "==> Dry run (what-if)..."
  az deployment sub what-if \
    --subscription "$SUBSCRIPTION" \
    --name "$DEPLOYMENT_NAME" \
    --location "$LOCATION" \
    --template-file "$REPO_ROOT/infra/main.bicep" \
    --parameters "${PARAMS[@]}" \
    --only-show-errors
  echo "==> Dry run complete; nothing was changed."
  exit 0
fi

START_TIME=$(date +%s)
echo "🚀 clock started ($(date '+%H:%M:%S'))"

echo "==> Deploying..."
OUTPUTS="$(az deployment sub create \
  --subscription "$SUBSCRIPTION" \
  --name "$DEPLOYMENT_NAME" \
  --location "$LOCATION" \
  --template-file "$REPO_ROOT/infra/main.bicep" \
  --parameters "${PARAMS[@]}" \
  --only-show-errors \
  --query properties.outputs \
  -o json)"

ENDPOINT="$(echo "$OUTPUTS" | jq -r '.endpoint.value')"
PROJECT_ENDPOINT="$(echo "$OUTPUTS" | jq -r '.projectEndpoint.value')"
ACCOUNT="$(echo "$OUTPUTS" | jq -r '.accountName.value')"
ROUTER="$(echo "$OUTPUTS" | jq -r '.routerDeploymentName.value')"
RG_OUT="$(echo "$OUTPUTS" | jq -r '.resourceGroupName.value')"

# Cost guard. The router's full candidate pool includes Anthropic, xAI,
# DeepSeek and Meta models, which are billed separately from Azure consumption
# rather than against Azure credits. infra/main.bicep pins the subset to OpenAI
# models only; verify that the deployed router actually reflects that, because
# an empty subset silently means "every model this router version supports".
echo "==> Verifying the router routes only to OpenAI models..."
ROUTING="$(az cognitiveservices account deployment show \
  --subscription "$SUBSCRIPTION" \
  --resource-group "$RG_OUT" \
  --name "$ACCOUNT" \
  --deployment-name "$ROUTER" \
  --only-show-errors \
  --query properties.routing -o json)"

SUBSET_COUNT="$(echo "$ROUTING" | jq -r '(.models // []) | length')"
NON_OPENAI="$(echo "$ROUTING" | jq -r '[(.models // [])[] | select(.format != "OpenAI") | .name] | join(", ")')"

if [[ "$SUBSET_COUNT" == "0" ]]; then
  echo "Error: the router has no model subset, so it may route to third-party models" >&2
  echo "       (Anthropic, xAI, DeepSeek, Meta) that bill separately from Azure credits." >&2
  echo "       Set routerModels in infra/main.bicep and redeploy." >&2
  exit 1
fi
if [[ -n "$NON_OPENAI" ]]; then
  echo "Error: the router subset contains non-OpenAI models: $NON_OPENAI" >&2
  echo "       These bill separately from Azure consumption. Fix routerModels and redeploy." >&2
  exit 1
fi
echo "    $SUBSET_COUNT OpenAI models, no third-party models"

DURATION=$(( $(date +%s) - START_TIME ))
echo "🏁 clock stopped (took ${DURATION}s, ended $(date '+%H:%M:%S'))"

cat <<EOF

==> Deployed

    Resource group:    $RG_OUT
    Foundry account:   $ACCOUNT
    Project endpoint:  $PROJECT_ENDPOINT
    Account endpoint:  $ENDPOINT   (the router speaks Chat Completions only here)
    Router deployment: $ROUTER
    Routing mode:      $ROUTING_MODE

Next:

    ./scripts/setup-cli.sh --subscription $SUBSCRIPTION --resource-group $RG_OUT

That installs the qq command and writes your local config. Or configure by hand:

    export QQ_ENDPOINT="$PROJECT_ENDPOINT"
    export QQ_DEPLOYMENT="$ROUTER"

For 'qq --search', also export BRAVE_API_KEY or run: qq config set brave_api_key '<key>'

No API key is printed here on purpose. qq prefers Microsoft Entra ID; run
'az login' and you are done. If you want key auth instead, see the README.
EOF
