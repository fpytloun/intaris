"""Executed scripts successfully written earlier in the session are shown."""

import pytest

from intaris.evaluation_context import runtime_view
from intaris.session_scripts import (
    MAX_SCRIPT_CHARS,
    attach,
    executed_paths,
    session_script,
)

SCRIPT = "import shutil\nshutil.rmtree('/workspace/build')\n"


class Events:
    """Minimal canonical event store: recorded tool calls and results."""

    def __init__(self):
        self.events = []

    def call(self, call_id, name, arguments, status="completed", is_error=False):
        self.events.append(
            {
                "type": "tool_call",
                "data": {"call_id": call_id, "name": name, "arguments": arguments},
            }
        )
        if status is not None:
            self.events.append(
                {
                    "type": "tool_result",
                    "data": {
                        "call_id": call_id,
                        "execution_outcome": {"status": status, "is_error": is_error},
                    },
                }
            )

    def read_tail(self, user_id, session_id, limit, event_types=None):
        return [e for e in self.events if e["type"] in event_types][-limit:]


def run(events, command="python3 /tmp/a.py"):
    return session_script(events, "u", "s", "bash", {"command": command})


@pytest.mark.parametrize(
    "command,workdir,expected",
    [
        ("python3 /tmp/a.py", None, ["/tmp/a.py"]),
        ("python3 -u /tmp/a.py --flag", None, ["/tmp/a.py"]),
        ("uv run --no-project python /tmp/a.py", None, ["/tmp/a.py"]),
        ("X=1 timeout 30 bash scripts/run.sh", "/repo", ["/repo/scripts/run.sh"]),
        ("/tmp/venv/bin/python /tmp/b.py 2>&1 | tail -5", None, ["/tmp/b.py"]),
        ("cd x && ./tool.sh", None, []),  # relative without workdir: unknown
        ("python3 -c 'print(1)'", None, []),
        ("python3 - <<'EOF'\nprint(1)\nEOF", None, []),
        ("git status", None, []),
    ],
)
def test_executed_paths(command, workdir, expected):
    assert executed_paths(command, workdir) == expected


def test_successful_full_write_is_shown_for_an_executed_script():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    script = run(events)
    assert script["content"] == SCRIPT
    assert script["path"] == "/tmp/a.py"
    assert script["source_call_id"] == "c1"
    assert script["authority"] == "none"
    assert script["replayed_edits"] == 0 and not script["truncated"]


@pytest.mark.parametrize(
    "status,is_error",
    [
        (None, False),  # proposed or pending: no recorded result
        ("denied", False),
        ("failed", True),
        ("completed", True),
    ],
)
def test_unexecuted_or_failed_writes_never_provide_script_text(status, is_error):
    events = Events()
    events.call(
        "c1",
        "write",
        {"file_path": "/tmp/a.py", "content": "print('harmless')"},
        status=status,
        is_error=is_error,
    )
    assert run(events) is None


def test_latest_successful_write_wins_over_a_later_unexecuted_proposal():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2", "write", {"file_path": "/tmp/a.py", "content": "print(1)"}, status=None
    )
    assert run(events)["content"] == SCRIPT


def test_added_patch_file_is_reconstructed():
    events = Events()
    patch = (
        "*** Begin Patch\n*** Add File: /tmp/c.py\n+print('a')\n+print('b')\n"
        "*** End Patch\n"
    )
    events.call("c1", "apply_patch", {"patchText": patch})
    assert run(events, "python3 /tmp/c.py")["content"] == "print('a')\nprint('b')"


def test_later_partial_edits_are_replayed_onto_the_full_write():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2",
        "edit",
        {"file_path": "/tmp/a.py", "old_string": "build", "new_string": "dist"},
    )
    events.call(
        "c3",
        "multiedit",
        {
            "file_path": "/tmp/a.py",
            "edits": [
                {"old_string": "import shutil", "new_string": "import shutil, os"},
                {"old_string": "rmtree", "new_string": "copytree"},
            ],
        },
    )
    script = run(events)
    assert script["replayed_edits"] == 2
    assert script["source_call_id"] == "c1"
    assert script["content"] == (
        "import shutil, os\nshutil.copytree('/workspace/dist')\n"
    )


def test_update_patch_hunks_are_replayed():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    patch = (
        "*** Begin Patch\n*** Update File: /tmp/a.py\n@@\n import shutil\n"
        "-shutil.rmtree('/workspace/build')\n+print('dry run')\n*** End Patch\n"
    )
    events.call("c2", "apply_patch", {"patchText": patch})
    script = run(events)
    assert script["replayed_edits"] == 1
    assert script["content"] == "import shutil\nprint('dry run')\n"


def test_update_hunk_tolerates_indent_drift_but_requires_a_unique_match():
    """Patch appliers accept context with differing leading whitespace."""
    body = "def f():\n    a()\n    b()\n"
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": body})
    drifted = (
        "*** Begin Patch\n*** Update File: /tmp/a.py\n@@\n"
        "   a()\n-   b()\n+   c()\n*** End Patch\n"
    )
    events.call("c2", "apply_patch", {"patchText": drifted})
    # The hunk's own context and added lines replace the located block:
    # " <sp><sp>a()" is a context line carrying two spaces, "+<sp><sp><sp>c()"
    # an added line carrying three. Drift in the file is tolerated for
    # locating the block; the replayed text is the hunk's text.
    assert run(events)["content"] == "def f():\n  a()\n   c()\n"
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": "x()\nx()\n"})
    events.call(
        "c2",
        "apply_patch",
        {
            "patchText": "*** Begin Patch\n*** Update File: /tmp/a.py\n@@\n"
            "-  x()\n+  y()\n*** End Patch\n"
        },
    )
    assert run(events) is None


def patch_update(path, hunks):
    body = "".join("@@\n" + "\n".join(h) + "\n" for h in hunks)
    return f"*** Begin Patch\n*** Update File: {path}\n{body}*** End Patch\n"


def test_hunk_lines_match_whole_lines_not_substrings():
    """``danger()`` must not match inside ``    danger()`` as a substring."""
    events = Events()
    events.call(
        "c1", "write", {"file_path": "/tmp/a.py", "content": "def f():\n    danger()\n"}
    )
    events.call(
        "c2",
        "apply_patch",
        {"patchText": patch_update("/tmp/a.py", [["-danger()", "+safe()"]])},
    )
    # Exact whole-line match fails; stripped match is unique; the hunk's own
    # unindented line is what replays. The result is the hunk's text, never
    # a substring splice that would keep the file's indentation.
    assert run(events)["content"] == "def f():\nsafe()\n"


def test_later_hunk_cannot_match_text_inserted_by_an_earlier_hunk():
    events = Events()
    events.call(
        "c1",
        "write",
        {"file_path": "/tmp/a.py", "content": "a()\n  b()\n  c()\n"},
    )
    events.call(
        "c2",
        "apply_patch",
        {
            "patchText": patch_update(
                "/tmp/a.py",
                [["-a()", "+b()", "+c()"], ["-b()", "-c()", "+safe()"]],
            )
        },
    )
    # Hunk 2 resolves against the original lines after hunk 1's span, so it
    # targets the original indented block, not hunk 1's inserted lines.
    assert run(events)["content"] == "b()\nc()\nsafe()\n"


def test_hunks_resolve_forward_only():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": "x()\ny()\nx()\n"})
    events.call(
        "c2",
        "apply_patch",
        {"patchText": patch_update("/tmp/a.py", [["-y()", "+z()"], ["-x()", "+w()"]])},
    )
    # After hunk 1 the cursor is past the first x(); only the last x() remains.
    assert run(events)["content"] == "x()\nz()\nw()\n"


def test_delete_then_add_same_path_in_one_patch_uses_the_added_content():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": "old\n"})
    events.call(
        "c2",
        "apply_patch",
        {
            "patchText": "*** Begin Patch\n*** Delete File: /tmp/a.py\n"
            "*** Add File: /tmp/a.py\n+new\n*** End Patch\n"
        },
    )
    script = run(events)
    assert script["source_call_id"] == "c2"
    assert script["content"] == "new"


def test_overlapping_multiedit_applies_in_order():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": "ab\n"})
    events.call(
        "c2",
        "multiedit",
        {
            "file_path": "/tmp/a.py",
            "edits": [
                {"old_string": "ab", "new_string": "abc"},
                {"old_string": "bc", "new_string": "X"},
            ],
        },
    )
    assert run(events)["content"] == "aX\n"


@pytest.mark.parametrize(
    "edit",
    [
        {"old_string": "not present", "new_string": "x"},  # no match
        {"old_string": "shutil", "new_string": "x"},  # ambiguous without replace_all
        {"old_string": "", "new_string": "x"},  # empty anchor
    ],
)
def test_inexact_edit_leaves_the_file_unknown(edit):
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call("c2", "edit", {"file_path": "/tmp/a.py", **edit})
    assert run(events) is None


def test_ambiguous_edit_with_replace_all_is_replayed():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2",
        "edit",
        {
            "file_path": "/tmp/a.py",
            "old_string": "shutil",
            "new_string": "sh",
            "replace_all": True,
        },
    )
    assert run(events)["content"] == "import sh\nsh.rmtree('/workspace/build')\n"


def test_delete_patch_after_write_leaves_the_file_unknown():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2",
        "apply_patch",
        {"patchText": "*** Begin Patch\n*** Delete File: /tmp/a.py\n*** End Patch\n"},
    )
    assert run(events) is None


def test_a_later_full_write_replaces_earlier_edits():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": "old"})
    events.call(
        "c2", "edit", {"file_path": "/tmp/a.py", "old_string": "old", "new_string": "x"}
    )
    events.call("c3", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    script = run(events)
    assert script["source_call_id"] == "c3"
    assert script["replayed_edits"] == 0
    assert script["content"] == SCRIPT


@pytest.mark.parametrize(
    "write_tool,edit_tool,path_key,old_key,new_key",
    [
        ("Write", "Edit", "file_path", "old_string", "new_string"),  # Claude Code
        ("write", "edit", "filePath", "oldString", "newString"),  # OpenCode
        ("write", "edit", "path", "old_string", "new_string"),
    ],
)
def test_harness_tool_names_and_argument_shapes(
    write_tool, edit_tool, path_key, old_key, new_key
):
    events = Events()
    events.call("c1", write_tool, {path_key: "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2", edit_tool, {path_key: "/tmp/a.py", old_key: "build", new_key: "dist"}
    )
    script = run(events)
    assert script is not None
    assert script["replayed_edits"] == 1
    assert "dist" in script["content"] and "build" not in script["content"]


def test_multiedit_pascal_case_from_claude_code():
    events = Events()
    events.call("c1", "Write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2",
        "MultiEdit",
        {
            "file_path": "/tmp/a.py",
            "edits": [{"old_string": "build", "new_string": "dist"}],
        },
    )
    assert "dist" in run(events)["content"]


def test_unwritten_file_or_missing_event_store_stays_unknown():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/other.py", "content": SCRIPT})
    assert run(events) is None
    assert run(None) is None


def test_large_script_is_bounded():
    events = Events()
    events.call(
        "c1", "write", {"file_path": "/tmp/big.py", "content": "x = 1\n" * 5000}
    )
    script = run(events, "python3 /tmp/big.py")
    assert script["truncated"]
    assert len(script["content"]) <= MAX_SCRIPT_CHARS + 200
    assert "OMITTED MIDDLE" in script["content"]


def test_client_cannot_supply_script_text_and_it_reaches_runtime_view():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    forged = {"executed_script": {"content": "print('harmless')"}}
    attach(forged, events, "u", "s", "bash", {"command": "git status"})
    assert "executed_script" not in forged
    context = {}
    attach(context, events, "u", "s", "bash", {"command": "python3 /tmp/a.py"})
    assert runtime_view(context)["executed_script"]["content"] == SCRIPT
