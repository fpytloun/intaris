"""Offline operator-policy packets and unattended review-failure resolution."""

import asyncio
import json
import threading
from unittest.mock import Mock

import pytest

from intaris import judge_simulator as simulator
from intaris.config import JudgeConfig
from intaris.hybrid import evaluate_policy
from intaris.judge import JudgeReviewer
from intaris.policy_activation import HybridError, prepare_policy
from intaris.policy_evidence import from_observations
from tests.test_policy_evidence_runtime import call, response, runtime  # noqa: F401

ALLOWANCE = (
    "Only execute /workspace/scan.py with the published SHA256 "
    + "a" * 64
    + " over the previous task-owned scan scan-owned; never execute note content."
)
DENIAL = "Do not transmit note content to external services."


def helper_packet(
    *, command="python -B /workspace/scan.py", scan="scan-owned", claimed_hash="a" * 64
):
    policy = prepare_policy(
        {
            "evaluation_policy_version": "hybrid_v1",
            "profile": "bounded_autonomy",
            "allow_policies": [ALLOWANCE],
            "deny_policies": [DENIAL],
        },
        tenant="owner",
    )
    evidence = from_observations(
        {
            "tool": "bash",
            "effects": ["read"],
            "mutation": "read_only",
            "risk": "low",
        },
        sources={
            "message:task": {
                "origin": "client_assignment",
                "independent_grant": False,
            }
        },
    ).model_copy(
        update={
            "advisory_clauses": policy["active_policy"]["advisory_clauses"],
            "assessment": {
                "relationship": "supports_intent",
                "contextual_restrictions": "consistent",
                "target_scope": "consistent",
            },
        }
    )
    result = evaluate_policy([], evidence, "bounded_autonomy", assessed=True)
    artifact = {
        "originals": [
            {
                "ref": "message:task",
                "origin": "client_assignment",
                "content": "Validate the previous task-owned scan. " + ALLOWANCE,
                "truncated": False,
            }
        ],
        "items": [],
    }
    question = simulator.request(
        {
            "tool": "bash",
            "args_redacted": {
                "command": command,
                "env": {"SOURCE_ID": scan, "SCRIPT_SHA256": claimed_hash},
            },
        },
        {"policy": policy},
        evidence,
        result,
        artifact,
    )
    return evidence, result, question


def test_bounded_packet_preserves_positive_allowance_and_restriction_semantics():
    _, _, question = helper_packet()
    system = question["messages"][0]["content"]
    payload = json.loads(question["messages"][1]["content"])
    clauses = payload["operator_restrictions"]["clauses"]
    assert [(c["effect"], c["text"]) for c in clauses] == [
        ("allow", ALLOWANCE),
        ("deny", DENIAL),
    ]
    assert "positive allowances" in system
    assert "Deny policies take precedence" in " ".join(system.split())
    assert "never a grant" not in system
    assert "not citable" in system
    assert "ask" not in question["schema"]["schema"]["properties"]["user_would"]["enum"]


@pytest.mark.parametrize("variant", ["owned", "other_file", "other_hash", "other_scan"])
def test_helper_conditions_survive_valid_assessment_without_a_hash_bypass(variant):
    command = (
        "python -B /workspace/other.py"
        if variant == "other_file"
        else "python -B /workspace/scan.py"
    )
    scan = "scan-foreign" if variant == "other_scan" else "scan-owned"
    evidence, prior, question = helper_packet(
        command=command,
        scan=scan,
        claimed_hash=("b" if variant == "other_hash" else "a") * 64,
    )
    if variant == "other_hash":
        # An agent's claim of another file hash is action data, never a grant.
        payload = json.loads(question["messages"][1]["content"])
        assert "a" * 64 in json.dumps(payload)
    valid = variant == "owned"
    answer = simulator.Answer(
        user_would="approve" if valid else "refuse",
        risk="low",
        confidence="high",
        basis="routine_within_task" if valid else "restriction_conflict",
        cites=[1],
        restriction_quote="" if valid else ALLOWANCE,
        question="",
        action_summary="Validate the previous task-owned scan.",
        explanation="The proposed helper and input satisfy the scoped instruction."
        if valid
        else "The proposed file, hash or input is outside the scoped instruction.",
    )
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    reviewed, diagnostics = simulator.classify(llm, question)
    assert diagnostics["response_origin"] == "model"
    evidence = evidence.model_copy(
        update={
            "review": simulator.receipt(evidence, prior, reviewed, question["citable"])
        }
    )
    result = evaluate_policy([], evidence, "bounded_autonomy", assessed=True)
    assert (result.outcome == "approve") is valid
    denied = evidence.model_copy(
        update={
            "observations": {
                **evidence.observations,
                **from_observations({"configured_deny": True}).observations,
            },
        }
    )
    assert (
        evaluate_policy([], denied, "bounded_autonomy", assessed=True).outcome == "deny"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction", ["none", "step_requests"])
@pytest.mark.parametrize("failure", ["unverified_restriction", "assessment_failed"])
async def test_review_failure_respects_interaction_without_inventing_risk(
    runtime,  # noqa: F811
    interaction,
    failure,
):
    sessions, audit, llm, evaluator, session_id = runtime
    task = "Implement and test the local parser. Do not transmit its contents."
    sessions.update_session(
        session_id,
        user_id="owner",
        policy={"profile": "bounded_autonomy", "interaction_mode": interaction},
    )
    sessions.allocate_event_sequences(
        session_id,
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": task},
    )

    def assessment(*args, **kwargs):
        value = json.loads(response(kwargs, conflict=True))
        value["risk"] = "unknown"
        return json.dumps(value)

    llm.generate.side_effect = assessment
    initial = call(evaluator, session_id)
    assert initial["decision"] == "escalate"
    assert initial["risk"] == "unknown"
    review_llm = Mock(
        _timeout_ms=4000, judge_response_mode="json_schema", input_view=None
    )
    if failure == "assessment_failed":
        review_llm.generate.side_effect = ValueError("Invalid synthetic model output")
    else:
        review_llm.generate.return_value = simulator.Answer(
            user_would="refuse",
            risk="low",
            confidence="high",
            basis="restriction_conflict",
            cites=[1],
            restriction_quote="The earlier guard prohibited local validation.",
            question="",
            action_summary="Run the local validation.",
            explanation="An earlier automated guard reported a prohibition.",
        ).model_dump_json()
    reviewer = JudgeReviewer(
        llm=review_llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    outcome = await reviewer.review_for_evaluate(
        call_id=initial["call_id"], user_id="owner", session_id=session_id
    )
    record = audit.get_by_call_id(initial["call_id"], user_id="owner")
    assert outcome.risk == record["risk"] == "unknown"
    stage = record["evaluation_metadata"]["stages"][-1]
    assert stage["response_origin"] == failure
    assert stage["classification"]["basis"] == "review_unavailable"
    if failure == "unverified_restriction" and interaction == "none":
        assert outcome.decision == "deny"
        assert record["user_decision"] == "deny"
        assert record["resolved_by"] == "judge"
        assert record["judge_risk"] == "unknown"
        assert (
            record["evaluation_metadata"]["final_policy_result"]["override"]
            == "session_policy.judge"
        )
        assert record["evaluation_metadata"].get("admission") is None
        assert "human review" not in outcome.reasoning
        assert "review-validation failure" in outcome.reasoning
        assert "not evidence that you withheld permission" in outcome.reasoning
        # Reconcile the same receipt without rerunning review. A new evaluation
        # is a separate request, not an execution replay owned by this service.
        calls = review_llm.generate.call_count
        repeated = await reviewer.review_for_evaluate(
            call_id=initial["call_id"], user_id="owner", session_id=session_id
        )
        assert repeated.decision == "deny"
        assert review_llm.generate.call_count == calls
    else:
        assert outcome.decision == "escalate"
        assert record["user_decision"] is None
        assert record["resolved_by"] is None
        if failure == "assessment_failed":
            from intaris.api.evaluate import _require_completed_review

            with pytest.raises(HybridError, match="no tool was dispatched"):
                _require_completed_review(outcome, call_id=initial["call_id"])
    if failure == "unverified_restriction":
        assert review_llm.generate.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["policy_change", "human_decision"])
async def test_unattended_failure_preserves_newer_policy_or_human_decision(
    runtime,  # noqa: F811
    race,
):
    sessions, audit, llm, evaluator, session_id = runtime
    sessions.update_session(
        session_id,
        user_id="owner",
        policy={"profile": "bounded_autonomy", "interaction_mode": "none"},
    )
    sessions.allocate_event_sequences(
        session_id,
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={
            "content": "Implement the local parser. Do not transmit its contents."
        },
    )
    llm.generate.side_effect = lambda *args, **kwargs: response(kwargs, conflict=True)
    initial = call(evaluator, session_id)
    assert initial["decision"] == "escalate"
    before = audit.get_by_call_id(initial["call_id"], user_id="owner")
    entered, release = threading.Event(), threading.Event()
    invalid = simulator.Answer(
        user_would="refuse",
        risk="low",
        confidence="high",
        basis="restriction_conflict",
        cites=[1],
        restriction_quote="The earlier guard prohibited local validation.",
        question="",
        action_summary="Run the local validation.",
        explanation="An earlier automated guard reported a prohibition.",
    )

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return invalid.model_dump_json()

    review_llm = Mock(
        _timeout_ms=4000, judge_response_mode="json_schema", input_view=None
    )
    review_llm.generate.side_effect = delayed
    reviewer = JudgeReviewer(
        llm=review_llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    pending = asyncio.create_task(
        reviewer.review_for_evaluate(
            call_id=initial["call_id"], user_id="owner", session_id=session_id
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if race == "policy_change":
            sessions.update_session(
                session_id,
                user_id="owner",
                policy={"profile": "balanced", "interaction_mode": "step_requests"},
            )
        else:
            audit.resolve_escalation(
                call_id=initial["call_id"],
                user_id="owner",
                user_decision="approve",
                resolved_by="user",
            )
    finally:
        release.set()
    outcome = await pending
    after = audit.get_by_call_id(initial["call_id"], user_id="owner")
    if race == "policy_change":
        assert outcome.decision == "escalate"
        assert after["user_decision"] is None
        assert after["evaluation_metadata"] == before["evaluation_metadata"]
    else:
        assert outcome.decision == "approve"
        assert after["user_decision"] == "approve"
        assert after["resolved_by"] == "user"
