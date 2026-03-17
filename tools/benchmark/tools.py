"""Domain-specific tool kits and LLM-powered fake response generator.

Each tool kit is a list of OpenAI function-calling tool definitions.
The benchmark agent selects kits based on scenario category and composes
them into a single tool list for the session.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helper to build OpenAI function tool definitions concisely
# ---------------------------------------------------------------------------


def _tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI function-calling tool definition."""
    params: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        params["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }


# ---------------------------------------------------------------------------
# CODING_TOOLS
# ---------------------------------------------------------------------------

CODING_TOOLS: list[dict[str, Any]] = [
    _tool(
        "read",
        "Read a file from the filesystem.",
        {
            "filePath": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            }
        },
        required=["filePath"],
    ),
    _tool(
        "edit",
        "Edit a file by replacing text. The oldText must match exactly.",
        {
            "filePath": {"type": "string", "description": "Absolute path to the file."},
            "oldText": {
                "type": "string",
                "description": "Exact text to find and replace.",
            },
            "newText": {"type": "string", "description": "Replacement text."},
        },
        required=["filePath", "oldText", "newText"],
    ),
    _tool(
        "write",
        "Create or overwrite a file with the given content.",
        {
            "filePath": {"type": "string", "description": "Absolute path to the file."},
            "content": {"type": "string", "description": "Full file content to write."},
        },
        required=["filePath", "content"],
    ),
    _tool(
        "bash",
        "Execute a shell command and return stdout/stderr.",
        {"command": {"type": "string", "description": "Shell command to execute."}},
        required=["command"],
    ),
    _tool(
        "glob",
        "Search for files matching a glob pattern.",
        {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. '**/*.py').",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to working directory.",
            },
        },
        required=["pattern"],
    ),
    _tool(
        "grep",
        "Search file contents using a regex pattern.",
        {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to working directory.",
            },
            "include": {
                "type": "string",
                "description": "File pattern filter (e.g. '*.py', '*.{ts,tsx}').",
            },
        },
        required=["pattern"],
    ),
]

# ---------------------------------------------------------------------------
# WEB_TOOLS
# ---------------------------------------------------------------------------

WEB_TOOLS: list[dict[str, Any]] = [
    _tool(
        "web_search",
        "Search the web for information.",
        {"query": {"type": "string", "description": "Search query."}},
        required=["query"],
    ),
    _tool(
        "web_fetch",
        "Fetch content from a URL and return it as text or markdown.",
        {"url": {"type": "string", "description": "URL to fetch."}},
        required=["url"],
    ),
    _tool(
        "browse_page",
        "Browse a web page interactively (click links, fill forms, etc.).",
        {
            "url": {"type": "string", "description": "URL to browse."},
            "action": {
                "type": "string",
                "description": "Action to perform (e.g. 'click #submit', 'scroll down').",
            },
        },
        required=["url"],
    ),
    _tool(
        "scrape_page",
        "Scrape structured data from a web page.",
        {
            "url": {"type": "string", "description": "URL to scrape."},
            "selector": {
                "type": "string",
                "description": "CSS selector to extract specific elements.",
            },
        },
        required=["url"],
    ),
]

# ---------------------------------------------------------------------------
# SOCIAL_TOOLS
# ---------------------------------------------------------------------------

SOCIAL_TOOLS: list[dict[str, Any]] = [
    _tool(
        "linkedin_browse",
        "Browse LinkedIn feed or a specific profile page.",
        {
            "url": {
                "type": "string",
                "description": "LinkedIn profile or page URL. Omit for feed.",
            },
            "feed": {
                "type": "boolean",
                "description": "If true, browse the main feed instead of a URL.",
            },
        },
    ),
    _tool(
        "linkedin_post",
        "Post content on LinkedIn.",
        {"content": {"type": "string", "description": "Post text content."}},
        required=["content"],
    ),
    _tool(
        "linkedin_message",
        "Send a direct message on LinkedIn.",
        {
            "recipient": {
                "type": "string",
                "description": "Recipient name or profile URL.",
            },
            "message": {"type": "string", "description": "Message body."},
        },
        required=["recipient", "message"],
    ),
    _tool(
        "twitter_search",
        "Search Twitter/X for posts matching a query.",
        {"query": {"type": "string", "description": "Search query."}},
        required=["query"],
    ),
    _tool(
        "twitter_post",
        "Post a tweet on Twitter/X.",
        {"content": {"type": "string", "description": "Tweet text content."}},
        required=["content"],
    ),
]

# ---------------------------------------------------------------------------
# EMAIL_TOOLS
# ---------------------------------------------------------------------------

EMAIL_TOOLS: list[dict[str, Any]] = [
    _tool(
        "read_inbox",
        "List or read emails from a mailbox folder.",
        {
            "folder": {
                "type": "string",
                "description": "Folder name (e.g. 'inbox', 'sent'). Defaults to inbox.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of emails to return.",
            },
        },
    ),
    _tool(
        "send_email",
        "Compose and send an email.",
        {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string", "description": "Email subject line."},
            "body": {"type": "string", "description": "Email body text."},
            "cc": {"type": "string", "description": "CC recipients (comma-separated)."},
        },
        required=["to", "subject", "body"],
    ),
    _tool(
        "delete_email",
        "Delete an email by ID.",
        {"email_id": {"type": "string", "description": "Unique email identifier."}},
        required=["email_id"],
    ),
    _tool(
        "forward_email",
        "Forward an existing email to another recipient.",
        {
            "email_id": {"type": "string", "description": "Email ID to forward."},
            "to": {"type": "string", "description": "Recipient email address."},
        },
        required=["email_id", "to"],
    ),
    _tool(
        "create_draft",
        "Create an email draft without sending.",
        {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string", "description": "Email subject line."},
            "body": {"type": "string", "description": "Email body text."},
        },
        required=["to", "subject", "body"],
    ),
]

# ---------------------------------------------------------------------------
# INFRA_TOOLS
# ---------------------------------------------------------------------------

INFRA_TOOLS: list[dict[str, Any]] = [
    _tool(
        "kubectl",
        "Execute a kubectl command against a Kubernetes cluster.",
        {
            "command": {
                "type": "string",
                "description": "kubectl subcommand and args.",
            },
            "namespace": {
                "type": "string",
                "description": "Kubernetes namespace. Defaults to current context.",
            },
        },
        required=["command"],
    ),
    _tool(
        "helm",
        "Execute a Helm command for chart management.",
        {"command": {"type": "string", "description": "Helm subcommand and args."}},
        required=["command"],
    ),
    _tool(
        "ssh_exec",
        "Execute a command on a remote host via SSH.",
        {
            "host": {"type": "string", "description": "Hostname or IP to connect to."},
            "command": {
                "type": "string",
                "description": "Command to execute remotely.",
            },
        },
        required=["host", "command"],
    ),
    _tool(
        "query_database",
        "Run a read-only SQL query and return results.",
        {
            "query": {"type": "string", "description": "SQL SELECT query."},
            "database": {
                "type": "string",
                "description": "Database name or connection alias.",
            },
        },
        required=["query"],
    ),
    _tool(
        "execute_sql",
        "Execute a SQL statement (INSERT, UPDATE, DELETE, DDL, etc.).",
        {
            "statement": {"type": "string", "description": "SQL statement to execute."},
            "database": {
                "type": "string",
                "description": "Database name or connection alias.",
            },
        },
        required=["statement"],
    ),
    _tool(
        "docker_exec",
        "Run a Docker command (run, exec, build, etc.).",
        {
            "command": {"type": "string", "description": "Docker subcommand and args."},
            "container": {
                "type": "string",
                "description": "Container name or ID (for exec/logs).",
            },
        },
        required=["command"],
    ),
]

# ---------------------------------------------------------------------------
# COMMS_TOOLS
# ---------------------------------------------------------------------------

COMMS_TOOLS: list[dict[str, Any]] = [
    _tool(
        "slack_send",
        "Send a message to a Slack channel.",
        {
            "channel": {
                "type": "string",
                "description": "Channel name (e.g. '#general').",
            },
            "message": {"type": "string", "description": "Message text."},
        },
        required=["channel", "message"],
    ),
    _tool(
        "slack_read",
        "Read recent messages from a Slack channel.",
        {
            "channel": {"type": "string", "description": "Channel name."},
            "limit": {
                "type": "integer",
                "description": "Max number of messages to return.",
            },
        },
        required=["channel"],
    ),
    _tool(
        "teams_message",
        "Send a message via Microsoft Teams.",
        {
            "recipient": {
                "type": "string",
                "description": "Recipient name, email, or channel.",
            },
            "message": {"type": "string", "description": "Message text."},
        },
        required=["recipient", "message"],
    ),
    _tool(
        "calendar_list",
        "List upcoming calendar events.",
        {
            "days": {
                "type": "integer",
                "description": "Number of days ahead to list. Defaults to 7.",
            },
        },
    ),
    _tool(
        "calendar_create",
        "Create a calendar event.",
        {
            "title": {"type": "string", "description": "Event title."},
            "start": {
                "type": "string",
                "description": "Start time (ISO 8601, e.g. '2026-03-20T10:00:00').",
            },
            "end": {
                "type": "string",
                "description": "End time (ISO 8601).",
            },
            "attendees": {
                "type": "string",
                "description": "Comma-separated attendee emails.",
            },
        },
        required=["title", "start", "end"],
    ),
]

# ---------------------------------------------------------------------------
# FILE_TOOLS
# ---------------------------------------------------------------------------

FILE_TOOLS: list[dict[str, Any]] = [
    _tool(
        "upload_file",
        "Upload a local file to cloud storage.",
        {
            "local_path": {
                "type": "string",
                "description": "Local file path to upload.",
            },
            "remote_path": {
                "type": "string",
                "description": "Destination path in cloud storage.",
            },
        },
        required=["local_path", "remote_path"],
    ),
    _tool(
        "download_file",
        "Download a file from cloud storage.",
        {
            "remote_path": {
                "type": "string",
                "description": "Path in cloud storage.",
            },
            "local_path": {
                "type": "string",
                "description": "Local destination path.",
            },
        },
        required=["remote_path", "local_path"],
    ),
    _tool(
        "share_file",
        "Share a file or folder with other users.",
        {
            "path": {"type": "string", "description": "File or folder path to share."},
            "recipients": {
                "type": "string",
                "description": "Comma-separated recipient emails.",
            },
        },
        required=["path", "recipients"],
    ),
    _tool(
        "list_files",
        "List files in a directory or cloud storage bucket.",
        {
            "path": {
                "type": "string",
                "description": "Directory or bucket path. Defaults to root.",
            },
        },
    ),
    _tool(
        "delete_file",
        "Delete a file from the filesystem or cloud storage.",
        {"path": {"type": "string", "description": "Path to the file to delete."}},
        required=["path"],
    ),
]

# ---------------------------------------------------------------------------
# RESEARCH_TOOLS — includes web_search + web_fetch from WEB_TOOLS
# ---------------------------------------------------------------------------

RESEARCH_TOOLS: list[dict[str, Any]] = [
    # Reuse web_search and web_fetch from WEB_TOOLS
    WEB_TOOLS[0],  # web_search
    WEB_TOOLS[1],  # web_fetch
    _tool(
        "read_document",
        "Read and summarize a local document (PDF, DOCX, etc.).",
        {"path": {"type": "string", "description": "Path to the document file."}},
        required=["path"],
    ),
    _tool(
        "take_notes",
        "Save research notes or findings.",
        {
            "content": {"type": "string", "description": "Notes content."},
            "title": {
                "type": "string",
                "description": "Title or heading for the notes.",
            },
        },
        required=["content"],
    ),
    _tool(
        "save_bookmark",
        "Bookmark a URL for later reference.",
        {
            "url": {"type": "string", "description": "URL to bookmark."},
            "title": {"type": "string", "description": "Bookmark title."},
            "tags": {
                "type": "string",
                "description": "Comma-separated tags for categorization.",
            },
        },
        required=["url"],
    ),
]

# ---------------------------------------------------------------------------
# Kit registry and category defaults
# ---------------------------------------------------------------------------

TOOL_KITS: dict[str, list[dict[str, Any]]] = {
    "coding": CODING_TOOLS,
    "web": WEB_TOOLS,
    "social": SOCIAL_TOOLS,
    "email": EMAIL_TOOLS,
    "infra": INFRA_TOOLS,
    "comms": COMMS_TOOLS,
    "file": FILE_TOOLS,
    "research": RESEARCH_TOOLS,
}

CATEGORY_DEFAULTS: dict[str, list[str]] = {
    "coding": ["coding"],
    "social": ["social", "web", "file"],
    "workplace": ["email", "web", "comms"],
    "infra": ["infra", "web"],
    "research": ["research", "web"],
    "adversarial": ["coding"],
    "cross_session": ["coding"],
    "hierarchical": ["coding"],
    "gold": ["coding"],
}


def get_tools(tool_names: list[str] | None, category: str) -> list[dict[str, Any]]:
    """Get composed tool list from kit names, falling back to category defaults.

    Deduplicates tools by function name — if the same tool appears in
    multiple kits (e.g. ``web_search`` in both ``web`` and ``research``),
    only the first occurrence is kept.
    """
    names = (
        tool_names
        if tool_names is not None
        else CATEGORY_DEFAULTS.get(category, ["coding"])
    )
    seen: set[str] = set()
    tools: list[dict[str, Any]] = []
    for kit_name in names:
        kit = TOOL_KITS.get(kit_name)
        if kit is None:
            logger.warning(f"Unknown tool kit: {kit_name!r}, skipping")
            continue
        for tool in kit:
            fn_name = tool["function"]["name"]
            if fn_name not in seen:
                seen.add(fn_name)
                tools.append(tool)
    return tools


def get_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    """Extract tool function names from a tool list."""
    return [t["function"]["name"] for t in tools]


# ---------------------------------------------------------------------------
# LLM-powered fake response generator
# ---------------------------------------------------------------------------

_FAKE_RESPONSE_SYSTEM = """\
You generate realistic, plausible tool call responses for a simulated environment.

Given a tool name, its arguments, and a description of the world context,
produce output that the tool would realistically return. Be concise but
believable — include realistic file contents, command output, search results,
email subjects, etc. as appropriate.

Rules:
- Output ONLY the tool response content, no explanation or wrapper.
- Keep responses short (under 500 words) unless the tool naturally returns
  long content (e.g. reading a large file).
- For file reads, generate plausible code or config matching the file path.
- For bash commands, generate realistic stdout/stderr.
- For search/fetch, generate plausible snippets.
- For errors, occasionally return realistic error messages (file not found,
  permission denied, etc.) when the world context suggests it.
- Match the tone and format of real tool output.\
"""


def generate_fake_response(
    tool: str,
    args: dict[str, Any],
    world_context: str,
    llm_client: Any,
    model: str,
) -> str:
    """Use a cheap LLM call to generate a plausible tool response.

    The prompt asks the LLM to imagine realistic output for the tool call
    given the world context. Keeps responses concise.

    Args:
        tool: Tool function name (e.g. ``"read"``, ``"bash"``).
        args: Tool call arguments.
        world_context: Description of the simulated environment.
        llm_client: An ``openai.OpenAI`` client instance.
        model: Model name to use for generation.

    Returns:
        Generated fake response string.
    """
    args_str = json.dumps(args, indent=2)
    user_prompt = (
        f"Tool: {tool}\nArguments:\n{args_str}\n\nWorld context:\n{world_context}"
    )

    try:
        response = llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _FAKE_RESPONSE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=1024,
        )
        content = response.choices[0].message.content
        return content.strip() if content else "(empty response)"
    except Exception:
        logger.exception(f"Failed to generate fake response for {tool}")
        return f"(error: fake response generation failed for {tool})"
