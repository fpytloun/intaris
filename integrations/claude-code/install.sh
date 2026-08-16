#!/usr/bin/env bash
# Installs the Intaris Claude Code hooks integration.
#
# Copies scripts/*.sh into ~/.claude/scripts/ and merges hooks.json's hook
# entries into ~/.claude/settings.json — jq-merged so existing settings
# (model, permissions, other hooks) are preserved, not overwritten the way
# `cp hooks.json ~/.claude/settings.json` from the README's old manual
# instructions would. Safe to re-run: any previously-installed intaris
# entries for a given hook event are replaced, not duplicated, and any
# non-intaris hooks for that same event are left alone.
#
# Usage: ./install.sh [--dest DIR]
#   --dest DIR   Install into DIR/scripts and DIR/settings.json instead of
#                ~/.claude (mainly for testing this script itself).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${HOME}/.claude"

while [ $# -gt 0 ]; do
    case "$1" in
        --dest)
            DEST="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required to install this integration (it is also required to run it)." >&2
    echo "Install it: brew install jq (macOS) or apt install jq (Linux)." >&2
    exit 1
fi

SCRIPTS_SRC="${SCRIPT_DIR}/scripts"
HOOKS_JSON="${SCRIPT_DIR}/hooks.json"
SCRIPTS_DEST="${DEST}/scripts"
SETTINGS_DEST="${DEST}/settings.json"

if [ ! -d "$SCRIPTS_SRC" ] || [ ! -f "$HOOKS_JSON" ]; then
    echo "Run this from a checkout of the intaris repo (expected ${SCRIPTS_SRC} and ${HOOKS_JSON})." >&2
    exit 1
fi

echo "Installing scripts to ${SCRIPTS_DEST}"
mkdir -p "$SCRIPTS_DEST"
for f in "${SCRIPTS_SRC}"/intaris-*.sh; do
    cp "$f" "${SCRIPTS_DEST}/$(basename "$f")"
done
chmod +x "${SCRIPTS_DEST}"/intaris-*.sh
# intaris-lib.sh is sourced only, never executed directly — no +x needed,
# but chmod +x on it is harmless, so the loop above covers it too.

echo "Merging hooks into ${SETTINGS_DEST}"
mkdir -p "$DEST"

EXISTING="{}"
if [ -f "$SETTINGS_DEST" ]; then
    if ! EXISTING=$(jq '.' "$SETTINGS_DEST" 2>/dev/null); then
        echo "Existing ${SETTINGS_DEST} is not valid JSON — refusing to touch it." >&2
        echo "Fix or remove it, then re-run this script." >&2
        exit 1
    fi
fi

NEW_HOOKS=$(jq '.hooks' "$HOOKS_JSON")

MERGED=$(jq -n \
    --argjson existing "$EXISTING" \
    --argjson new_hooks "$NEW_HOOKS" \
    '
    def is_intaris_group:
        (.hooks // []) | any(.command // "" | test("intaris-[A-Za-z_-]+\\.sh"));

    ($existing.hooks // {}) as $orig_hooks
    | $new_hooks
    | to_entries
    | reduce .[] as $e ($orig_hooks;
        .[$e.key] = ((.[$e.key] // []) | map(select(is_intaris_group | not))) + $e.value
      ) as $merged_hooks
    | $existing
    | .hooks = $merged_hooks
    '
)

# Write via mktemp + mv, not a direct redirect, so a failure mid-write can
# never leave settings.json truncated.
TMP_SETTINGS=$(mktemp "${SETTINGS_DEST}.XXXXXX")
echo "$MERGED" > "$TMP_SETTINGS"
mv "$TMP_SETTINGS" "$SETTINGS_DEST"

echo "Done."
echo
echo "Set the required environment variables (INTARIS_URL, INTARIS_API_KEY, etc.)"
echo "in your shell profile — see README.md for the full list — then start Claude Code."
echo "Set INTARIS_DEBUG=true for one run to verify hooks are firing (look for [intaris] in stderr)."
