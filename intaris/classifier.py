"""Tool call classifier for intaris.

Classifies tool calls into READ, WRITE, or CRITICAL categories using
an explicit read-only allowlist and critical pattern detection.

Security model: default-deny. Everything not explicitly allowlisted
as read-only goes through LLM evaluation. Unknown tools, third-party
MCP tools, and unrecognized bash commands are all classified as WRITE.
"""

from __future__ import annotations

import logging
import re
import shlex
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class Classification(Enum):
    """Tool call classification result."""

    READ = "read"
    WRITE = "write"
    CRITICAL = "critical"


# ── Read-Only Allowlist ───────────────────────────────────────────────
# Conservative: only tools/commands that cannot modify state.

# Built-in tools that are always read-only.
_READ_ONLY_TOOLS: set[str] = {
    "read",
    "glob",
    "grep",
    "search",
    "find",
    "list",
    "get",
    "view",
    "show",
    "describe",
    "explain",
    "help",
    "info",
    "status",
    "whoami",
    "version",
}

# Bash commands that are read-only (no arguments needed for classification).
_READ_ONLY_BASH_COMMANDS: set[str] = {
    "ls",
    "cat",
    "head",
    "tail",
    "find",
    "tree",
    "wc",
    "grep",
    "rg",
    "fd",
    "pwd",
    "echo",
    "which",
    "type",
    "file",
    "stat",
    "du",
    "df",
    "env",
    "printenv",
    "uname",
    "hostname",
    "whoami",
    "id",
    "date",
    "uptime",
    "free",
    "ps",
    "top",
    "less",
    "more",
    "diff",
    "sort",
    "uniq",
    "cut",
    "tr",
    "awk",
    "sed",  # Note: sed is read-only only without -i flag
    "jq",
    "yq",
    "python",  # Classified as WRITE by default; see _is_read_only_bash
    "node",
}

# Git subcommands that are always read-only (no argument checks needed).
_READ_ONLY_GIT_SUBCOMMANDS: set[str] = {
    "status",
    "log",
    "diff",
    "show",
    "describe",
    "rev-parse",
    "rev-list",
    "ls-files",
    "ls-tree",
    "cat-file",
    "name-rev",
    "shortlog",
    "blame",
}

# Git subcommands that require argument-aware checks.
# These are handled in _is_git_subcommand_read_only().
_GIT_SUBCOMMANDS_WITH_CHECKS: set[str] = {
    "branch",
    "remote",
    "tag",
    "stash",
    "config",
}

# MCP tools that are read-only (from Mnemory and common MCP servers).
_READ_ONLY_MCP_TOOLS: set[str] = {
    "search_memories",
    "find_memories",
    "ask_memories",
    "list_memories",
    "get_artifact",
    "get_artifact_url",
    "list_artifacts",
    "list_categories",
    "get_core_memories",
    "get_recent_memories",
    "initialize_memory",
}

# ── Critical Patterns ─────────────────────────────────────────────────
# Commands that are always denied regardless of context.

_CRITICAL_PATTERNS: list[re.Pattern[str]] = [
    # Destructive filesystem operations
    re.compile(
        r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|--recursive\s+--force|-[a-zA-Z]*f[a-zA-Z]*r)\s+/\s*$"
    ),
    re.compile(
        r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|--recursive\s+--force|-[a-zA-Z]*f[a-zA-Z]*r)\s+/[a-z]+\s*$"
    ),
    re.compile(r"\brm\s+-rf\s+\*\s*$"),
    # Disk destruction
    re.compile(r"\bdd\s+.*if=/dev/(zero|random|urandom)"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bfdisk\b"),
    re.compile(r"\bparted\b"),
    # Permission escalation on root
    re.compile(r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)?777\s+/\s*$"),
    re.compile(r"\bchown\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)?\S+\s+/\s*$"),
    # Fork bomb
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"),
    # Dangerous network operations
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh"),
    re.compile(r"\bwget\b.*\|\s*(ba)?sh"),
    # System shutdown/reboot
    re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+[06])\b"),
    # Kernel module manipulation
    re.compile(r"\b(insmod|rmmod|modprobe)\b"),
    # iptables flush (drops all firewall rules)
    re.compile(r"\biptables\s+-F\b"),
]


def classify(
    tool: str,
    args: dict[str, Any],
    *,
    session_policy: dict[str, Any] | None = None,
) -> Classification:
    """Classify a tool call as READ, WRITE, or CRITICAL.

    Classification priority:
    1. Session policy overrides (if provided)
    2. Critical pattern detection (for bash commands)
    3. Read-only allowlist
    4. Default: WRITE (goes to LLM evaluation)

    Args:
        tool: Tool name (e.g., "bash", "edit", "read", "mcp:add_memory").
        args: Tool arguments.
        session_policy: Optional per-session policy with custom rules.

    Returns:
        Classification enum value.
    """
    # Step 1: Check session policy overrides
    if session_policy:
        override = _check_session_policy(tool, args, session_policy)
        if override is not None:
            return override

    # Step 2: Check critical patterns (bash only)
    if tool == "bash":
        command = _extract_bash_command(args)
        if command and _is_critical(command):
            return Classification.CRITICAL

    # Step 3: Check read-only allowlist
    if _is_read_only(tool, args):
        return Classification.READ

    # Step 4: Default to WRITE (goes to LLM evaluation)
    return Classification.WRITE


def _extract_bash_command(args: dict[str, Any]) -> str:
    """Extract the command string from bash tool args."""
    # OpenCode/Claude Code use "command" key
    command = args.get("command", "")
    if not command:
        # Some tools use "cmd" key
        command = args.get("cmd", "")
    return str(command).strip()


def _is_critical(command: str) -> bool:
    """Check if a bash command matches any critical pattern."""
    for pattern in _CRITICAL_PATTERNS:
        if pattern.search(command):
            logger.warning("Critical pattern matched: %s", pattern.pattern)
            return True
    return False


def _is_read_only(tool: str, args: dict[str, Any]) -> bool:
    """Check if a tool call is read-only."""
    # Built-in read-only tools
    if tool in _READ_ONLY_TOOLS:
        return True

    # MCP tools (may be prefixed with server name)
    tool_name = tool.split(":")[-1] if ":" in tool else tool
    if tool_name in _READ_ONLY_MCP_TOOLS:
        return True

    # Bash commands
    if tool == "bash":
        return _is_read_only_bash(args)

    return False


def _is_read_only_bash(args: dict[str, Any]) -> bool:
    """Check if a bash command is read-only.

    Parses the command to extract the base command and checks against
    the read-only allowlist. Special handling for git subcommands,
    sed -i (write), and piped commands.
    """
    command = _extract_bash_command(args)
    if not command:
        return False

    # Handle piped commands: ALL commands in the pipe must be read-only
    if "|" in command:
        parts = command.split("|")
        return all(
            _is_single_command_read_only(part.strip()) for part in parts if part.strip()
        )

    # Handle chained commands (&&, ;) and background (&)
    for sep in ("&&", ";"):
        if sep in command:
            parts = command.split(sep)
            return all(
                _is_single_command_read_only(part.strip())
                for part in parts
                if part.strip()
            )

    # Background execution: 'cmd1 & cmd2' — both must be read-only.
    # Check after && to avoid splitting on && first.
    if " & " in command or command.endswith(" &"):
        parts = command.split("&")
        return all(
            _is_single_command_read_only(part.strip()) for part in parts if part.strip()
        )

    return _is_single_command_read_only(command)


# Shell redirection operators that indicate a write operation.
_REDIRECT_OPS: set[str] = {">", ">>", "2>", "2>>", "&>", "&>>", "<", "<<"}


def _is_single_command_read_only(command: str) -> bool:
    """Check if a single (non-piped, non-chained) command is read-only."""
    # Check for shell redirection BEFORE shlex.split, since shlex treats
    # > as a regular token. We scan the raw command for redirect operators.
    if _has_shell_redirect(command):
        return False

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Malformed command — treat as write for safety
        return False

    if not tokens:
        return False

    base_cmd = tokens[0]

    # Git: check subcommand
    if base_cmd == "git" and len(tokens) > 1:
        subcommand = tokens[1]
        if subcommand in _READ_ONLY_GIT_SUBCOMMANDS:
            return True
        if subcommand in _GIT_SUBCOMMANDS_WITH_CHECKS:
            return _is_git_subcommand_read_only(subcommand, tokens[2:])
        return False

    # sed with -i flag is a write operation
    if base_cmd == "sed" and any(t == "-i" or t.startswith("-i") for t in tokens[1:]):
        return False

    # python/node are not read-only by default (can execute arbitrary code)
    if base_cmd in ("python", "python3", "node"):
        return False

    # Check against read-only bash commands
    return base_cmd in _READ_ONLY_BASH_COMMANDS


def _has_shell_redirect(command: str) -> bool:
    """Check if a command contains shell redirection operators.

    Scans the raw command string for output/input redirection that would
    make an otherwise read-only command into a write operation.
    Examples: 'ls > file.txt', 'echo data >> log', 'cat < input'.

    Uses regex to avoid false positives inside quoted strings.
    """
    # Match redirection operators NOT inside quotes.
    # This is a simplified check — it won't catch all edge cases with
    # nested quoting, but covers the common attack vectors.
    # Pattern: unquoted >, >>, 2>, 2>>, &>, &>>, <, <<
    return bool(
        re.search(
            r"""(?<!['"\\])"""  # Not preceded by quote or escape
            r"(?:"
            r"\d*>>?"  # [n]> or [n]>>
            r"|&>>"  # &>>
            r"|&>"  # &>
            r"|<<?(?!<)"  # < or << (but not <<<)
            r")",
            command,
        )
    )


def _is_git_subcommand_read_only(subcommand: str, remaining: list[str]) -> bool:
    """Check if a git subcommand with specific arguments is read-only.

    Handles git subcommands that can be either read or write depending
    on their arguments (branch, tag, remote, stash, config).

    Args:
        subcommand: The git subcommand (e.g., "branch", "tag").
        remaining: Tokens after the subcommand.
    """
    if subcommand == "stash":
        # Only 'git stash list' and 'git stash show' are read-only
        if remaining and remaining[0] in ("list", "show"):
            return True
        return False

    if subcommand == "config":
        # Only --get, --get-all, --list, -l are read-only
        config_args = set(remaining)
        read_flags = {"--get", "--get-all", "--list", "-l"}
        if config_args & read_flags:
            return True
        return False

    if subcommand == "branch":
        # 'git branch' (no args) → list branches (read)
        # 'git branch -a', '-r', '--list' → list branches (read)
        # 'git branch -v', '-vv' → list with verbose (read)
        # 'git branch <name>' → create branch (write)
        # 'git branch -d/-D <name>' → delete branch (write)
        # 'git branch -m/-M <old> <new>' → rename branch (write)
        if not remaining:
            return True
        read_flags = {
            "-a",
            "-r",
            "--list",
            "-v",
            "-vv",
            "--merged",
            "--no-merged",
            "--contains",
            "--no-contains",
            "--sort",
            "--format",
            "--color",
            "--no-color",
            "--column",
            "--no-column",
        }
        # All remaining args must be read-only flags or their values
        for arg in remaining:
            if arg.startswith("-") and arg not in read_flags:
                return False
            if not arg.startswith("-"):
                # Positional argument = branch name = write operation
                return False
        return True

    if subcommand == "tag":
        # 'git tag' (no args) → list tags (read)
        # 'git tag -l', '--list' → list tags (read)
        # 'git tag -l "pattern"' → list with pattern (read)
        # 'git tag <name>' → create tag (write)
        # 'git tag -d <name>' → delete tag (write)
        if not remaining:
            return True
        if remaining[0] in ("-l", "--list"):
            return True
        return False

    if subcommand == "remote":
        # 'git remote' (no args) → list remotes (read)
        # 'git remote -v' → list with URLs (read)
        # 'git remote show <name>' → show remote details (read)
        # 'git remote get-url <name>' → get URL (read)
        # 'git remote add/remove/rename/set-url → write
        if not remaining:
            return True
        if remaining[0] in ("-v", "--verbose"):
            return True
        if remaining[0] in ("show", "get-url"):
            return True
        return False

    return False


def _check_session_policy(
    tool: str,
    args: dict[str, Any],
    policy: dict[str, Any],
) -> Classification | None:
    """Check session policy for tool classification overrides.

    Policy format:
    {
        "allow_tools": ["tool_name", ...],     # Force READ classification
        "deny_tools": ["tool_name", ...],      # Force CRITICAL classification
        "allow_commands": ["pattern", ...],     # Glob patterns for bash
        "deny_commands": ["pattern", ...]       # Glob patterns for bash
    }

    Deny rules take priority over allow rules.
    Patterns use simple glob matching (fnmatch), NOT regex.

    Returns:
        Classification override, or None if no policy match.
    """
    import fnmatch

    # Check deny rules first (higher priority)
    deny_tools = policy.get("deny_tools", [])
    if tool in deny_tools:
        return Classification.CRITICAL

    if tool == "bash":
        command = _extract_bash_command(args)
        deny_commands = policy.get("deny_commands", [])
        for pattern in deny_commands:
            if isinstance(pattern, str) and fnmatch.fnmatch(command, pattern):
                return Classification.CRITICAL

    # Check allow rules
    allow_tools = policy.get("allow_tools", [])
    if tool in allow_tools:
        return Classification.READ

    if tool == "bash":
        command = _extract_bash_command(args)
        allow_commands = policy.get("allow_commands", [])
        for pattern in allow_commands:
            if isinstance(pattern, str) and fnmatch.fnmatch(command, pattern):
                return Classification.READ

    return None
