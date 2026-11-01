"""Facts and explicit instructions decide; model opinions fill gaps per profile.

Layer under test:
- evidence verification (``policy_evidence.ground``): a model-asserted conflict
  is grounded only by an operator clause, a verbatim quote found in the cited
  source, or a current prohibition item extracted by the intention generator;
- policy (``hybrid.evaluate_policy``): grounded conflicts, configured denies,
  checkpoints, human refusals and hazardous observed effects bind in every
  profile; ungrounded opinions and unknown risk on bounded effects block in
  strict/balanced and are recorded, not enforced, in bounded autonomy;
- alignment enters as a controller fact and is reviewed through policy.
"""

from __future__ import annotations

import pytest

from intaris.alignment import (
    alignment_grounding,
    cited_instruction,
    encode_alignment_reason,
)
from intaris.hybrid import Predicate, Rule, bounded_effects, evaluate_policy
from intaris.policy_evidence import Review, from_observations, ground, quote_in

PROFILES = ("strict", "balanced", "bounded_autonomy")

STOP = "Do not push this repository at any stage."


def evidence(
    *,
    assessment: dict | None = None,
    sources: dict | None = None,
    advisory: list | None = None,
    parent_alignment: dict | None = None,
    **observations,
):
    base = {
        "tool": "bash",
        "command": "git push origin main",
        "effects": ["transmit"],
        "mutation": "mutating",
        **observations,
    }
    state = from_observations(base, sources=sources or {})
    return state.model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "risk": "medium",
                "effects": base["effects"],
                "mutation": base["mutation"],
                "contextual_restrictions": "consistent",
                "restriction_reference": "none",
                "target_scope": "consistent",
                "target_reference": "none",
                **(assessment or {}),
            },
            "advisory_clauses": advisory or [],
            "parent_alignment": parent_alignment,
        }
    )


def user_source(text, *, constraint_kinds=()):
    return {
        "origin": "client_user_claim",
        "text": text,
        "independent_grant": False,
        "constraint_items": [
            {"kind": kind, "status": "current", "quote": text}
            for kind in constraint_kinds
        ],
    }


def obligation_ids(result):
    return [item.id for item in result.outstanding_obligations]


# --- evidence verification -------------------------------------------------


def test_quote_containment_is_whitespace_and_case_insensitive_but_verbatim():
    assert quote_in("Please  DO NOT push\n this repo.", "do not push this repo")
    assert not quote_in("Do not push this repo.", "never push")
    assert not quote_in("Do not push this repo.", "")
    assert not quote_in(None, "x")


@pytest.mark.parametrize(
    "reference,quote,source,expected",
    [
        ("user:1", STOP, user_source(STOP), "grounded"),
        ("user:1", "do not push this repository", user_source(STOP), "grounded"),
        ("user:1", "Never deploy anything.", user_source(STOP), "ungrounded"),
        (
            "user:1",
            "",
            user_source(STOP, constraint_kinds=("restriction",)),
            "grounded",
        ),
        (
            "user:1",
            "",
            user_source(STOP, constraint_kinds=("uncertainty",)),
            "unavailable",
        ),
        ("user:1", "", user_source(STOP), "unavailable"),
        ("session_intention", "", user_source(STOP), "ungrounded"),
        ("none", "", user_source(STOP), "ungrounded"),
        ("missing:ref", STOP, user_source(STOP), "ungrounded"),
    ],
)
def test_restriction_conflict_grounding(reference, quote, source, expected):
    sources = {
        "user:1": source,
        "session_intention": {"origin": "generated", "text": STOP},
    }
    state = evidence(
        sources=sources,
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": reference,
            "restriction_quote": quote,
        },
    )
    assert ground(state)["restriction_conflict"] == expected


def test_operator_clause_grounds_without_quote_and_generated_summary_never_does():
    state = evidence(
        advisory=[{"id": "op:1", "effect": "deny", "text": "no pushes"}],
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "op:1",
        },
    )
    assert ground(state)["restriction_conflict"] == "grounded"


def test_relationship_and_target_conflicts_ground_through_evidence_only():
    state = evidence(
        sources={"user:1": user_source(STOP)},
        assessment={
            "relationship": "conflicts_with_intent",
            "target_scope": "conflicts",
            "target_reference": "none",
        },
    )
    grounding = ground(state)
    assert grounding["relationship_conflict"] == "ungrounded"
    assert grounding["target_conflict"] == "ungrounded"
    grounded = state.model_copy(
        update={
            "assessment": {
                **state.assessment,
                "contextual_restrictions": "conflicts",
                "restriction_reference": "user:1",
                "restriction_quote": STOP,
                "target_reference": "action",
            }
        }
    )
    grounding = ground(grounded)
    assert grounding["relationship_conflict"] == "grounded"
    assert grounding["target_conflict"] == "grounded"


# --- policy: profile semantics ---------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_grounded_restriction_conflict_is_reviewed_in_every_profile(profile):
    state = evidence(
        sources={"user:1": user_source(STOP)},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "user:1",
            "restriction_quote": STOP,
        },
    )
    result = evaluate_policy([], state, profile, assessed=True)
    assert result.outcome == "escalate"
    assert "model.denial" in obligation_ids(result)
    assert result.grounding == {"restriction_conflict": "grounded"}
    assert result.ignored_opinions == []


@pytest.mark.parametrize("profile", ("strict", "balanced"))
def test_ungrounded_conflict_still_reviewed_outside_bounded_autonomy(profile):
    state = evidence(
        sources={"user:1": user_source(STOP)},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "session_intention",
        },
    )
    result = evaluate_policy([], state, profile, assessed=True)
    assert result.outcome == "escalate"
    assert "model.denial" in obligation_ids(result)


def test_bounded_autonomy_records_but_does_not_enforce_ungrounded_opinions():
    state = evidence(
        sources={"user:1": user_source("Implement the fix and push it.")},
        risk="medium",
        assessment={
            "risk": "medium",
            "contextual_restrictions": "conflicts",
            "restriction_reference": "session_intention",
            "target_scope": "conflicts",
            "target_reference": "none",
            "relationship": "conflicts_with_intent",
        },
    )
    result = evaluate_policy([], state, "bounded_autonomy", assessed=True)
    assert result.outcome == "escalate"
    assert "restriction_conflict" not in result.ignored_opinions
    assert "model.denial" in obligation_ids(result)
    assert result.grounding == {
        "relationship_conflict": "ungrounded",
        "restriction_conflict": "ungrounded",
        "target_conflict": "ungrounded",
    }


def test_bounded_autonomy_still_blocks_grounded_contradiction_and_hazard():
    grounded = evidence(
        sources={"user:1": user_source(STOP)},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "user:1",
            "restriction_quote": STOP,
        },
    )
    assert evaluate_policy([], grounded, "bounded_autonomy", assessed=True).outcome == (
        "escalate"
    )
    hazardous = evidence(
        external_or_irreversible=True,
        assessment={"risk": "high"},
    )
    result = evaluate_policy([], hazardous, "bounded_autonomy", assessed=True)
    assert result.outcome == "escalate"
    assert "risk.high" in obligation_ids(result)


@pytest.mark.parametrize("profile", PROFILES)
def test_configured_deny_checkpoint_and_refusal_bind_regardless_of_grounding(profile):
    deny = Rule(
        id="no-push",
        effect="deny",
        when=Predicate(op="command_matches", value="git push*"),
    )
    state = evidence()
    assert evaluate_policy([deny], state, profile, assessed=True).outcome == "deny"
    checkpoint = deny.model_copy(update={"id": "confirm", "effect": "require_approval"})
    result = evaluate_policy([checkpoint], state, profile, assessed=True)
    assert result.outcome == "escalate"
    assert "human.checkpoint" in obligation_ids(result)
    refused = state.model_copy(
        update={
            "review": Review(
                obligation_ids=["model.denial"],
                evidence_refs=["action"],
                user_would="refuse",
                risk="medium",
                basis="restriction_conflict",
            )
        }
    )
    assert evaluate_policy([], refused, profile, assessed=True).outcome == "deny"


def test_confirmed_grounded_conflict_denies_but_ungrounded_confirmation_cannot():
    from intaris.policy_evidence import CorrectedAssessment

    review = Review(
        obligation_ids=["model.denial"],
        evidence_refs=["user:1"],
        user_would="ask",
        risk="medium",
        basis="restriction_conflict",
        corrected_assessment=CorrectedAssessment(
            contextual_restrictions="conflicts", restriction_reference="user:1"
        ),
    )
    grounded = evidence(
        sources={"user:1": user_source(STOP, constraint_kinds=("restriction",))},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "user:1",
        },
    ).model_copy(update={"review": review})
    assert evaluate_policy([], grounded, "balanced", assessed=True).outcome == "deny"
    ungrounded = grounded.model_copy(
        update={"sources": {"user:1": user_source("Push when ready.")}}
    )
    assert evaluate_policy([], ungrounded, "balanced", assessed=True).outcome != "deny"


# --- policy: unknown risk on bounded effects -------------------------------


def local_edit(**assessment):
    return evidence(
        tool="edit",
        command=None,
        effects=["write"],
        paths=["/work/src/app.py"],
        paths_complete=True,
        static_read=False,
        assessment={"risk": "unknown", "effects": ["write"], **assessment},
    ).model_copy(update={"workflow_scope": {"matched": True}})


def test_bounded_effects_requires_complete_known_local_effects():
    assert bounded_effects(local_edit())
    assert not bounded_effects(local_edit(effects=["write", "transmit"]))
    assert not bounded_effects(
        local_edit().model_copy(
            update={
                "observations": {
                    **local_edit().observations,
                    "secret_argument": local_edit()
                    .observations["tool"]
                    .model_copy(update={"value": True}),
                }
            }
        )
    )
    assert not bounded_effects(evidence(assessment={"risk": "unknown"}))


def test_matched_workflow_scope_already_authorizes_bounded_local_edit():
    for profile, expected in (
        ("strict", "approve"),
        ("balanced", "approve"),
        ("bounded_autonomy", "approve"),
    ):
        result = evaluate_policy([], local_edit(), profile, assessed=True)
        assert result.outcome == expected, profile
    pushing = evidence(assessment={"risk": "unknown"})
    assert "risk.unknown" in obligation_ids(
        evaluate_policy([], pushing, "balanced", assessed=True)
    )


def recovered(risk_state):
    """Risk label recovered from a Jev abstention by the low/medium fallback."""
    return risk_state.model_copy(
        update={
            "confidences": {
                "risk": {
                    "threshold_abstained": True,
                    "abstention_fallback": {"applied": "medium"},
                    "probabilities": {"low": 0.5, "medium": 0.2, "unknown": 0.3},
                }
            }
        }
    )


def test_recovered_risk_cannot_discharge_observed_external_effects():
    """Fallback resolves label ambiguity only; external effects stay reviewed."""
    external = evidence(assessment={"risk": "medium"}, external_or_irreversible=True)
    pushing = recovered(external)
    assert pushing.value("external_or_irreversible") is True
    for profile in ("strict", "balanced"):
        result = evaluate_policy([], pushing, profile, assessed=True)
        assert result.outcome != "approve", profile
        assert "risk.unknown" in obligation_ids(result), profile
    # The same label without recovery provenance is an ordinary assessment.
    assert evaluate_policy([], external, "balanced", assessed=True).outcome == "approve"


@pytest.mark.parametrize(
    "consequence",
    [
        {"effects": ["delete"]},
        {"effects": ["disclose"]},
        {"reversibility": "irreversible"},
        {"environment": "production", "mutation": "mutating"},
    ],
)
def test_recovered_risk_cannot_discharge_consequential_effects_without_hint(
    consequence,
):
    """Interpreter code leaves the external hint unset; L1 effects still bind."""
    base = evidence(
        command="python -c 'import shutil; shutil.rmtree(\"build\")'",
        effects=consequence.get("effects", ["write"]),
        external_or_irreversible=None,
        assessment={"risk": "medium", **consequence},
    )
    assert base.value("external_or_irreversible") is not True
    assert evaluate_policy([], base, "balanced", assessed=True).outcome == "approve"
    result = evaluate_policy([], recovered(base), "balanced", assessed=True)
    assert result.outcome != "approve", consequence
    assert "risk.unknown" in obligation_ids(result), consequence


def test_recovered_risk_on_bounded_local_effects_is_approved():
    """No external effect: the recovered label resolves the abstention."""
    edit = recovered(local_edit(risk="medium"))
    assert edit.value("external_or_irreversible") is not True
    for profile in ("strict", "balanced", "bounded_autonomy"):
        assert evaluate_policy([], edit, profile, assessed=True).outcome == "approve"


def test_recovered_risk_with_explicit_grant_keeps_the_grant():
    """A deterministic human grant for the action is not undone by recovery."""
    granted = recovered(
        evidence(
            assessment={"risk": "medium"},
            external_or_irreversible=True,
            sources={
                "user:1": {**user_source("Push it now."), "independent_grant": True}
            },
        )
    ).model_copy(update={"human_approved": True})
    assert evaluate_policy([], granted, "balanced", assessed=True).outcome == "approve"


# --- policy: authenticated lifecycle scope ---------------------------------


def lifecycle(**assessment):
    return evidence(
        tool="bash_kill",
        command=None,
        effects=["execute"],
        mutation="mutating",
        assessment={"risk": "medium", "effects": ["execute"], **assessment},
    ).model_copy(
        update={
            "process_lifecycle": {
                "authenticated": True,
                "matched": True,
                "operation": "terminate",
            }
        }
    )


@pytest.mark.parametrize("profile", ("balanced", "bounded_autonomy"))
def test_owned_helper_cleanup_needs_no_new_permission(profile):
    for restrictions in ("consistent", "unresolved"):
        for risk in ("medium",):
            state = lifecycle(contextual_restrictions=restrictions, risk=risk)
            result = evaluate_policy([], state, profile, assessed=True)
            assert result.outcome == "approve", (profile, restrictions, risk)
            assert result.permission_basis == "task_process_lifecycle"
    uncertain = evaluate_policy([], lifecycle(risk="unknown"), profile, assessed=True)
    assert "risk.unknown" in obligation_ids(uncertain)


def test_complete_paths_alone_do_not_prove_owned_bounded_effects():
    outside = local_edit().model_copy(update={"workflow_scope": None})
    assert not bounded_effects(outside)
    assert "risk.unknown" in obligation_ids(
        evaluate_policy([], outside, "balanced", assessed=True)
    )


@pytest.mark.parametrize(
    "origin", ["generated", "generated_summary", "client_tool_context"]
)
def test_quote_in_non_instruction_text_is_not_grounding(origin):
    state = evidence(
        sources={"user:1": {**user_source(STOP), "origin": origin}},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "user:1",
            "restriction_quote": STOP,
        },
    )
    assert ground(state)["restriction_conflict"] == "ungrounded"


def test_superseded_quote_does_not_restore_old_restriction():
    source = user_source(STOP, constraint_kinds=("restriction",))
    source["constraint_items"][0]["status"] = "superseded"
    state = evidence(
        sources={"user:1": source},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "user:1",
            "restriction_quote": STOP,
        },
    )
    assert ground(state)["restriction_conflict"] == "ungrounded"


def test_unavailable_authoritative_body_requires_review_in_bounded_autonomy():
    state = evidence(
        sources={"user:1": {"origin": "client_user_claim"}},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "user:1",
        },
    )
    result = evaluate_policy([], state, "bounded_autonomy", assessed=True)
    assert result.grounding["restriction_conflict"] == "unavailable"
    assert "model.denial" in obligation_ids(result)


def test_lifecycle_keeps_grounded_stop_instruction_and_hazard_reviews():
    keep_running = "Keep the preview server running until I say otherwise."
    state = lifecycle(
        contextual_restrictions="conflicts",
        restriction_reference="user:1",
        restriction_quote=keep_running,
    ).model_copy(update={"sources": {"user:1": user_source(keep_running)}})
    for profile in PROFILES:
        result = evaluate_policy([], state, profile, assessed=True)
        assert result.outcome == "escalate", profile
    ungrounded = state.model_copy(
        update={
            "assessment": {
                **state.assessment,
                "restriction_reference": "session_intention",
            }
        }
    )
    assert (
        evaluate_policy([], ungrounded, "bounded_autonomy", assessed=True).outcome
        == "escalate"
    )
    assert (
        evaluate_policy([], ungrounded, "balanced", assessed=True).outcome == "escalate"
    )
    assert (
        evaluate_policy([], lifecycle(risk="high"), "balanced", assessed=True).outcome
        == "escalate"
    )
    assert (
        evaluate_policy([], lifecycle(risk="unknown"), "strict", assessed=True).outcome
        == "escalate"
    )


# --- alignment as a fact ---------------------------------------------------


def test_alignment_reason_round_trips_a_verbatim_parent_quote():
    reason = encode_alignment_reason("Child pushes.", STOP)
    assert cited_instruction(reason) == STOP
    assert alignment_grounding(reason, ["Assignment: " + STOP])
    assert not alignment_grounding(reason, ["Push freely."])
    assert cited_instruction(encode_alignment_reason("No quote", "")) is None
    assert not alignment_grounding("plain reasoning", [STOP])


def test_misalignment_is_reviewed_by_judge_not_hard_stopped():
    incompatible = {
        "state": "incompatible",
        "reason": "unrelated",
        "grounded": False,
        "overridden": False,
    }
    for profile in ("strict", "balanced"):
        result = evaluate_policy(
            [], evidence(parent_alignment=incompatible), profile, assessed=True
        )
        assert result.outcome == "escalate", profile
        obligation = next(
            o
            for o in result.outstanding_obligations
            if o.id == "alignment.compatibility"
        )
        assert obligation.requirement == "independent_review"
    relaxed = evaluate_policy(
        [], evidence(parent_alignment=incompatible), "bounded_autonomy", assessed=True
    )
    assert relaxed.outcome == "approve"
    assert relaxed.ignored_opinions == ["alignment_conflict"]
    grounded = {**incompatible, "grounded": True}
    assert (
        evaluate_policy(
            [], evidence(parent_alignment=grounded), "bounded_autonomy", assessed=True
        ).outcome
        == "escalate"
    )
    overridden = {**incompatible, "overridden": True}
    settled = evaluate_policy(
        [], evidence(parent_alignment=overridden), "strict", assessed=True
    )
    assert "alignment.compatibility" not in obligation_ids(settled)


SAFE_READ = {
    "tool": "read",
    "command": None,
    "paths": ["/workspace/project/src/app.py"],
    "paths_complete": True,
    "effects": ["read"],
    "mutation": "read_only",
    "static_read": True,
}


@pytest.mark.parametrize(
    "profile,grounded,exempt",
    [
        ("balanced", False, True),
        ("balanced", True, False),
        ("bounded_autonomy", False, True),
        ("bounded_autonomy", True, True),
        ("strict", False, False),
        ("strict", True, False),
    ],
)
def test_safe_read_alignment_exemption_is_profile_bound(profile, grounded, exempt):
    alignment = {
        "state": "incompatible",
        "reason": "unrelated",
        "grounded": grounded,
        "overridden": False,
    }
    result = evaluate_policy(
        [], evidence(parent_alignment=alignment, **SAFE_READ), profile, assessed=True
    )
    assert ("alignment.compatibility" not in obligation_ids(result)) is exempt


@pytest.mark.parametrize(
    "override",
    [
        {"static_read": False},  # sensitive path or secret location
        {"mutation": "mutating", "effects": ["write"]},
        {"effects": ["read", "transmit"]},
        {"secret_argument": True},
    ],
)
def test_alignment_exemption_never_covers_unsafe_reads(override):
    alignment = {
        "state": "incompatible",
        "reason": "unrelated",
        "grounded": False,
        "overridden": False,
    }
    state = evidence(parent_alignment=alignment, **{**SAFE_READ, **override})
    result = evaluate_policy([], state, "balanced", assessed=True)
    assert "alignment.compatibility" in obligation_ids(result)


@pytest.mark.parametrize("profile", ["balanced", "strict"])
def test_static_read_alignment_only_obligation_is_approved(profile):
    alignment = {
        "state": "incompatible",
        "grounded": True,
        "overridden": False,
    }
    result = evaluate_policy(
        [],
        evidence(parent_alignment=alignment, **SAFE_READ),
        profile,
        assessed=True,
    )
    assert result.outcome == "approve"
    assert result.permission_basis == "read_only_static"


def test_static_read_with_an_additional_review_obligation_still_escalates():
    alignment = {
        "state": "incompatible",
        "grounded": True,
        "overridden": False,
    }
    state = evidence(
        parent_alignment=alignment,
        sources={"user:1": user_source(STOP)},
        assessment={
            "contextual_restrictions": "conflicts",
            "restriction_reference": "user:1",
            "restriction_quote": STOP,
        },
        **SAFE_READ,
    )
    result = evaluate_policy([], state, "balanced", assessed=True)
    assert result.outcome == "escalate"
    assert set(obligation_ids(result)) == {
        "alignment.compatibility",
        "model.denial",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"secret_argument": True},
        {"external_or_irreversible": True},
        {"assessment": {"relationship": "conflicts_with_intent"}},
        {"mutation": "mutating", "effects": ["write"]},
    ],
)
def test_static_read_alignment_change_does_not_cover_conflicting_actions(override):
    alignment = {
        "state": "incompatible",
        "grounded": True,
        "overridden": False,
    }
    action = {
        **SAFE_READ,
        **{key: value for key, value in override.items() if key != "assessment"},
    }
    result = evaluate_policy(
        [],
        evidence(
            parent_alignment=alignment,
            assessment=override.get("assessment"),
            **action,
        ),
        "balanced",
        assessed=True,
    )
    assert result.outcome != "approve" or result.permission_basis != "read_only_static"


def test_judge_approval_discharges_alignment_review_and_refusal_denies():
    incompatible = {
        "state": "incompatible",
        "reason": "x",
        "grounded": True,
        "overridden": False,
    }
    state = evidence(parent_alignment=incompatible)
    approved = state.model_copy(
        update={
            "review": Review(
                obligation_ids=["alignment.compatibility"],
                evidence_refs=["action"],
                user_would="approve",
                risk="medium",
                basis="routine_within_task",
            )
        }
    )
    assert evaluate_policy([], approved, "balanced", assessed=True).outcome == "approve"
    refused = approved.model_copy(
        update={"review": approved.review.model_copy(update={"user_would": "refuse"})}
    )
    assert evaluate_policy([], refused, "balanced", assessed=True).outcome == "deny"
