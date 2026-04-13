#!/usr/bin/env bash
# intaris subagent hook for Claude Code
#
# Called on SubagentStart. Creates a child Intaris session linked to the
# parent session for hierarchical session tracking and alignment enforcement.
#
# Environment variables:
#   INTARIS_URL        - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY    - API key for authentication (required)
#   INTARIS_AGENT_ID   - Agent ID (default: claude-code)
#   INTARIS_USER_ID    - User ID (optional if API key maps to user)
#   INTARIS_INTENTION  - Session intention override (default: auto-generated)
#   INTARIS_ALLOW_PATHS - Comma-separated parent directories to allow reads from
#   INTARIS_DEBUG      - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

require_jq

# Read hook input from stdin
INPUT=$(cat)

# Extract fields
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null || true)
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty' 2>/dev/null || true)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ] || [ -z "$AGENT_ID" ]; then
    log "Missing session_id or agent_id in SubagentStart input, skipping"
    echo '{}'
    exit 0
fi

if ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

# Validate agent_id format (same rules as session_id — no path traversal)
if ! validate_session_id "$AGENT_ID"; then
    echo '{}'
    exit 0
fi

# Load parent state file to get parent's Intaris session ID
PARENT_FILE=$(state_file_for "$SESSION_ID")

if [ ! -f "$PARENT_FILE" ]; then
    log "No parent state file, skipping subagent creation"
    echo '{}'
    exit 0
fi

acquire_lock "$PARENT_FILE" || { echo '{}'; exit 0; }
PARENT_SESSION_ID=$(jq -r '.session_id // empty' "$PARENT_FILE" 2>/dev/null || true)
PARENT_CWD=$(jq -r '.cwd // empty' "$PARENT_FILE" 2>/dev/null || true)
release_lock "$PARENT_FILE"

if [ -z "$PARENT_SESSION_ID" ]; then
    log "No session_id in parent state file, skipping"
    echo '{}'
    exit 0
fi

# Use parent CWD if not provided in hook input
CWD="${CWD:-$PARENT_CWD}"

# Generate child Intaris session ID (double-hyphen separator to avoid ambiguity)
CHILD_SESSION_ID="cc-${SESSION_ID}--${AGENT_ID}"

# Build intention for child session
if [ -n "$INTARIS_INTENTION" ]; then
    INTENTION="$INTARIS_INTENTION"
else
    AGENT_LABEL="${AGENT_TYPE:-sub-agent}"
    INTENTION="Claude Code ${AGENT_LABEL} sub-agent session in ${CWD:-unknown}"
fi

build_headers

# Build allow_paths policy (inherit from parent config)
POLICY_JSON=$(build_allow_paths_policy)

# Build request body
BODY=$(jq -n \
    --arg session_id "$CHILD_SESSION_ID" \
    --arg intention "$INTENTION" \
    --arg parent_session_id "$PARENT_SESSION_ID" \
    --arg cwd "$CWD" \
    --arg agent_type "${AGENT_TYPE:-}" \
    --argjson policy "$POLICY_JSON" \
    '{
        session_id: $session_id,
        intention: $intention,
        parent_session_id: $parent_session_id,
        details: {
            source: "claude-code",
            working_directory: $cwd,
            agent_type: $agent_type
        }
    } + (if $policy != null then {policy: $policy} else {} end)')

log "Creating child session: $CHILD_SESSION_ID (parent: $PARENT_SESSION_ID, type: ${AGENT_TYPE:-unknown})"

# Create child session (2s timeout)
curl -s --max-time 2 \
    -X POST \
    "${HEADERS[@]}" \
    -d "$BODY" \
    "${INTARIS_URL}/api/v1/intention" >/dev/null 2>&1 || log "Failed to create child session"

# Write child state file
CHILD_FILE=$(state_file_for_subagent "$SESSION_ID" "$AGENT_ID")
CHILD_STATE=$(jq -n \
    --arg sid "$CHILD_SESSION_ID" \
    --arg cwd "$CWD" \
    --arg agent_type "${AGENT_TYPE:-}" \
    '{
        session_id: $sid,
        call_count: 0,
        approved: 0,
        denied: 0,
        escalated: 0,
        recent_tools: [],
        cwd: $cwd,
        agent_type: $agent_type,
        last_assistant_text: "",
        subagents: {}
    }')

write_state "$CHILD_FILE" "$CHILD_STATE"

# Add mapping to parent state file's subagents map
acquire_lock "$PARENT_FILE" || { echo '{}'; exit 0; }
if [ -f "$PARENT_FILE" ]; then
    UPDATED=$(jq --arg aid "$AGENT_ID" --arg csid "$CHILD_SESSION_ID" \
        '.subagents[$aid] = $csid' "$PARENT_FILE" 2>/dev/null || cat "$PARENT_FILE")
    write_state "$PARENT_FILE" "$UPDATED"
fi
release_lock "$PARENT_FILE"

log "Child session created: $CHILD_SESSION_ID"

# Output empty (cannot block subagent creation)
echo '{}'
