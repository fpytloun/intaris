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
    # Intrinsically observational controller tools, not generic service aliases.
    "list_directory",
    "get_current_datetime",
    "skill_load",
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
    "printf",
    "sleep",
    "true",
    "false",
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
    "merge-base",
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
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b"),
    re.compile(r"\bwget\b.*\|\s*(ba)?sh\b"),
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
    "canonicalPath",
    "canonical_path",
    "realPath",
    "real_path",
    "resolvedPath",
    "resolved_path",
)

# Regex to extract absolute paths from bash command strings.
# Matches sequences starting with / followed by path characters.
# Intentionally simple — catches obvious cases like "cat /etc/shadow"
# without trying to handle all shell constructs.
_BASH_ABSOLUTE_PATH_RE = re.compile(r"(?<![a-zA-Z0-9_:])(/[a-zA-Z0-9_./-]+)")

# Read-only shell commands whose non-option operands commonly name files.
# Commands such as ``echo`` are excluded because filename-like tokens are data.
_BASH_FILE_OPERAND_COMMANDS: set[str] = {
    "awk",
    "cat",
    "cut",
    "diff",
    "du",
    "file",
    "grep",
    "head",
    "less",
    "ls",
    "more",
    "rg",
    "sort",
    "stat",
    "sed",
    "tail",
    "tree",
    "uniq",
    "wc",
}

_BASH_COMMAND_WRAPPERS: set[str] = {
    "command",
    "env",
    "time",
}

_SENSITIVE_LITERAL_BASENAMES: set[str] = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".pgpass",
    ".git-credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ecdsa_sk",
    "id_ed25519",
    "id_ed25519_sk",
}

_BENIGN_TEMPLATE_BASENAMES: set[str] = {
    ".env.example",
    ".env.sample",
    ".env.template",
}

_SENSITIVE_BASENAME_GLOBS: tuple[str, ...] = (
    ".env",
    ".env*",
    ".env.*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".pgpass",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
    "*.key",
    "*private*.pem",
    "*privkey*.pem",
    "*-key.pem",
    "*_key.pem",
    "*.key.pem",
)

_SENSITIVE_SUFFIXES: tuple[str, ...] = (".key",)

# SSH private key names (``id_rsa``, ``id_ed25519_sk``, ``id_rsa.bak``), never
# the public ``*.pub`` half.
_PUBLIC_PEM_RE = re.compile(
    r"cert|crt|chain|bundle|public|(?:^|[._-])(?:pub|ca|root)(?:$|[._-])"
)
_SSH_PRIVATE_KEY_RE = re.compile(r"id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?(?:\..+)?")

# Credential directories under a home directory (matched as path segments so
# ``~/.ssh``, ``/home/u/.ssh`` and unresolved relative forms all match; the
# directory itself counts, because grep/glob over it reads its contents).
_SENSITIVE_HOME_DIRS: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".config/gcloud",
    ".config/gh",
)

# Credential files matched on their trailing segments.
_SENSITIVE_HOME_FILES: tuple[str, ...] = (".docker/config.json",)

# System credential and process-environment locations, matched on segments so
# resolved, ``..``-relative and /proc/self or /proc/<pid> forms all match.
_SENSITIVE_SYSTEM_RE = re.compile(
    r"(?:^|/)(?:etc/(?:g?shadow|sudoers(?:\.d(?:/.*)?)?)"
    r"|proc/[^/]+/(?:task/[^/]+/)?(?:environ|mem))$"
)

# The same locations as shell globs, for operands such as /etc/shad* or
# /proc/*/env* that expand to them.
_SENSITIVE_SYSTEM_PATHS: tuple[str, ...] = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d/*",
    "/proc/*/environ",
    "/proc/*/mem",
)

_ENV_OPTIONS_WITH_SEPARATE_VALUES: set[str] = {
    "-u",
    "--unset",
    "-C",
    "--chdir",
    "-a",
    "--argv0",
}

_ENV_OPTIONS_WITH_ATTACHED_VALUES: tuple[str, ...] = (
    "-u",
    "-C",
    "-a",
    "--unset=",
    "--chdir=",
    "--argv0=",
)

# apply_patch envelope headers that introduce filesystem targets.
_APPLY_PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (?P<path>.+)$", re.MULTILINE
)
_APPLY_PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (?P<path>.+)$", re.MULTILINE)
_UNIFIED_DIFF_PATH_RE = re.compile(r"^(?:---|\+\+\+) (?P<path>.+)$", re.MULTILINE)


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


def extract_apply_patch_paths(patch_text: str) -> list[str]:
    """Extract target paths from apply_patch input.

    Parses only structural patch headers, not arbitrary diff body content. For
    move/rename and unified diff patches, both old and new paths are targets.
    """
    paths: list[str] = []
    for match in _APPLY_PATCH_PATH_RE.finditer(patch_text):
        path = match.group("path").strip()
        if path:
            paths.append(path)
    for match in _APPLY_PATCH_MOVE_RE.finditer(patch_text):
        path = match.group("path").strip()
        if path:
            paths.append(path)
    for match in _UNIFIED_DIFF_PATH_RE.finditer(patch_text):
        path = _normalize_unified_diff_path(match.group("path"))
        if path and path not in paths:
            paths.append(path)
    return paths


def _normalize_unified_diff_path(path_header: str) -> str | None:
    """Normalize a unified diff ---/+++ path header."""
    path = path_header.strip().split("\t", 1)[0].strip()
    if not path or path == "/dev/null":
        return None
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


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


def _is_sensitive_path(path: str) -> bool:
    """Return whether a path conventionally contains project secrets."""
    normalized = path.replace("\\", "/").rstrip("/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    if _is_credential_location(normalized, basename):
        return True
    if any(char in basename for char in "*?["):
        if any(
            fnmatch.fnmatchcase(literal, basename)
            for literal in _SENSITIVE_LITERAL_BASENAMES
        ):
            return True
        return any(
            fnmatch.fnmatchcase(basename, pattern)
            for pattern in _SENSITIVE_BASENAME_GLOBS
        )
    if _is_benign_env_template(basename):
        return False
    return (
        basename in _SENSITIVE_LITERAL_BASENAMES
        or (basename.startswith(".env.") and not _is_benign_env_template(basename))
        or basename.endswith(_SENSITIVE_SUFFIXES)
        or (
            # A .pem may hold a private key (e.g. an EC2 key pair) unless its
            # name marks it as a public certificate or CA bundle.
            basename.endswith(".pem")
            and (
                # Private-key markers win over public-certificate markers.
                "private" in basename
                or "privkey" in basename
                or bool(re.search(r"(?:^|[._-])key(?:$|[._-])", basename))
                or not _PUBLIC_PEM_RE.search(basename.removesuffix(".pem"))
            )
        )
        or (
            # id_rsa, id_ed25519_sk, id_rsa.bak and extensionless id_deploy;
            # never the public *.pub half.
            (
                bool(_SSH_PRIVATE_KEY_RE.fullmatch(basename))
                or (basename.startswith("id_") and "." not in basename)
            )
            and not basename.endswith(".pub")
        )
        or normalized == "config/database.yml"
        or normalized.endswith("/config/database.yml")
    )


def _is_credential_location(normalized: str, basename: str) -> bool:
    """Return whether a path names a well-known credential location.

    Works on unresolved (``~/.ssh/id_rsa``), relative and absolute forms, so
    it does not depend on a session working directory.
    """
    if not normalized:
        return False
    full = os.path.normpath(normalized).replace("\\", "/")
    segmented = "/" + full.lstrip("/") + "/"
    for directory in _SENSITIVE_HOME_DIRS:
        if f"/{directory}/" in segmented:
            if directory == ".ssh" and (
                basename == "known_hosts" or basename.endswith(".pub")
            ):
                return False
            return True
    segments = full.split("/")
    for name in _SENSITIVE_HOME_FILES:
        parts = name.split("/")
        # Parent directories match literally; the file name may be a glob
        # that would expand to the credential file.
        if (
            len(segments) >= len(parts)
            and segments[-len(parts) : -1] == parts[:-1]
            and fnmatch.fnmatchcase(parts[-1], segments[-1])
        ):
            return True
    if _SENSITIVE_SYSTEM_RE.search(full):
        return True
    return (
        full.startswith("/")
        and any(char in full for char in "*?[")
        and any(
            full.count("/") == pattern.count("/") and fnmatch.fnmatchcase(pattern, full)
            for pattern in _SENSITIVE_SYSTEM_PATHS
        )
    )


# Options of ps that take a value; the value is not a BSD option cluster.
_PS_VALUE_OPTIONS = {
    "-o",
    "-O",
    "-p",
    "-q",
    "-u",
    "-U",
    "-g",
    "-G",
    "-C",
    "-t",
    "--format",
    "--pid",
    "--ppid",
    "--user",
    "--sort",
}


def _segment_discloses_secrets(segment: list[str]) -> bool:
    """Recognize common environment dumps and secret readers in one command.

    Pragmatic, not obfuscation-proof (docs/purpose-and-threat-model.md): it
    routes ordinary disclosure forms to evaluation instead of the static path.
    """
    segment = [os.path.basename(segment[0]), *segment[1:]]
    tokens = _unwrap_read_command(segment)
    if not tokens:
        # ``env`` (with options or assignments only) prints the environment.
        return segment[0] == "env"
    command, args = os.path.basename(tokens[0]), tokens[1:]
    if command == "printenv":
        return True
    if command == "export":
        return not args or "-p" in args
    if command in {"declare", "typeset"}:
        return not any("=" in a for a in args) and (
            not args or any(a.startswith("-") and ("p" in a or "x" in a) for a in args)
        )
    if command == "set":
        return not args
    if command == "compgen":
        return any(a in {"-v", "-e"} or a in {"variable", "export"} for a in args)
    if command == "ps":
        previous = ""
        for a in args:
            if previous in _PS_VALUE_OPTIONS:
                previous = ""
                continue
            if a == "-E" or (re.fullmatch(r"[A-Za-z]+", a) and "e" in a):
                return True
            previous = a
        return False
    if command == "kubectl":
        secret = any(
            a in {"secret", "secrets"} or a.startswith(("secret/", "secrets/"))
            for a in args
        )
        if not secret:
            return False
        if "describe" in args:
            return True
        output = any(
            a in {"-oyaml", "-ojson"}
            or re.match(r"(?:-o|--output)=?(?:yaml|json|jsonpath|go-template)", a)
            or (
                previous in {"-o", "--output"}
                and re.match(r"(?:yaml|json|jsonpath|go-template)", a)
            )
            for previous, a in zip(["", *args], args)
        )
        return "get" in args and output
    if command in {"docker", "podman"}:
        return "inspect" in args[:2]
    if command == "systemctl" and "show" in args:
        properties = []
        for previous, a in zip(["", *args], args):
            if previous in {"-p", "--property"}:
                properties.append(a)
            elif a.startswith("--property="):
                properties.append(a.split("=", 1)[1])
            elif a.startswith("-p") and a != "-p":
                properties.append(a[2:])
        return not properties or any("Environment" in p for p in properties)
    return False


def reads_secret_location(
    tool: str, args: dict[str, Any], working_directory: str | None
) -> bool:
    """Return whether a call reads credentials/secrets or dumps an environment.

    Independent of the session working directory: such reads are never a
    static read and always go to normal evaluation against intention.
    """
    candidates = resolve_tool_paths(tool, args, working_directory or "")
    if tool.lower() == "glob":
        pattern = args.get("pattern")
        if isinstance(pattern, str) and pattern:
            candidates.append(pattern)
    if any(_is_sensitive_path(path) for path in candidates):
        return True
    if tool != "bash":
        return False
    command = _extract_bash_command(args)
    return any(
        _segment_discloses_secrets(segment)
        for segment in _simple_shell_segments(command)
    )


def _is_benign_env_template(basename: str) -> bool:
    """Return True for committed env templates that should stay cheap reads."""
    return basename in _BENIGN_TEMPLATE_BASENAMES or basename.endswith(
        (".example", ".sample", ".template")
    )


def extract_sensitive_bash_paths(command: str) -> list[str]:
    """Extract sensitive relative or absolute file operands from shell text."""
    paths: list[str] = []
    for segment in re.split(r"\|\||&&|[|;&]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        tokens = _unwrap_read_command(tokens)
        if not tokens or tokens[0] not in _BASH_FILE_OPERAND_COMMANDS:
            continue
        for candidate in _file_operands_for_command(tokens):
            if _is_sensitive_path(candidate):
                paths.append(candidate)
    return paths


def _unwrap_read_command(tokens: list[str]) -> list[str]:
    """Remove harmless command wrappers before path-sensitive inspection."""
    remaining = list(tokens)
    while remaining and remaining[0] in _BASH_COMMAND_WRAPPERS:
        wrapper = remaining.pop(0)
        if wrapper == "env":
            while remaining:
                token = remaining[0]
                if token == "--":
                    remaining.pop(0)
                    break
                if token in {"-S", "--split-string"}:
                    remaining.pop(0)
                    if remaining:
                        split_value = remaining.pop(0)
                        remaining = _split_env_split_string(split_value) + remaining
                    continue
                if token.startswith("--split-string="):
                    split_value = token.split("=", 1)[1]
                    remaining.pop(0)
                    remaining = _split_env_split_string(split_value) + remaining
                    continue
                if token.startswith("-S") and token != "-S":
                    split_value = token[2:]
                    remaining.pop(0)
                    remaining = _split_env_split_string(split_value) + remaining
                    continue
                if token in _ENV_OPTIONS_WITH_SEPARATE_VALUES:
                    remaining.pop(0)
                    if remaining:
                        remaining.pop(0)
                    continue
                if any(
                    token.startswith(prefix) and token != prefix
                    for prefix in _ENV_OPTIONS_WITH_ATTACHED_VALUES
                ):
                    remaining.pop(0)
                    continue
                if token.startswith("-"):
                    remaining.pop(0)
                    continue
                if _looks_like_env_assignment(token):
                    remaining.pop(0)
                    continue
                break
    return remaining


def _split_env_split_string(value: str) -> list[str]:
    """Parse GNU env -S/--split-string command text into shell tokens."""
    try:
        return shlex.split(value)
    except ValueError:
        return []


def _looks_like_env_assignment(token: str) -> bool:
    """Return True for VAR=value prefixes used before env-wrapped commands."""
    if "=" not in token:
        return False
    name, _ = token.split("=", 1)
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _file_operands_for_command(tokens: list[str]) -> list[str]:
    """Return likely filesystem operands for read-only shell commands."""
    command = tokens[0]
    args = tokens[1:]
    if command in {"grep", "rg"}:
        return _grep_file_operands(args)
    if command == "sed":
        return _sed_file_operands(args)
    if command == "awk":
        return _awk_file_operands(args)
    return _generic_file_operands(args)


def _generic_file_operands(args: list[str]) -> list[str]:
    """Return non-option operands, skipping common option values."""
    operands: list[str] = []
    skip_next = False
    for index, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            operands.extend(arg for arg in args[index + 1 :] if arg)
            break
        if token.startswith("-"):
            if token in {"-d", "-n", "-c", "-m", "--label", "-L"}:
                skip_next = True
            continue
        operands.append(token)
    return operands


def _grep_file_operands(args: list[str]) -> list[str]:
    """Return grep/rg file operands while skipping the search pattern."""
    operands: list[str] = []
    pattern_seen = False
    pending_value: str | None = None
    for index, token in enumerate(args):
        if pending_value is not None:
            if pending_value == "file":
                operands.append(token)
                pattern_seen = True
            elif pending_value == "pattern":
                pattern_seen = True
            pending_value = None
            continue
        if token == "--":
            remaining = args[index + 1 :]
            if not pattern_seen and remaining:
                remaining = remaining[1:]
            operands.extend(arg for arg in remaining if arg)
            break
        if token in {"-f", "--file"}:
            pending_value = "file"
            continue
        attached_file = _attached_option_value(token, "-f", "--file")
        if attached_file is not None:
            if attached_file:
                operands.append(attached_file)
                pattern_seen = True
            continue
        if token in {"-e", "--regexp"}:
            pending_value = "pattern"
            continue
        if _attached_option_value(token, "-e", "--regexp") is not None:
            pattern_seen = True
            continue
        if token in {
            "--exclude",
            "--include",
            "--exclude-dir",
            "--include-dir",
            "--label",
            "--binary-files",
            "--devices",
            "--directories",
            "-m",
            "-A",
            "-B",
            "-C",
        }:
            pending_value = "option"
            continue
        if token.startswith("-"):
            continue
        if not pattern_seen:
            pattern_seen = True
            continue
        operands.append(token)
    return operands


def _sed_file_operands(args: list[str]) -> list[str]:
    """Return sed input file operands while skipping scripts and options."""
    operands: list[str] = []
    script_seen = False
    pending_value: str | None = None
    for index, token in enumerate(args):
        if pending_value is not None:
            if pending_value == "file":
                operands.append(token)
            script_seen = True
            pending_value = None
            continue
        if token == "--":
            remaining = args[index + 1 :]
            if not script_seen and remaining:
                remaining = remaining[1:]
            operands.extend(arg for arg in remaining if arg)
            break
        if token in {"-f", "--file"}:
            pending_value = "file"
            continue
        attached_file = _attached_option_value(token, "-f", "--file")
        if attached_file is not None:
            if attached_file:
                operands.append(attached_file)
                script_seen = True
            continue
        if token in {"-e", "--expression"}:
            pending_value = "script"
            continue
        if _attached_option_value(token, "-e", "--expression") is not None:
            script_seen = True
            continue
        if token.startswith("-"):
            continue
        if not script_seen:
            script_seen = True
            continue
        operands.append(token)
    return operands


def _awk_file_operands(args: list[str]) -> list[str]:
    """Return awk file operands while skipping the program and option values."""
    operands: list[str] = []
    program_seen = False
    pending_value: str | None = None
    for index, token in enumerate(args):
        if pending_value is not None:
            if pending_value == "file":
                operands.append(token)
                program_seen = True
            pending_value = None
            continue
        if token == "--":
            remaining = args[index + 1 :]
            if not program_seen and remaining:
                remaining = remaining[1:]
            operands.extend(arg for arg in remaining if arg)
            break
        if token == "-f":
            pending_value = "file"
            continue
        attached_file = _attached_option_value(token, "-f")
        if attached_file is not None:
            if attached_file:
                operands.append(attached_file)
                program_seen = True
            continue
        if token == "-v":
            pending_value = "var"
            continue
        if token.startswith("-"):
            continue
        if not program_seen:
            program_seen = True
            continue
        if _looks_like_env_assignment(token):
            continue
        operands.append(token)
    return operands


def _attached_option_value(
    token: str,
    short_option: str,
    long_option: str | None = None,
) -> str | None:
    """Return an attached option value such as ``-f.env`` or ``--file=.env``."""
    if long_option and token.startswith(f"{long_option}="):
        return token.split("=", 1)[1]
    if token.startswith(short_option) and token != short_option:
        return token[len(short_option) :]
    return None


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

    # Secret-bearing paths still require evaluator review, even when they are
    # inside the project or covered by an allow_paths boundary.
    if any(_is_sensitive_path(path) for path in resolved_paths):
        return Classification.WRITE

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
        resolved = resolve_tool_paths(tool, args, working_directory)
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
            resolved = resolve_tool_paths(tool, args, working_directory)
            if resolved:
                path_override = _check_path_policy(
                    resolved, working_directory, session_policy or {}
                )
                if path_override is not None:
                    return path_override
        # Credential/secret reads and environment dumps are never a static
        # read, with or without a project directory.
        if reads_secret_location(tool, args, working_directory):
            return Classification.WRITE
        return Classification.READ

    # Step 8: Default to WRITE (goes to LLM evaluation)
    return Classification.WRITE


def name_effect(tool: str) -> str | None:
    """An additive controller witness from the final tool-name verb."""
    segment = re.split(r"[.:/]|__", tool.lower())[-1]
    verb = re.split(r"[_-]", segment)[0]
    if verb in {"delete", "remove", "purge", "trash"}:
        return "delete"
    if verb in {"send", "post", "publish", "push", "deploy"}:
        return "transmit"
    if verb in {"write", "update", "edit", "create", "move", "rename"}:
        return "write"
    if verb in {
        "read",
        "get",
        "list",
        "search",
        "inspect",
        "describe",
        "show",
        "fetch",
    }:
        return "read"
    return None


_DISCLOSURE_COMMANDS = {
    "git",
    "printenv",
    "env",
    "echo",
    "cat",
    "less",
    "more",
    "head",
    "tail",
}
_SECRET_ARGUMENT_PATTERNS = (
    "token",
    "tokens",
    "secret",
    "secrets",
    "password",
    "passwords",
    "passwd",
    "credential",
    "credentials",
    "api_key",
    "private_key",
    ".pem",
    ".p12",
    ".key",
    ".env",
    "id_rsa",
)


# Git global options that consume the next token, used to locate grep patterns.
_GIT_GLOBAL_VALUE_OPTIONS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--super-prefix",
}
_GIT_GREP_VALUE_OPTIONS = {
    "-f",
    "-A",
    "-B",
    "-C",
    "-m",
    "--after-context",
    "--before-context",
    "--context",
    "--max-count",
    "--max-depth",
    "--threads",
}


def _git_subcommand_index(arguments: list[str]) -> int:
    """Locate the subcommand without treating global option values as operands."""
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 2 if arguments[index] in _GIT_GLOBAL_VALUE_OPTIONS else 1
    return index


def _git_grep_pattern_indexes(arguments: list[str]) -> set[int]:
    """Indexes of `git grep` search patterns among the arguments after `git`.

    A search pattern is text to look for, not a file whose contents are
    printed, so a word like `token` in it is not a disclosure signal. Only
    `-e`/`--regexp` values, or the first positional when no `-e` is given,
    are patterns; tree-ish, pathspecs and `-f` pattern files stay checked.
    """
    clean = [argument.strip("'\"") for argument in arguments]
    index = _git_subcommand_index(clean)
    if index >= len(clean) or clean[index] != "grep":
        return set()
    patterns: set[int] = set()
    explicit = False
    index += 1
    while index < len(clean):
        argument = clean[index]
        if argument == "--":
            break
        if argument in {"-e", "--regexp"}:
            if index + 1 < len(clean):
                patterns.add(index + 1)
            explicit = True
            index += 2
            continue
        if argument.startswith("--regexp=") or (
            argument.startswith("-e") and len(argument) > 2
        ):
            patterns.add(index)
            explicit = True
            index += 1
            continue
        if argument in _GIT_GREP_VALUE_OPTIONS:
            if argument == "-f":
                explicit = True
            index += 2
            continue
        if argument.startswith("-f") and len(argument) > 2:
            explicit = True
        if argument.startswith("-"):
            index += 1
            continue
        if not explicit:
            patterns.add(index)
        break
    return patterns


def _git_disclosure_arguments(arguments: list[str]) -> list[str]:
    """Select operands of Git operations that can output secret-bearing content.

    Branch names, commit messages and authentication configuration used by a
    write are not disclosure evidence. Unknown operations still undergo normal
    assessment; this witness does not grant permission or classify them as reads.
    """
    clean = [argument.strip("'\"") for argument in arguments]
    index = _git_subcommand_index(clean)
    if index >= len(clean):
        return []
    subcommand = clean[index]
    operands = clean[index + 1 :]
    if subcommand == "credential":
        # `fill` outputs credentials rather than merely naming a credential helper.
        return clean[index:] if operands[:1] == ["fill"] else []
    if subcommand == "stash":
        if operands[:1] != ["show"]:
            return []
        operands = operands[1:]
    elif subcommand in {"log", "reflog"}:
        if not any(
            a
            in {
                "-p",
                "-u",
                "--patch",
                "--patch-with-stat",
                "--patch-with-raw",
                "-c",
                "--cc",
                "--dd",
                "--remerge-diff",
                "-W",
                "--function-context",
                "--word-diff",
                "--color-words",
                "--unified",
            }
            or a.startswith(
                ("-U", "-L", "--unified=", "--word-diff=", "--color-words=")
            )
            for a in operands
        ):
            return []
    elif subcommand == "config":
        # Setters consume values; getters may print a secret from a named key.
        positional = []
        skip_value = False
        for argument in operands:
            if skip_value:
                skip_value = False
            elif argument in {"--file", "-f", "--blob", "--type", "--default"}:
                skip_value = True
            elif not argument.startswith("-"):
                positional.append(argument)
        if not any(
            a
            in {
                "get",
                "list",
                "--get",
                "--get-all",
                "--get-regexp",
                "--get-urlmatch",
                "--list",
                "-l",
            }
            for a in operands
        ) and (
            len(positional) != 1
            or any(
                a
                in {
                    "--add",
                    "--unset",
                    "--unset-all",
                    "--replace-all",
                    "--rename-section",
                    "--remove-section",
                    "--edit",
                    "-e",
                }
                for a in operands
            )
        ):
            return []
    elif subcommand not in {"show", "diff", "grep", "blame", "cat-file", "archive"}:
        return []
    patterns = _git_grep_pattern_indexes(clean) if subcommand == "grep" else set()
    result = []
    positional = False
    skip_value = False
    metadata_only = False
    for position in range(index + 1, len(clean)):
        argument = clean[position]
        if argument == "--" and not positional:
            positional = True
            continue
        if not positional:
            if skip_value:
                skip_value = False
                continue
            if position in patterns:
                continue
            # Pickaxe options consume a query string, not a filename. Optional
            # values (e.g. --pretty) must use '=' and cannot consume the next path.
            if argument in {"-S", "-G"}:
                skip_value = True
                continue
            if argument.startswith("-"):
                if subcommand in {"log", "reflog"} and argument.startswith("-L"):
                    result.append(argument[2:])
                if subcommand == "diff" and argument in {
                    "--name-only",
                    "--name-status",
                    "--quiet",
                }:
                    metadata_only = True
                # -f names a grep pattern file, which remains a read target.
                if (
                    subcommand == "grep"
                    and argument.startswith("-f")
                    and len(argument) > 2
                ):
                    result.append(argument[2:])
                continue
        result.append(argument)
    return [] if metadata_only else result


def secret_argument_disclosure(command: str) -> bool:
    """Recognize explicit secret-bearing output arguments without executing shell."""
    try:
        # Retain quotes so an argument such as ";" is not a shell boundary.
        lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        if index and not (
            tokens[index - 1] and all(c in ";&|(\n" for c in tokens[index - 1])
        ):
            continue
        name = os.path.basename(token.strip("'\""))
        if name not in _DISCLOSURE_COMMANDS:
            continue
        segment = []
        for argument in tokens[index + 1 :]:
            if argument and all(char in ";&|()<>\n" for char in argument):
                break
            segment.append(argument)
        if name == "env":
            child = _unwrap_read_command(
                ["env", *(argument.strip("'\"") for argument in segment)]
            )
            if child:
                # With a command, env executes it instead of printing its
                # environment. Inspect the child operation, not all its args.
                name = os.path.basename(child[0])
                if name not in _DISCLOSURE_COMMANDS | {
                    "sh",
                    "bash",
                    "zsh",
                    "dash",
                    "ksh",
                }:
                    continue
                segment = child[1:]
        candidates = _git_disclosure_arguments(segment) if name == "git" else segment
        for argument in candidates:
            if any(
                re.search(
                    ("" if pattern.startswith(".") else r"(?<![a-z0-9])")
                    + re.escape(pattern)
                    + r"(?![a-z0-9])",
                    argument.lower(),
                )
                for pattern in _SECRET_ARGUMENT_PATTERNS
            ):
                return True
    return False


def observe_hybrid(
    tool: str,
    args: dict[str, Any],
    *,
    policy: dict,
    working_directory: str | None = None,
    tool_preferences: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Separate actual static observations from configured permission shortcuts."""
    paths = resolve_tool_paths(tool, args, working_directory or "")
    factual = classify(tool, args, working_directory=working_directory)
    pref = _get_tool_preference(tool, tool_preferences)
    facts = {
        "tool": tool,
        "command": _extract_bash_command(args) if tool == "bash" else None,
        "paths": paths,
        "paths_complete": tool in {"read", "edit", "write", "apply_patch"},
        "operation": "read" if is_read_only(tool, args) else "unknown",
        "static_read": factual == Classification.READ,
        "configured_deny": bool(
            _check_session_policy_deny(tool, args, policy) is not None
            or (paths and _check_deny_paths(paths, policy))
            or pref == "deny"
        ),
        "configured_allow": bool(
            _check_session_policy_allow(tool, args, policy) is not None
            or pref == "auto-approve"
        ),
        "configured_review": pref == "escalate",
    }
    if factual == Classification.CRITICAL:
        facts["risk"] = "critical"
    if facts["operation"] == "read" and paths and working_directory:
        path_result = _check_path_policy(paths, working_directory, policy)
        if path_result in {None, Classification.READ}:
            facts["static_read"] = True
    if tool == "bash" and secret_argument_disclosure(facts["command"] or ""):
        facts.update(
            static_read=False,
            operation="disclose",
            effects=["disclose"],
            secret_argument=True,
            risk="critical" if facts.get("risk") == "critical" else "high",
        )
    if any(_is_sensitive_path(path) for path in paths) or reads_secret_location(
        tool, args, working_directory
    ):
        facts["static_read"] = False
    from intaris.effect_scope import external_action

    facts["external_or_irreversible"] = external_action(tool, args, paths=paths)
    if tool == "bash":
        operands = [
            token
            for segment in _read_segments(facts["command"] or "")
            for token in segment
        ]
        if any(any(c in token for c in "*?[]{}~") for token in operands) and policy.get(
            "deny_paths"
        ):
            facts["static_read"] = False
        if any(re.search(r"(?:^|/)\.\*", token) for token in operands):
            facts["static_read"] = False
        if any("{" in token or "}" in token for token in operands):
            # Brace alternatives can spell explicit secret filenames that the
            # literal target check cannot see (HEAD:.{env,env.local}).
            facts["static_read"] = False
    return facts


def resolve_tool_paths(
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

    # apply_patch stores target paths inside the patch envelope rather than
    # in a conventional file_path/path argument.
    if tool == "apply_patch":
        patch_text = args.get("patchText")
        if isinstance(patch_text, str) and patch_text:
            raw_paths.extend(extract_apply_patch_paths(patch_text))

    # For bash commands, also extract absolute paths from the command string
    if tool == "bash":
        command = _extract_bash_command(args)
        if command:
            raw_paths.extend(extract_bash_paths(command))
            raw_paths.extend(extract_sensitive_bash_paths(command))
            for segment in _simple_shell_segments(command):
                tokens = _unwrap_read_command(segment)
                if tokens and tokens[0] in _BASH_FILE_OPERAND_COMMANDS:
                    raw_paths.extend(_file_operands_for_command(tokens))

    paths = [resolve_path(p, working_directory) for p in raw_paths]
    if tool == "bash":
        for segment in _read_segments(_extract_bash_command(args)):
            if _system_command_name(segment[0]) != "git":
                continue
            directory = working_directory
            index = 1
            while index < len(segment):
                if segment[index] == "--no-pager":
                    index += 1
                elif segment[index] == "-C" and index + 1 < len(segment):
                    # Git applies repeated -C paths relative to the previous one.
                    if segment[index + 1]:
                        directory = resolve_path(segment[index + 1], directory)
                        paths.append(directory)
                    index += 2
                else:
                    break
            # Only the routine-read grammar supplies Git operand semantics.
            # Write commands can carry arbitrary text (for example commit -m).
            if not verified_read_only_bash({"command": shlex.join(segment)}):
                continue
            positional = False
            patterns = _git_grep_pattern_indexes(segment[1:])
            for position in range(index + 1, len(segment)):
                operand = segment[position]
                if operand == "--" and not positional:
                    positional = True
                    continue
                if not positional and (
                    operand.startswith("-") or position - 1 in patterns
                ):
                    continue
                # Retain both literal paths and revision:path candidates.
                targets = [operand]
                if not positional and ":" in operand:
                    revision, target = operand.split(":", 1)
                    if re.fullmatch(r"[A-Za-z0-9_./@^~-]+", revision) and target:
                        targets.append(target)
                paths.extend(resolve_path(target, directory) for target in targets)
    return paths


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


def _simple_shell_segments(command: str) -> list[list[str]]:
    """Tokenize only the existing simple command subset; no script interpretation."""
    if "\n" in command or "<<" in command:
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        segments: list[list[str]] = [[]]
        for token in lexer:
            if token and all(char in "|;&" for char in token):
                segments.append([])
            else:
                segments[-1].append(token)
        return [segment for segment in segments if segment]
    except ValueError:
        return []


def _is_critical(command: str) -> bool:
    """Match command positions, not keywords embedded in documents or filenames."""
    command = _critical_command_view(command)
    from intaris.shell_observations import substitutions

    command, nested_commands = substitutions(command)
    stripped = _strip_quoted_strings(command)
    if stripped.lstrip().startswith(":()"):
        return bool(_CRITICAL_PATTERNS[9].search(stripped))
    segments = _simple_shell_segments(command)
    if not segments:
        # Unsupported forms still require assessment. Scan actual command
        # positions/substitutions, not arbitrary whole-text keyword matches.
        if "\n" in command:
            if any(_is_critical(line) for line in command.splitlines() if line.strip()):
                return True
        return any(_is_critical(nested) for nested in nested_commands)
    for segment in segments:
        tokens = _unwrap_read_command(segment)
        if tokens and tokens[0] == "sudo":
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
        if not tokens:
            continue
        executable = os.path.basename(tokens[0])
        if executable in {"sh", "bash", "zsh", "dash"} and "-c" in tokens:
            index = tokens.index("-c") + 1
            if index < len(tokens) and _is_critical(tokens[index]):
                return True
        normalized = " ".join([executable, *tokens[1:]])
        for pattern in _CRITICAL_PATTERNS:
            if pattern.match(normalized):
                return True
        if executable in {"curl", "wget"} and re.search(r"\|\s*(?:ba)?sh\b", stripped):
            return True
    return any(_is_critical(nested) for nested in nested_commands)


def _critical_command_view(command: str) -> str:
    """Remove only supported inert cat/tee quoted-heredoc data, never code."""
    lines = command.splitlines(keepends=True)
    result = []
    index = 0
    header = re.compile(
        r"^\s*(?:cat|tee)(?:\s+>{1,2}\s*[^;&|$`\s]+)?"
        r"\s*<<(-?)\s*(['\"])([A-Za-z_]\w*)\2"
        r"(?:\s+>{1,2}\s*[^;&|$`\s]+)?\s*$"
    )
    while index < len(lines):
        match = header.fullmatch(lines[index].rstrip("\n"))
        if match:
            end = index + 1
            while end < len(lines):
                marker = lines[end].rstrip("\r\n")
                if match[1]:
                    marker = marker.lstrip("\t")
                if marker == match[3]:
                    break
                end += 1
            if end < len(lines):
                result.append("cat\n")
                index = end + 1
                continue
        # Other syntactically delimited here-documents are not an executable
        # keyword catalogue. For an unknown receiver, only command substitutions
        # are established execution; the body otherwise needs semantic assessment.
        generic = re.search(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1", lines[index])
        if generic and not re.search(r"\b(?:sh|bash|zsh|dash)\b", lines[index]):
            end = index + 1
            while end < len(lines) and lines[end].strip() != generic[2]:
                end += 1
            if end < len(lines):
                result.append(lines[index])
                if not generic[1]:
                    for body_line in lines[index + 1 : end]:
                        for first, second in re.findall(
                            r"\$\(([^()]*)\)|`([^`]*)`", body_line
                        ):
                            result.append((first or second) + "\n")
                index = end + 1
                continue
        result.append(lines[index])
        index += 1
    return "".join(result)


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
    if tool.lower() in _READ_ONLY_TOOLS or (
        tool.lower() != "bash" and verified_read_only(tool, args)
    ):
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
    segments = _read_segments(_extract_bash_command(args), legacy=True)
    return bool(segments) and all(
        _is_single_command_read_only(shlex.join(tokens)) for tokens in segments
    )


def _read_segments(command: str, *, legacy: bool = False) -> list[list[str]]:
    """Parse only a small shell grammar; never execute or expand input."""
    from intaris.shell_observations import unquoted_redirect

    # Discarding output (>/dev/null, 2>/dev/null, &>/dev/null) and merging
    # streams (2>&1) write nothing persistent; drop them before parsing.
    command = re.sub(
        r"(?:(?<=\s)|^)(?:[0-9]?|&)>>?\s*/dev/null(?=\s|$|[;&|])", " ", command or ""
    )
    command = re.sub(r"(?<=\s)[0-9]?>&[0-9](?=\s|$|[;&|])", " ", command).strip()
    forbidden = ("\n", "\r", "$", "`", "\\", "(", ")", "<")
    if (
        not command
        or any(c in command for c in forbidden)
        or (unquoted_redirect(command) if legacy else any(c in command for c in "<>"))
    ):
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        segments, current = [], []
        for token in lexer:
            if token in (
                {";", "&&", "||", "|", "&"} if legacy else {";", "&&", "||", "|"}
            ):
                if not current:
                    return []
                segments.append(current)
                current = []
            elif token and all(c in ";&|" for c in token):
                return []
            else:
                current.append(token)
        if not current:
            return []
        return [*segments, current]
    except ValueError:
        return []


def _system_command_name(command: str) -> str:
    """Normalize bare names and supported system binary paths consistently."""
    for prefix in ("/usr/local/bin/", "/usr/bin/", "/bin/"):
        if command.startswith(prefix) and "/" not in command[len(prefix) :]:
            return command[len(prefix) :]
    return command


def _git_read_arguments(arguments: list[str]) -> list[str]:
    """Remove only non-executable global options before a Git subcommand."""
    while arguments:
        if arguments[0] == "--no-pager":
            arguments = arguments[1:]
        elif arguments[0] == "-C" and len(arguments) >= 2:
            arguments = arguments[2:]
        else:
            break
    return arguments


def _git_grep_opens_pager(arguments: list[str]) -> bool:
    """Recognize grep's executable pager option, including short flag bundles."""
    for argument in arguments:
        if argument == "--":
            break
        key = argument.split("=", 1)[0]
        if (
            key.startswith("--")
            and len(key) > 2
            and "--open-files-in-pager".startswith(key)
        ):
            return True
        if (
            argument.startswith("-")
            and not argument.startswith("--")
            and "O" in argument
        ):
            return True
    return False


def verified_read_only_bash(args: dict[str, Any]) -> bool:
    """Pragmatic routine-read witness under docs/git-static-trust.md."""
    segments = _read_segments(_extract_bash_command(args))
    if not segments:
        return False
    for tokens in segments:
        cmd, *arguments = tokens
        cmd = _system_command_name(cmd)
        if cmd == "git":
            arguments = _git_read_arguments(arguments)
        if cmd == "git" and arguments[:1] == ["branch"]:
            # Listing only; any name argument would create a branch.
            if not set(arguments[1:]) <= {
                "--show-current",
                "--list",
                "-a",
                "--all",
                "-r",
                "--remotes",
                "-v",
                "-vv",
                "--verbose",
            }:
                return False
        elif cmd == "git":
            # `-C <dir>` only changes directory and `--no-pager` only drops
            # paging; neither changes what a read subcommand does. `-c` is
            # not skipped: settings such as core.fsmonitor execute commands.
            if not arguments or arguments[0] not in {
                "status",
                "rev-parse",
                "log",
                "diff",
                "show",
                "describe",
                "rev-list",
                "ls-files",
                "ls-tree",
                "shortlog",
                "config",
                "remote",
                "merge-base",
                "cat-file",
                "grep",
                "blame",
            }:
                return False
            # `git grep -O <cmd>` runs a pager command on the matches.
            if arguments[0] == "grep" and _git_grep_opens_pager(arguments[1:]):
                return False
            # See docs/git-static-trust.md: existing config is trusted, command
            # line config/executable overrides and transports are not.
            if any(
                re.search(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|ext::|^[^/]+@[^:]+:)", a)
                for a in arguments
            ):
                return False
            if arguments[0] == "config" and (
                len(arguments) < 2 or arguments[1] not in {"--get", "--list"}
            ):
                return False
            if arguments[0] == "remote" and any(
                a not in {"-v", "--verbose"} for a in arguments[1:]
            ):
                return False
            # Read subcommands accept ordinary formatting/query options without
            # enumerating every harmless Git spelling. Block concrete overrides
            # and writes instead; no per-placeholder helper-chain reasoning.
            options = (
                arguments[1 : arguments.index("--")]
                if "--" in arguments
                else arguments[1:]
            )
            blocked = {
                "--output",
                "--config-env",
                "--exec-path",
                "--upload-pack",
                "--receive-pack",
                "--work-tree",
                "--exec",
                "--batch-command",
                "--edit",
                "--unset",
                "--unset-all",
                "--add",
                "--replace-all",
                "--rename-section",
                "--remove-section",
                "--file",
                "--blob",
            }
            for option in options:
                key = option.split("=", 1)[0]
                if (
                    key.startswith("--")
                    and len(key) > 2
                    and any(flag.startswith(key) for flag in blocked)
                ):
                    return False
                if option.startswith(("-o", "-c", "-u")) and not option.startswith(
                    "--"
                ):
                    return False
                if key.startswith("--git-dir") and arguments[0] != "rev-parse":
                    return False
            if arguments[0] == "config" and any(
                a.startswith("-")
                and a
                not in {"--get", "--list", "--show-origin", "--show-scope", "--null"}
                for a in options
            ):
                return False
        elif cmd == "ps":
            # BSD e/eww/auxe and configurable formats can expose process
            # environments. Do not infer disclosure safety from mutation alone.
            if arguments not in ([], ["aux"], ["ax"], ["-ef"], ["-e"], ["-f"]):
                return False
        elif cmd == "journalctl":
            # Reading the journal; maintenance flags rotate, vacuum or flush it.
            # journalctl accepts unambiguous long-option abbreviations.
            maintenance = {
                "--rotate",
                "--vacuum-size",
                "--vacuum-time",
                "--vacuum-files",
                "--flush",
                "--sync",
                "--relinquish-var",
                "--smart-relinquish-var",
                "--setup-keys",
                "--update-catalog",
            }
            for a in arguments:
                key = a.split("=", 1)[0]
                if (
                    key.startswith("--")
                    and len(key) > 2
                    and any(flag.startswith(key) for flag in maintenance)
                ):
                    return False
        elif cmd == "systemctl":
            # Only value-less query flags, so the first other token is the verb.
            flags = {
                "--user",
                "--system",
                "--all",
                "-a",
                "--no-pager",
                "--no-legend",
                "--full",
                "-l",
                "--failed",
                "--plain",
                "-q",
                "--quiet",
            }
            verbs = [a for a in arguments if a not in flags]
            if any(a.startswith("-") for a in verbs) or not verbs:
                return False
            if verbs[0] not in {
                "status",
                "show",
                "cat",
                "list-units",
                "list-timers",
                "list-unit-files",
                "list-sockets",
                "is-active",
                "is-enabled",
                "is-failed",
            }:
                return False
        elif cmd == "kubectl":
            # Observational verbs only. Secrets go to evaluation via the
            # disclosure check; exec, port-forward and writes are not reads.
            valued = {
                "-n",
                "--namespace",
                "--context",
                "--cluster",
                "--kubeconfig",
                "--user",
                "-l",
                "--selector",
                "-o",
                "--output",
                "-c",
                "--container",
                "--since",
                "--since-time",
                "--tail",
                "--field-selector",
                "--sort-by",
                "--request-timeout",
            }
            # Before the verb accept only known global options, so an unknown
            # value-taking flag cannot hide the real verb.
            pre_verb = {"-n", "--namespace", "--context", "--cluster", "--kubeconfig"}
            positional, skip, seen_verb = [], False, False
            for a in arguments:
                if skip:
                    skip = False
                elif a.startswith("-"):
                    key = a.split("=", 1)[0]
                    if not seen_verb and key not in pre_verb:
                        return False
                    skip = a in valued
                else:
                    seen_verb = True
                    positional.append(a)
            verb = positional[:1]
            if verb == ["auth"]:
                if positional[1:2] != ["can-i"]:
                    return False
            elif verb == ["cluster-info"] and "dump" in positional:
                return False
            elif not verb or verb[0] not in {
                "logs",
                "get",
                "describe",
                "top",
                "version",
                "explain",
                "api-resources",
                "api-versions",
                "cluster-info",
                "events",
            }:
                return False
            if any(
                p in {"secret", "secrets"} or p.startswith(("secret/", "secrets/"))
                for p in positional
            ):
                return False
        elif cmd == "rclone":
            if arguments != ["version"]:
                return False
        elif cmd == "sort":
            # -o/--output writes a file; --compress-program executes one.
            # Short options may be clustered (-uo FILE).
            if any(
                (a.startswith("--o") or a.startswith("--c") and a != "--check")
                or (a.startswith("-") and not a.startswith("--") and "o" in a)
                for a in arguments
            ):
                return False
        elif cmd == "uniq":
            # A second operand is an output file; allow filter use only, with
            # numbers only as values of -f/-s/-w.
            for previous, a in zip(["", *arguments], arguments):
                if (
                    a == "--"
                    or not a.startswith("-")
                    and not (a.isdigit() and previous in {"-f", "-s", "-w"})
                ):
                    return False
        elif cmd == "date":
            # -s/--set and a positional MMDDhhmm operand set the system clock;
            # allow only +FORMAT operands and values of -d/-r/-f.
            for previous, a in zip(["", *arguments], arguments):
                if a.startswith("--s") or (
                    a.startswith("-") and not a.startswith("--") and "s" in a
                ):
                    return False
                if not a.startswith(("-", "+")) and previous not in {
                    "-d",
                    "-r",
                    "-f",
                    "--date",
                    "--reference",
                    "--file",
                }:
                    return False
        elif cmd in {
            "stat",
            "pgrep",
            "free",
            "nproc",
            "which",
            "sleep",
            "true",
            "false",
            "printf",
        }:
            pass
        elif cmd in {
            "ls",
            "cat",
            "head",
            "tail",
            "grep",
            "rg",
            "wc",
            "cut",
            "tr",
            "pwd",
            "du",
            "df",
            "uname",
            "whoami",
            "id",
            "uptime",
            "echo",
            "diff",
        }:
            if cmd == "rg" and any(
                a.startswith(("--pre", "--hostname-bin")) for a in arguments
            ):
                return False
        else:
            return False
    return True


def verified_read_only(tool: str, args: dict[str, Any]) -> bool:
    """Mutation proof from the canonical local allowlist, not MCP exemptions."""
    # Exact known observational MCP operations, not arbitrary get/list prefixes.
    base = re.split(r":|__", tool)[-1]
    observational_mcp = {
        "knowledgebase_list_artifacts",
        "search_memories",
        "find_memories",
        "get_core_memories",
        "get_recent_memories",
        "list_memories",
        "list_categories",
        "get_artifact",
        "get_artifact_url",
        "list_artifacts",
        "list-chats",
    }
    return base in observational_mcp or (
        verified_read_only_bash(args)
        if tool == "bash"
        else tool.lower() in _READ_ONLY_TOOLS - {"todowrite"}
    )


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
        arguments = _git_read_arguments(tokens[1:])
        if not arguments:
            return False
        subcommand = arguments[0]
        if subcommand == "grep":
            return not _git_grep_opens_pager(arguments[1:])
        if subcommand in _READ_ONLY_GIT_SUBCOMMANDS:
            return True
        if subcommand in _GIT_SUBCOMMANDS_WITH_CHECKS:
            return _is_git_subcommand_read_only(subcommand, arguments[1:])
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
