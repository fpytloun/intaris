"""Offline actual-app/SDK acceptance of single generation and existing render."""

import pytest

from tests.test_api import _reset_server_globals, env_no_auth, tmp_db  # noqa: F401
from tests.test_source_context_service import (
    create,
    evaluate,
    reason,
    record,
    service,
    service_databases,  # noqa: F401
)

# Expose the existing fixture without changing its ownership or old assertions.
service = service


@pytest.mark.parametrize(
    "service",
    [
        (backend, "source_context", "single", db)
        for backend in ("llm", "jev")
        for db in ("sqlite", "postgresql")
    ],
    indirect=True,
)
def test_no_user_service_uses_mixed_input_not_a_gap(service):
    client, app, wire = service
    sid = create(client)
    result = evaluate(client, sid)
    metadata = result["evaluation_metadata"]
    assert metadata["context_input"]["mode"] == "mixed_no_user_evidence"
    assert metadata.get("task_context") is None
    assert not [
        body for stage, body in wire if stage in {"extract", "intention", "mock-error"}
    ]
    l1 = next(body for stage, body in wire if stage.startswith("l1-"))
    prompt = l1["state"]["prompt"] if "state" in l1 else l1["messages"][1]["content"]
    assert "Inspect the local repository" in prompt
    assert "Gaps:" not in prompt


@pytest.mark.parametrize(
    "service",
    [("llm", "source_context", "single", db) for db in ("sqlite", "postgresql")],
    indirect=True,
)
@pytest.mark.parametrize("form", ["single_permission", "alternatives"])
def test_confirmation_proof_through_app_and_l1_lines(service, form):
    client, app, wire = service
    sid = create(client)
    action = "upload the snapshot after review"
    question = (
        f"May I {action}?"
        if form == "single_permission"
        else f"Should I update the config or {action}?"
    )
    actions = [action] if form == "single_permission" else ["update the config", action]
    app.state.offline_provider_hooks["intention_answer"] = lambda sources: {
        "title": "Snapshot work",
        "intention": "Handle the recorded reply.",
        "items": [
            {
                "kind": "approval",
                "msg": sources[-1]["msg"],
                "quote": "yes",
                "status": "current",
                "confirmation": {"form": form, "actions": actions},
            }
        ],
        "current": [],
    }
    record(
        client,
        sid,
        {"type": "assistant_message", "data": {"content": question}},
        {"type": "user_message", "data": {"content": "yes"}},
    )
    reason(client, sid)
    result = evaluate(client, sid)
    artifact = result["evaluation_metadata"]["task_context"]
    value = artifact["items"][0]
    assert value["kind"] == (
        "approval" if form == "single_permission" else "uncertainty"
    )
    assert len([body for stage, body in wire if stage == "intention"]) == 1
    assert not [body for stage, body in wire if stage == "extract"]
    from intaris import l1_projection
    from tools.l1_projection_sizes import specimen

    _, state, _ = specimen("inbox-inspection-only")
    rendered = l1_projection.render(artifact, state, {})
    assert "Approved action:" not in rendered
    assert action in rendered
    assert "linked_non_authorizing_context" in rendered
    if form == "single_permission":
        assert value["quote"] == "yes"
        assert value["kind"] == "approval"
    else:
        assert value["kind"] == "uncertainty"


@pytest.mark.parametrize(
    "service",
    [
        (backend, "source_context", "single", db)
        for backend in ("llm", "jev")
        for db in ("sqlite", "postgresql")
    ],
    indirect=True,
)
def test_actual_service_single_generation_and_linked_yes(service):
    client, app, wire = service
    sid = create(client)
    record(
        client,
        sid,
        {
            "type": "assistant_message",
            "data": {"content": "May I send exactly draft X to Y?"},
        },
        {"type": "user_message", "data": {"content": "yes"}},
    )
    reason(client, sid)
    result = evaluate(client, sid)
    assert not [body for stage, body in wire if stage == "mock-error"]
    assert len([body for stage, body in wire if stage == "intention"]) == 1
    assert not [body for stage, body in wire if stage == "extract"]
    artifact = result["evaluation_metadata"]["task_context"]
    assert artifact["status"] == "ready"
    assert artifact["extraction_calls_this_evaluation"] == 0
    assert artifact["items"][0]["kind"] == "approval"
    assert artifact["items"][0]["quote"] == "yes"
    assert (
        artifact["items"][0]["antecedent"]["content"]
        == "May I send exactly draft X to Y?"
    )
    request = next(body for stage, body in wire if stage == "intention")
    assert request["model"] == "openai/gpt-oss-120b"
    assert request["reasoning_effort"] == "low"
    evaluate(client, sid)
    assert len([body for stage, body in wire if stage == "intention"]) == 1
    assert not [body for stage, body in wire if stage == "extract"]


@pytest.mark.parametrize(
    "service",
    [("llm", "source_context", "single", db) for db in ("sqlite", "postgresql")],
    indirect=True,
)
@pytest.mark.parametrize(
    "variant",
    [
        "linked",
        "intervening_user",
        "synthetic",
        "truncated",
        "wrong_identity",
    ],
)
def test_opencode_identity_bound_text_part_antecedents(service, variant):
    client, app, wire = service
    sid = create(client)
    proposal = "May I send exactly draft X to Y?"
    events = [
        {
            "type": "message",
            "data": {"role": "assistant", "messageID": "assistant-turn"},
        },
        {
            "type": "part",
            "data": {
                "messageID": "assistant-turn",
                "part": {
                    "id": "text-part",
                    "type": "text",
                    "messageID": "assistant-turn",
                    "text": proposal,
                },
            },
        },
    ]
    part = events[1]["data"]["part"]
    if variant == "synthetic":
        events.append(
            {
                "type": "part",
                "data": {
                    "messageID": "assistant-turn",
                    "part": {**part, "synthetic": True},
                },
            }
        )
    if variant == "truncated":
        part["text"] += "x" * 1600
    if variant == "wrong_identity":
        part["messageID"] = "different-assistant-turn"
    if variant == "intervening_user":
        events.append(
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "messageID": "other-user-turn",
                    "text": "Different task.",
                },
            }
        )
    events.extend(
        [
            {
                "type": "message",
                "data": {"role": "user", "messageID": "confirmation-turn"},
            },
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "messageID": "confirmation-turn",
                    "text": "yes",
                },
            },
        ]
    )
    record(client, sid, *events)
    reason(client, sid)
    result = evaluate(client, sid)
    assert not [body for stage, body in wire if stage == "mock-error"]
    assert len([body for stage, body in wire if stage == "intention"]) == 1
    assert not [body for stage, body in wire if stage == "extract"]
    artifact = result["evaluation_metadata"]["task_context"]
    assert artifact["status"] == "ready"
    value = artifact["items"][0]
    assert value["quote"] == "yes"
    assert value["kind"] == ("approval" if variant == "linked" else "uncertainty")
    if variant == "linked":
        assert value["antecedent"]["content"] == proposal
        assert value["antecedent"]["origin"] == "linked_non_authorizing_context"
    assert artifact["extraction_calls_this_evaluation"] == 0
    evaluate(client, sid)
    assert len([body for stage, body in wire if stage == "intention"]) == 1
    assert not [body for stage, body in wire if stage == "extract"]


@pytest.mark.parametrize(
    "service",
    [("llm", "source_context", "single", "sqlite")],
    indirect=True,
)
def test_public_history_harness_preserves_recorded_confirmation_link(service):
    import json

    from tools.benchmark.client import IntarisClient
    from tools.benchmark.source_history_runner import PublicHistoryLifecycle

    client, app, wire = service
    sid = create(client)
    bridge = IntarisClient("http://testserver", "", "default", "offline-agent")

    def request(method, path, **kwargs):
        result = client.request(method, path, **kwargs)
        assert result.is_success, result.text
        return result.json()

    bridge._request = request
    try:
        history = PublicHistoryLifecycle(bridge, sid, None)
        history.record(
            {
                "id": "recorded-proposal",
                "kind": "context",
                "text": "May I send the summary to the internal inbox?",
            },
            origin="agent",
        )
        recorded = history.record(
            {
                "id": "recorded-answer",
                "kind": "request",
                "text": "yes",
                "reply_to": "recorded-proposal",
            },
            origin="client_attributed_user",
        )
        result = evaluate(client, sid)
        source = result["evaluation_metadata"]["task_context"]["items"][0]
        assert source["kind"] == "approval"
        assert source["quote"] == "yes"
        assert (
            source["antecedent"]["content"]
            == "May I send the summary to the internal inbox?"
        )
        assert recorded["event_seq"] > 0
        audit = bridge.get_audit_record(recorded["reasoning_call_id"])
        assert audit["args_redacted"]["source_event_seq"] == recorded["event_seq"]
        generations = [body for stage, body in wire if stage == "intention"]
        assert len(generations) == 1
        payload = json.loads(generations[0]["messages"][1]["content"].splitlines()[1])
        assert (
            payload["sources"][-1]["preceding_assistant"]
            == source["antecedent"]["content"]
        )
        assert not [body for stage, body in wire if stage == "extract"]
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "service",
    [("llm", "source_context", "single", "sqlite")],
    indirect=True,
)
def test_fenced_debug_request_repair_through_recording_reasoning_and_evaluation(
    service,
):
    from tests.test_intention_evidence_boundaries import MESSAGE, REQUEST
    from tests.test_single_intention import item

    client, app, wire = service
    sid = create(client)
    attempts = 0

    def intention_answer(sources):
        nonlocal attempts
        attempts += 1
        msg = sources[-1]["msg"]
        assert sources[-1]["segments"][1]["kind"] == "embedded_context"
        return {
            "title": "Debug restart failure",
            "intention": "Debug the restart failure.",
            "items": (
                [item(MESSAGE, msg=msg)]
                if attempts == 1
                else [item(REQUEST, msg=msg), item(MESSAGE, "context", msg=msg)]
            ),
            "current": [0],
        }

    app.state.offline_provider_hooks["intention_answer"] = intention_answer
    record(client, sid, {"type": "user_message", "data": {"content": MESSAGE}})
    response = client.post(
        "/api/v1/reasoning",
        json={
            "session_id": sid,
            "from_events": True,
            "wait_for_intention": True,
            "wait_timeout_ms": 10000,
        },
    )
    assert response.status_code == 200, response.text
    persisted = client.get(f"/api/v1/session/{sid}").json()
    assert persisted["title"] == "Debug restart failure"
    result = evaluate(client, sid)
    artifact = result["evaluation_metadata"]["task_context"]
    assert artifact["status"] == "ready"
    assert artifact["items"][0]["kind"] == "request"
    assert artifact["items"][0]["quote"] == REQUEST
    assert artifact["items"][1]["kind"] == "context"
    assert attempts == 2
    assert not [body for stage, body in wire if stage in {"extract", "mock-error"}]


@pytest.mark.parametrize(
    "service",
    [
        ("llm", "source_context", "single", "sqlite"),
        ("llm", "source_context", "single", "postgresql"),
    ],
    indirect=True,
)
@pytest.mark.parametrize("valid_profile", [False, True])
def test_paragraph_title_and_failed_profile_fallback_through_api(
    service, valid_profile
):
    from tests.test_paragraph_intention_titles import MESSAGE, REQUEST
    from tests.test_single_intention import item

    client, app, wire = service
    sid = create(client)
    before = client.get(f"/api/v1/session/{sid}").json()

    def intention_answer(sources):
        return {
            "title": "Nested sidebar error",
            "intention": "Investigate the sidebar error.",
            "items": [
                item(
                    REQUEST if valid_profile else "Absent quotation",
                    msg=sources[-1]["msg"],
                )
            ],
            "current": [0],
        }

    app.state.offline_provider_hooks["intention_answer"] = intention_answer
    record(client, sid, {"type": "user_message", "data": {"content": MESSAGE}})
    response = client.post(
        "/api/v1/reasoning",
        json={
            "session_id": sid,
            "from_events": True,
            "wait_for_intention": True,
            "wait_timeout_ms": 10000,
        },
    )
    assert response.status_code == 200, response.text
    persisted = client.get(f"/api/v1/session/{sid}").json()
    assert persisted["title"] == "Nested sidebar error"
    if not valid_profile:
        assert persisted["intention"] == before["intention"]
    artifact = evaluate(client, sid)["evaluation_metadata"]["task_context"]
    assert artifact["status"] == ("ready" if valid_profile else "raw")
    if not valid_profile:
        assert artifact["fallback"] == "raw_sources"
        assert artifact["items"] == []
    assert any(source["content"] == MESSAGE for source in artifact["originals"])
    assert not [body for stage, body in wire if stage in {"extract", "mock-error"}]
