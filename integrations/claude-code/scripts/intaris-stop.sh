#!/usr/bin/env bash
# intaris stop hook for Claude Code
#
# Called on Stop. Behavior depends on stop_hook_active:
#
# stop_hook_active=false (genuine final stop):
#   - Store last_assistant_message in state file
#   - Signal session completion (PATCH status + POST agent-summary)
#   - Complete child sessions
#   - Upload transcript (if recording enabled)
#
# stop_hook_active=true (Claude continuing from another stop hook):
#   - Store last_assistant_message in state file only
#   - Do NOT signal completion
#
# Environment variables:
#   INTARIS_URL                - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY            - API key for authentication (required)
#   INTARIS_AGENT_ID           - Agent ID (default: claude-code)
#   INTARIS_USER_ID            - User ID (optional if API key maps to user)
#   INTARIS_SESSION_RECORDING  - Enable session recording (default: false)
#   INTARIS_DEBUG              - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

if ! require_jq; then
    log "jq is required for Stop hook, skipping"
    echo '{}'
    exit 0
fi

# Read hook input from stdin
INPUT=$(cat)

# Extract session info from the hook input
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null || echo "false")
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ]; then
    log "No session_id in hook input, skipping"
    echo '{}'
    exit 0
fi

if ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

# Load session state
SESSION_FILE=$(state_file_for "$SESSION_ID")

if [ ! -f "$SESSION_FILE" ]; then
    log "No state file found, skipping"
    echo '{}'
    exit 0
fi

# -- Always: Store last_assistant_message in state file ----------------------
# This provides context for the next UserPromptSubmit → /reasoning call.

if [ -n "$LAST_MSG" ]; then
    # Truncate to 4000 chars to bound state file size and /reasoning context
    LAST_MSG=$(printf '%s' "$LAST_MSG" | cut -c1-4000)

    acquire_lock "$SESSION_FILE" || { echo '{}'; exit 0; }
    if [ -f "$SESSION_FILE" ]; then
        UPDATED=$(jq --arg text "$LAST_MSG" '.last_assistant_text = $text' "$SESSION_FILE" 2>/dev/null || cat "$SESSION_FILE")
        [ -n "$UPDATED" ] && write_state "$SESSION_FILE" "$UPDATED"
    fi
    release_lock "$SESSION_FILE"
fi

# -- Always: Record assistant message event for session recording ------------
# Fires on every Stop (intermediate and final) since each corresponds to one
# assistant turn. Needs session_id from state file.

if [ "$INTARIS_SESSION_RECORDING" = "true" ] && [ -n "$LAST_MSG" ]; then
    STOP_SID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)
    if [ -n "$STOP_SID" ]; then
        build_headers
        RECORD_BODY=$(jq -n --arg text "$LAST_MSG" \
            '[{type: "message", data: {role: "assistant", text: $text}}]')
        curl -s --max-time 2 \
            -X POST \
            "${HEADERS[@]}" \
            -H "X-Intaris-Source: claude-code" \
            -d "$RECORD_BODY" \
            "${INTARIS_URL}/api/v1/session/${STOP_SID}/events" >/dev/null 2>&1 || true
        log "Recorded assistant message event (${#LAST_MSG} chars)"
    fi
fi

# -- If stop_hook_active=true, Claude is continuing — don't complete ---------

if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    log "stop_hook_active=true, storing assistant text only"
    echo '{}'
    exit 0
fi

# -- Genuine final stop: signal completion -----------------------------------

acquire_lock "$SESSION_FILE" || { echo '{}'; exit 0; }

INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)
CALL_COUNT=$(jq -r '.call_count // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
APPROVED=$(jq -r '.approved // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
DENIED=$(jq -r '.denied // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
ESCALATED=$(jq -r '.escalated // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
SUBAGENTS=$(jq -c '.subagents // {}' "$SESSION_FILE" 2>/dev/null || echo '{}')

# Fall back to state file cwd if not in hook input
if [ -z "$CWD" ]; then
    CWD=$(jq -r '.cwd // empty' "$SESSION_FILE" 2>/dev/null || true)
fi

release_lock "$SESSION_FILE"

if [ -z "$INTARIS_SESSION_ID" ]; then
    log "No session_id in state file, skipping"
    echo '{}'
    exit 0
fi

build_headers

# -- Complete child sessions -------------------------------------------------

if [ "$SUBAGENTS" != "{}" ] && [ "$SUBAGENTS" != "null" ]; then
    # Iterate subagent mappings and signal completion for each. Uses
    # process substitution + `read -r` rather than unquoted `for x in $(...)`
    # so an agent id is never subject to word-splitting or glob expansion.
    while IFS= read -r agent_id; do
        [ -z "$agent_id" ] && continue
        child_sid=$(echo "$SUBAGENTS" | jq -r --arg k "$agent_id" '.[$k]' 2>/dev/null || true)
        if [ -n "$child_sid" ]; then
            log "Completing child session: $child_sid (agent: $agent_id)"
            CHILD_FILE=$(state_file_for_subagent "$SESSION_ID" "$agent_id")
            CHILD_SUMMARY_BODY=''
            if [ -f "$CHILD_FILE" ]; then
                acquire_lock "$CHILD_FILE" || true
                if [ -f "$CHILD_FILE" ]; then
                    CHILD_CALL_COUNT=$(jq -r '.call_count // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
                    CHILD_APPROVED=$(jq -r '.approved // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
                    CHILD_DENIED=$(jq -r '.denied // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
                    CHILD_ESCALATED=$(jq -r '.escalated // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
                    CHILD_CWD=$(jq -r '.cwd // empty' "$CHILD_FILE" 2>/dev/null || true)
                    CHILD_AGENT_TYPE=$(jq -r '.agent_type // empty' "$CHILD_FILE" 2>/dev/null || true)
                    CHILD_LABEL="${CHILD_AGENT_TYPE:-sub-agent}"
                    CHILD_SUMMARY="Claude Code ${CHILD_LABEL} session completed. ${CHILD_CALL_COUNT} tool calls (${CHILD_APPROVED} approved, ${CHILD_DENIED} denied, ${CHILD_ESCALATED} escalated)."
                    if [ -n "$CHILD_CWD" ]; then
                        CHILD_SUMMARY="${CHILD_SUMMARY} Working directory: ${CHILD_CWD}"
                    fi
                    CHILD_SUMMARY_BODY=$(jq -n --arg s "$CHILD_SUMMARY" '{summary: $s}')
                fi
                release_lock "$CHILD_FILE"
            fi

            curl -s --max-time 2 \
                -X PATCH \
                "${HEADERS[@]}" \
                -d '{"status":"completed"}' \
                "${INTARIS_URL}/api/v1/session/${child_sid}/status" >/dev/null 2>&1 || true

            if [ -n "$CHILD_SUMMARY_BODY" ]; then
                curl -s --max-time 2 \
                    -X POST \
                    "${HEADERS[@]}" \
                    -d "$CHILD_SUMMARY_BODY" \
                    "${INTARIS_URL}/api/v1/session/${child_sid}/agent-summary" >/dev/null 2>&1 || true
            fi

            # Clean up child state file
            rm -f "$CHILD_FILE"
        fi
    done < <(jq -r 'keys[]' <<< "$SUBAGENTS" 2>/dev/null || true)
fi

# -- Transition Parent Session To Idle (before transcript upload) -------------

# Build agent summary
SUMMARY="Claude Code session idle. ${CALL_COUNT} tool calls (${APPROVED} approved, ${DENIED} denied, ${ESCALATED} escalated)."
if [ -n "$CWD" ]; then
    SUMMARY="${SUMMARY} Working directory: ${CWD}"
fi

STATUS_BODY='{"status":"idle"}'
SUMMARY_BODY=$(jq -n --arg s "$SUMMARY" '{summary: $s}')

log "Transitioning session to idle: $INTARIS_SESSION_ID"

# Send status update and agent summary in parallel (fire-and-forget)
curl -s --max-time 2 \
    -X PATCH \
    "${HEADERS[@]}" \
    -d "$STATUS_BODY" \
    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/status" >/dev/null 2>&1 &

curl -s --max-time 2 \
    -X POST \
    "${HEADERS[@]}" \
    -d "$SUMMARY_BODY" \
    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/agent-summary" >/dev/null 2>&1 &

wait

log "Session idle signaled: $INTARIS_SESSION_ID"

# -- Session Recording: upload transcript (best-effort, after completion) ----
# Transcript upload runs after completion signals so session state is always
# correct even if the hook is killed during upload (8s hook timeout).
#
# Stop fires once per assistant turn, not once per session, so a naive
# "re-read and re-upload the whole file" approach re-uploads every prior
# turn's lines on every subsequent turn (quadratic over a session) and used
# to fork one jq process per JSONL line on top of that. Instead we persist
# how many lines were already uploaded (transcript_upload_offset, in the
# session state file) and only read/parse/upload the delta each time, using
# a single jq process to parse the whole delta at once.

if [ "$INTARIS_SESSION_RECORDING" = "true" ]; then
    TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)

    if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
        TOTAL_LINES=$(wc -l < "$TRANSCRIPT_PATH" | tr -d ' ')
        TOTAL_LINES=${TOTAL_LINES:-0}

        UPLOAD_OFFSET=0
        if [ -f "$SESSION_FILE" ]; then
            UPLOAD_OFFSET=$(jq -r '.transcript_upload_offset // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
        fi
        case "$UPLOAD_OFFSET" in ''|*[!0-9]*) UPLOAD_OFFSET=0 ;; esac

        # If the transcript is shorter than our recorded offset (rotated,
        # truncated, or a stale offset from a different file), start over
        # rather than skipping everything or reading a negative range.
        if [ "$TOTAL_LINES" -lt "$UPLOAD_OFFSET" ]; then
            log "Transcript shorter than recorded offset ($TOTAL_LINES < $UPLOAD_OFFSET) — resetting"
            UPLOAD_OFFSET=0
        fi

        if [ "$TOTAL_LINES" -gt "$UPLOAD_OFFSET" ]; then
            log "Uploading transcript delta from: $TRANSCRIPT_PATH (lines $((UPLOAD_OFFSET + 1))-${TOTAL_LINES})"

            CHUNK_SIZE=500

            # Single jq process parses the whole delta at once instead of
            # one process per line.
            ALL_EVENTS=$(tail -n "+$((UPLOAD_OFFSET + 1))" "$TRANSCRIPT_PATH" | jq -c -R -s '
                split("\n")
                | map(select(length > 0))
                | map((try fromjson catch null))
                | map(select(. != null))
                | map({type: "transcript", data: .})
            ' 2>/dev/null || echo '[]')

            NEW_COUNT=$(echo "$ALL_EVENTS" | jq 'length' 2>/dev/null || echo "0")

            TOTAL_UPLOADED=0
            i=0
            while [ "$i" -lt "$NEW_COUNT" ]; do
                CHUNK=$(echo "$ALL_EVENTS" | jq -c ".[$i:$((i + CHUNK_SIZE))]" 2>/dev/null || echo '[]')
                curl -s --max-time 4 \
                    -X POST \
                    "${HEADERS[@]}" \
                    -H "X-Intaris-Source: claude-code" \
                    -d "$CHUNK" \
                    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/events" >/dev/null 2>&1 || true
                TOTAL_UPLOADED=$((TOTAL_UPLOADED + CHUNK_SIZE))
                i=$((i + CHUNK_SIZE))
            done

            if [ "$NEW_COUNT" -gt 0 ]; then
                log "Uploaded $NEW_COUNT transcript events"
                curl -s --max-time 2 \
                    -X POST \
                    "${HEADERS[@]}" \
                    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/events/flush" >/dev/null 2>&1 || true
            fi

            # Advance the offset past every line we just read, regardless of
            # whether it parsed as JSON, so a malformed line is never
            # retried on every future turn.
            acquire_lock "$SESSION_FILE" || true
            if [ -f "$SESSION_FILE" ]; then
                UPDATED_STATE=$(jq --argjson off "$TOTAL_LINES" '.transcript_upload_offset = $off' "$SESSION_FILE" 2>/dev/null || true)
                [ -n "$UPDATED_STATE" ] && write_state "$SESSION_FILE" "$UPDATED_STATE"
            fi
            release_lock "$SESSION_FILE"
        else
            log "No new transcript lines to upload"
        fi
    else
        log "No transcript path available or file not found"
    fi
fi

# Output empty (no modifications to Claude's behavior)
echo '{}'
