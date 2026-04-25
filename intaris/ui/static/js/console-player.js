/**
 * Console session player — renders session recordings as a conversation view.
 *
 * Processes raw session events into renderable blocks that mimic the agent's
 * terminal output. Supports multiple agent sources:
 *   - OpenCode: uses message, part, tool_call, tool_result, evaluation events
 *   - Claude Code: uses transcript events (Anthropic API JSONL format)
 *   - Generic: best-effort from tool_call, tool_result, evaluation events
 *
 * Features:
 *   - Markdown rendering via marked.js + syntax highlighting via highlight.js
 *   - Event correlation (tool_call + evaluation + tool_result → single block)
 *   - Part deduplication (streaming updates → latest state only)
 *   - Collapsible tool calls and reasoning sections
 *   - Live tailing via WebSocket
 *   - Auto-scroll
 */

// ── Markdown rendering ──────────────────────────────────────────

/**
 * Configure marked.js with highlight.js integration.
 */
if (typeof marked !== 'undefined') {
  marked.setOptions({
    breaks: true,
    gfm: true,
    highlight: function (code, lang) {
      if (typeof hljs !== 'undefined') {
        if (lang && hljs.getLanguage(lang)) {
          try {
            return hljs.highlight(code, { language: lang }).value;
          } catch (_) {}
        }
        try {
          return hljs.highlightAuto(code).value;
        } catch (_) {}
      }
      return code;
    },
  });
}

function renderMarkdown(text) {
  if (!text || typeof marked === 'undefined') return escapeHtml(text || '');
  try {
    const html = marked.parse(text);
    // Sanitize to prevent XSS from user-controlled content in assistant messages
    const safeHtml = typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(html) : html;
    return markOutgoingLinks(safeHtml);
  } catch (_) {
    return escapeHtml(text);
  }
}

function markOutgoingLinks(html) {
  if (typeof document === 'undefined') return html;
  const template = document.createElement('template');
  template.innerHTML = html;
  template.content.querySelectorAll('a[href]').forEach((link) => {
    const href = (link.getAttribute('href') || '').trim().toLowerCase();
    if (href.startsWith('http://') || href.startsWith('https://') || href.startsWith('mailto:')) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    }
  });
  return template.innerHTML;
}

function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Source detection ─────────────────────────────────────────────

function detectSource(events) {
  const hasParts = events.some(e => e.type === 'part');
  const hasTranscripts = events.some(e => e.type === 'transcript');
  const sources = new Set(events.map(e => e.source).filter(Boolean));
  const hasCognisMessages = events.some(e => ['system_message', 'developer_message', 'context_snapshot'].includes(e.type));
  const hasCognisMetadata = events.some(e => ['user_message', 'assistant_message'].includes(e.type) && (e.data?.turn_id || e.data?.source));

  if (hasParts || sources.has('opencode')) return 'opencode';
  if (hasTranscripts || sources.has('claude-code')) return 'claude-code';
  if (sources.has('openclaw')) return 'openclaw';
  if (hasCognisMessages || hasCognisMetadata || [...sources].some(s => typeof s === 'string' && s.startsWith('cognis'))) return 'cognis';
  return null;
}

function formatMessageMeta(data) {
  const parts = [];
  if (data?.source) parts.push(data.source);
  if (data?.turn_id) {
    let turn = data.turn_id;
    if (Number.isInteger(data.position)) turn += '#' + data.position;
    parts.push(turn);
  }
  return parts.join(' · ');
}

function normalizeMessageContent(rawContent) {
  if (rawContent == null) return '';
  if (typeof rawContent === 'string') return rawContent;
  try {
    return JSON.stringify(rawContent, null, 2);
  } catch (_) {
    return String(rawContent);
  }
}

function buildRoleMessageBlock(blockType, label, data, ts, id, seq) {
  const content = normalizeMessageContent(data?.content ?? data?.text ?? '');
  if (!content || !content.trim()) return null;
  return {
    type: blockType,
    id,
    seq,
    text: content,
    html: renderMarkdown(content),
    label,
    meta: formatMessageMeta(data),
    ts,
  };
}

function buildContextSnapshotBlock(data, ts, id, seq) {
  const entries = Array.isArray(data?.entries) ? data.entries : [];
  const firstEntries = entries
    .slice(0, 3)
    .map(entry => [entry?.role, entry?.source, entry?.seq].filter(v => v !== undefined && v !== null && v !== '').join(':'))
    .filter(Boolean);
  return {
    type: 'context-snapshot',
    id,
    seq,
    snapshotSource: data?.source || 'unknown',
    entryCount: entries.length,
    turnId: data?.turn_id || '',
    summary: firstEntries.join(' · '),
    ts: data?.captured_at || ts,
  };
}

// ── Tool helpers ─────────────────────────────────────────────────

function toolSubtitle(tool, args) {
  if (!args || typeof args !== 'object') return '';
  const t = (tool || '').toLowerCase();

  if (['read', 'write', 'edit', 'mcp_read', 'mcp_write', 'mcp_edit'].includes(t))
    return args.filePath || args.file_path || args.path || '';
  if (['bash', 'mcp_bash', 'exec'].includes(t))
    return (args.command || '').substring(0, 100);
  if (['glob', 'mcp_glob'].includes(t))
    return args.pattern || '';
  if (['grep', 'mcp_grep'].includes(t))
    return args.pattern || '';
  if (['task', 'mcp_task'].includes(t))
    return args.description || '';
  if (['todowrite', 'mcp_todowrite'].includes(t))
    return (args.todos || []).length + ' items';
  if (['webfetch', 'mcp_webfetch'].includes(t))
    return args.url || '';

  // MCP tools: first short string value
  for (const v of Object.values(args)) {
    if (typeof v === 'string' && v.length > 0 && v.length <= 120) return v;
  }
  return '';
}

function formatArgsDisplay(tool, args) {
  if (!args || typeof args !== 'object') return '';
  const t = (tool || '').toLowerCase();

  // For bash, show just the command
  if (['bash', 'mcp_bash', 'exec'].includes(t) && args.command) {
    return args.command;
  }

  // For read/write/edit, show the path prominently
  if (['read', 'write', 'edit', 'mcp_read', 'mcp_write', 'mcp_edit'].includes(t)) {
    const path = args.filePath || args.file_path || args.path || '';
    const filtered = {};
    for (const [k, v] of Object.entries(args)) {
      if (['filePath', 'file_path', 'path'].includes(k)) continue;
      if (typeof v === 'string' && v.length > 500) {
        filtered[k] = v.substring(0, 500) + '...';
      } else {
        filtered[k] = v;
      }
    }
    const extra = Object.keys(filtered).length > 0
      ? '\n' + JSON.stringify(filtered, null, 2) : '';
    return path + extra;
  }

  // Default: JSON with truncation for long values
  const truncated = {};
  for (const [k, v] of Object.entries(args)) {
    if (typeof v === 'string' && v.length > 1000) {
      truncated[k] = v.substring(0, 1000) + '...';
    } else {
      truncated[k] = v;
    }
  }
  return JSON.stringify(truncated, null, 2);
}

function formatOutput(output, maxLines) {
  if (output == null) return null;
  maxLines = maxLines || 100;
  let text = typeof output === 'string' ? output : JSON.stringify(output, null, 2);
  const lines = text.split('\n');
  if (lines.length > maxLines) {
    return lines.slice(0, maxLines).join('\n') + '\n... (' + (lines.length - maxLines) + ' more lines)';
  }
  return text;
}

function highlightCode(code) {
  if (!code || typeof hljs === 'undefined') return escapeHtml(code || '');
  try {
    const html = hljs.highlightAuto(code).value;
    // Sanitize to prevent XSS from user-controlled content in tool output
    if (typeof DOMPurify !== 'undefined') return DOMPurify.sanitize(html);
    return html;
  } catch (_) {
    return escapeHtml(code);
  }
}

function formatLatency(ms) {
  if (ms == null) return '';
  if (ms < 1000) return ms + 'ms';
  return (ms / 1000).toFixed(1) + 's';
}

function formatTokens(tokens) {
  if (!tokens) return '';
  const parts = [];
  const input = tokens.input || 0;
  const output = tokens.output || 0;
  const reasoning = tokens.reasoning || 0;
  const cacheRead = tokens.cache?.read || 0;
  const cacheWrite = tokens.cache?.write || 0;
  const total = input + output + reasoning;
  if (total === 0) return '';
  parts.push(total.toLocaleString() + ' tokens');
  if (cacheRead > 0) parts.push(cacheRead.toLocaleString() + ' cached');
  return parts.join(', ');
}

// ── OpenCode event processor ─────────────────────────────────────

function processOpenCode(events) {
  const blocks = [];
  let blockId = 0;

  // Step 1: Deduplicate parts — keep latest per part.id
  const latestPartByPartId = new Map();
  for (const event of events) {
    if (event.type === 'part' && event.data?.part?.id) {
      const existing = latestPartByPartId.get(event.data.part.id);
      if (!existing || event.seq > existing.seq) {
        latestPartByPartId.set(event.data.part.id, event);
      }
    }
  }
  const latestPartSeqs = new Set([...latestPartByPartId.values()].map(e => e.seq));

  // Step 1b: Collect user message IDs to filter out echoed text parts
  const userMessageIds = new Set();
  for (const event of events) {
    if (event.type === 'message' && event.data?.role === 'user' && event.data?.messageID) {
      userMessageIds.add(event.data.messageID);
    }
  }

  // Step 2: Build correlation maps
  const toolResultsByCallId = new Map();
  const evaluationsBySeq = [];

  for (const event of events) {
    if (event.type === 'tool_result') {
      const cid = event.data?.callID || event.data?.call_id || event.data?.toolCallId;
      if (cid) toolResultsByCallId.set(cid, event);
    }
    if (event.type === 'evaluation') {
      evaluationsBySeq.push(event);
    }
  }

  // Track consumed events (tool_result, evaluation) to avoid double-rendering
  const consumed = new Set();
  // Track which user messageIDs already have a rendered user-message block
  // (used to deduplicate text parts that echo user input)
  const renderedUserMessages = new Set();
  // Track the latest rendered user text so a reasoning record derived from
  // the same user turn can be suppressed without hiding unrelated repeats.
  let latestRenderedUserText = null;

  // Step 3: Walk events in order, generate blocks
  for (const event of events) {
    const data = event.data || {};

    // ── User message ──
    if (event.type === 'message' && data.role === 'user') {
      // Skip empty user messages
      if (!data.text || !data.text.trim()) continue;
      if (data.messageID) renderedUserMessages.add(data.messageID);
      latestRenderedUserText = data.text.trim();
      blocks.push({
        type: 'user-message',
        id: 'b' + (blockId++),
        text: data.text,
        ts: event.ts,
      });
      continue;
    }

    if (event.type === 'user_message') {
      const content = normalizeMessageContent(data.content);
      if (!content || !content.trim()) continue;
      latestRenderedUserText = content.trim();
      blocks.push({
        type: 'user-message',
        id: 'b' + (blockId++),
        text: content,
        meta: formatMessageMeta(data),
        ts: event.ts,
      });
      continue;
    }

    if (event.type === 'system_message') {
      const block = buildRoleMessageBlock('system-message', 'System', data, event.ts, 'b' + (blockId++), event.seq);
      if (block) blocks.push(block);
      continue;
    }

    if (event.type === 'developer_message') {
      const block = buildRoleMessageBlock('developer-message', 'Developer', data, event.ts, 'b' + (blockId++), event.seq);
      if (block) blocks.push(block);
      continue;
    }

    if (event.type === 'context_snapshot') {
      blocks.push(buildContextSnapshotBlock(data, event.ts, 'b' + (blockId++), event.seq));
      continue;
    }

    // Assistant message handling — source-dependent.
    // OpenCode sends assistant text via part events; these message events
    // contain only metadata (model, tokens) so we skip them.
    // Other sources (OpenClaw, etc.) send assistant text directly in
    // message events, so we render them as assistant-text blocks.
    if (event.type === 'message' && data.role === 'assistant') {
      if (event.source === 'opencode') {
        continue;
      }
      const content = normalizeMessageContent(data.text);
      if (content && content.trim()) {
        blocks.push({
          type: 'assistant-text',
          id: 'b' + (blockId++),
          text: content,
          html: renderMarkdown(content),
          meta: formatMessageMeta(data),
          ts: event.ts,
        });
      }
      continue;
    }

    if (event.type === 'assistant_message') {
      const content = normalizeMessageContent(data.content);
      if (!content || !content.trim()) continue;
      blocks.push({
        type: 'assistant-text',
        id: 'b' + (blockId++),
        text: content,
        html: renderMarkdown(content),
        meta: formatMessageMeta(data),
        ts: event.ts,
      });
      continue;
    }

    // ── Part events (deduplicated) ──
    if (event.type === 'part') {
      // Skip if not the latest for this part.id
      if (event.data?.part?.id && !latestPartSeqs.has(event.seq)) continue;

      const part = data.part || {};
      const partType = part.type;

      // Skip redundant/internal part types
      if (['tool', 'snapshot', 'compaction', 'step-start'].includes(partType)) continue;

      if (partType === 'text') {
        // Skip synthetic parts (system-generated echoes)
        if (part.synthetic) continue;
        // Text parts belonging to user messages: render as user-message
        // fallback if the message event was missing (race condition in
        // chat.message hook), otherwise skip to avoid duplicates.
        if (part.messageID && userMessageIds.has(part.messageID)) {
          if (renderedUserMessages.has(part.messageID)) continue;
          if (!part.text || !part.text.trim()) continue;
          renderedUserMessages.add(part.messageID);
          latestRenderedUserText = part.text.trim();
          blocks.push({
            type: 'user-message',
            id: 'b' + (blockId++),
            text: part.text,
            ts: event.ts,
          });
          continue;
        }
        // Skip empty text parts
        if (!part.text && !data.delta) continue;
        blocks.push({
          type: 'assistant-text',
          id: 'b' + (blockId++),
          text: part.text || '',
          html: renderMarkdown(part.text || ''),
          ts: event.ts,
        });
        continue;
      }

      if (partType === 'reasoning') {
        if (!part.text) continue;
        blocks.push({
          type: 'reasoning',
          id: 'b' + (blockId++),
          text: part.text || '',
          ts: event.ts,
        });
        continue;
      }

      if (partType === 'step-finish') {
        const tokens = part.tokens || {};
        const totalTokens = (tokens.input || 0) + (tokens.output || 0) + (tokens.reasoning || 0);
        blocks.push({
          type: 'step-finish',
          id: 'b' + (blockId++),
          tokens: tokens,
          cost: part.cost || 0,
          reason: part.reason || '',
          summary: [
            part.reason || 'Step complete',
            totalTokens > 0 ? totalTokens.toLocaleString() + ' tokens' : '',
            part.cost > 0 ? '$' + part.cost.toFixed(4) : '',
          ].filter(Boolean).join(' · '),
          ts: event.ts,
        });
        continue;
      }

      if (partType === 'subtask') {
        // SubtaskPart.sessionID is the child's OpenCode session ID;
        // Intaris session IDs use the oc- prefix convention.
        const childSessionId = part.sessionID ? ('oc-' + part.sessionID) : null;
        blocks.push({
          type: 'subtask',
          id: 'b' + (blockId++),
          description: part.description || part.prompt || '',
          agent: part.agent || '',
          childSessionId,
          ts: event.ts,
        });
        continue;
      }

      // Other part types (file, agent, retry, patch) — skip for now
      continue;
    }

    // ── Tool call → correlate with evaluation + result ──
    if (event.type === 'tool_call') {
      const callID = data.callID || data.call_id || data.toolCallId;
      const toolName = data.tool || data.name || '?';
      const args = data.args || {};

      // Find matching evaluation (same tool, seq between this and next tool_call)
      let matchedEval = null;
      for (const evalEvent of evaluationsBySeq) {
        if (consumed.has(evalEvent.seq)) continue;
        if (evalEvent.data?.tool === toolName && evalEvent.seq > event.seq) {
          matchedEval = evalEvent;
          consumed.add(evalEvent.seq);
          break;
        }
      }

      // Find matching tool_result
      let matchedResult = null;
      if (callID && toolResultsByCallId.has(callID)) {
        matchedResult = toolResultsByCallId.get(callID);
        consumed.add(matchedResult.seq);
      }

      const evalData = matchedEval?.data || {};
      const resultData = matchedResult?.data || {};
      const output = resultData.output != null ? resultData.output : resultData.result;

      blocks.push({
        type: 'tool-group',
        id: 'b' + (blockId++),
        tool: toolName,
        subtitle: toolSubtitle(toolName, args),
        args: args,
        argsDisplay: formatArgsDisplay(toolName, args),
        decision: evalData.decision || null,
        risk: evalData.risk || null,
        latency: formatLatency(evalData.latency_ms),
        evalPath: evalData.path || null,
        output: formatOutput(output),
        outputHtml: output != null ? highlightCode(formatOutput(output)) : null,
        isError: resultData.isError || false,
        title: resultData.title || '',
        ts: event.ts,
      });
      continue;
    }

    // ── Skip consumed events ──
    if (event.type === 'tool_result' || event.type === 'evaluation') {
      if (consumed.has(event.seq)) continue;
      // Uncorrelated evaluation — render standalone (intaris-sourced)
      if (event.type === 'evaluation') {
        blocks.push({
          type: 'tool-group',
          id: 'b' + (blockId++),
          tool: data.tool || data.name || '?',
          subtitle: data.reasoning || '',
          args: data.args_redacted || {},
          argsDisplay: formatArgsDisplay(data.tool || data.name, data.args_redacted || {}),
          decision: data.decision || null,
          risk: data.risk || null,
          latency: formatLatency(data.latency_ms),
          evalPath: data.path || null,
          output: null,
          outputHtml: null,
          isError: false,
          title: '',
          ts: event.ts,
          intaris: true,
        });
      }
      continue;
    }

    // ── Lifecycle ──
    if (event.type === 'lifecycle') {
      blocks.push({
        type: 'lifecycle',
        id: 'b' + (blockId++),
        event: data.event || '',
        status: data.status || '',
        ts: event.ts,
      });
      continue;
    }

    if (event.type === 'delegation') {
      blocks.push({
        type: 'subtask',
        id: 'b' + (blockId++),
        description: data.task || data.result_summary || data.status || 'Delegation',
        agent: data.mode || '',
        childSessionId: data.child_session_id || null,
        ts: event.ts,
      });
      continue;
    }

    // ── Reasoning (server-side, from /reasoning endpoint) ──
    if (event.type === 'reasoning') {
      if (data.content && data.content.startsWith('User message:')) {
        const userText = data.content.slice('User message:'.length).trim();
        if (!userText) continue;
        if (latestRenderedUserText && userText === latestRenderedUserText) continue;
        latestRenderedUserText = userText;
        blocks.push({
          type: 'user-message',
          id: 'b' + (blockId++),
          text: userText,
          meta: data.from_events ? 'derived from events' : 'via /reasoning',
          ts: event.ts,
        });
        continue;
      }
      if (data.content) {
        blocks.push({
          type: 'reasoning',
          id: 'b' + (blockId++),
          text: data.content,
          ts: event.ts,
        });
      }
      continue;
    }

    if (event.type === 'compaction_summary') {
      if (!data.summary) continue;
      blocks.push({
        type: 'reasoning',
        id: 'b' + (blockId++),
        text: data.summary,
        ts: event.ts,
      });
      continue;
    }

    // Skip checkpoint, transcript (not expected for OpenCode)
  }

  return blocks;
}

// ── Claude Code transcript processor ─────────────────────────────

function processClaudeCode(events) {
  const blocks = [];
  let blockId = 0;

  // Separate transcript events from other events
  const transcriptEvents = events.filter(e => e.type === 'transcript');
  const otherEvents = events.filter(e => e.type !== 'transcript');

  // Build evaluation map from non-transcript events (tool name → evaluation)
  const evaluations = otherEvents.filter(e => e.type === 'evaluation');
  let evalIdx = 0;

  // Build tool_result map from non-transcript events
  const toolResults = new Map();
  for (const e of otherEvents) {
    if (e.type === 'tool_result' && e.data?.tool) {
      if (!toolResults.has(e.data.tool)) toolResults.set(e.data.tool, []);
      toolResults.get(e.data.tool).push(e);
    }
  }
  const toolResultCounters = new Map(); // tool → index consumed

  // Build tool_result map from transcript (tool_use_id → content)
  const transcriptToolResults = new Map();
  for (const event of transcriptEvents) {
    const data = event.data || {};
    // Claude Code transcript: user messages contain tool_result blocks
    if (data.role === 'user' || data.type === 'user') {
      const msg = data.message || data;
      const content = msg.content || [];
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block.type === 'tool_result' && block.tool_use_id) {
            transcriptToolResults.set(block.tool_use_id, block.content || block.output || '');
          }
        }
      }
    }
  }

  // Process transcript events in order
  for (const event of transcriptEvents) {
    const data = event.data || {};
    const role = data.role || data.type;
    const msg = data.message || data;
    const content = msg.content || [];

    // Skip system messages
    if (role === 'system') continue;
    // Skip summary/result/file-history-snapshot
    if (['summary', 'result', 'file-history-snapshot'].includes(role)) continue;

    // User messages
    if (role === 'user') {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block.type === 'text' && block.text) {
            blocks.push({
              type: 'user-message',
              id: 'b' + (blockId++),
              seq: event.seq,
              text: block.text,
              ts: event.ts,
            });
          }
          // tool_result blocks are consumed by correlation, skip here
        }
      } else if (typeof content === 'string' && content.trim()) {
        blocks.push({
          type: 'user-message',
          id: 'b' + (blockId++),
          seq: event.seq,
          text: content,
          ts: event.ts,
        });
      }
      continue;
    }

    // Assistant messages
    if (role === 'assistant') {
      if (Array.isArray(content)) {
        for (const block of content) {
          // Text content
          if (block.type === 'text' && block.text) {
            blocks.push({
              type: 'assistant-text',
              id: 'b' + (blockId++),
              seq: event.seq,
              text: block.text,
              html: renderMarkdown(block.text),
              ts: event.ts,
            });
          }

          // Thinking/reasoning
          if (block.type === 'thinking' && block.thinking) {
            blocks.push({
              type: 'reasoning',
              id: 'b' + (blockId++),
              seq: event.seq,
              text: block.thinking,
              ts: event.ts,
            });
          }

          // Tool use
          if (block.type === 'tool_use') {
            const toolName = block.name || '?';
            const args = block.input || {};
            const toolUseId = block.id;

            // Find matching evaluation
            let matchedEval = null;
            if (evalIdx < evaluations.length) {
              const candidate = evaluations[evalIdx];
              if (candidate.data?.tool === toolName) {
                matchedEval = candidate;
                evalIdx++;
              }
            }

            // Find matching tool_result from transcript
            const output = transcriptToolResults.get(toolUseId) || null;

            const evalData = matchedEval?.data || {};

            blocks.push({
              type: 'tool-group',
              id: 'b' + (blockId++),
              seq: event.seq,
              tool: toolName,
              subtitle: toolSubtitle(toolName, args),
              args: args,
              argsDisplay: formatArgsDisplay(toolName, args),
              decision: evalData.decision || null,
              risk: evalData.risk || null,
              latency: formatLatency(evalData.latency_ms),
              evalPath: evalData.path || null,
              output: formatOutput(output),
              outputHtml: output != null ? highlightCode(formatOutput(output)) : null,
              isError: false,
              title: '',
              ts: event.ts,
            });
          }
        }
      }

      // Assistant message metadata (tokens, cost)
      const usage = msg.usage || {};
      if (usage.input_tokens || usage.output_tokens) {
        blocks.push({
          type: 'assistant-meta',
          id: 'b' + (blockId++),
          seq: event.seq,
          model: msg.model || '',
          tokens: {
            input: usage.input_tokens || 0,
            output: usage.output_tokens || 0,
            reasoning: 0,
            cache: { read: usage.cache_read_input_tokens || 0, write: usage.cache_creation_input_tokens || 0 },
          },
          tokenSummary: formatTokens({
            input: usage.input_tokens || 0,
            output: usage.output_tokens || 0,
            reasoning: 0,
            cache: { read: usage.cache_read_input_tokens || 0, write: usage.cache_creation_input_tokens || 0 },
          }),
          cost: 0, // Claude Code doesn't report cost in transcript
          finish: msg.stop_reason || '',
          agent: '',
          ts: event.ts,
        });
      }
      continue;
    }
  }

  // Process any lifecycle events from non-transcript events
  for (const event of otherEvents) {
    const data = event.data || {};

    if (event.type === 'user_message') {
      const content = normalizeMessageContent(data.content);
      if (!content || !content.trim()) continue;
      blocks.push({
        type: 'user-message',
        id: 'b' + (blockId++),
        seq: event.seq,
        text: content,
        meta: formatMessageMeta(data),
        ts: event.ts,
      });
      continue;
    }

    if (event.type === 'assistant_message') {
      const content = normalizeMessageContent(data.content);
      if (!content || !content.trim()) continue;
      blocks.push({
        type: 'assistant-text',
        id: 'b' + (blockId++),
        seq: event.seq,
        text: content,
        html: renderMarkdown(content),
        meta: formatMessageMeta(data),
        ts: event.ts,
      });
      continue;
    }

    if (event.type === 'system_message') {
      const block = buildRoleMessageBlock('system-message', 'System', data, event.ts, 'b' + (blockId++), event.seq);
      if (block) blocks.push(block);
      continue;
    }

    if (event.type === 'developer_message') {
      const block = buildRoleMessageBlock('developer-message', 'Developer', data, event.ts, 'b' + (blockId++), event.seq);
      if (block) blocks.push(block);
      continue;
    }

    if (event.type === 'context_snapshot') {
      blocks.push(buildContextSnapshotBlock(data, event.ts, 'b' + (blockId++), event.seq));
      continue;
    }

    if (event.type === 'lifecycle') {
      blocks.push({
        type: 'lifecycle',
        id: 'b' + (blockId++),
        seq: event.seq,
        event: data.event || '',
        status: data.status || '',
        ts: event.ts,
      });
    }
  }

  // Sort by event sequence to preserve the recorded stream order.
  blocks.sort((a, b) => {
    const seqA = Number.isInteger(a.seq) ? a.seq : Number.MAX_SAFE_INTEGER;
    const seqB = Number.isInteger(b.seq) ? b.seq : Number.MAX_SAFE_INTEGER;
    if (seqA !== seqB) return seqA - seqB;
    if (a.ts && b.ts) return new Date(a.ts) - new Date(b.ts);
    return 0;
  });

  return blocks;
}

// ── Alpine.js component ──────────────────────────────────────────

function consolePlayer() {
  return {
    // State
    sessionId: null,
    parentSessionId: null,
    events: [],
    blocks: [],
    source: null,
    visible: false,
    loading: false,
    error: null,

    // Pagination
    lastSeq: 0,
    hasMore: false,
    pageSize: 200,

    // Live tail
    liveTail: false,
    ws: null,
    autoScroll: true,

    // Time range filter
    afterTs: '',
    beforeTs: '',

    // Source filter: false = agent-only (exclude intaris), true = all sources
    showAllSources: false,

    // Collapsible state
    expandedTools: {},
    expandedReasoning: {},

    // ── Lifecycle ──

    async open(sessionId, opts = {}) {
      this.sessionId = sessionId;
      this.parentSessionId = null;
      this.events = [];
      this.blocks = [];
      this.source = null;
      this.lastSeq = 0;
      this.hasMore = false;
      this.error = null;
      this.visible = true;
      this.autoScroll = true;
      this.afterTs = opts.afterTs ? this._toLocalDatetime(opts.afterTs) : '';
      this.beforeTs = opts.beforeTs ? this._toLocalDatetime(opts.beforeTs) : '';
      this.showAllSources = false;
      this.expandedTools = {};
      this.expandedReasoning = {};
      this.stopLiveTail();
      // Fetch session details for parent link (best-effort, non-blocking)
      IntarisAPI.getSession(sessionId)
        .then(s => { this.parentSessionId = s.parent_session_id || null; })
        .catch(() => {});
      await this.loadEvents();
      this.processEvents();
      // Fallback: if no agent events found, retry with all sources
      if (this.events.length === 0 && !this.showAllSources) {
        this.showAllSources = true;
        await this.loadEvents();
        this.processEvents();
      }
      this.scrollToBottom();
      this.startLiveTail();
    },

    close() {
      this.visible = false;
      this.sessionId = null;
      this.parentSessionId = null;
      this.events = [];
      this.blocks = [];
      this.stopLiveTail();
    },

    // ── Time range filtering ──

    /**
     * Apply time range filter and reload.
     */
    async applyTimeFilter() {
      this.events = [];
      this.blocks = [];
      this.lastSeq = 0;
      this.hasMore = false;
      this.autoScroll = true;
      await this.loadEvents();
      this.processEvents();
    },

    /**
     * Clear time range filter and reload.
     */
    async clearTimeFilter() {
      this.afterTs = '';
      this.beforeTs = '';
      this.events = [];
      this.blocks = [];
      this.lastSeq = 0;
      this.hasMore = false;
      this.autoScroll = true;
      await this.loadEvents();
      this.processEvents();
    },

    /**
     * Whether a time filter is active.
     */
    get timeFilterActive() {
      return !!(this.afterTs || this.beforeTs);
    },

    /**
     * Convert ISO 8601 timestamp to datetime-local input value.
     */
    _toLocalDatetime(iso) {
      if (!iso) return '';
      return iso.replace(/\.\d+Z?$/, '').replace(/Z$/, '').substring(0, 19);
    },

    /**
     * Convert datetime-local input value to ISO 8601 string.
     */
    _toISOString(local) {
      return local || '';
    },

    /**
     * Toggle between agent-only and all sources, reload events.
     */
    async toggleSources() {
      this.showAllSources = !this.showAllSources;
      this.events = [];
      this.blocks = [];
      this.lastSeq = 0;
      this.hasMore = false;
      this.autoScroll = true;
      await this.loadEvents();
      this.processEvents();
      this.scrollToBottom();
    },

    // ── Data loading ──

    async loadEvents() {
      if (!this.sessionId || this.loading) return;
      this.loading = true;
      this.error = null;

      try {
        const params = {
          after_seq: this.lastSeq,
          limit: this.pageSize,
        };
        if (!this.showAllSources) params.exclude_source = 'intaris';
        if (this.afterTs) params.after_ts = this._toISOString(this.afterTs);
        if (this.beforeTs) params.before_ts = this._toISOString(this.beforeTs);

        const result = await IntarisAPI.getSessionEvents(this.sessionId, params);
        const newEvents = result.events || [];

        const existingSeqs = new Set(this.events.map(e => e.seq));
        for (const event of newEvents) {
          if (!existingSeqs.has(event.seq)) {
            this.events.push(event);
            existingSeqs.add(event.seq);
          }
        }

        if (newEvents.length > 0) {
          this.lastSeq = newEvents[newEvents.length - 1].seq;
        }
        this.hasMore = result.has_more || false;
      } catch (e) {
        this.error = 'Failed to load events: ' + e.message;
      } finally {
        this.loading = false;
      }
    },

    async loadMore() {
      if (this.hasMore && !this.loading) {
        await this.loadEvents();
        this.processEvents();
        if (this.autoScroll) this.scrollToBottom();
      }
    },

    // ── Event processing ──

    processEvents() {
      this.source = detectSource(this.events);
      if (this.source === 'opencode') {
        this.blocks = processOpenCode(this.events);
      } else if (this.source === 'claude-code') {
        this.blocks = processClaudeCode(this.events);
      } else {
        // Generic fallback: just show tool_call + evaluation events
        this.blocks = processOpenCode(this.events);
      }
    },

    // ── Live tail ──

    startLiveTail() {
      if (this.liveTail || !this.sessionId) return;
      this.liveTail = true;

      this.ws = IntarisAPI.connectWebSocket({
        sessionId: this.sessionId,
        onMessage: (data) => {
          if (data.type === 'session_event' && data.event) {
            const event = data.event;
            // Skip intaris-source events unless showing all sources
            if (!this.showAllSources && event.source === 'intaris') return;
            if (!this.events.some(e => e.seq === event.seq)) {
              this.events.push(event);
              if (event.seq > this.lastSeq) this.lastSeq = event.seq;
              this.processEvents();
              if (this.autoScroll) this.scrollToBottom();
            }
          }
        },
        onClose: () => {
          this.liveTail = false;
          this.ws = null;
        },
        onError: () => {
          this.liveTail = false;
          this.ws = null;
        },
      });
    },

    stopLiveTail() {
      this.liveTail = false;
      if (this.ws) {
        this.ws.close();
        this.ws = null;
      }
    },

    toggleLiveTail() {
      if (this.liveTail) {
        this.stopLiveTail();
      } else {
        this.startLiveTail();
      }
    },

    // ── Scroll ──

    scrollToBottom() {
      this.$nextTick(() => {
        const el = this.$refs.consoleList;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    handleScroll() {
      const el = this.$refs.consoleList;
      if (!el) return;
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
      this.autoScroll = atBottom;
      if (atBottom && this.hasMore && !this.loading) {
        this.loadMore();
      }
    },

    // ── Collapsible state ──

    toggleTool(id) {
      this.expandedTools[id] = !this.expandedTools[id];
    },

    isToolExpanded(id) {
      return !!this.expandedTools[id];
    },

    toggleReasoning(id) {
      this.expandedReasoning[id] = !this.expandedReasoning[id];
    },

    isReasoningExpanded(id) {
      return !!this.expandedReasoning[id];
    },

    // ── Display helpers ──

    formatTime(ts) {
      if (!ts) return '';
      return new Date(ts).toLocaleTimeString();
    },

    decisionClass(decision) {
      if (decision === 'approve') return 'badge badge-approve';
      if (decision === 'deny') return 'badge badge-deny';
      if (decision === 'escalate') return 'badge badge-escalate';
      return 'badge badge-low';
    },

    get blockCount() {
      return this.blocks.length;
    },

    sourceLabel() {
      if (this.source === 'opencode') return 'OpenCode';
      if (this.source === 'openclaw') return 'OpenClaw';
      if (this.source === 'claude-code') return 'Claude Code';
      if (this.source === 'cognis') return 'Cognis';
      return 'Unknown';
    },
  };
}
