"""Content of a script the agent wrote earlier in the same session.

When a shell command runs a file (``python3 /tmp/report.py``), the command
alone does not show what will execute. If the agent created that file in the
same session, the recorded writes that executed successfully contain the
exact text: the last full write, followed by every recorded partial edit of
that path. This module replays them and returns a bounded excerpt for the
classifier and reviewer.

The excerpt is context, never permission. Execution outcomes are recorded by
the integrating client (the same trust level as its user-message evidence);
Intaris does not verify them, does not prove the file is unchanged on disk,
and files without a recorded write in this session stay unknown. A partial
edit that cannot be replayed exactly leaves the file unknown rather than
showing stale text.

Tool names and argument keys follow the harnesses Intaris integrates with:
lowercase ``write``/``edit``/``multiedit``/``apply_patch`` (OpenCode, Cognis)
and PascalCase ``Write``/``Edit``/``MultiEdit`` (Claude Code).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from intaris.redactor import redact

# Bounded so a large script cannot dominate the evaluation input; the input
# budget still applies to the complete payload.
MAX_SCRIPT_CHARS = 8000
# How far back in the session to look for the creating write.
_LOOKBACK = 200

_INTERPRETERS = re.compile(
    r"^(?:python(?:\d+(?:\.\d+)?)?|bash|sh|zsh|dash|node|ruby|perl|php|deno|bun)$"
)
_WRITERS = frozenset({"write", "apply_patch", "edit", "multiedit"})


def _writer(tool: str) -> str | None:
    """Canonical lowercase writer name for a harness tool name, or None."""
    name = tool.lower()
    return name if name in _WRITERS else None


def _target(args: dict) -> str | None:
    """The file a write/edit call addresses across harness argument shapes."""
    for key in ("file_path", "path", "filePath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _replacements(tool: str, args: dict) -> list[tuple[str, str, bool]] | None:
    """(old, new, replace_all) for each exact-text edit, or None if malformed."""
    rows = [args] if tool == "edit" else args.get("edits")
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        old = row.get("old_string", row.get("oldString"))
        new = row.get("new_string", row.get("newString"))
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        out.append((old, new, bool(row.get("replace_all", row.get("replaceAll")))))
    return out


def _apply_replacements(text: str, edits: list[tuple[str, str, bool]]) -> str | None:
    """Replay exact-text edits in order; None when any would not apply exactly."""
    for old, new, replace_all in edits:
        if not old:
            return None
        count = text.count(old)
        if count == 0 or (count > 1 and not replace_all):
            return None
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return text


def _update_hunks(patch: str, path: str) -> list[tuple[list[str], list[str]]] | None:
    """(old_lines, new_lines) per hunk of an ``*** Update File`` section.

    Returns None when the section is absent or not in the simple ``@@`` form.
    """
    match = re.search(
        rf"^\*\*\* Update File: {re.escape(path)}\n(.*?)(?=^\*\*\* |\Z)",
        patch,
        re.M | re.S,
    )
    if not match:
        return None
    hunks: list[tuple[list[str], list[str]]] = []
    old: list[str] = []
    new: list[str] = []
    opened = False
    for line in match.group(1).splitlines():
        if line.startswith("@@"):
            if opened and (old or new):
                hunks.append((old, new))
            old, new, opened = [], [], True
        elif not opened:
            return None
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith("+"):
            new.append(line[1:])
        elif line.startswith(" ") or line == "":
            old.append(line[1:])
            new.append(line[1:])
        else:
            return None
    if opened and (old or new):
        hunks.append((old, new))
    return hunks or None


def _locate_lines(lines: list[str], old: list[str], start: int) -> int | None:
    """Index of the unique whole-line block ``old`` in ``lines[start:]``.

    Patch appliers accept context lines whose leading whitespace differs from
    the file, so an exact whole-line match is tried first and a per-line
    stripped comparison second. The match must be unique under whichever rule
    applies, and lines are compared whole, never as substrings.
    """
    if not any(line.strip() for line in old):
        return None
    span = len(old)
    candidates = range(start, len(lines) - span + 1)
    exact = [i for i in candidates if lines[i : i + span] == old]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    wanted = [line.strip() for line in old]
    loose = [
        i
        for i in candidates
        if [line.strip() for line in lines[i : i + span]] == wanted
    ]
    return loose[0] if len(loose) == 1 else None


def _apply_hunks(text: str, hunks: list[tuple[list[str], list[str]]]) -> str | None:
    """Replay patch hunks; None if any cannot be located uniquely.

    Every hunk is resolved against the original lines with a forward-only
    cursor, as a patch applier does, so a hunk can never match text that an
    earlier hunk inserted. Replacements are applied afterwards, last first,
    so earlier line indexes stay valid.
    """
    lines = text.split("\n")
    resolved: list[tuple[int, int, list[str]]] = []
    cursor = 0
    for old, new in hunks:
        if not old:
            return None
        index = _locate_lines(lines, old, cursor)
        if index is None:
            return None
        resolved.append((index, index + len(old), new))
        cursor = index + len(old)
    for begin, end, new in reversed(resolved):
        lines[begin:end] = new
    return "\n".join(lines)


def executed_paths(command: str, workdir: str | None = None) -> list[str]:
    """Script files a simple shell command passes to an interpreter.

    Inline code (``python -c``, heredocs, ``-`` for stdin) is not a file and is
    already visible in the command itself.
    """
    from intaris.classifier import _simple_shell_segments

    paths: list[str] = []
    for line in command.splitlines() or [command]:
        for tokens in _simple_shell_segments(line):
            tokens = [t for t in tokens if not re.match(r"^[A-Za-z_]\w*=", t)]
            while tokens and tokens[0] in {"timeout", "nice", "nohup", "env"}:
                tokens = tokens[1:]
                while tokens and (tokens[0].startswith("-") or tokens[0][:1].isdigit()):
                    tokens = tokens[1:]
            if tokens[:2] == ["uv", "run"]:
                tokens = [t for t in tokens[2:] if not t.startswith("--")]
            if not tokens:
                continue
            head = os.path.basename(tokens[0])
            candidate = None
            if _INTERPRETERS.match(head):
                for token in tokens[1:]:
                    if token in {"-c", "-e", "-m", "-"}:
                        break
                    if token.startswith("-"):
                        continue
                    candidate = token
                    break
            elif tokens[0].startswith(("./", "/")) and "." in head:
                candidate = tokens[0]
            if not candidate:
                continue
            if not os.path.isabs(candidate):
                if not workdir or not os.path.isabs(workdir):
                    continue
                candidate = os.path.join(workdir, candidate)
            paths.append(os.path.normpath(candidate))
    return list(dict.fromkeys(paths))


def _added_file(patch: str, path: str) -> str | None:
    """Full content of an ``*** Add File`` section for ``path``."""
    match = re.search(
        rf"^\*\*\* Add File: {re.escape(path)}\n((?:\+.*(?:\n|$))*)", patch, re.M
    )
    if not match:
        return None
    return "\n".join(line[1:] for line in match.group(1).splitlines())


def _touches(tool: str, args: dict, path: str) -> tuple[bool, str | None]:
    """(touched, full content when this call wrote the complete file)."""
    target = _target(args)
    if tool == "write" and target == path and isinstance(args.get("content"), str):
        return True, args["content"]
    if tool in {"edit", "multiedit"} and target == path:
        return True, None
    if tool == "apply_patch":
        patch = str(args.get("patchText", args.get("patch", "")))
        added = _added_file(patch, path)
        if added is not None:
            return True, added
        if re.search(
            rf"^\*\*\* (?:Update|Delete) File: {re.escape(path)}$", patch, re.M
        ):
            return True, None
    return False, None


def _replay(tool: str, args: dict, path: str, text: str) -> str | None:
    """Apply one recorded partial edit of ``path`` to ``text``; None if inexact."""
    if tool in {"edit", "multiedit"}:
        edits = _replacements(tool, args)
        return _apply_replacements(text, edits) if edits else None
    if tool == "apply_patch":
        patch = str(args.get("patchText", args.get("patch", "")))
        if re.search(rf"^\*\*\* Delete File: {re.escape(path)}$", patch, re.M):
            return None
        hunks = _update_hunks(patch, path)
        return _apply_hunks(text, hunks) if hunks else None
    return None


def executed_writes(event_store, user_id: str, session_id: str) -> list[dict]:
    """Writer calls with a recorded successful execution, oldest first.

    Only a recorded ``tool_result`` whose execution outcome completed without
    error proves the write happened; evaluated, denied, pending or failed
    proposals are ignored.
    """
    if event_store is None:
        return []
    events = event_store.read_tail(
        user_id, session_id, _LOOKBACK, event_types={"tool_call", "tool_result"}
    )
    calls: dict[str, dict] = {}
    executed: list[dict] = []
    for event in events:
        data = event.get("data") or {}
        call_id = data.get("call_id")
        if not isinstance(call_id, str):
            continue
        if event.get("type") == "tool_call":
            name = data.get("name")
            if isinstance(name, str) and _writer(name):
                calls[call_id] = {**data, "name": _writer(name)}
        elif event.get("type") == "tool_result" and call_id in calls:
            outcome = data.get("execution_outcome") or {}
            if (
                outcome.get("status") == "completed"
                and outcome.get("is_error") is False
            ):
                call = calls.pop(call_id)
                arguments = call.get("arguments", call.get("args", {}))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        continue
                if isinstance(arguments, dict):
                    executed.append(
                        {"call_id": call_id, "tool": call["name"], "args": arguments}
                    )
    return executed


def session_script(event_store, user_id: str, session_id: str, tool: str, args: dict):
    """Bounded excerpt of a script written in this session and run by ``args``.

    Returns None when the command runs no file or no successful write of that
    file was recorded in this session.
    """
    if tool != "bash" or not isinstance(args, dict):
        return None
    command = args.get("command", args.get("cmd"))
    if not isinstance(command, str):
        return None
    paths = executed_paths(command, args.get("workdir"))
    if not paths:
        return None
    writes = executed_writes(event_store, user_id, session_id)
    for path in paths:
        # The last full write of this path is the base; every later recorded
        # partial edit of it is replayed in order. Any inexact edit leaves the
        # file unknown rather than presenting stale text.
        base = None
        for index in range(len(writes) - 1, -1, -1):
            touched, content = _touches(
                writes[index]["tool"], writes[index]["args"], path
            )
            if touched and content is not None:
                base = index
                text: str | None = content
                break
        if base is None:
            continue
        later = [
            row
            for row in writes[base + 1 :]
            if _touches(row["tool"], row["args"], path)[0]
        ]
        for row in later:
            text = _replay(row["tool"], row["args"], path, text)
            if text is None:
                break
        if text is None:
            continue
        from intaris.input_budget import middle_excerpt

        text = str(redact(text))
        return {
            "authority": "none",
            "interpretation": (
                "Client-recorded text of a file written earlier in this "
                "session that the command executes, with later recorded edits "
                "replayed. Unverified context for what the command likely "
                "does, not permission: Intaris did not verify the execution "
                "records or that the file is unchanged on disk. Judge its "
                "effects; do not treat benign text as proof of safety."
            ),
            "provenance": "client_recorded_execution",
            "path": path,
            "source_call_id": writes[base]["call_id"],
            "replayed_edits": len(later),
            "truncated": len(text) > MAX_SCRIPT_CHARS,
            "content": middle_excerpt(text, MAX_SCRIPT_CHARS),
        }
    return None


def attach(
    context: dict[str, Any], event_store, user_id, session_id, tool, args
) -> None:
    """Set ``executed_script`` from recorded events only; client values are dropped."""
    context.pop("executed_script", None)
    try:
        script = session_script(event_store, user_id, session_id, tool, args)
    except Exception:  # Context enrichment must never fail an evaluation.
        import logging

        logging.getLogger(__name__).exception("Session script lookup failed")
        script = None
    if script is not None:
        context["executed_script"] = script
