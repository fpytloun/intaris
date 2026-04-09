"""Shared matching for authoritative human precedent.

Maps tool calls into coarse capability families so final human approvals can
apply across equivalent low-risk tools (for example ``web_search`` to
``web_fetch`` or Todoist lookup tools to other lookup tools) without turning
into blanket approval for all calls to the same tool name.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

_MUTATING_VERBS = {
    "add",
    "create",
    "update",
    "delete",
    "remove",
    "complete",
    "close",
    "reopen",
    "archive",
    "move",
}
_LOOKUP_VERBS = {"find", "list", "get", "search"}
_READ_BASH_CMDS = {"rg", "grep", "find", "ls", "cat", "head", "tail", "tree", "wc"}
_GIT_READ_CMDS = {"status", "diff", "log", "show", "branch", "rev-parse"}


@dataclass(frozen=True)
class PrecedentSignature:
    """Normalized family/scope signature for a tool call."""

    family: str
    scope: str


def _normalize_tool(tool: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (tool or "").lower()).strip("_")


def _command_tokens(command: str) -> list[str]:
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _first_pathish(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    for key in ("filePath", "file_path", "path", "directory", "dir", "folder"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    pattern = args.get("pattern")
    if isinstance(pattern, str) and pattern:
        base = re.split(r"[*{[]", pattern, maxsplit=1)[0].rstrip("/")
        return base or pattern
    return ""


def _path_scope(path: str) -> str:
    if not path:
        return ""
    normalized = os.path.normpath(path)
    if normalized in (".", os.sep):
        return normalized
    return os.path.dirname(normalized) or normalized


def _todoist_scope(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    for key in (
        "projectId",
        "project_id",
        "sectionId",
        "section_id",
        "taskId",
        "task_id",
    ):
        value = args.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    return ""


def build_precedent_signature(
    tool: str, args: dict[str, Any] | None
) -> PrecedentSignature | None:
    """Classify a tool call into a coarse precedent family and scope."""
    normalized = _normalize_tool(tool)

    if normalized in {"web_search", "web_fetch"}:
        return PrecedentSignature(family="web_lookup", scope="")

    if normalized in {"read", "grep", "glob"}:
        return PrecedentSignature(
            family="repo_read", scope=_path_scope(_first_pathish(args))
        )

    if normalized in {"write", "edit"}:
        return PrecedentSignature(
            family="repo_write", scope=_path_scope(_first_pathish(args))
        )

    if normalized == "bash":
        command = str((args or {}).get("command") or "")
        tokens = _command_tokens(command)
        if not tokens:
            return None
        cmd = tokens[0].lower()
        if cmd in _READ_BASH_CMDS:
            return PrecedentSignature(
                family="repo_read",
                scope=_path_scope(tokens[-1] if len(tokens) > 1 else ""),
            )
        if cmd == "git" and len(tokens) > 1 and tokens[1].lower() in _GIT_READ_CMDS:
            return PrecedentSignature(family="git_read", scope="")
        return None

    if "todoist" in normalized:
        tokens = set(normalized.split("_"))
        if tokens & _LOOKUP_VERBS:
            return PrecedentSignature(
                family="todoist_lookup", scope=_todoist_scope(args)
            )
        if tokens & _MUTATING_VERBS:
            return PrecedentSignature(
                family="todoist_mutation", scope=_todoist_scope(args)
            )
        return PrecedentSignature(family=f"tool:{normalized}", scope="")

    return (
        PrecedentSignature(family=f"tool:{normalized}", scope="")
        if normalized
        else None
    )


def _scopes_compatible(current: PrecedentSignature, prior: PrecedentSignature) -> bool:
    if current.family != prior.family:
        return False
    if not current.scope or not prior.scope:
        return True
    return current.scope.startswith(prior.scope) or prior.scope.startswith(
        current.scope
    )


def find_authoritative_precedent(
    tool: str,
    args: dict[str, Any] | None,
    user_decisions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the latest matching human approval, unless a newer denial blocks it."""
    current = build_precedent_signature(tool, args)
    if current is None:
        return None

    for record in user_decisions:
        prior = build_precedent_signature(
            str(record.get("tool") or ""),
            record.get("args_redacted") or {},
        )
        if prior is None or not _scopes_compatible(current, prior):
            continue

        decision = str(record.get("user_decision") or "").lower()
        if decision == "deny":
            return None
        if decision == "approve":
            return record

    return None
