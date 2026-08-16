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
# Should match the PreToolUse hook's configured timeout in hooks.json
# (seconds, not milliseconds — see hooks.json). Used to derive an internal
# safety margin so intaris-evaluate.sh exits with a clean deny before
# Claude Code kills the hook outright.
INTARIS_HOOK_TIMEOUT="${INTARIS_HOOK_TIMEOUT:-60}"
INTARIS_ESCALATION_TIMEOUT="${INTARIS_ESCALATION_TIMEOUT:-55}"
INTARIS_SESSION_RECORDING="${INTARIS_SESSION_RECORDING:-false}"
INTARIS_DEBUG="${INTARIS_DEBUG:-false}"

# Set by allow_tool/deny_tool/deny_tool_raw once a PreToolUse decision has
# been emitted. Consulted by intaris-evaluate.sh's fail-closed exit trap so
# an unexpected crash (missing jq, corrupt state, etc.) cannot silently
# allow a tool call by exiting before a decision was printed.
DECISION_EMITTED=0

# -- Logging -----------------------------------------------------------------

log() {
    if [ "$INTARIS_DEBUG" = "true" ]; then
        echo "[intaris] $*" >&2
    fi
}

# -- Guards ------------------------------------------------------------------

# Check that jq is available.
require_jq() {
    if ! command -v jq >/dev/null 2>&1; then
        return 1
    fi
    return 0
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

# -- Exit Hooks ---------------------------------------------------------------
#
# A single EXIT trap dispatches to a registry so multiple concerns (lock
# cleanup, fail-closed enforcement) can each register independently without
# clobbering each other's trap. Lock cleanup always runs first so a
# fail-closed hook that calls `exit` from inside the trap doesn't skip it.
# Usage: register_exit_hook "function_name"

_INTARIS_EXIT_HOOKS=()

register_exit_hook() {
    _INTARIS_EXIT_HOOKS+=("$1")
}

_run_exit_hooks() {
    _cleanup_locks
    local hook
    for hook in ${_INTARIS_EXIT_HOOKS[@]+"${_INTARIS_EXIT_HOOKS[@]}"}; do
        "$hook"
    done
}
trap _run_exit_hooks EXIT

# -- File Locking ------------------------------------------------------------
#
# Uses mkdir-based locking (atomic on POSIX, works everywhere).
# Lock files are cleaned up via the shared exit-hook trap above.

# Track active lock dirs for cleanup
_INTARIS_ACTIVE_LOCKDIRS=()

_cleanup_locks() {
    for lockdir in ${_INTARIS_ACTIVE_LOCKDIRS[@]+"${_INTARIS_ACTIVE_LOCKDIRS[@]}"}; do
        rmdir "$lockdir" 2>/dev/null || true
    done
}

# Acquire a lock on a state file. Blocks up to ~2 seconds under normal
# contention, plus up to a few hundred ms per stale-lock reclaim attempt
# (bounded — see max_stale_reclaims below).
# Usage: acquire_lock "/path/to/state.json"
# Returns 0 on success, 1 on failure.
acquire_lock() {
    local lockdir="$1.lock.d"
    local attempts=0
    local stale_reclaims=0
    local max_stale_reclaims=5
    while ! mkdir "$lockdir" 2>/dev/null; do
        attempts=$((attempts + 1))
        if [ $attempts -ge 20 ]; then
            # Check for a stale lock (older than 30s). Every real lock hold
            # in this codebase is a handful of jq calls plus a mv — well
            # under a second — so anything this old almost certainly means
            # the holder died without cleaning up.
            if [ -d "$lockdir" ]; then
                # BSD stat's `-f FORMAT` and GNU stat's `-f` (filesystem info,
                # a different mode entirely) collide on the same flag. If
                # coreutils' GNU `stat` shadows the system one on PATH (e.g.
                # homebrew coreutils ahead of /usr/bin — common on macOS),
                # `stat -f %m` does not fail, it silently prints filesystem
                # info instead of the mtime, and the `||` fallback below
                # never triggers because the command "succeeded". Validate
                # the result is a bare integer before trusting either form.
                local mtime
                mtime=$(stat -f %m "$lockdir" 2>/dev/null)
                if ! [[ "$mtime" =~ ^[0-9]+$ ]]; then
                    mtime=$(stat -c %Y "$lockdir" 2>/dev/null)
                fi
                local lock_age=-1
                if [[ "$mtime" =~ ^[0-9]+$ ]]; then
                    lock_age=$(( $(date +%s) - mtime ))
                else
                    log "Could not determine lock age for $lockdir (unrecognized stat output); not reclaiming"
                fi
                if [ "$lock_age" -gt 30 ] && [ "$stale_reclaims" -lt "$max_stale_reclaims" ]; then
                    log "Removing stale lock: $lockdir (age: ${lock_age}s)"
                    rmdir "$lockdir" 2>/dev/null
                    stale_reclaims=$((stale_reclaims + 1))
                    attempts=0
                    sleep 0.1
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

# Release a lock on a state file. Only removes the lock directory if this
# process is the one that acquired it (tracked via _INTARIS_ACTIVE_LOCKDIRS).
# This matters because callers routinely do `acquire_lock ... || true` and
# then unconditionally call release_lock — without the ownership check that
# would delete the actual holder's lock out from under it, letting a third
# process into the critical section and corrupting the state file.
# Usage: release_lock "/path/to/state.json"
release_lock() {
    local lockdir="$1.lock.d"
    local d owned=0
    for d in ${_INTARIS_ACTIVE_LOCKDIRS[@]+"${_INTARIS_ACTIVE_LOCKDIRS[@]}"}; do
        if [ "$d" = "$lockdir" ]; then
            owned=1
            break
        fi
    done
    [ "$owned" -eq 1 ] || return 0

    rmdir "$lockdir" 2>/dev/null || true

    local new_list=()
    for d in "${_INTARIS_ACTIVE_LOCKDIRS[@]}"; do
        [ "$d" != "$lockdir" ] && new_list+=("$d")
    done
    _INTARIS_ACTIVE_LOCKDIRS=(${new_list[@]+"${new_list[@]}"})
}

# -- Atomic State File Writes ------------------------------------------------

# Write content to a state file atomically (write to a unique temp file,
# then mv). Uses mktemp rather than a fixed "${file}.tmp" name so that two
# concurrent writers to the same state file never target the same temp path
# and interleave their writes.
# Usage: write_state "/path/to/state.json" "$json_content"
write_state() {
    local file="$1"
    local content="$2"
    local tmp
    tmp=$(mktemp "${file}.XXXXXX") || return 1
    chmod 600 "$tmp"
    echo "$content" > "$tmp"
    mv "$tmp" "$file"
}

# -- Allow Paths Policy ------------------------------------------------------

# Build allow_paths policy JSON from built-in safe paths plus INTARIS_ALLOW_PATHS.
# Returns "null" only if no patterns are available, or a JSON object with
# allow_paths array otherwise.
# Usage: POLICY_JSON=$(build_allow_paths_policy)
# Add a directory's /* glob pattern to $patterns, plus its resolved
# physical-path form if that differs. On macOS, /tmp, /var/tmp and
# $TMPDIR are symlinks into /private/... — a literal "/tmp/*" pattern
# never matches an already-resolved real path like
# /private/tmp/claude-501/.../scratchpad, which is exactly where Claude
# Code's own tool scratchpad lives. Silently does nothing if the
# directory doesn't exist or can't be resolved.
_intaris_add_glob_patterns() {
    local dir="${1%/}"
    [ -z "$dir" ] && return
    patterns=$(echo "$patterns" | jq --arg pat "${dir}/*" '. + [$pat]')
    local real
    real=$(cd "$dir" 2>/dev/null && pwd -P || :)
    if [ -n "$real" ] && [ "$real" != "$dir" ]; then
        patterns=$(echo "$patterns" | jq --arg pat "${real}/*" '. + [$pat]')
    fi
}

build_allow_paths_policy() {
    local patterns="[]"

    _intaris_add_glob_patterns "/tmp"
    _intaris_add_glob_patterns "/var/tmp"

    if [ -n "${TMPDIR:-}" ]; then
        _intaris_add_glob_patterns "$TMPDIR"
    fi

    if [ -n "${HOME:-}" ]; then
        _intaris_add_glob_patterns "$HOME/.claude/plans"
    fi

    if [ -z "$INTARIS_ALLOW_PATHS" ]; then
        if [ "$(echo "$patterns" | jq 'length')" -gt 0 ]; then
            jq -n --argjson ap "$patterns" '{"allow_paths": $ap}'
        else
            echo "null"
        fi
        return
    fi

    local IFS=','
    # shellcheck disable=SC2086
    set -- $INTARIS_ALLOW_PATHS
    for ap_entry in "$@"; do
        # Trim whitespace via pure parameter expansion, not `xargs` — xargs
        # re-parses its input as shell-like words, so a path containing a
        # quote or backslash comes out mangled. Parameter expansion just
        # strips leading/trailing whitespace bytes, nothing else.
        ap_entry="${ap_entry#"${ap_entry%%[![:space:]]*}"}"
        ap_entry="${ap_entry%"${ap_entry##*[![:space:]]}"}"
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

# Output a PreToolUse deny decision in the non-deprecated hookSpecificOutput
# format. An optional second argument adds a systemMessage — a warning
# surfaced directly in the terminal — for cases like an escalation timing
# out silently after up to a minute of the user seeing nothing at all.
# Usage: deny_tool "reason text" ["system message shown to the user"]
deny_tool() {
    local reason="$1"
    local system_message="${2:-}"
    DECISION_EMITTED=1
    if [ -n "$system_message" ]; then
        jq -n --arg reason "$reason" --arg msg "$system_message" '{
            hookSpecificOutput: {
                hookEventName: "PreToolUse",
                permissionDecision: "deny",
                permissionDecisionReason: $reason
            },
            systemMessage: $msg
        }'
    else
        jq -n --arg reason "$reason" '{
            hookSpecificOutput: {
                hookEventName: "PreToolUse",
                permissionDecision: "deny",
                permissionDecisionReason: $reason
            }
        }'
    fi
}

# Output a PreToolUse allow decision (empty JSON = allow).
allow_tool() {
    DECISION_EMITTED=1
    echo '{}'
}

# Deny a PreToolUse call without depending on jq. Claude Code treats exit
# code 2 from a PreToolUse hook as a blocking error and feeds stderr back to
# the model, so no JSON body is required. Use this whenever jq availability
# itself is in question (e.g. the require_jq guard) — deny_tool above would
# silently emit nothing if jq were missing, turning a deny into an allow.
# Usage: deny_tool_raw "reason text"
deny_tool_raw() {
    DECISION_EMITTED=1
    echo "[intaris] $1" >&2
    exit 2
}
