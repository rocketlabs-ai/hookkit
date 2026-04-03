#!/usr/bin/env bash
# install.sh — HookKit installer
#
# Copies hooks to ~/.claude/hookkit/ and prints the settings.json snippet
# you need to add to .claude/settings.json.
#
# Usage:
#   bash install.sh              # install all hooks
#   bash install.sh --list       # list what would be installed
#   bash install.sh --check      # verify Python 3 is available

set -e

HOOKKIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_SRC="$HOOKKIT_DIR/hooks"
HOOKS_DEST="$HOME/.claude/hookkit"
SETTINGS_FILE="$HOME/.claude/settings.json"

# --- Colors (if terminal supports it) ---
if [ -t 1 ]; then
  BOLD="\033[1m"
  GREEN="\033[32m"
  YELLOW="\033[33m"
  RED="\033[31m"
  RESET="\033[0m"
else
  BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

log()  { echo -e "${GREEN}✓${RESET} $*"; }
warn() { echo -e "${YELLOW}!${RESET} $*"; }
err()  { echo -e "${RED}✗${RESET} $*" >&2; }

# --- Flags ---
LIST_ONLY=false
CHECK_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --list)  LIST_ONLY=true ;;
    --check) CHECK_ONLY=true ;;
  esac
done

# --- Check Python ---
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
  err "Python 3 not found. HookKit requires Python 3.8+."
  err "Install Python from https://python.org and try again."
  exit 1
fi

PYTHON=$(command -v python3 2>/dev/null || command -v python)
PY_VERSION=$("$PYTHON" --version 2>&1)
log "Found: $PY_VERSION ($PYTHON)"

if [ "$CHECK_ONLY" = true ]; then
  log "Python check passed. Run without --check to install."
  exit 0
fi

# --- List mode ---
HOOKS=(
  "cost-tracker.py     PostToolUse  Estimates API cost per tool call, fires budget warnings"
  "loop-detector.py    PostToolUse  Detects agent loops (tool repetition, errors, stalls)"
  "context-monitor.py  PostToolUse  Warns when context window fills (50%%, 65%%, 75%%)"
  "glassworm-scanner.py PostToolUse Scans npm/pip installs for invisible Unicode malware"
  "outbound-gate.py    PreToolUse   Blocks git push, package installs, and outbound fetches"
  "session-snapshot.py Stop         Captures session state snapshot on exit"
)

if [ "$LIST_ONLY" = true ]; then
  echo -e "\n${BOLD}HookKit hooks:${RESET}\n"
  for h in "${HOOKS[@]}"; do
    name=$(echo "$h" | awk '{print $1}')
    type=$(echo "$h" | awk '{print $2}')
    desc=$(echo "$h" | cut -d' ' -f3-)
    printf "  %-28s %-14s %s\n" "$name" "[$type]" "$desc"
  done
  echo ""
  exit 0
fi

# --- Install ---
echo -e "\n${BOLD}Installing HookKit...${RESET}\n"

mkdir -p "$HOOKS_DEST"

for hook_file in "$HOOKS_SRC"/*.py; do
  name=$(basename "$hook_file")
  dest="$HOOKS_DEST/$name"
  cp "$hook_file" "$dest"
  chmod +x "$dest"
  log "Installed: $dest"
done

echo ""
echo -e "${BOLD}Installation complete.${RESET}"
echo ""
echo -e "${YELLOW}Next step:${RESET} Add hooks to your .claude/settings.json"
echo ""
echo "Open: $HOOKKIT_DIR/examples/settings-snippet.json"
echo "Then merge the \"hooks\" section into: $SETTINGS_FILE"
echo ""
echo "Replace '/path/to/hookkit' with: $HOOKS_DEST"
echo ""
echo -e "${BOLD}Hook path to use in settings.json:${RESET}"
echo ""

for hook_file in "$HOOKS_DEST"/*.py; do
  name=$(basename "$hook_file" .py)
  echo "  $PYTHON $hook_file"
done

echo ""
echo "See README.md for the full settings.json snippet and configuration options."
echo ""
