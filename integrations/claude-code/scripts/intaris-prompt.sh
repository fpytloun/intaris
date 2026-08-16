#!/usr/bin/env bash
# intaris prompt hook for Claude Code
#
# Called on UserPromptSubmit. Forwards user messages to Intaris as reasoning
# records for intention tracking and behavioral analysis. Includes the
# assistant's last response as context to help interpret short user replies
# like "ok, do it".
#
# Environment variables:
#   INTARIS_URL        - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY    - API key for authentication (required)
#   INTARIS_AGENT_ID   - Agent ID (default: claude-code)
#   INTARIS_USER_ID    - User ID (optional if API key maps to user)
#   INTARIS_SESSION_RECORDING - Enable session recording (default: false)
#   INTARIS_DEBUG      - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

if ! require_jq; then
    log "jq is required for UserPromptSubmit, skipping"
    echo '{}'
    exit 0
fi

# Read hook input from stdin
INPUT=$(cat)

# Extract fields
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ] || [ -z "$PROMPT" ]; then
    echo '{}'
    exit 0
fi

if ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

# Load state file to get Intaris session ID
SESSION_FILE=$(state_file_for "$SESSION_ID")

if [ ! -f "$SESSION_FILE" ]; then
    log "No state file for prompt hook, skipping (session not created yet)"
    echo '{}'
    exit 0
fi

acquire_lock "$SESSION_FILE" || { echo '{}'; exit 0; }

INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)
LAST_ASSISTANT_TEXT=$(jq -r '.last_assistant_text // empty' "$SESSION_FILE" 2>/dev/null || true)

release_lock "$SESSION_FILE"

if [ -z "$INTARIS_SESSION_ID" ]; then
    log "No session_id in state file, skipping"
    echo '{}'
    exit 0
fi

build_headers

# -- Resume the session to active ---------------------------------------
# intaris-stop.sh marks the session "idle" at the end of every assistant
# turn (Stop fires per turn, not per session), but nothing ever marked it
# back "active" — so between turns the Intaris UI shows a session that is
# actively being worked on as idle. It only recovered as a side effect of
# intaris-evaluate.sh's reactivation path on the next tool call. Do it here
# too, backgrounded (and disowned) since it doesn't need to complete before
# the prompt is allowed through.
( curl -s --max-time 2 \
    -X PATCH \
    "${HEADERS[@]}" \
    -d '{"status":"active"}' \
    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/status" >/dev/null 2>&1 || true ) &
disown

# -- Record user message + forward to /reasoning, entirely in the background
#
# None of this gates, blocks, or alters the user's prompt — it's pure
# behavioral analysis. It used to run inline as up to three sequential
# blocking curls (record + flush + /reasoning, up to 2+2+5s), which stalled
# every prompt submission. Move the whole sequence into a backgrounded,
# disowned subshell so UserPromptSubmit returns immediately; the ordering
# dependency between the event record, its flush, and /reasoning's
# from_events flag is preserved exactly, just off the hot path.
#
# This subshell is a fork of the current shell, so it inherits every
# function and variable already defined above (acquire_lock, write_state,
# HEADERS, SESSION_ID, PROMPT, etc.) without re-sourcing anything.
(
    # -- Refresh the session intention from the current prompt ---------------
    # intaris-session.sh sets a static "Claude Code coding session in <cwd>"
    # intention once at SessionStart and never updates it — not even on
    # session resume (see intaris-session.sh's 409 handling). A generic cwd
    # string degrades every downstream alignment evaluation for the whole
    # session. Refresh it from the user's own words on every prompt instead.
    # Best-effort: failure here must never affect anything else in this hook.
    INTENTION_UPDATE=$(jq -n --arg text "User message: $PROMPT" '{intention: ($text[0:500])}')
    curl -s --max-time 2 \
        -X PATCH \
        "${HEADERS[@]}" \
        -d "$INTENTION_UPDATE" \
        "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}" >/dev/null 2>&1 || true

    # -- Record user message event FIRST (before the slow /reasoning call) ---
    # This ensures the event is recorded even if /reasoning times out. When
    # recording is enabled, /reasoning with from_events=true only works
    # after the event store buffer is flushed. If either append or flush
    # fails, fall back to sending the content directly in the reasoning
    # request body.
    EVENT_RECORDED=false
    if [ "$INTARIS_SESSION_RECORDING" = "true" ]; then
        RECORD_BODY=$(jq -n \
            --arg prompt "$PROMPT" \
            '[{type: "message", data: {role: "user", text: $prompt}}]')

        EVENT_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
            -X POST \
            "${HEADERS[@]}" \
            -H "X-Intaris-Source: claude-code" \
            -d "$RECORD_BODY" \
            "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/events" 2>/dev/null || echo "000")

        if [ "$EVENT_STATUS" = "200" ]; then
            FLUSH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
                -X POST \
                "${HEADERS[@]}" \
                "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/events/flush" 2>/dev/null || echo "000")

            if [ "$FLUSH_STATUS" = "200" ]; then
                EVENT_RECORDED=true
            else
                log "Event flush failed (HTTP $FLUSH_STATUS), will send content directly"
            fi
        else
            log "Event recording failed (HTTP $EVENT_STATUS), will send content directly"
        fi
    fi

    # -- Forward user message to /reasoning for intention tracking -----------
    log "Forwarding user message to /reasoning (session: $INTARIS_SESSION_ID)"

    # Only use from_events=true when the user message event is safely
    # flushed and we do not need local assistant context. If we still have
    # last_assistant_text, prefer the direct path so short follow-up
    # prompts keep their context even when the previous assistant message
    # event never made it to the event store.
    if [ "$EVENT_RECORDED" = "true" ] && [ -z "$LAST_ASSISTANT_TEXT" ]; then
        REASONING_BODY=$(jq -n \
            --arg sid "$INTARIS_SESSION_ID" \
            '{session_id: $sid, content: "", from_events: true}')
    else
        REASONING_BODY=$(jq -n \
            --arg sid "$INTARIS_SESSION_ID" \
            --arg content "User message: $PROMPT" \
            --arg context "$LAST_ASSISTANT_TEXT" \
            '{session_id: $sid, content: $content} + (if $context != "" then {context: $context} else {} end)')
    fi

    RESPONSE=$(curl -s --max-time 5 \
        -w "\n%{http_code}" \
        -X POST \
        "${HEADERS[@]}" \
        -d "$REASONING_BODY" \
        "${INTARIS_URL}/api/v1/reasoning" 2>/dev/null || printf '\n000')

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)

    if [ "$HTTP_CODE" = "200" ]; then
        log "Reasoning record sent successfully"
        # Clear last_assistant_text (consumed for context)
        acquire_lock "$SESSION_FILE" || exit 0
        if [ -f "$SESSION_FILE" ]; then
            UPDATED=$(jq '.last_assistant_text = ""' "$SESSION_FILE" 2>/dev/null || cat "$SESSION_FILE")
            [ -n "$UPDATED" ] && write_state "$SESSION_FILE" "$UPDATED"
        fi
        release_lock "$SESSION_FILE"
    else
        log "Failed to send reasoning record (HTTP $HTTP_CODE)"
    fi
) &
disown

# Output empty immediately (never block user prompts)
echo '{}'
