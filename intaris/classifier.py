"""Tool call classifier for intaris.

Classifies tool calls into READ, WRITE, CRITICAL, or ESCALATE categories
using an explicit read-only allowlist, critical pattern detection,
optional per-tool preference overrides, and filesystem path policy.

Security model: default-deny. Everything not explicitly allowlisted
as read-only goes through LLM evaluation. Unknown tools, third-party
MCP tools, and unrecognized bash commands are all classified as WRITE.

Classification priority (deny always wins):
1.   Session policy deny (deny_tools, deny_commands) → CRITICAL
1.5. Session policy deny_paths (fnmatch on resolved paths) → CRITICAL
2.   Tool preference deny → CRITICAL
3.   Tool preference escalate → ESCALATE
4.   Session policy allow (allow_tools, allow_commands) → READ
5.   Tool preference auto-approve → READ
6.   Critical pattern detection (bash only) → CRITICAL
7.   Built-in read-only allowlist → READ
7.5. Path outside project (not in allow_paths) → WRITE
8.   Default → WRITE
"""

from __future__ import annotations

import fnmatch
import logging
import os
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
    ESCALATE = "escalate"


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
    # Task management tools (metadata only, no side effects)
    "todoread",
    "todowrite",
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
# For namespaced tools, the base tool name is matched after stripping
# the server prefix (colon or double-underscore conventions).
# The OpenCode single-underscore format (server_tool) is ambiguous,
# so known full names are listed explicitly.
_READ_ONLY_MCP_TOOLS: set[str] = {
    # Mnemory tools (agent memory infrastructure — not project-modifying)
    "initialize_memory",
    "add_memory",
    "add_memories",
    "search_memories",
    "find_memories",
    "ask_memories",
    "get_core_memories",
    "get_recent_memories",
    "list_memories",
    "list_categories",
    "update_memory",
    "delete_memory",
    "delete_memories",
    "delete_all_memories",
    "save_artifact",
    "get_artifact",
    "get_artifact_url",
    "list_artifacts",
    "delete_artifact",
    # Thinking / reasoning tools (no side effects)
    "sequentialthinking",
    "sequentialthinking_sequentialthinking",  # OpenCode: server_tool format
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

# ── Path Policy ───────────────────────────────────────────────────────
# Filesystem path awareness for tool call classification.

# Known argument keys that contain file paths.
PATH_ARG_KEYS: tuple[str, ...] = (
    "filePath",
    "file_path",
    "path",
    "directory",
    "dir",
    "folder",
    "filename",
)

# Regex to extract absolute paths from bash command strings.
# Matches sequences starting with / followed by path characters.
# Intentionally simple — catches obvious cases like "cat /etc/shadow"
# without trying to handle all shell constructs.
_BASH_ABSOLUTE_PATH_RE = re.compile(r"(?<![a-zA-Z0-9_:])(/[a-zA-Z0-9_./-]+)")


def extract_paths(args: dict[str, Any]) -> list[str]:
    """Extract file paths from tool arguments.

    Scans known argument keys for string values that look like file paths.

    Args:
        args: Tool arguments dict.

    Returns:
        List of path strings found in args (may be empty).
    """
    paths: list[str] = []
    for key in PATH_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val:
            paths.append(val)
    return paths


def extract_bash_paths(command: str) -> list[str]:
    """Extract absolute paths from a bash command string.

    Uses a simple regex to find absolute paths. Intentionally
    conservative — only catches obvious cases like ``cat /etc/shadow``.
    Does not handle shell variables, tilde expansion, or subshells.

    Args:
        command: Bash command string.

    Returns:
        List of absolute path strings found in the command.
    """
    # Strip quoted strings first to avoid matching paths inside
    # string literals that may be data, not filesystem targets.
    stripped = _strip_quoted_strings(command)
    return _BASH_ABSOLUTE_PATH_RE.findall(stripped)


def resolve_path(path: str, working_directory: str) -> str:
    """Resolve a file path against a working directory.

    Absolute paths are normalized in place. Relative paths are joined
    with the working directory first. Uses ``os.path.normpath`` to
    collapse ``..`` components (prevents traversal bypasses).

    Note: does NOT resolve symlinks (would require filesystem access).

    Args:
        path: File path (absolute or relative).
        working_directory: Session's working directory.

    Returns:
        Normalized absolute path string.
    """
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(working_directory, path))


def is_path_within(resolved_path: str, directory: str) -> bool:
    """Check if a resolved path is within a directory.

    Both paths should already be normalized (via ``os.path.normpath``).
    Uses string prefix matching with a trailing separator to avoid
    false positives (e.g., ``/home/user2`` is not within ``/home/user``).

    Args:
        resolved_path: Normalized absolute path.
        directory: Normalized absolute directory path.

    Returns:
        True if resolved_path is within or equal to directory.
    """
    norm_dir = os.path.normpath(directory)
    norm_path = os.path.normpath(resolved_path)
    # Exact match (path IS the directory)
    if norm_path == norm_dir:
        return True
    # Prefix match with separator
    return norm_path.startswith(norm_dir + os.sep)


def _check_deny_paths(
    resolved_paths: list[str],
    policy: dict[str, Any],
) -> bool:
    """Check if any resolved path matches deny_paths patterns.

    Args:
        resolved_paths: List of normalized absolute paths.
        policy: Session policy dict.

    Returns:
        True if any path matches a deny_paths pattern.
    """
    deny_paths = policy.get("deny_paths", [])
    if not deny_paths:
        return False

    for rp in resolved_paths:
        for pattern in deny_paths:
            if isinstance(pattern, str) and fnmatch.fnmatch(rp, pattern):
                return True
    return False


def _check_allow_paths(
    resolved_paths: list[str],
    policy: dict[str, Any],
) -> bool:
    """Check if ALL resolved paths match allow_paths patterns.

    Args:
        resolved_paths: List of normalized absolute paths.
        policy: Session policy dict.

    Returns:
        True if every path matches at least one allow_paths pattern.
    """
    allow_paths = policy.get("allow_paths", [])
    if not allow_paths:
        return False

    for rp in resolved_paths:
        matched = False
        for pattern in allow_paths:
            if isinstance(pattern, str) and fnmatch.fnmatch(rp, pattern):
                matched = True
                break
        if not matched:
            return False
    return True


def _check_path_policy(
    resolved_paths: list[str],
    working_directory: str,
    policy: dict[str, Any],
) -> Classification | None:
    """Check resolved paths against path policy and project boundary.

    Priority:
    1. deny_paths match → CRITICAL
    2. allow_paths match (all paths) → None (no override)
    3. Any path outside working_directory → WRITE
    4. All paths within project → None (no override)

    Args:
        resolved_paths: List of normalized absolute paths.
        working_directory: Session's working directory.
        policy: Session policy dict (may contain allow_paths, deny_paths).

    Returns:
        Classification override, or None if no policy applies.
    """
    if not resolved_paths:
        return None

    # 1. deny_paths always wins
    if _check_deny_paths(resolved_paths, policy):
        return Classification.CRITICAL

    # 2. allow_paths exempts from out-of-project override
    if _check_allow_paths(resolved_paths, policy):
        return None

    # 3. Check if any path is outside the project
    for rp in resolved_paths:
        if not is_path_within(rp, working_directory):
            return Classification.WRITE

    # 4. All paths within project
    return None


def classify(
    tool: str,
    args: dict[str, Any],
    *,
    session_policy: dict[str, Any] | None = None,
    tool_preferences: dict[str, str] | None = None,
    working_directory: str | None = None,
) -> Classification:
    """Classify a tool call as READ, WRITE, CRITICAL, or ESCALATE.

    Classification priority (deny always wins):
    1.   Session policy deny (deny_tools, deny_commands) → CRITICAL
    1.5. Session policy deny_paths (fnmatch on resolved paths) → CRITICAL
    2.   Tool preference deny → CRITICAL
    3.   Tool preference escalate → ESCALATE
    4.   Session policy allow (allow_tools, allow_commands) → READ
    5.   Tool preference auto-approve → READ
    6.   Critical pattern detection (bash only) → CRITICAL
    7.   Built-in read-only allowlist → READ
    7.5. Path outside project (not in allow_paths) → WRITE
    8.   Default → WRITE (goes to LLM evaluation)

    Args:
        tool: Tool name (e.g., "bash", "edit", "read", "mcp:add_memory").
        args: Tool arguments.
        session_policy: Optional per-session policy with custom rules.
        tool_preferences: Optional per-tool preference overrides mapping
            'server:tool' or 'tool' → preference string.
        working_directory: Optional session working directory for path
            policy enforcement. When set, file paths in tool args are
            resolved against this directory and checked against path
            policy (deny_paths, allow_paths) and project boundary.

    Returns:
        Classification enum value.
    """
    # Step 1: Session policy deny rules (highest priority deny)
    if session_policy:
        deny_override = _check_session_policy_deny(tool, args, session_policy)
        if deny_override is not None:
            return deny_override

    # Step 1.5: Session policy deny_paths (resolved path matching)
    if working_directory and session_policy:
        resolved = _resolve_tool_paths(tool, args, working_directory)
        if resolved and _check_deny_paths(resolved, session_policy):
            return Classification.CRITICAL

    # Step 2: Tool preference deny
    pref = _get_tool_preference(tool, tool_preferences)
    if pref == "deny":
        return Classification.CRITICAL

    # Step 3: Tool preference escalate
    if pref == "escalate":
        return Classification.ESCALATE

    # Step 4: Session policy allow rules
    if session_policy:
        allow_override = _check_session_policy_allow(tool, args, session_policy)
        if allow_override is not None:
            return allow_override

    # Step 5: Tool preference auto-approve
    if pref == "auto-approve":
        return Classification.READ

    # Step 6: Check critical patterns (bash only)
    if tool == "bash":
        command = _extract_bash_command(args)
        if command and _is_critical(command):
            return Classification.CRITICAL

    # Step 7: Check read-only allowlist
    if is_read_only(tool, args):
        # Step 7.5: Path policy check for read-only tools.
        # If the tool would be READ but targets paths outside the project
        # directory, reclassify as WRITE to force LLM evaluation.
        if working_directory:
            resolved = _resolve_tool_paths(tool, args, working_directory)
            if resolved:
                path_override = _check_path_policy(
                    resolved, working_directory, session_policy or {}
                )
                if path_override is not None:
                    return path_override
        return Classification.READ

    # Step 8: Default to WRITE (goes to LLM evaluation)
    return Classification.WRITE


def _resolve_tool_paths(
    tool: str, args: dict[str, Any], working_directory: str
) -> list[str]:
    """Extract and resolve file paths from tool arguments.

    For structured tools (Read, Edit, etc.), extracts paths from known
    argument keys. For read-only bash commands, also extracts absolute
    paths from the command string.

    Args:
        tool: Tool name.
        args: Tool arguments.
        working_directory: Session's working directory for resolution.

    Returns:
        List of resolved (normalized absolute) paths.
    """
    raw_paths = extract_paths(args)

    # For bash commands, also extract absolute paths from the command string
    if tool == "bash":
        command = _extract_bash_command(args)
        if command:
            raw_paths.extend(extract_bash_paths(command))

    if not raw_paths:
        return []

    return [resolve_path(p, working_directory) for p in raw_paths]


def _get_tool_preference(tool: str, preferences: dict[str, str] | None) -> str | None:
    """Look up tool preference with fallback.

    Tries exact match first (e.g., 'server:tool'), then just the
    tool name part after the colon.
    """
    if not preferences:
        return None

    # Exact match (e.g., "tavily:search")
    if tool in preferences:
        return preferences[tool]

    # Fallback: just the tool name after colon (e.g., "search")
    if ":" in tool:
        tool_name = tool.split(":", 1)[1]
        if tool_name in preferences:
            return preferences[tool_name]

    return None


def _extract_bash_command(args: dict[str, Any]) -> str:
    """Extract the command string from bash tool args."""
    # OpenCode/Claude Code use "command" key
    command = args.get("command", "")
    if not command:
        # Some tools use "cmd" key
        command = args.get("cmd", "")
    return str(command).strip()


# Regex to match single-quoted or double-quoted strings.
# Handles escaped quotes inside double-quoted strings (\").
# Single-quoted strings have no escape mechanism in bash.
_QUOTED_STRING_RE = re.compile(r"""'[^']*'|"(?:[^"\\]|\\.)*\"""")


def _strip_quoted_strings(command: str) -> str:
    """Remove quoted string content from a command.

    Replaces single-quoted and double-quoted strings with empty
    placeholders to prevent their content from triggering pattern
    matches. Used before critical pattern detection to avoid false
    positives from keywords appearing in arguments (e.g., "shutdown"
    in a git commit message).
    """
    return _QUOTED_STRING_RE.sub('""', command)


def _is_critical(command: str) -> bool:
    """Check if a bash command matches any critical pattern."""
    # Strip quoted strings to prevent false positives from keywords
    # appearing in arguments (e.g., "shutdown" in a commit message).
    stripped = _strip_quoted_strings(command)
    for pattern in _CRITICAL_PATTERNS:
        if pattern.search(stripped):
            logger.warning("Critical pattern matched: %s", pattern.pattern)
            return True
    return False


def is_read_only(tool: str, args: dict[str, Any]) -> bool:
    """Check if a tool call is read-only.

    Checks the tool name against the built-in read-only allowlist.
    For bash commands, also checks the command string for read-only
    commands, piped chains, and git subcommands.

    This is a pure classification check — it does NOT consider session
    policy, tool preferences, or path policy. Use ``classify()`` for
    the full classification pipeline.

    Args:
        tool: Tool name.
        args: Tool arguments.

    Returns:
        True if the tool call is read-only by its nature.
    """
    # Built-in read-only tools (case-insensitive to handle both
    # OpenCode lowercase and Claude Code capitalized conventions)
    if tool.lower() in _READ_ONLY_TOOLS:
        return True

    # MCP tools (may be prefixed with server name)
    tool_name = tool.split(":")[-1] if ":" in tool else tool
    if tool_name in _READ_ONLY_MCP_TOOLS:
        return True

    # Handle Claude Code double-underscore convention: mcp__server__tool → tool
    if tool.startswith("mcp__") and "__" in tool[5:]:
        tool_name = tool.rsplit("__", 1)[-1]
        if tool_name in _READ_ONLY_MCP_TOOLS:
            return True

    # Handle OpenCode single-underscore convention: server_tool → tool
    # Try progressively shorter suffixes to handle tool names that
    # themselves contain underscores (e.g., "mnemory_get_core_memories"
    # → try "get_core_memories", then "core_memories", etc.)
    if "_" in tool and not tool.startswith("mcp__"):
        parts = tool.split("_")
        for i in range(1, len(parts)):
            candidate = "_".join(parts[i:])
            if candidate in _READ_ONLY_MCP_TOOLS:
                return True

    # Bash commands (case-sensitive — "bash" is always lowercase)
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
        "deny_commands": ["pattern", ...],     # Glob patterns for bash
        "allow_paths": ["pattern", ...],       # Exempt paths from project boundary
        "deny_paths": ["pattern", ...]         # Always deny (fnmatch on resolved)
    }

    Deny rules take priority over allow rules.
    Patterns use simple glob matching (fnmatch), NOT regex.

    Returns:
        Classification override, or None if no policy match.
    """
    deny = _check_session_policy_deny(tool, args, policy)
    if deny is not None:
        return deny
    return _check_session_policy_allow(tool, args, policy)


def _check_session_policy_deny(
    tool: str,
    args: dict[str, Any],
    policy: dict[str, Any],
) -> Classification | None:
    """Check session policy deny rules only."""
    deny_tools = policy.get("deny_tools", [])
    if tool in deny_tools:
        return Classification.CRITICAL

    if tool == "bash":
        command = _extract_bash_command(args)
        deny_commands = policy.get("deny_commands", [])
        for pattern in deny_commands:
            if isinstance(pattern, str) and fnmatch.fnmatch(command, pattern):
                return Classification.CRITICAL

    return None


def _check_session_policy_allow(
    tool: str,
    args: dict[str, Any],
    policy: dict[str, Any],
) -> Classification | None:
    """Check session policy allow rules only."""
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
