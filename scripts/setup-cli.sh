#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# setup-cli.sh — Install the qq command locally and point it at your deployment
#
# Usage:
#   ./scripts/setup-cli.sh
#   ./scripts/setup-cli.sh --resource-group rg-qq-dev
#   ./scripts/setup-cli.sh --endpoint https://x.openai.azure.com --deployment qq-router
#
# Lifecycle: rare — run once per machine; idempotent, safe to re-run.
#
# Installs qq as an isolated Python tool (uv preferred, pipx as a fallback),
# discovers the Foundry project endpoint from Azure when it can (the account
# endpoint is the fallback), and writes the local config file with owner-only
# permissions. It never touches your shell rc files unless you pass
# --update-shell.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib/clock.sh
source "$SCRIPT_DIR/lib/clock.sh"

# Management API version for Cognitive Services; matches infra/modules/ai.bicep.
CS_API_VERSION="2026-05-01"

SUBSCRIPTION=""
RESOURCE_GROUP=""
ENDPOINT=""
DEPLOYMENT=""
PROJECT_NAME=""
USE_PROJECT=true
TOOL=""
USE_API_KEY=false
UPDATE_SHELL=false
SKIP_CONFIG=false
FORCE=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Install the qq command and write its local configuration.
Lifecycle: rare — run once per machine; idempotent, safe to re-run.

Options:
  --subscription <id>      Azure subscription (default: the current az subscription)
  --resource-group <name>  Resource group holding the Foundry account (default: discover)
  --endpoint <url>         Set the endpoint (project or account) and skip Azure discovery
  --deployment <name>      Router deployment name (default: discover, else qq-router)
  --project <name>         Foundry project to use (default: the first one on the account)
  --no-project             Use the account endpoint instead; Chat Completions only, no --search
  --use-api-key            Fetch an API key from Azure and store it (default: Entra ID)
  --tool <uv|pipx>         Force the installer (default: uv if present, else pipx)
  --update-shell           Let the installer add its bin directory to your PATH
  --no-config              Install only; do not write the config file
  --force                  Reinstall even if qq is already installed
  -h, --help               Show this help

Environment variables:
  AZURE_SUBSCRIPTION_ID    Used when --subscription is not given and no CLI default is set

Examples:
  $(basename "$0")
  $(basename "$0") --resource-group rg-qq-dev
  $(basename "$0") --endpoint https://qq-dev-abc.openai.azure.com --no-config
EOF
  exit "${1:-0}"
}

case "${1:-}" in
  -h|--help|help) usage ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help)   usage ;;
    --subscription)   SUBSCRIPTION="${2:?--subscription needs a value}"; shift 2 ;;
    --resource-group) RESOURCE_GROUP="${2:?--resource-group needs a value}"; shift 2 ;;
    --endpoint)       ENDPOINT="${2:?--endpoint needs a value}"; shift 2 ;;
    --deployment)     DEPLOYMENT="${2:?--deployment needs a value}"; shift 2 ;;
    --project)        PROJECT_NAME="${2:?--project needs a value}"; shift 2 ;;
    --no-project)     USE_PROJECT=false; shift ;;
    --tool)           TOOL="${2:?--tool needs a value}"; shift 2 ;;
    --use-api-key)    USE_API_KEY=true; shift ;;
    --update-shell)   UPDATE_SHELL=true; shift ;;
    --no-config)      SKIP_CONFIG=true; shift ;;
    --force)          FORCE=true; shift ;;
    *)                echo "Error: unknown option '$1'" >&2; usage 1 ;;
  esac
done

# --- pick an installer -----------------------------------------------------

if [[ -z "$TOOL" ]]; then
  if command -v uv >/dev/null 2>&1; then
    TOOL="uv"
  elif command -v pipx >/dev/null 2>&1; then
    TOOL="pipx"
  else
    echo "Error: neither 'uv' nor 'pipx' is installed." >&2
    echo "  brew install uv        # recommended" >&2
    echo "  brew install pipx" >&2
    exit 1
  fi
fi

case "$TOOL" in
  uv)   command -v uv   >/dev/null 2>&1 || { echo "Error: uv is not installed."   >&2; echo "  brew install uv"   >&2; exit 1; } ;;
  pipx) command -v pipx >/dev/null 2>&1 || { echo "Error: pipx is not installed." >&2; echo "  brew install pipx" >&2; exit 1; } ;;
  *)    echo "Error: --tool must be 'uv' or 'pipx' (got '$TOOL')." >&2; exit 1 ;;
esac

if command -v qq >/dev/null 2>&1 && [[ "$FORCE" != true ]]; then
  echo "==> qq is already installed at $(command -v qq); reinstalling to pick up changes."
fi

START_TIME=$(date +%s)
echo "🚀 clock started ($(date '+%H:%M:%S'))"

# --- discover the endpoint from Azure --------------------------------------

if [[ -z "$ENDPOINT" ]]; then
  if ! command -v az >/dev/null 2>&1; then
    echo "Error: no --endpoint given and the Azure CLI is not installed." >&2
    echo "  Pass --endpoint https://<resource>.openai.azure.com, or: brew install azure-cli" >&2
    exit 1
  fi
  command -v jq >/dev/null 2>&1 || { echo "Error: 'jq' is not installed." >&2; echo "  brew install jq" >&2; exit 1; }

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

  echo "==> Looking for the qq Foundry account..."
  if [[ -n "$RESOURCE_GROUP" ]]; then
    ACCOUNTS="$(az cognitiveservices account list \
      --subscription "$SUBSCRIPTION" \
      --resource-group "$RESOURCE_GROUP" \
      --only-show-errors -o json)"
  else
    ACCOUNTS="$(az cognitiveservices account list \
      --subscription "$SUBSCRIPTION" \
      --only-show-errors -o json)"
  fi

  ACCOUNT_LINE="$(echo "$ACCOUNTS" | jq -r '
    [ .[]
      | select(.kind == "AIServices")
      | select((.tags.application // "") == "qq") ]
    | first
    | if . == null then empty else "\(.name)\t\(.resourceGroup)" end')"

  if [[ -z "$ACCOUNT_LINE" ]]; then
    echo "Error: could not find a Foundry account tagged application=qq." >&2
    echo "  Run ./scripts/deploy.sh first, or pass --endpoint and --deployment by hand." >&2
    exit 1
  fi

  ACCOUNT_NAME="$(echo "$ACCOUNT_LINE" | cut -f1)"
  RESOURCE_GROUP="$(echo "$ACCOUNT_LINE" | cut -f2)"
  echo "    Found $ACCOUNT_NAME in $RESOURCE_GROUP"

  ACCOUNT_JSON="$(az cognitiveservices account show \
    --subscription "$SUBSCRIPTION" \
    --name "$ACCOUNT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --only-show-errors -o json)"

  ACCOUNT_ENDPOINT="$(echo "$ACCOUNT_JSON" | jq -r '
    .properties.endpoints["OpenAI Language Model Instance API"]
    // .properties.endpoint')"
  ACCOUNT_ID="$(echo "$ACCOUNT_JSON" | jq -r '.id')"

  # Prefer the project endpoint. model-router only accepts the Responses API
  # through a project, and that is what 'qq --search' needs. The Azure CLI has
  # no 'account project' command, so ask the management API directly, with the
  # same API version the Bicep uses.
  if [[ "$USE_PROJECT" == true ]]; then
    PROJECTS_JSON="$(az rest \
      --subscription "$SUBSCRIPTION" \
      --method get \
      --url "https://management.azure.com${ACCOUNT_ID}/projects?api-version=${CS_API_VERSION}" \
      --only-show-errors -o json)"
    ENDPOINT="$(echo "$PROJECTS_JSON" | jq -r --arg want "$PROJECT_NAME" '
      [ .value[]
        | select($want == "" or (.name | split("/") | last) == $want)
        | .properties.endpoints["AI Foundry API"] // empty ]
      | first // empty')"
    if [[ -n "$ENDPOINT" ]]; then
      echo "    Project endpoint: $ENDPOINT"
    elif [[ -n "$PROJECT_NAME" ]]; then
      echo "Error: no Foundry project named '$PROJECT_NAME' on $ACCOUNT_NAME." >&2
      echo "  Run ./scripts/deploy.sh --project $PROJECT_NAME, or drop --project." >&2
      exit 1
    else
      echo "    note: no Foundry project on this account; using the account endpoint."
      echo "          The router speaks Chat Completions only there, so 'qq --search'"
      echo "          will not work. Re-run ./scripts/deploy.sh to create the project."
    fi
  fi
  if [[ -z "$ENDPOINT" ]]; then
    ENDPOINT="$ACCOUNT_ENDPOINT"
    echo "    Account endpoint: $ENDPOINT"
  fi

  # Pin the Entra tenant. Without it, DefaultAzureCredential asks the Azure CLI
  # for a token using the CLI's current subscription, which is global state that
  # any other shell can change. If that points at a different tenant, Azure
  # rejects the call even though the user is logged in.
  TENANT_ID="$(az account show --subscription "$SUBSCRIPTION" --query tenantId -o tsv)"

  DEPLOYMENTS_JSON="$(az cognitiveservices account deployment list \
    --subscription "$SUBSCRIPTION" \
    --name "$ACCOUNT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --only-show-errors -o json)"

  if [[ -z "$DEPLOYMENT" ]]; then
    DEPLOYMENT="$(echo "$DEPLOYMENTS_JSON" \
      | jq -r '[ .[] | select(.properties.model.name == "model-router") | .name ] | first // empty')"
  fi

  # Record what the deployment is backed by. The inference API does not report
  # this, so 'qq -vv' can only show it if we capture it here.
  ROUTER_DESC="$(echo "$DEPLOYMENTS_JSON" | jq -r --arg d "$DEPLOYMENT" '
    [ .[] | select(.name == $d) | "\(.properties.model.name):\(.properties.model.version)" ]
    | first // empty')"
fi

[[ -n "$DEPLOYMENT" ]] || DEPLOYMENT="qq-router"

# --- install ---------------------------------------------------------------

echo "==> Installing qq with $TOOL..."
if [[ "$TOOL" == "uv" ]]; then
  uv tool install --force "$REPO_ROOT"
  BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
else
  pipx install --force "$REPO_ROOT"
  BIN_DIR="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
fi

QQ_BIN="$BIN_DIR/qq"
if [[ ! -x "$QQ_BIN" ]]; then
  QQ_BIN="$(command -v qq || true)"
fi
if [[ -z "$QQ_BIN" ]]; then
  echo "Error: qq was installed but the executable could not be located." >&2
  exit 1
fi
echo "    Installed: $QQ_BIN"

if [[ "$UPDATE_SHELL" == true ]]; then
  echo "==> Adding the tool bin directory to your PATH..."
  if [[ "$TOOL" == "uv" ]]; then
    uv tool update-shell
  else
    pipx ensurepath
  fi
fi

# --- configure -------------------------------------------------------------

if [[ "$SKIP_CONFIG" != true ]]; then
  echo "==> Writing local configuration..."
  "$QQ_BIN" config set endpoint "$ENDPOINT" >/dev/null
  "$QQ_BIN" config set deployment "$DEPLOYMENT" >/dev/null
  if [[ -n "${TENANT_ID:-}" ]]; then
    "$QQ_BIN" config set tenant "$TENANT_ID" >/dev/null
    echo "    Entra tenant: $TENANT_ID"
  fi
  # The subscription pins which Azure CLI account qq asks for a token. Without
  # it, a tenant pin alone still resolves against the CLI's current default
  # account, and that default can drift to an unrelated directory.
  if [[ -n "${SUBSCRIPTION:-}" ]]; then
    "$QQ_BIN" config set subscription "$SUBSCRIPTION" >/dev/null
    echo "    Subscription: $SUBSCRIPTION"
  fi
  if [[ -n "${ROUTER_DESC:-}" ]]; then
    "$QQ_BIN" config set router "$ROUTER_DESC" >/dev/null
    echo "    Backed by: $ROUTER_DESC"
  fi

  if [[ "$USE_API_KEY" == true ]]; then
    if [[ -z "${ACCOUNT_NAME:-}" ]]; then
      echo "Error: --use-api-key needs Azure discovery; drop --endpoint or set the key by hand." >&2
      echo "  qq config set api_key '<key>'" >&2
      exit 1
    fi
    echo "    Fetching an API key (it is written to the config file, never printed)..."
    KEY="$(az cognitiveservices account keys list \
      --subscription "$SUBSCRIPTION" \
      --name "$ACCOUNT_NAME" \
      --resource-group "$RESOURCE_GROUP" \
      --only-show-errors \
      --query key1 -o tsv)"
    "$QQ_BIN" config set api_key "$KEY" >/dev/null
    "$QQ_BIN" config set auth key >/dev/null
    unset KEY
    echo "    Auth mode: API key"
  else
    "$QQ_BIN" config set auth entra >/dev/null
    echo "    Auth mode: Microsoft Entra ID (passwordless)"
  fi

  echo "    Config file: $("$QQ_BIN" config path)"

  # QQ_ENDPOINT outranks the config file, on purpose. One exported in this
  # shell that points elsewhere means the config just written will not be used
  # until it is unset. (The generic AZURE_OPENAI_ENDPOINT sits below the file,
  # so it cannot cause this.)
  if [[ -n "${QQ_ENDPOINT:-}" && "${QQ_ENDPOINT%/}" != "${ENDPOINT%/}" ]]; then
    echo
    echo "    warning: QQ_ENDPOINT is set in this shell and points elsewhere:"
    echo "             $QQ_ENDPOINT"
    echo "             It overrides the config file just written. Unset it to use the config."
  fi
fi

DURATION=$(( $(date +%s) - START_TIME ))
echo "🏁 clock stopped (took $(format_duration "$DURATION"), ended $(date '+%H:%M:%S'))"

echo
echo "==> Checking the installation..."
"$QQ_BIN" doctor || true

cat <<EOF

==> Done

    Command:    $QQ_BIN
    Endpoint:   $ENDPOINT
    Deployment: $DEPLOYMENT

Try it:

    qq what is the gh cli command to list all repos

For web search, add a Brave Search API key and ask with --search:

    export BRAVE_API_KEY='<key>'          # or: qq config set brave_api_key '<key>'
    qq --search what is the newest stable Python release

EOF

if ! command -v qq >/dev/null 2>&1; then
  cat <<EOF
Note: $BIN_DIR is not on your PATH, so 'qq' will not resolve in a new shell.
Add it yourself, or re-run with --update-shell:

    ./scripts/setup-cli.sh --update-shell

EOF
fi
