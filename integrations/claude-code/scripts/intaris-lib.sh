#!/usr/bin/env bash
# intaris-lib.sh — Shared library for Intaris Claude Code hooks.
#
# Source this file at the top of each hook script:
#   . "$(dirname "$0")/intaris-lib.sh"
#
# Provides:
#   - Environment variable defaults
#   - Logging (log, log_error)
#   - jq guard (require_jq)
#   - Session ID validation (validate_session_id)
#   - HTTP header construction (build_headers)
#   - State file locking (acquire_lock, release_lock)
#   - Atomic state file writes (write_state)
#   - State file directory (state_dir, state_file_for)

# -- Environment Variables ---------------------------------------------------

INTARIS_URL="${INTARIS_URL:-http://localhost:8060}"
INTARIS_API_KEY="${INTARIS_API_KEY:-}"
INTARIS_AGENT_ID="${INTARIS_AGENT_ID:-claude-code}"
INTARIS_USER_ID="${INTARIS_USER_ID:-}"
INTARIS_FAIL_OPEN="${INTARIS_FAIL_OPEN:-false}"
INTARIS_INTENTION="${INTARIS_INTENTION:-}"
INTARIS_ALLOW_PATHS="${INTARIS_ALLOW_PATHS:-}"
INTARIS_CHECKPOINT_INTERVAL="${INTARIS_CHECKPOINT_INTERVAL:-25}"
INTARIS_ESCALATION_TIMEOUT="${INTARIS_ESCALATION_TIMEOUT:-55}"
INTARIS_SESSION_RECORDING="${INTARIS_SESSION_RECORDING:-false}"
INTARIS_DEBUG="${INTARIS_DEBUG:-false}"

# -- Logging -----------------------------------------------------------------

log() {
    if [ "$INTARIS_DEBUG" = "true" ]; then
        echo "[intaris] $*" >&2
    fi
}

# -- Guards ------------------------------------------------------------------

# Check that jq is available. If not, output empty JSON and exit.
require_jq() {
    if ! command -v jq >/dev/null 2>&1; then
        echo '{}'
        exit 0
    fi
}

# Validate session ID format to prevent path traversal in state file paths.
# Returns 0 if valid, 1 if invalid.
validate_session_id() {
    local sid="$1"
    if [ -z "$sid" ]; then
        return 1
    fi
    if [[ "$sid" =~ [/\\] ]] || [[ "$sid" == *".."* ]]; then
        log "Invalid session_id format: $sid"
        return 1
    fi
    return 0
}

# -- HTTP Headers ------------------------------------------------------------

# Build common curl headers array. Sets the global HEADERS variable.
# Usage: build_headers; curl "${HEADERS[@]}" ...
build_headers() {
    HEADERS=(-H "Content-Type: application/json" -H "X-Agent-Id: $INTARIS_AGENT_ID")
    if [ -n "$INTARIS_API_KEY" ]; then
        HEADERS+=(-H "Authorization: Bearer $INTARIS_API_KEY")
    fi
    if [ -n "$INTARIS_USER_ID" ]; then
        HEADERS+=(-H "X-User-Id: $INTARIS_USER_ID")
    fi
}

# -- State File Management ---------------------------------------------------

# Use per-user temp directory for state files (more secure than /tmp).
# Falls back to /tmp if TMPDIR is not set.
INTARIS_STATE_DIR="${TMPDIR:-/tmp}"

# Get the state file path for a given Claude Code session ID.
# Usage: state_file_for "session-id"
state_file_for() {
    echo "${INTARIS_STATE_DIR}/intaris_state_${1}.json"
}

# Get the state file path for a subagent.
# Usage: state_file_for_subagent "session-id" "agent-id"
state_file_for_subagent() {
    echo "${INTARIS_STATE_DIR}/intaris_state_${1}_${2}.json"
}

# -- File Locking ------------------------------------------------------------
#
# Uses mkdir-based locking (atomic on POSIX, works everywhere).
# Lock files are cleaned up via trap on EXIT.

# Track active lock dirs for cleanup
_INTARIS_ACTIVE_LOCKDIRS=()

_cleanup_locks() {
    for lockdir in ${_INTARIS_ACTIVE_LOCKDIRS[@]+"${_INTARIS_ACTIVE_LOCKDIRS[@]}"}; do
        rmdir "$lockdir" 2>/dev/null || true
    done
}
trap _cleanup_locks EXIT

# Acquire a lock on a state file. Blocks up to 2 seconds.
# Usage: acquire_lock "/path/to/state.json"
# Returns 0 on success, 1 on failure.
acquire_lock() {
    local lockdir="$1.lock.d"
    local attempts=0
    while ! mkdir "$lockdir" 2>/dev/null; do
        attempts=$((attempts + 1))
        if [ $attempts -ge 20 ]; then
            # Check for stale lock (older than 30s)
            if [ -d "$lockdir" ]; then
                local lock_age
                lock_age=$(( $(date +%s) - $(stat -f %m "$lockdir" 2>/dev/null || stat -c %Y "$lockdir" 2>/dev/null || echo "0") ))
                if [ "$lock_age" -gt 30 ]; then
                    log "Removing stale lock: $lockdir (age: ${lock_age}s)"
                    rmdir "$lockdir" 2>/dev/null || true
                    continue
                fi
            fi
            return 1
        fi
        sleep 0.1
    done
    _INTARIS_ACTIVE_LOCKDIRS+=("$lockdir")
    return 0
}

# Release a lock on a state file.
# Usage: release_lock "/path/to/state.json"
release_lock() {
    local lockdir="$1.lock.d"
    rmdir "$lockdir" 2>/dev/null || true
}

# -- Atomic State File Writes ------------------------------------------------

# Write content to a state file atomically (write to .tmp, then mv).
# Usage: write_state "/path/to/state.json" "$json_content"
write_state() {
    local file="$1"
    local content="$2"
    echo "$content" > "${file}.tmp"
    chmod 600 "${file}.tmp"
    mv "${file}.tmp" "$file"
}

# -- Allow Paths Policy ------------------------------------------------------

# Build allow_paths policy JSON from INTARIS_ALLOW_PATHS env var.
# Returns "null" if no paths configured, or a JSON object with allow_paths array.
# Usage: POLICY_JSON=$(build_allow_paths_policy)
build_allow_paths_policy() {
    if [ -z "$INTARIS_ALLOW_PATHS" ]; then
        echo "null"
        return
    fi

    local patterns="[]"
    local IFS=','
    # shellcheck disable=SC2086
    set -- $INTARIS_ALLOW_PATHS
    for ap_entry in "$@"; do
        ap_entry=$(echo "$ap_entry" | xargs)  # trim whitespace
        [ -z "$ap_entry" ] && continue
        # Expand ~ to home directory
        if [[ "$ap_entry" == "~/"* ]] || [[ "$ap_entry" == "~" ]]; then
            ap_entry="${HOME}${ap_entry:1}"
        fi
        # Ensure trailing /* for glob matching
        if [[ "$ap_entry" != *"*" ]]; then
            if [[ "$ap_entry" == */ ]]; then
                ap_entry="${ap_entry}*"
            else
                ap_entry="${ap_entry}/*"
            fi
        fi
        patterns=$(echo "$patterns" | jq --arg pat "$ap_entry" '. + [$pat]')
    done

    if [ "$(echo "$patterns" | jq 'length')" -gt 0 ]; then
        jq -n --argjson ap "$patterns" '{"allow_paths": $ap}'
    else
        echo "null"
    fi
}

# -- Output Helpers ----------------------------------------------------------

# Output a PreToolUse deny decision in the non-deprecated hookSpecificOutput format.
# Usage: deny_tool "reason text"
deny_tool() {
    local reason="$1"
    jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: $reason
        }
    }'
}

# Output a PreToolUse allow decision (empty JSON = allow).
allow_tool() {
    echo '{}'
}
