"""Relaxed profiles treat a routine low-risk Judge deferral as an approval.

The upgrade never crosses a consequential boundary or a restriction conflict.
"""

import json
from unittest.mock import Mock

import pytest

from intaris import judge_simulator
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import finalize_result
from intaris.policy_evidence import from_observations

BASH = {
    "tool": "bash",
    "effects": ["write", "transmit"],
    "mutation": "mutating",
    "external_or_irreversible": True,
}
WRITE = {"tool": "bash", "effects": ["write"], "mutation": "mutating"}


def question_for(
    profile,
    outcomes=frozenset({"approve", "escalate", "deny"}),
    facts=WRITE,
    chat_mode=None,
):
    state = from_observations(facts)
    return judge_simulator.request(
        {"tool": facts["tool"], "args_redacted": {}},
        {"policy": {"profile": profile}, "intention": "Operate homelab alerts"},
        state,
        evaluate_policy([], state, profile),
        {"originals": [], "items": []},
        outcomes=outcomes,
        chat_mode=chat_mode,
    )


def judged(question, **answer):
    value = dict(
        user_would="ask",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[],
        question="Proceed?",
        action_summary="Run the scoped maintenance command",
        explanation="The exact routine action is supported by the assignment.",
        restriction_quote="",
    )
    value.update(answer)
    llm = Mock(generate=Mock(return_value=json.dumps(value)))
    return judge_simulator.classify(llm, question)


CAPPED = frozenset({"approve", "escalate"})


@pytest.mark.parametrize(
    "profile,outcomes",
    [
        ("balanced", CAPPED),
        ("balanced", None),
        ("bounded_autonomy", CAPPED),
        ("bounded_autonomy", None),
    ],
)
@pytest.mark.parametrize("basis", ["routine_within_task", "explicit_request"])
def test_relaxed_routine_low_risk_ask_is_approve(profile, outcomes, basis):
    question = question_for(profile, *([outcomes] if outcomes else []))
    answer, diagnostics = judged(question, basis=basis)
    assert answer.user_would == "approve"
    assert answer.question == ""
    assert diagnostics["upgrade_reason"] == "ask_with_approving_basis"
    # The model's own vote stays auditable.
    assert diagnostics["model_answer"]["user_would"] == "ask"


def test_upgrade_never_covers_a_classified_restriction_conflict():
    state = from_observations(WRITE).model_copy(
        update={"assessment": {"contextual_restrictions": "conflicts"}}
    )
    question = judge_simulator.request(
        {"tool": "bash", "args_redacted": {}},
        {"policy": {"profile": "balanced"}, "intention": "Operate homelab alerts"},
        state,
        evaluate_policy([], state, "balanced"),
        {"originals": [], "items": []},
    )
    answer, diagnostics = judged(question)
    assert answer.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


@pytest.mark.parametrize(
    "facts",
    [
        {**WRITE, "effects": ["write", "delete"]},
        {**WRITE, "effects": ["disclose"]},
        {**WRITE, "reversibility": "irreversible"},
        {**WRITE, "environment": "production"},
    ],
)
def test_upgrade_never_crosses_a_consequential_boundary(facts):
    answer, diagnostics = judged(question_for("balanced", CAPPED, facts))
    assert answer.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


def test_upgrade_never_applies_in_a_plan_mode_turn():
    answer, diagnostics = judged(
        question_for("balanced", CAPPED, WRITE, chat_mode="plan")
    )
    assert answer.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


def test_upgraded_routine_approval_satisfies_balanced_policy():
    state = from_observations(WRITE).model_copy(
        update={"assessment": {"relationship": "supports_intent", "risk": "unknown"}}
    )
    pending = evaluate_policy([], state, "balanced", assessed=True)
    assert pending.outcome == "escalate"
    answer, _ = judged(question_for("balanced", CAPPED))
    reviewed = state.model_copy(
        update={"review": judge_simulator.receipt(state, pending, answer)}
    )
    assert evaluate_policy([], reviewed, "balanced", assessed=True).outcome == (
        "approve"
    )


def test_bounded_external_ask_with_approving_basis_is_approve():
    answer, _ = judged(question_for("bounded_autonomy", CAPPED, BASH))
    assert answer.user_would == "ask"


def test_balanced_external_routine_approval_needs_no_per_action_cite():
    question = question_for("balanced", CAPPED, BASH)
    assert judged(question)[0].user_would == "ask"
    assert judged(question, cites=[1])[0].user_would == "ask"


def test_strict_ask_is_unchanged():
    answer, diagnostics = judged(question_for("strict"))
    assert answer.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


@pytest.mark.parametrize(
    "answer",
    [
        {"risk": "medium"},
        {"risk": "high"},
        {"basis": "outside_task"},
        {"basis": "restriction_conflict"},
        {"basis": "irreversible_unclear"},
        {"restriction_quote": "Do not run this yet."},
    ],
)
def test_ask_with_other_basis_or_higher_risk_still_asks(answer):
    result, diagnostics = judged(question_for("balanced", CAPPED), **answer)
    assert result.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


def test_unavailable_judge_is_never_upgraded():
    llm = Mock(generate=Mock(side_effect=RuntimeError("down")))
    answer, diagnostics = judge_simulator.classify(
        llm, question_for("balanced", CAPPED)
    )
    assert answer.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


def test_upgraded_approval_still_yields_to_caps_and_human_checkpoints():
    state = from_observations(BASH).model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "risk": "low",
            }
        }
    )
    pending = evaluate_policy([], state, "bounded_autonomy")
    answer, _ = judged(question_for("bounded_autonomy", CAPPED, BASH))
    reviewed = state.model_copy(
        update={"review": judge_simulator.receipt(state, pending, answer)}
    )
    approved = evaluate_policy([], reviewed, "bounded_autonomy", assessed=True)
    assert approved.outcome == "escalate"
    assert any(o.id == "review.deferred" for o in approved.outstanding_obligations)
    floor = finalize_result(approved, maximum=None, minimum="escalate")
    assert floor.decision == "escalate"
    human = finalize_result(
        approved, maximum=None, minimum=None, human_resolution="deny"
    )
    assert human.decision == "deny"


def test_relaxed_contracts_call_own_cleanup_routine():
    assert "Distinguish harmless goal deviation" in judge_simulator.SYSTEM
    assert "policy applies outcomes" in judge_simulator.SYSTEM


def test_ask_on_a_hash_conditioned_grant_is_never_upgraded():
    """The routine upgrade cannot approve what only a verified hash could.

    Same local low-risk shape that upgrades on `explicit_request`, but the
    cited grant names a SHA-256 and no executor-verified digest is present.
    The ask stays an ask; the hash-independent control upgrades.
    """
    digest = "e" * 64
    script = f"/tmp/scratch/{digest}/tool.py"
    facts = {
        **WRITE,
        "command": f"python3 {script}",
    }
    state = from_observations(facts)
    grant = f"May run only tool.py published in the asset store, SHA256 {digest}."
    question = judge_simulator.request(
        {"tool": "bash", "args_redacted": {"command": facts["command"]}},
        {"policy": {"profile": "balanced"}, "intention": "Run the published tool"},
        state,
        evaluate_policy([], state, "balanced"),
        {
            "originals": [
                {
                    "ref": "message:grant",
                    "origin": "client_user_claim",
                    "content": grant,
                    "truncated": False,
                }
            ],
            "items": [],
        },
    )
    assert not question["consequential"] and not question["ask_upgrade_blocked"]
    index = next(
        r["index"] for r in question["citation_sources"] if r["ref"] == "message:grant"
    )
    assert index in question["hash_conditioned"]
    answer, diagnostics = judged(question, basis="explicit_request", cites=[index])
    assert answer.user_would == "ask"
    assert diagnostics.get("upgrade_reason") is None
    control, diagnostics = judged(question, basis="routine_within_task")
    assert control.user_would == "approve"
    assert diagnostics["upgrade_reason"] == "ask_with_approving_basis"
