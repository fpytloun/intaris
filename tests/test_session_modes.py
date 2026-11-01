"""Synthetic participation, inference-boundary and opt-out race regressions."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from intaris import analyzer
from intaris.background import BackgroundWorker, TaskQueue
from intaris.config import AnalysisConfig, JevConfig
from intaris.intention import generate_intention
from intaris.jev import JevClient
from intaris.session import SessionStore
from intaris.session_modes import (
    AnalyticsScope,
    GuardrailsFence,
    ParticipationRevoked,
    dispatch_scope,
)
from tests.test_analytics_evidence import call, model, rows, session, task
from tests.test_analytics_evidence import db as db
from tests.test_api import _reset_server_globals as _reset_server_globals
from tests.test_api import client_no_auth as client_no_auth
from tests.test_api import env_no_auth as env_no_auth
from tests.test_api import tmp_db as tmp_db
from tests.test_model_retry import _StatusError
from tests.test_model_retry import llm_with as llm_with


def toggle(db, sid="session-a", **modes):
    return SessionStore(db).update_session(sid, user_id="tenant", **modes)


def summarize(db, sid="session-a", text="Allowed summary"):
    return analyzer._store_summary(
        db,
        user_id="tenant",
        session_id=sid,
        window_start="2026-01-01T00:00:00+00:00",
        window_end="2026-01-02T00:00:00+00:00",
        trigger="manual",
        summary_type="window",
        result={"summary": text, "intent_alignment": "aligned"},
        stats={},
    )


def test_default_and_null_updates_preserve_authorization(db):
    initial = session(db)
    assert initial["analytics_enabled"] is True
    assert initial["guardrails_enabled"] is True
    noop = toggle(db, analytics_enabled=None, guardrails_enabled=None)
    assert noop["analytics_revision"] == 1
    disabled = toggle(db, analytics_enabled=False)
    assert disabled["analytics_enabled"] is False
    assert disabled["guardrails_enabled"] is True
    assert disabled["analytics_revision"] == 2
    assert disabled["state_revision"] == initial["state_revision"]
    assert disabled["policy_revision"] == initial["policy_revision"]
    assert toggle(db, analytics_enabled=False)["analytics_revision"] == 2
    assert toggle(db, analytics_enabled=True)["analytics_revision"] == 3


def test_existing_schema_upgrade_and_repeated_bootstrap(db):
    original = session(db)
    summarize(db)
    with db.cursor() as cur:
        for column in (
            "analytics_enabled",
            "guardrails_enabled",
            "analytics_revision",
            "guardrails_revision",
        ):
            cur.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
    db._ensure_tables()
    db._ensure_tables()
    restored = SessionStore(db).get("session-a", user_id="tenant")
    assert restored["analytics_enabled"] is True
    assert restored["guardrails_enabled"] is True
    assert restored["analytics_revision"] == restored["guardrails_revision"] == 1
    assert restored["intention"] == original["intention"]
    assert len(rows(db)) == 1


def test_guardrails_disable_clears_pending_state_but_not_evidence(db):
    session(db, "parent")
    initial = session(db, parent="parent")
    store = SessionStore(db)
    store.set_alignment_state(
        "session-a", user_id="tenant", state="misaligned", reason="Review"
    )
    disabled = toggle(db, guardrails_enabled=False)
    assert disabled["alignment_state"] == "ready"
    assert disabled["alignment_reason"] is None
    assert disabled["analytics_enabled"] is True
    assert disabled["guardrails_revision"] == 2
    assert disabled["state_revision"] > initial["state_revision"]
    assert not store.set_alignment_state(
        "session-a",
        user_id="tenant",
        state="pending",
        expected_state_revision=disabled["state_revision"],
    )
    assert not store.update_generated_intention(
        "session-a",
        user_id="tenant",
        intention="Stale",
        intention_source="user",
        title=None,
    )
    enabled = toggle(db, guardrails_enabled=True)
    assert enabled["alignment_state"] == "pending"
    assert not store.update_generated_intention(
        "session-a",
        user_id="tenant",
        intention="Stale ABA",
        intention_source="user",
        title=None,
        expected_guardrails_revision=1,
    )
    assert store.get("session-a", user_id="tenant")["intention"] == initial["intention"]


def test_queue_optout_revokes_claim_and_old_aba_task(db):
    session(db)
    call(db)
    queue = TaskQueue(db)
    task_id = queue.enqueue(
        "summary", "tenant", session_id="session-a", payload={"trigger": "manual"}
    )
    claimed = queue.claim_next()
    assert claimed["id"] == task_id
    toggle(db, analytics_enabled=False)
    assert queue.enqueue("summary", "tenant", session_id="session-a") is None
    llm = model()
    result = analyzer.generate_summary(db, llm, claimed)
    assert result == {"status": "skipped", "reason": "analytics_disabled"}
    toggle(db, analytics_enabled=True)
    assert analyzer.generate_summary(db, llm, claimed)["status"] == "skipped"
    assert not queue.complete(
        task_id, {"summary_id": "unpublished"}, claim_token=claimed["claim_token"]
    )
    llm.generate.assert_not_called()
    assert rows(db) == []
    assert queue.enqueue("summary", "tenant", session_id="session-a")
    assert analyzer.generate_summary(db, llm, queue.claim_next()).get("summary_id")


@pytest.mark.parametrize("reenable", [False, True])
def test_dispatched_summary_cannot_store_after_optout(db, reenable):
    session(db)
    call(db)
    entered, release = threading.Event(), threading.Event()
    llm = model()
    response = llm.generate.return_value

    def blocked(**kwargs):
        entered.set()
        assert release.wait(10)
        return response

    llm.generate.side_effect = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(analyzer.generate_summary, db, llm, task())
        try:
            assert entered.wait(10)
            toggle(db, analytics_enabled=False)
            if reenable:
                toggle(db, analytics_enabled=True)
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "skipped"
    assert llm.generate.call_count == 1
    assert rows(db) == []
    assert SessionStore(db).get("session-a", user_id="tenant")["summary_count"] == 0


def test_optout_between_response_and_persistence_is_fenced(db, monkeypatch):
    session(db)
    call(db)
    original = analyzer._store_summary

    def optout_before_store(*args, **kwargs):
        toggle(db, analytics_enabled=False)
        return original(*args, **kwargs)

    monkeypatch.setattr(analyzer, "_store_summary", optout_before_store)
    assert analyzer.generate_summary(db, model(), task())["status"] == "skipped"
    assert rows(db) == []


def test_only_affected_sources_revoke_task_completion(db):
    session(db)
    session(db, "unrelated", agent="other-worker")
    call(db)
    queue = TaskQueue(db)
    task_id = queue.enqueue(
        "summary", "tenant", session_id="session-a", payload={"trigger": "manual"}
    )
    claimed = queue.claim_next()
    result = analyzer.generate_summary(db, model(), claimed)
    assert result.get("summary_id")
    toggle(db, "unrelated", analytics_enabled=False)
    assert queue.complete(task_id, result, claim_token=claimed["claim_token"])


def test_l3_completion_is_revoked_after_execution_returns(db):
    for sid in ("allowed-a", "allowed-b"):
        session(db, sid)
        summarize(db, sid, sid)
    queue = TaskQueue(db)
    task_id = queue.enqueue("analysis", "tenant", agent_id="worker")
    claimed = queue.claim_next()
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "risk_level": 1,
            "findings": [],
            "recommendations": [],
            "context_summary": "Allowed",
        }
    )
    result = analyzer.run_analysis(db, llm, claimed)
    assert result.get("analysis_id")
    toggle(db, "allowed-a", analytics_enabled=False)
    assert not queue.complete(task_id, result, claim_token=claimed["claim_token"])
    with db.cursor() as cur:
        cur.execute("SELECT status FROM analysis_tasks WHERE id = ?", (task_id,))
        assert cur.fetchone()["status"] == "cancelled"


@pytest.mark.parametrize("guardrails", [False, True])
def test_proactive_intention_independent_of_analytics(db, guardrails):
    session(db)
    call(db)
    toggle(db, analytics_enabled=False, guardrails_enabled=guardrails)
    llm = MagicMock()
    llm.generate.return_value = "Review the project without edits"
    result = generate_intention(
        db=db,
        llm=llm,
        session_store=SessionStore(db),
        user_id="tenant",
        session_id="session-a",
    )
    assert bool(result) is guardrails
    assert llm.generate.call_count == int(guardrails)
    if not guardrails:
        assert (
            TaskQueue(db).enqueue_bootstrap_if_no_user_message("tenant", "session-a")
            is None
        )


def test_proactive_intention_aba_cannot_publish_or_store(db):
    original = session(db)
    call(db)
    llm, bus, events = MagicMock(), MagicMock(), MagicMock()

    def optout(*args, **kwargs):
        toggle(db, guardrails_enabled=False)
        toggle(db, guardrails_enabled=True)
        return "A stale generated intention"

    llm.generate.side_effect = optout
    assert (
        generate_intention(
            db=db,
            llm=llm,
            session_store=SessionStore(db),
            user_id="tenant",
            session_id="session-a",
            event_bus=bus,
            event_store=events,
        )
        is None
    )
    bus.publish.assert_not_called()
    events.append.assert_not_called()
    assert (
        SessionStore(db).get("session-a", user_id="tenant")["intention"]
        == original["intention"]
    )


def test_provider_retry_observes_optout_before_new_dispatch(db, llm_with, monkeypatch):
    session(db)
    fence = GuardrailsFence(db, "tenant", "session-a")
    client, calls, _ = llm_with([_StatusError(503), '{"ok": true}'])
    monkeypatch.setattr(
        "intaris.llm.time.sleep", lambda delay: toggle(db, guardrails_enabled=False)
    )
    with dispatch_scope(fence.check), pytest.raises(ParticipationRevoked):
        client.generate([{"role": "user", "content": "Synthetic request"}])
    assert len(calls) == 1
    client.close()


def test_direct_jev_intention_retry_is_fenced(db, monkeypatch):
    session(db)
    call(db)
    toggle(db, policy={"evaluation_policy_version": "legacy", "intention_gate": "jev"})
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503, request=request, json={"error": "temporary"})

    gate = JevClient(
        JevConfig(api_key="synthetic-test-key"),
        transport=httpx.MockTransport(respond),
    )
    monkeypatch.setattr(
        "intaris.jev.time.sleep", lambda delay: toggle(db, guardrails_enabled=False)
    )
    llm = model()
    assert (
        generate_intention(
            db=db,
            llm=llm,
            session_store=SessionStore(db),
            user_id="tenant",
            session_id="session-a",
            jev=gate,
        )
        is None
    )
    assert sum(request.method == "POST" for request in calls) == 1
    llm.generate.assert_not_called()
    gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_worker_rejects_revoked_l3_before_alerts_and_success_metrics(
    db, monkeypatch, revoke
):
    import asyncio

    for sid in ("allowed-a", "allowed-b"):
        session(db, sid)
        summarize(db, sid, sid)
    queue = TaskQueue(db)
    task_id = queue.enqueue("analysis", "tenant", agent_id="worker")
    worker = BackgroundWorker(db, AnalysisConfig(), task_queue=queue)
    worker._catchup_event = asyncio.Event()
    worker._catchup_event.set()
    worker._running = True
    worker._event_bus = MagicMock()
    worker._notification_dispatcher = MagicMock()
    worker._notify_analysis_alert = AsyncMock()
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "risk_level": 9,
            "findings": [],
            "recommendations": [],
            "context_summary": "Elevated",
        }
    )

    async def execute(claimed):
        result = analyzer.run_analysis(db, llm, claimed)
        assert result.get("analysis_id")
        if revoke:
            toggle(db, "allowed-a", analytics_enabled=False)
        worker._running = False
        return result

    monkeypatch.setattr(worker, "_execute_analysis_task", execute)
    await worker._queue_wakeup.start()
    try:
        await asyncio.wait_for(worker._worker_loop(), timeout=10)
    finally:
        await worker._queue_wakeup.stop()
    assert worker._notify_analysis_alert.await_count == int(not revoke)
    assert worker._event_bus.publish.call_count == int(not revoke)
    assert worker.metrics.analyses_completed_total == int(not revoke)
    with db.cursor() as cur:
        cur.execute("SELECT status FROM analysis_tasks WHERE id = ?", (task_id,))
        assert cur.fetchone()["status"] == ("cancelled" if revoke else "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_persisted_partial_alert_survives_retry_exhaustion_unless_revoked(
    db, monkeypatch, revoke
):
    import asyncio

    session(db)
    queue = TaskQueue(db)
    task_id = queue.enqueue("summary", "tenant", session_id="session-a")
    worker = BackgroundWorker(db, AnalysisConfig(), task_queue=queue)
    worker._catchup_event = asyncio.Event()
    worker._catchup_event.set()
    worker._running = True
    worker._event_bus = MagicMock()
    worker._notification_dispatcher = MagicMock()
    worker._notify_summary_alert = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_get_analysis_llm", model)
    monkeypatch.setattr("intaris.background._RETRY_BACKOFF", [0, 0, 0])
    attempts = 0

    def partial_failure(db, llm, claimed, event_store):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            analyzer._store_summary(
                db,
                user_id="tenant",
                session_id="session-a",
                window_start="2026-01-01T00:00:00+00:00",
                window_end="2026-01-02T00:00:00+00:00",
                trigger="manual",
                summary_type="window",
                result={
                    "summary": "Persisted concern",
                    "intent_alignment": "misaligned",
                },
                stats={},
            )
            if revoke:
                toggle(db, analytics_enabled=False)
        if revoke or attempts == 3:
            worker._running = False
        raise RuntimeError("Later partition unavailable")

    monkeypatch.setattr(analyzer, "generate_summary", partial_failure)
    await worker._queue_wakeup.start()
    try:
        await asyncio.wait_for(worker._worker_loop(), timeout=10)
    finally:
        await worker._queue_wakeup.stop()
    assert attempts == (1 if revoke else 3)
    assert worker._notify_summary_alert.await_count == int(not revoke)
    assert worker.metrics.summaries_generated_total == 0
    assert not any(
        c.args[0]["type"] == "task_completed"
        for c in worker._event_bus.publish.call_args_list
    )
    with db.cursor() as cur:
        cur.execute("SELECT status FROM analysis_tasks WHERE id = ?", (task_id,))
        assert cur.fetchone()["status"] == ("cancelled" if revoke else "failed")
    assert len(rows(db)) == 1


@pytest.mark.parametrize("reenable", [False, True])
def test_alignment_inflight_optout_cannot_restore_misalignment(db, reenable):
    import asyncio

    from intaris.alignment import AlignmentBarrier

    session(db, "parent")
    session(db, parent="parent")
    entered, release = threading.Event(), threading.Event()
    barrier = AlignmentBarrier(db=db, llm=MagicMock())
    bus = MagicMock()
    barrier.set_event_bus(bus)

    def review(**kwargs):
        entered.set()
        assert release.wait(10)
        return False, "Stale alignment result"

    barrier.check = MagicMock(side_effect=review)

    async def run():
        await barrier.trigger("tenant", "session-a")
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            toggle(db, guardrails_enabled=False)
            if reenable:
                toggle(db, guardrails_enabled=True)
        finally:
            release.set()
        await barrier.wait("tenant", "session-a")
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert barrier.is_misaligned("tenant", "session-a") is None
    bus.publish.assert_not_called()
    assert (
        SessionStore(db).get("session-a", user_id="tenant")["alignment_state"]
        != "misaligned"
    )


def test_l3_excludes_direct_and_ambiguous_ancestor_summaries(db):
    session(db, "allowed-a")
    session(db, "allowed-b")
    session(db, "parent")
    session(db, "excluded", parent="parent")
    summarize(db, "allowed-a", "ALLOWED_A")
    summarize(db, "allowed-b", "ALLOWED_B")
    summarize(db, "excluded", "EXCLUDED_DIRECT")
    summarize(db, "parent", "EXCLUDED_OLD_AGGREGATE")
    toggle(db, "excluded", analytics_enabled=False)
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "risk_level": 1,
            "findings": [],
            "recommendations": [],
            "context_summary": "Allowed sessions",
        }
    )
    result = analyzer.run_analysis(
        db, llm, {"user_id": "tenant", "agent_id": "worker", "payload": {}}
    )
    assert result.get("analysis_id")
    prompt = repr(llm.generate.call_args_list)
    assert "ALLOWED_A" in prompt and "ALLOWED_B" in prompt
    assert "EXCLUDED_DIRECT" not in prompt
    assert "EXCLUDED_OLD_AGGREGATE" not in prompt
    assert rows(db, "excluded")[0]["summary"] == "EXCLUDED_DIRECT"
    assert rows(db, "parent")[0]["summary"] == "EXCLUDED_OLD_AGGREGATE"
    assert analyzer._get_prior_summary(db, "tenant", "parent") is None
    toggle(db, "excluded", analytics_enabled=True)
    assert (
        analyzer._get_prior_summary(db, "tenant", "parent") == "EXCLUDED_OLD_AGGREGATE"
    )


def test_new_clean_parent_summary_can_be_reused_without_erasing_old(db):
    session(db, "parent")
    session(db, "child", parent="parent")
    summarize(db, "parent", "OLD_CHILD_CONTENT")
    toggle(db, "child", analytics_enabled=False)
    summarize(db, "parent", "CLEAN_PARENT")
    scope = AnalyticsScope(db, "tenant")
    summaries = rows(db, "parent")
    assert not scope.usable_summary(summaries[0])
    assert scope.usable_summary(summaries[1])
    assert len(summaries) == 2


def test_child_collection_reuses_one_participation_snapshot(db, monkeypatch):
    session(db, "parent")
    for index in range(3):
        sid = f"child-{index}"
        session(db, sid, parent="parent")
        summarize(db, sid)
    children = analyzer._get_child_sessions(db, "tenant", "parent")
    snapshots = MagicMock(wraps=AnalyticsScope)
    monkeypatch.setattr("intaris.session_modes.AnalyticsScope", snapshots)

    assert len(analyzer._collect_child_data(db, "tenant", children)) == 3
    assert snapshots.call_count == 1


def test_parent_recovery_reuses_snapshot_per_tenant(db, monkeypatch):
    for index in range(3):
        parent = f"parent-{index}"
        child = f"child-{index}"
        session(db, parent)
        session(db, child, parent=parent)
        summarize(db, child)
    worker = BackgroundWorker(db, AnalysisConfig(enabled=True))
    snapshots = MagicMock(wraps=AnalyticsScope)
    monkeypatch.setattr("intaris.session_modes.AnalyticsScope", snapshots)

    assert worker._enqueue_changed_parents() == 3
    assert snapshots.call_count == 1


def test_descendant_checks_do_not_rescan_unrelated_sessions():
    database = MagicMock()
    data = [
        {
            "session_id": f"unrelated-{index}",
            "parent_session_id": None,
            "analytics_enabled": True,
            "analytics_revision": 1,
        }
        for index in range(25000)
    ]
    for sid, parent, enabled in (
        ("root", "grandchild", True),
        ("child", "root", True),
        ("grandchild", "child", False),
    ):
        data.append(
            {
                "session_id": sid,
                "parent_session_id": parent,
                "analytics_enabled": enabled,
                "analytics_revision": 1,
            }
        )
    database.cursor.return_value.__enter__.return_value.fetchall.return_value = data
    scope = AnalyticsScope(database, "tenant")

    class IndexedOnly(dict):
        def items(self):
            raise AssertionError("Descendant traversal rescanned all sessions")

    scope.sessions = IndexedOnly(scope.sessions)
    assert scope.descendants("root") == {"child", "grandchild"}
    assert scope.exclusions("root") == {"grandchild"}
    scope.include("root")
    assert scope.watched == {"root", "child", "grandchild"}
    assert scope.descendants("missing") == set()


def test_parent_recovery_keeps_tenant_snapshots_separate(db, monkeypatch):
    for tenant in ("tenant-a", "tenant-b"):
        for sid, parent in (("parent", None), ("child", "parent")):
            SessionStore(db).create(
                user_id=tenant,
                session_id=sid,
                intention="Inspect synthetic fixtures",
                parent_session_id=parent,
            )
        analyzer._store_summary(
            db,
            user_id=tenant,
            session_id="child",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-02T00:00:00+00:00",
            trigger="manual",
            summary_type="window",
            result={"summary": tenant, "intent_alignment": "aligned"},
            stats={},
        )
    snapshots = MagicMock(wraps=AnalyticsScope)
    monkeypatch.setattr("intaris.session_modes.AnalyticsScope", snapshots)
    collect = analyzer._collect_child_data
    seen = []

    def check_scope(database, user_id, children, *, scope):
        assert scope.user_id == user_id
        result = collect(database, user_id, children, scope=scope)
        assert result[0]["summary"] == user_id
        seen.append(user_id)
        return result

    monkeypatch.setattr(analyzer, "_collect_child_data", check_scope)
    worker = BackgroundWorker(db, AnalysisConfig(enabled=True))
    assert worker._enqueue_changed_parents() == 2
    assert snapshots.call_count == 2
    assert seen == ["tenant-a", "tenant-b"]


def test_l3_optout_during_dispatch_does_not_store_or_replace_profile(db):
    for sid in ("allowed-a", "allowed-b"):
        session(db, sid)
        summarize(db, sid, sid)
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "risk_level": 2,
            "findings": [],
            "recommendations": [],
            "context_summary": "Earlier profile",
        }
    )
    job = {"user_id": "tenant", "agent_id": "worker", "payload": {}}
    first = analyzer.run_analysis(db, llm, job)
    assert first.get("analysis_id")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM behavioral_profiles WHERE user_id = ?", ("tenant",))
        profile = dict(cur.fetchone())

    def optout(**kwargs):
        toggle(db, "allowed-a", analytics_enabled=False)
        toggle(db, "allowed-a", analytics_enabled=True)
        return llm.generate.return_value

    llm.generate.side_effect = optout
    assert analyzer.run_analysis(db, llm, job)["status"] == "skipped"
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM behavioral_analyses WHERE user_id = ?", ("tenant",)
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT * FROM behavioral_profiles WHERE user_id = ?", ("tenant",))
        assert dict(cur.fetchone()) == profile


@pytest.mark.parametrize("guardrails", [False, True])
@pytest.mark.parametrize("analytics", [False, True])
def test_api_modes_acknowledged_recorded_and_explicit_evaluation_enforced(
    client_no_auth, guardrails, analytics
):
    client = client_no_auth
    headers = {"X-User-Id": "mode-user", "X-Agent-Id": "mode-agent"}
    response = client.post(
        "/api/v1/intention",
        headers=headers,
        json={
            "session_id": "mode-session",
            "intention": "Read project",
            "analytics_enabled": analytics,
            "guardrails_enabled": guardrails,
        },
    )
    response.raise_for_status()
    assert response.json()["analytics_enabled"] is analytics
    assert response.json()["guardrails_enabled"] is guardrails
    body = client.get("/api/v1/session/mode-session", headers=headers).json()
    assert (
        body["analytics_enabled"] is analytics
        and body["guardrails_enabled"] is guardrails
    )
    patched = client.patch(
        "/api/v1/session/mode-session",
        headers=headers,
        json={
            "analytics_enabled": None,
            "guardrails_enabled": None,
        },
    )
    assert patched.json()["analytics_enabled"] is analytics
    assert patched.json()["guardrails_enabled"] is guardrails
    result = client.post(
        "/api/v1/evaluate",
        headers=headers,
        json={
            "session_id": "mode-session",
            "tool": "read",
            "args": {"path": "/workspace/project/file.py"},
            "minimum_outcome": "deny",
        },
    )
    result.raise_for_status()
    assert result.json()["decision"] == "deny"
    recorded = client.post(
        "/api/v1/session/mode-session/agent-summary",
        headers=headers,
        json={"summary": "Recorded, not generated"},
    )
    recorded.raise_for_status()
    history = client.get("/api/v1/session/mode-session/summary", headers=headers)
    history.raise_for_status()
    assert history.json()["analytics_enabled"] is analytics
    assert len(history.json()["agent_summaries"]) == 1
    if not analytics:
        rejected = client.post(
            "/api/v1/session/mode-session/summary/trigger", headers=headers
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "analytics_disabled"


def test_api_creation_rejects_non_boolean_and_patch_preserves_defaults(client_no_auth):
    headers = {"X-User-Id": "mode-user", "X-Agent-Id": "mode-agent"}
    for bad in (None, 0, "false"):
        response = client_no_auth.post(
            "/api/v1/intention",
            headers=headers,
            json={
                "session_id": "invalid-mode-value",
                "intention": "Read",
                "analytics_enabled": bad,
            },
        )
        assert response.status_code == 422
    created = client_no_auth.post(
        "/api/v1/intention",
        headers=headers,
        json={
            "session_id": "default-session",
            "intention": "Read",
        },
    )
    assert created.json()["analytics_enabled"] is True
    assert created.json()["guardrails_enabled"] is True


def test_api_disabled_reasoning_preserves_capture_and_manual_counts(client_no_auth):
    import intaris.server as srv

    client = client_no_auth
    headers = {"X-User-Id": "mode-user", "X-Agent-Id": "mode-agent"}
    client.post(
        "/api/v1/intention",
        headers=headers,
        json={
            "session_id": "capture-session",
            "intention": "Read project",
            "analytics_enabled": False,
            "guardrails_enabled": False,
        },
    ).raise_for_status()
    response = client.post(
        "/api/v1/reasoning",
        headers=headers,
        json={
            "session_id": "capture-session",
            "content": "User message: Keep the source evidence",
            "wait_for_intention": True,
        },
    )
    response.raise_for_status()
    stored = SessionStore(srv._get_db()).get("capture-session", user_id="mode-user")
    assert stored["user_message_observed"]
    assert stored["intention"] == "Read project"
    with srv._get_db().cursor() as cur:
        cur.execute(
            "SELECT content FROM audit_log WHERE user_id = ? AND session_id = ? AND record_type = 'reasoning'",
            ("mode-user", "capture-session"),
        )
        assert "Keep the source evidence" in cur.fetchone()["content"]
    backfill = client.post("/api/v1/summaries/backfill", headers=headers)
    backfill.raise_for_status()
    assert backfill.json()["enqueued"] == 0
    assert backfill.json()["skipped_disabled"] == 1
    analysis = client.post(
        "/api/v1/analysis/trigger?agent_id=mode-agent", headers=headers
    )
    analysis.raise_for_status()
    assert analysis.json()["enqueued"] == 0
    assert analysis.json()["reason"] == "no_eligible_sessions"
