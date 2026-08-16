"""Regression tests for the Claude Code integration hook scripts.

These execute the real bash scripts as subprocesses against a stub HTTP
server standing in for the Intaris backend, and assert on stdout JSON,
exit codes, and the resulting state files. Unlike
test_opencode_integration.py's source-grep, this exercises the actual
bash/jq logic — locking, fail-closed enforcement, state read-modify-write —
where the bugs this suite pins actually lived.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent / "integrations" / "claude-code" / "scripts"
)


class _StubHandler(BaseHTTPRequestHandler):
    """Records every request; replies per a routing table set by the test.

    `routes` maps (method, path_suffix) -> (status, json_body). The first
    matching suffix wins. Unmatched requests get 200 {}.
    """

    routes: dict[tuple[str, str], tuple[int, object]] = {}
    requests: list[dict[str, object]] = []

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = raw.decode("utf-8", "replace")
        type(self).requests.append({"method": method, "path": self.path, "body": body})

        status, payload = 200, {}
        for (m, suffix), resp in type(self).routes.items():
            if m == method and self.path.split("?")[0].endswith(suffix):
                status, payload = resp
                break

        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def log_message(self, format, *args) -> None:  # noqa: A002
        return


class StubIntaris:
    """Context-managed stub Intaris server with a scriptable routing table."""

    def __init__(
        self, routes: dict[tuple[str, str], tuple[int, object]] | None = None
    ) -> None:
        _StubHandler.routes = dict(routes or {})
        _StubHandler.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "StubIntaris":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def requests(self) -> list[dict[str, object]]:
        return _StubHandler.requests

    def set_routes(self, routes: dict[tuple[str, str], tuple[int, object]]) -> None:
        _StubHandler.routes = dict(routes)


def run_hook(
    script: str,
    payload: dict,
    *,
    state_dir: Path,
    url: str,
    env: dict[str, str] | None = None,
    path: str | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess:
    """Run a hook script as a real subprocess with the given JSON stdin."""
    full_env = {
        "PATH": path if path is not None else os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(state_dir)),
        "TMPDIR": str(state_dir),
        "INTARIS_URL": url,
    }
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPTS_DIR / script)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        env=full_env,
        timeout=timeout,
    )


def state_file(state_dir: Path, session_id: str) -> Path:
    return state_dir / f"intaris_state_{session_id}.json"


def child_state_file(state_dir: Path, session_id: str, agent_id: str) -> Path:
    return state_dir / f"intaris_state_{session_id}_{agent_id}.json"


def seed_state(
    state_dir: Path,
    session_id: str,
    *,
    intaris_session_id: str,
    call_count: int = 0,
    approved: int = 0,
    denied: int = 0,
    escalated: int = 0,
    extra: dict | None = None,
) -> None:
    data = {
        "session_id": intaris_session_id,
        "call_count": call_count,
        "approved": approved,
        "denied": denied,
        "escalated": escalated,
        "recent_tools": [],
        "call_id_map": [],
        "cwd": "/tmp",
        "last_assistant_text": "",
        "subagents": {},
    }
    if extra:
        data.update(extra)
    state_file(state_dir, session_id).write_text(json.dumps(data))


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def no_jq_path(tmp_path: Path) -> str:
    """A PATH with bash + coreutils but WITHOUT jq, to test the fail-closed
    behavior when jq is unavailable (the original bug: deny_tool itself
    required jq, so a missing jq silently allowed every tool call).
    """
    bin_dir = tmp_path / "no-jq-bin"
    bin_dir.mkdir()
    needed = [
        "bash",
        "dirname",
        "mkdir",
        "rm",
        "mv",
        "chmod",
        "stat",
        "mktemp",
        "tr",
        "sed",
        "head",
        "tail",
        "wc",
        "curl",
        "cat",
        "sleep",
        "date",
    ]
    for cmd in needed:
        real = shutil.which(cmd)
        if real:
            (bin_dir / cmd).symlink_to(real)
    return str(bin_dir)


def test_evaluate_approve_decision_is_allow(state_dir: Path) -> None:
    """A plain approve decision must produce an empty-object allow."""
    with StubIntaris(
        {
            ("POST", "/evaluate"): (
                200,
                {
                    "decision": "approve",
                    "reasoning": "ok",
                    "call_id": "c1",
                    "risk": "low",
                    "path": "fast",
                    "latency_ms": 1,
                    "session_status": "active",
                },
            )
        }
    ) as server:
        result = run_hook(
            "intaris-evaluate.sh",
            {
                "session_id": "s1",
                "tool_name": "Read",
                "tool_input": {"file_path": "/x"},
                "cwd": "/tmp",
            },
            state_dir=state_dir,
            url=server.url,
        )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}


def test_evaluate_deny_decision_shape(state_dir: Path) -> None:
    """A deny decision must surface hookSpecificOutput.permissionDecision == 'deny'."""
    with StubIntaris(
        {
            ("POST", "/evaluate"): (
                200,
                {
                    "decision": "deny",
                    "reasoning": "policy violation",
                    "session_status": "active",
                },
            )
        }
    ) as server:
        result = run_hook(
            "intaris-evaluate.sh",
            {
                "session_id": "s2",
                "tool_name": "Bash",
                "tool_input": {"note": "x"},
                "cwd": "/tmp",
            },
            state_dir=state_dir,
            url=server.url,
        )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "policy violation" in output["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_evaluate_missing_jq_denies_never_silently_allows(
    state_dir: Path, no_jq_path: str
) -> None:
    """Regression for the fail-open hole: deny_tool used to require jq, so
    a missing jq made the require_jq guard's own deny_tool call emit
    nothing, exit 0, and Claude Code would run the tool. It must now exit
    2 (a blocking PreToolUse error) with an empty stdout and a reason on
    stderr — never exit 0 with empty stdout, which Claude Code treats as
    an allow.
    """
    with StubIntaris() as server:
        result = run_hook(
            "intaris-evaluate.sh",
            {"session_id": "s3", "tool_name": "Bash", "tool_input": {}, "cwd": "/tmp"},
            state_dir=state_dir,
            url=server.url,
            path=no_jq_path,
        )
    assert result.returncode == 2, result.stderr
    assert result.stdout == b""
    assert b"jq" in result.stderr.lower() or b"jq" in result.stderr


def test_evaluate_crash_mid_script_still_denies(state_dir: Path) -> None:
    """The fail-closed exit trap must force a deny if the script dies
    unexpectedly before emitting a decision (simulated here via a PATH
    missing `date`, which the script calls very early)."""
    bin_dir = state_dir.parent / "no-date-bin"
    bin_dir.mkdir()
    for cmd in [
        "bash",
        "jq",
        "dirname",
        "mkdir",
        "rm",
        "mv",
        "chmod",
        "stat",
        "mktemp",
        "tr",
        "sed",
        "head",
        "tail",
        "wc",
        "curl",
        "cat",
        "sleep",
    ]:
        real = shutil.which(cmd)
        if real:
            (bin_dir / cmd).symlink_to(real)
    with StubIntaris() as server:
        result = run_hook(
            "intaris-evaluate.sh",
            {"session_id": "s4", "tool_name": "Read", "tool_input": {}, "cwd": "/tmp"},
            state_dir=state_dir,
            url=server.url,
            path=str(bin_dir),
        )
    assert result.returncode == 2, result.stderr
    assert result.stdout == b""
    assert (
        b"blocking" in result.stderr.lower()
        or b"without a decision" in result.stderr.lower()
    )


def test_evaluate_corrupt_state_file_still_emits_decision(state_dir: Path) -> None:
    """A state file that fails to parse must not crash the script under
    set -e before a decision is printed (the unguarded jq substitution in
    the state-write used to do exactly that)."""
    state_file(state_dir, "s5").write_text("{not valid json")
    with StubIntaris(
        {
            ("POST", "/evaluate"): (
                200,
                {
                    "decision": "approve",
                    "reasoning": "ok",
                    "call_id": "c1",
                    "risk": "low",
                    "path": "fast",
                    "latency_ms": 1,
                    "session_status": "active",
                },
            )
        }
    ) as server:
        result = run_hook(
            "intaris-evaluate.sh",
            {"session_id": "s5", "tool_name": "Read", "tool_input": {}, "cwd": "/tmp"},
            state_dir=state_dir,
            url=server.url,
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}


def test_evaluate_fail_open_false_denies_when_unreachable(state_dir: Path) -> None:
    """Default behavior: an unreachable Intaris server must deny, not allow."""
    result = run_hook(
        "intaris-evaluate.sh",
        {"session_id": "s6", "tool_name": "Read", "tool_input": {}, "cwd": "/tmp"},
        state_dir=state_dir,
        url="http://127.0.0.1:1",  # nothing listens here
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_evaluate_fail_open_true_allows_when_unreachable(state_dir: Path) -> None:
    """With INTARIS_FAIL_OPEN=true, an unreachable server must allow."""
    result = run_hook(
        "intaris-evaluate.sh",
        {"session_id": "s7", "tool_name": "Read", "tool_input": {}, "cwd": "/tmp"},
        state_dir=state_dir,
        url="http://127.0.0.1:1",
        env={"INTARIS_FAIL_OPEN": "true"},
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}


def test_evaluate_concurrent_calls_no_lost_counter_updates(state_dir: Path) -> None:
    """N concurrent PreToolUse evaluations against a session with artificial
    server latency must all land — call_count/approved must equal N exactly,
    not fewer. Regression for reading counters from a stale pre-/evaluate
    snapshot instead of the state file's on-disk value at write time.
    """

    class SlowHandler(_StubHandler):
        def _handle(self, method: str) -> None:  # noqa: D401
            if method == "POST" and self.path.endswith("/evaluate"):
                time.sleep(0.2)
            super()._handle(method)

    n = 12
    session_id = "conc1"
    intaris_sid = "cc-conc1"
    seed_state(state_dir, session_id, intaris_session_id=intaris_sid)

    with StubIntaris(
        {
            ("POST", "/evaluate"): (
                200,
                {
                    "decision": "approve",
                    "reasoning": "ok",
                    "call_id": "c",
                    "risk": "low",
                    "path": "fast",
                    "latency_ms": 1,
                    "session_status": "active",
                },
            )
        }
    ) as server:
        # Swap in the slow handler after the server starts (routes/requests
        # are inherited class attributes, so this keeps using them).
        server._server.RequestHandlerClass = SlowHandler  # type: ignore[attr-defined]

        threads = []
        results = []

        def worker(i: int) -> None:
            r = run_hook(
                "intaris-evaluate.sh",
                {
                    "session_id": session_id,
                    "tool_name": "Bash",
                    "tool_input": {"i": i},
                    "cwd": "/tmp",
                    "tool_use_id": f"tu-{i}",
                },
                state_dir=state_dir,
                url=server.url,
            )
            results.append(r)

        for i in range(n):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=15)

    for r in results:
        assert r.returncode == 0, r.stderr

    final = json.loads(state_file(state_dir, session_id).read_text())
    assert final["call_count"] == n
    assert final["approved"] == n


def test_subagent_stop_active_preserves_child_state(state_dir: Path) -> None:
    """stop_hook_active=true means the subagent is continuing, not done —
    SubagentStop must not complete/delete the child session state file in
    that case (it used to, dropping the child's policy and counters on
    every intermediate stop)."""
    session_id = "sa1"
    child_state_file(state_dir, session_id, "agent1").write_text(
        json.dumps(
            {
                "session_id": "cc-sa1--agent1",
                "call_count": 3,
                "approved": 3,
                "denied": 0,
                "escalated": 0,
                "cwd": "/tmp",
            }
        )
    )
    with StubIntaris() as server:
        result = run_hook(
            "intaris-subagent-stop.sh",
            {
                "session_id": session_id,
                "agent_id": "agent1",
                "agent_type": "Explore",
                "stop_hook_active": True,
            },
            state_dir=state_dir,
            url=server.url,
        )
    assert result.returncode == 0, result.stderr
    assert child_state_file(state_dir, session_id, "agent1").exists()
    assert not any(r["path"].endswith("/status") for r in server.requests)


def test_subagent_stop_final_completes_and_removes_child_state(state_dir: Path) -> None:
    """Contrast case: a genuine final SubagentStop (stop_hook_active=false)
    must complete the child session and remove its state file."""
    session_id = "sa2"
    child_state_file(state_dir, session_id, "agent1").write_text(
        json.dumps(
            {
                "session_id": "cc-sa2--agent1",
                "call_count": 3,
                "approved": 3,
                "denied": 0,
                "escalated": 0,
                "cwd": "/tmp",
            }
        )
    )
    with StubIntaris() as server:
        result = run_hook(
            "intaris-subagent-stop.sh",
            {
                "session_id": session_id,
                "agent_id": "agent1",
                "agent_type": "Explore",
                "stop_hook_active": False,
            },
            state_dir=state_dir,
            url=server.url,
        )
    assert result.returncode == 0, result.stderr
    assert not child_state_file(state_dir, session_id, "agent1").exists()
    assert any(r["path"].endswith("/status") for r in server.requests)


def test_evaluate_record_correlate_via_tool_use_id_not_heuristic(
    state_dir: Path,
) -> None:
    """Two calls with IDENTICAL tool_name + tool_input but different
    tool_use_id (Claude Code's routine parallel-tool-call shape) must each
    correlate to their own call_id in the PostToolUse recording — not
    whichever one happened to be evaluated most recently, which is what the
    previous tool-name+args heuristic actually matched on."""
    session_id = "corr1"
    seed_state(state_dir, session_id, intaris_session_id="cc-corr1")
    common = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/etc/hosts"},
        "cwd": "/tmp",
    }

    with StubIntaris() as server:
        server.set_routes(
            {
                ("POST", "/evaluate"): (
                    200,
                    {
                        "decision": "approve",
                        "reasoning": "ok",
                        "call_id": "call-A",
                        "risk": "low",
                        "path": "fast",
                        "latency_ms": 1,
                        "session_status": "active",
                    },
                )
            }
        )
        run_hook(
            "intaris-evaluate.sh",
            {"session_id": session_id, "tool_use_id": "tu-A", **common},
            state_dir=state_dir,
            url=server.url,
        )

        server.set_routes(
            {
                ("POST", "/evaluate"): (
                    200,
                    {
                        "decision": "approve",
                        "reasoning": "ok",
                        "call_id": "call-B",
                        "risk": "low",
                        "path": "fast",
                        "latency_ms": 1,
                        "session_status": "active",
                    },
                )
            }
        )
        run_hook(
            "intaris-evaluate.sh",
            {"session_id": session_id, "tool_use_id": "tu-B", **common},
            state_dir=state_dir,
            url=server.url,
        )

        server.set_routes({})
        # intaris-record.sh prints {} then fires its recording POST in a
        # backgrounded, disowned subshell — the subprocess call returns
        # before that POST necessarily lands, so poll briefly for it rather
        # than asserting immediately.
        result_b = run_hook(
            "intaris-record.sh",
            {
                "session_id": session_id,
                "tool_use_id": "tu-B",
                **common,
                "tool_response": {"ok": True},
                "duration_ms": 5,
            },
            state_dir=state_dir,
            url=server.url,
            env={"INTARIS_SESSION_RECORDING": "true"},
        )
        result_a = run_hook(
            "intaris-record.sh",
            {
                "session_id": session_id,
                "tool_use_id": "tu-A",
                **common,
                "tool_response": {"ok": True},
                "duration_ms": 5,
            },
            state_dir=state_dir,
            url=server.url,
            env={"INTARIS_SESSION_RECORDING": "true"},
        )

        got_call_ids: set[str] = set()
        deadline = time.time() + 5
        while time.time() < deadline:
            events = [r for r in server.requests if r["path"].endswith("/events")]
            result_events = [e for e in events if e["body"][0]["type"] == "tool_result"]
            got_call_ids = {e["body"][0]["data"]["call_id"] for e in result_events}
            if got_call_ids == {"call-A", "call-B"}:
                break
            time.sleep(0.1)

    assert result_a.returncode == 0 and result_b.returncode == 0
    assert got_call_ids == {"call-A", "call-B"}


def test_allow_paths_policy_resolves_macos_private_symlinks(state_dir: Path) -> None:
    """build_allow_paths_policy must include the resolved /private/* form
    of /tmp and $TMPDIR alongside the literal form, so a real caller
    resolving through the symlink (e.g. Claude Code's own scratchpad
    under /private/tmp/...) is not flagged as out of policy."""
    script = f"""#!/usr/bin/env bash
set -euo pipefail
. "{SCRIPTS_DIR}/intaris-lib.sh"
build_allow_paths_policy
"""
    runner = state_dir / "run_policy.sh"
    runner.write_text(script)
    runner.chmod(0o755)
    result = subprocess.run(
        ["bash", str(runner)],
        capture_output=True,
        env={**os.environ, "TMPDIR": str(state_dir), "INTARIS_ALLOW_PATHS": ""},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    policy = json.loads(result.stdout)
    allow_paths = policy["allow_paths"]
    real_tmpdir = os.path.realpath(str(state_dir))
    if real_tmpdir != str(state_dir):
        assert f"{real_tmpdir}/*" in allow_paths
    if os.path.realpath("/tmp") != "/tmp":
        assert f"{os.path.realpath('/tmp')}/*" in allow_paths
    assert "/tmp/*" in allow_paths
