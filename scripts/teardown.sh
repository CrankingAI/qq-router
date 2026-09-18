#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# teardown.sh — Delete the Azure resources qq created
#
# Usage:
#   ./scripts/teardown.sh --env dev
#   ./scripts/teardown.sh --resource-group rg-qq-dev --yes
#
# Lifecycle: destructive — deletes the whole resource group, including the
# Foundry account and the model-router deployment; requires --yes or an
# interactive confirmation.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib/clock.sh
source "$SCRIPT_DIR/lib/clock.sh"

ENV_NAME="dev"
NAME_PREFIX="qq"
RESOURCE_GROUP=""
SUBSCRIPTION=""
YES=false
PURGE=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Delete the resource group that ./scripts/deploy.sh created.
Lifecycle: destructive — deletes the Foundry account and model-router deployment;
requires --yes or an interactive confirmation.

Options:
  --env <name>             Environment suffix; implies rg-<prefix>-<env> (default: $ENV_NAME)
  --name-prefix <prefix>   Resource name prefix (default: $NAME_PREFIX)
  --resource-group <name>  Delete this resource group instead of the derived name
  --subscription <id>      Azure subscription (default: the current az subscription)
  --purge                  Also purge the soft-deleted Foundry account
  --yes                    Skip the interactive confirmation
  -h, --help               Show this help

Examples:
  $(basename "$0") --env dev
  $(basename "$0") --resource-group rg-qq-dev --yes --purge
EOF
  exit "${1:-0}"
}

case "${1:-}" in
  -h|--help|help) usage ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help)   usage ;;
    --env)            ENV_NAME="${2:?--env needs a value}"; shift 2 ;;
    --name-prefix)    NAME_PREFIX="${2:?--name-prefix needs a value}"; shift 2 ;;
    --resource-group) RESOURCE_GROUP="${2:?--resource-group needs a value}"; shift 2 ;;
    --subscription)   SUBSCRIPTION="${2:?--subscription needs a value}"; shift 2 ;;
    --purge)          PURGE=true; shift ;;
    --yes)            YES=true; shift ;;
    *)                echo "Error: unknown option '$1'" >&2; usage 1 ;;
  esac
done

need() {
  command -v "$1" >/dev/null 2>&1 && return 0
  echo "Error: '$1' is not installed." >&2
  echo "  $2" >&2
  exit 1
}
need az "brew install azure-cli"

[[ -n "$RESOURCE_GROUP" ]] || RESOURCE_GROUP="rg-${NAME_PREFIX}-${ENV_NAME}"

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

if ! az group show --subscription "$SUBSCRIPTION" --name "$RESOURCE_GROUP" --output none 2>/dev/null; then
  echo "Resource group '$RESOURCE_GROUP' does not exist; nothing to do."
  exit 0
fi

echo "==> About to delete"
az resource list \
  --subscription "$SUBSCRIPTION" \
  --resource-group "$RESOURCE_GROUP" \
  --only-show-errors \
  --query "[].{name:name,type:type}" -o table

ACCOUNTS="$(az cognitiveservices account list \
  --subscription "$SUBSCRIPTION" \
  --resource-group "$RESOURCE_GROUP" \
  --only-show-errors \
  --query "[].{n:name,l:location}" -o tsv || true)"

if [[ "$YES" != true ]]; then
  read -r -p "This will DELETE resource group '$RESOURCE_GROUP'. Type the name to confirm: " reply
  [[ "$reply" == "$RESOURCE_GROUP" ]] || { echo "Aborted; nothing changed."; exit 1; }
fi

START_TIME=$(date +%s)
echo "🚀 clock started ($(date '+%H:%M:%S'))"

echo "==> Deleting resource group $RESOURCE_GROUP..."
az group delete \
  --subscription "$SUBSCRIPTION" \
  --name "$RESOURCE_GROUP" \
  --yes \
  --only-show-errors \
  --output none

if [[ "$PURGE" == true && -n "$ACCOUNTS" ]]; then
  echo "==> Purging soft-deleted Foundry accounts..."
  while IFS=$'\t' read -r acct loc; do
    [[ -n "$acct" ]] || continue
    echo "    $acct ($loc)"
    az cognitiveservices account purge \
      --subscription "$SUBSCRIPTION" \
      --name "$acct" \
      --resource-group "$RESOURCE_GROUP" \
      --location "$loc" \
      --only-show-errors 2>/dev/null || echo "    (purge failed or already purged)"
  done <<< "$ACCOUNTS"
fi

DURATION=$(( $(date +%s) - START_TIME ))
echo "🏁 clock stopped (took $(format_duration "$DURATION"), ended $(date '+%H:%M:%S'))"

cat <<EOF

==> Azure resources deleted

To remove the local command as well:

    uv tool uninstall qq-cli     # or: pipx uninstall qq-cli
    rm -rf "\$(qq config path 2>/dev/null | xargs dirname)" 2>/dev/null || true

EOF
