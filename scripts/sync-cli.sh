#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# sync-cli.sh — Reinstall the qq command from this working tree
#
# Usage:
#   ./scripts/sync-cli.sh
#   ./scripts/sync-cli.sh --dry-run
#   ./scripts/sync-cli.sh --tool pipx
#   ./scripts/sync-cli.sh --verbose
#
# Lifecycle: routine — safe to re-run; use --dry-run to preview.
#
# The qq on your PATH is a snapshot: uv and pipx copy the source at install
# time, so edits here — a version bump included — do not reach it until it is
# reinstalled. This does that one thing: it never calls Azure and never touches
# the config file, so the endpoint, tenant and auth mode survive untouched.
# Use setup-cli.sh instead when those need to be (re)discovered.
#
# The installer runs quiet: it relists all 27 dependencies on every run even
# when it rebuilt one package, and this is a script you run after every edit.
# --verbose gives that output back when an install needs debugging.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib/clock.sh
source "$SCRIPT_DIR/lib/clock.sh"

TOOL=""
DRY_RUN=false
VERBOSE=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Reinstall qq from this working tree so the command on your PATH matches the code.
Lifecycle: routine — safe to re-run; use --dry-run to preview.

Options:
  --tool <uv|pipx>    Force the installer (default: uv if present, else pipx)
  --dry-run           Print the install command without running it
  -v, --verbose       Show the installer's own output (default: quiet)
  -h, --help          Show this help

Examples:
  $(basename "$0")
  $(basename "$0") --dry-run
  $(basename "$0") --verbose
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help) usage ;;
    --tool)         TOOL="${2:?--tool needs a value}"; shift 2 ;;
    --dry-run)      DRY_RUN=true; shift ;;
    -v|--verbose)   VERBOSE=true; shift ;;
    *)              echo "Error: unknown option '$1'" >&2; usage 1 ;;
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

# Assembled once and used for both --dry-run and the real run, so what the dry
# run prints is by construction what executes. Both installers spell it -q.
if [[ "$TOOL" == "uv" ]]; then
  INSTALL_CMD=(uv tool install)
  [[ "$VERBOSE" == true ]] || INSTALL_CMD+=(-q)
  INSTALL_CMD+=(--force --from "$REPO_ROOT" qq-cli)
else
  INSTALL_CMD=(pipx install)
  [[ "$VERBOSE" == true ]] || INSTALL_CMD+=(-q)
  INSTALL_CMD+=(--force "$REPO_ROOT")
fi

# --- what is here, and what is installed -----------------------------------

VERSION_FILE="$REPO_ROOT/src/qq/__init__.py"
[[ -f "$VERSION_FILE" ]] || { echo "Error: $VERSION_FILE is missing; is this the qq-router repo?" >&2; exit 1; }

# The single source of truth: pyproject declares the version dynamic and points
# hatch at this line, so reading it here is reading what will be installed.
REPO_VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$VERSION_FILE")"
[[ -n "$REPO_VERSION" ]] || { echo "Error: could not read __version__ from $VERSION_FILE." >&2; exit 1; }

qq_version_of() {  # <path-to-qq> -> "0.2.1", or empty if it will not run
  [[ -x "$1" ]] || return 0
  "$1" --version 2>/dev/null | awk 'NR == 1 { print $NF }'
}

BEFORE="$(qq_version_of "$(command -v qq || true)")"
echo "==> Working tree is $REPO_VERSION; installed qq is ${BEFORE:-not installed}"

if [[ -n "$BEFORE" && "$BEFORE" == "$REPO_VERSION" ]]; then
  echo "    Versions already match. Reinstalling anyway — the version only moves on a"
  echo "    release, so same-version edits are the common case."
fi

if [[ "$DRY_RUN" == true ]]; then
  echo "==> Dry run; would install $REPO_VERSION with $TOOL:"
  echo "    ${INSTALL_CMD[*]}"
  exit 0
fi

# --- install ---------------------------------------------------------------

START_TIME=$(date +%s)
echo "🚀 clock started ($(date '+%H:%M:%S'))"

echo "==> Reinstalling qq with $TOOL..."
"${INSTALL_CMD[@]}"

if [[ "$TOOL" == "uv" ]]; then
  BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
else
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

DURATION=$(( $(date +%s) - START_TIME ))
echo "🏁 clock stopped (took $(format_duration "$DURATION"), ended $(date '+%H:%M:%S'))"

# --- confirm the command on the PATH is the one just installed -------------

AFTER="$(qq_version_of "$QQ_BIN")"
echo
echo "==> Installed: $QQ_BIN ($AFTER)"

if [[ "$AFTER" != "$REPO_VERSION" ]]; then
  echo "    warning: it reports $AFTER but this tree is $REPO_VERSION." >&2
  echo "             The install did not take; check the output above." >&2
  exit 1
fi

ON_PATH="$(command -v qq || true)"
if [[ -z "$ON_PATH" ]]; then
  echo
  echo "    note: $BIN_DIR is not on your PATH, so 'qq' will not resolve in a new shell."
  echo "          Add it yourself, or run: ./scripts/setup-cli.sh --update-shell"
elif [[ "$ON_PATH" != "$QQ_BIN" ]]; then
  # The reinstall is real but invisible: another qq — a repo .venv, usually —
  # sits earlier on the PATH, and that is the one 'qq' still runs.
  SHADOW="$(qq_version_of "$ON_PATH")"
  echo
  echo "    warning: 'qq' still resolves to $ON_PATH (${SHADOW:-unknown})," >&2
  echo "             which shadows the copy just installed. Reorder your PATH," >&2
  echo "             or call $QQ_BIN directly." >&2
fi
