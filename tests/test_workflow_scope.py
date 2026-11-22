"""Offline controller declaration, policy, and database invariants."""

import json
import uuid

import pytest

from intaris.hybrid import digest, evaluate_policy
from intaris.hybrid_runtime import finalize_result
from intaris.policy_activation import HybridError
from intaris.policy_evidence import Review, from_observations
from intaris.workflow_scope import WorkflowScopeStore, WorkflowStep
from tests.test_hybrid_races import race_store as race_store

CONTEXT = {
    "governing_execution_id": "workflow-execution",
    "governing_snapshot_id": "workflow-snapshot",
    "executor_id": "workflow-executor",
}
ARGS = {"file_path": "/repo/README"}


def declaration(sid, **changes):
    return WorkflowStep(
        **{
            "version": 1,
            "origin": "controller_workflow_step",
            "session_id": sid,
            "task_id": "task-1",
            "workflow_id": "workflow-1",
            "step_name": "inspect",
            "step_run_id": "run-1",
            "logical_call_id": "call-1",
            "workflow_definition_digest": digest({"steps": ["inspect"]}),
            "step_definition_digest": digest({"tool": "read"}),
            "rendered_args_digest": digest(ARGS),
            "tool": "read",
            "evaluated_args_digest": digest(ARGS),
            "executor_id": CONTEXT["executor_id"],
            "authorized_by": "owner_configured",
            "projection": "exact",
            **changes,
        }
    )


@pytest.fixture
def workflow(race_store):
    sessions, audit, llm, evaluator, _ = race_store
    sid = str(uuid.uuid4())
    sessions.create(
        user_id="race-owner",
        session_id=sid,
        agent_id="controller",
        intention="Inspect repository",
    )
    store = WorkflowScopeStore(audit._db)
    return sessions, audit, llm, evaluator, sid, store


def test_legacy_session_declaration_is_unsupported_not_conflict(workflow):
    """Clients fall back to undeclared evaluation on 422; 409 blocks dispatch."""
    sessions, audit, *_ = workflow
    sid = str(uuid.uuid4())
    sessions.create(
        user_id="race-owner",
        session_id=sid,
        agent_id="controller",
        intention="Check sync",
        policy={"evaluation_policy_version": "legacy"},
    )
    store = WorkflowScopeStore(audit._db)
    with pytest.raises(HybridError) as exc:
        store.install(
            declaration(sid),
            user_id="race-owner",
            agent_id="controller",
            claims=CONTEXT,
        )
    assert exc.value.code == "workflow_scope_unsupported"
    assert exc.value.status == 422
    with audit._db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM workflow_step_declarations WHERE session_id = ?",
            (sid,),
        )
        assert cur.fetchone()["n"] == 0


def install(workflow, **changes):
    *_, sid, store = workflow
    return store.install(
        declaration(sid, **changes),
        user_id="race-owner",
        agent_id="controller",
        claims=CONTEXT,
    )


def match(workflow, **changes):
    *_, sid, store = workflow
    return store.match(
        **{
            "user_id": "race-owner",
            "session_id": sid,
            "agent_id": "controller",
            "logical_call_id": "call-1",
            "tool": "read",
            "args": ARGS,
            "controller_context": CONTEXT,
            **changes,
        }
    )


def evaluate(workflow, **changes):
    _, _, _, evaluator, sid, _ = workflow
    return evaluator.evaluate(
        **{
            "user_id": "race-owner",
            "session_id": sid,
            "agent_id": "controller",
            "logical_call_id": "call-1",
            "tool": "read",
            "args": ARGS,
            "controller_context": CONTEXT,
            **changes,
        }
    )


def test_idempotency_revision_and_immutable_slot(workflow):
    sessions, _, _, _, sid, _ = workflow
    before = sessions.get(sid, user_id="race-owner")
    first = install(workflow)
    assert first == install(workflow)
    after = sessions.get(sid, user_id="race-owner")
    assert after["state_revision"] == before["state_revision"] + 1
    assert after["intention"] == before["intention"]
    assert after["policy_revision"] == before["policy_revision"]
    with pytest.raises(HybridError) as exc:
        install(workflow, evaluated_args_digest=digest({}))
    assert exc.value.code == "workflow_step_conflict"
    assert (
        install(workflow, logical_call_id="call-2")["admission_revision"]
        > first["admission_revision"]
    )


def test_exact_scope_is_configured_permission_not_human_or_effect_proof(workflow):
    install(workflow)
    scope = match(workflow)
    assert scope["matched"]
    state = from_observations(
        {"tool": "opaque-tool", "effects": ["write"], "mutation": "mutating"}
    )
    state = state.model_copy(update={"workflow_scope": scope})
    result = evaluate_policy([], state, assessed=True)
    assert result.outcome == "approve"
    assert result.permission_basis == "controller_workflow_step"
    assert not state.sources and not state.human_approved
    opaque = from_observations({"effects": ["unknown"], "mutation": "unclear"})
    opaque = opaque.model_copy(update={"workflow_scope": scope})
    assert evaluate_policy([], opaque, assessed=True).outcome != "approve"


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "another-tenant"),
        ("session_id", "another-session"),
        ("tool", "write"),
        ("args", {}),
        ("agent_id", "another-agent"),
        ("logical_call_id", "another-call"),
        ("controller_context", None),
        *[("controller_context", {**CONTEXT, key: "other"}) for key in CONTEXT],
    ],
)
def test_wrong_binding_cannot_grant(workflow, field, value):
    install(workflow)
    assert not match(workflow, **{field: value})["matched"]


@pytest.mark.parametrize(
    "mode,code",
    [
        ("missing", "declaration_missing"),
        ("mismatch", "declaration_mismatch"),
        ("agent", "definition_unverified"),
        ("projection", "argument_projection_unsupported"),
    ],
)
@pytest.mark.parametrize("maximum", [None, "approve"])
def test_failure_only_forfeits_configured_grant(workflow, mode, code, maximum):
    """An unverified step is evaluated normally; it never forces escalation."""
    sessions, audit, llm, _, sid, _ = workflow
    if maximum:
        sessions.update_session(
            sid, user_id="race-owner", policy={"maximum_outcome": maximum}
        )
    install(
        workflow,
        **(
            {"authorized_by": "agent_composed"}
            if mode == "agent"
            else {"projection": "unsupported"}
            if mode == "projection"
            else {}
        ),
    )
    request = (
        {"controller_context": None}
        if mode == "missing"
        else ({"args": {}} if mode == "mismatch" else {})
    )
    result = evaluate(workflow, **request)
    assert not str(result.get("outcome_override") or "").startswith("workflow_step.")
    assert "workflow_step." not in result["reasoning"]
    assert result["evaluation_metadata"]["workflow_scope"]["reason_code"] == (
        "workflow_step." + code
    )
    from intaris.policy_evidence import Evidence

    state = Evidence.model_validate(result["evaluation_metadata"]["policy_evidence"])
    review = Review(
        obligation_ids=[
            o["id"] for o in result["evaluation_metadata"]["outstanding_obligations"]
        ],
        evidence_refs=[],
        user_would="approve",
        risk="low",
        basis="explicit_request",
    )
    reviewed = evaluate_policy(
        [], state.model_copy(update={"review": review}), assessed=True
    )
    for allowed in (None, frozenset({"approve", "deny"})):
        final = finalize_result(
            reviewed,
            maximum="approve",
            minimum=None,
            human_approved=True,
            judge_allowed=allowed,
            judge_uncertain="deny",
        )
        assert final.decision == "approve"


@pytest.mark.parametrize("guard", ["floor", "rule", "human", "read_only"])
@pytest.mark.parametrize("maximum", [None, "approve"])
def test_denials_win_over_configured_scope(workflow, guard, maximum):
    sessions, audit, _, _, sid, _ = workflow
    sessions.update_session(
        sid,
        user_id="race-owner",
        policy={
            **({"maximum_outcome": maximum} if maximum else {}),
            **({"deny_tools": ["read"]} if guard == "rule" else {}),
        },
    )
    install(workflow)
    request = {}
    if guard == "floor":
        request["minimum_outcome"] = "deny"
    if guard == "read_only":
        request = {
            "tool": "write",
            "context": {"chat_mode": "plan", "read_only_required": True},
        }
    if guard == "human":
        first = evaluate(workflow, minimum_outcome="escalate")
        audit.resolve_escalation(first["call_id"], "deny", user_id="race-owner")
    assert evaluate(workflow, **request)["decision"] == "deny"


@pytest.mark.parametrize("maximum", [None, "approve"])
def test_new_declaration_invalidates_inflight_admission(workflow, monkeypatch, maximum):
    sessions, audit, _, _, sid, _ = workflow
    if maximum:
        sessions.update_session(
            sid, user_id="race-owner", policy={"maximum_outcome": maximum}
        )
    install(workflow)
    original = audit.insert

    def interleave(**kwargs):
        install(workflow, logical_call_id="call-2")
        return original(**kwargs)

    monkeypatch.setattr(audit, "insert", interleave)
    with pytest.raises(HybridError) as exc:
        evaluate(workflow)
    assert exc.value.code in {"stale_admission", "workflow_step_conflict"}


def test_canonical_vectors():
    from pathlib import Path

    vectors = json.loads(
        Path(__file__).with_name("workflow_step_vectors.json").read_text()
    )
    for vector in vectors:
        assert digest(vector["args"]) == vector["evaluated_args_digest"]
        assert digest(vector["declaration"]) == vector["declaration_digest"]
        assert (
            digest(dict(reversed(list(vector["args"].items()))))
            == vector["evaluated_args_digest"]
        )


BUILTIN_ARGS = {"artifact_id": "art_" + "0" * 32}


def builtin_context(mutation_kind="read", source="builtin", selection="exact"):
    """Controller tool metadata as the controller sends it for a native tool."""
    return {
        "tool": {
            "name": "artifact_get_content",
            "description": "Read the exact text content of a saved artifact.",
            "source": {"type": source},
            "read_only": mutation_kind == "read",
            "descriptor": {
                "authority": "native",
                "schema_version": "cognis.tool.v2",
                "schema_hash": "0" * 64,
                "selection": selection,
                "selector": {},
                "operation": {
                    "name": "artifact_get_content",
                    "summary": "Read the exact text content of a saved artifact",
                    "mutation_kind": mutation_kind,
                    "side_effects": [],
                },
            },
        }
    }


def evaluate_builtin(workflow, *, declared=True, args=BUILTIN_ARGS, **context_changes):
    _, _, llm, _, _, _ = workflow
    install(
        workflow,
        tool="artifact_get_content",
        rendered_args_digest=digest(args),
        evaluated_args_digest=digest(args),
    )
    llm.generate.reset_mock()
    result = evaluate(
        workflow,
        tool="artifact_get_content",
        args=args if declared != "mismatch" else {"artifact_id": "art_" + "1" * 32},
        context=builtin_context(**context_changes),
        **({} if declared else {"controller_context": None}),
    )
    return result, llm.generate.call_count


def test_declared_builtin_read_takes_the_static_read_path_without_the_model(workflow):
    result, model_calls = evaluate_builtin(workflow)
    assert result["decision"] == "approve"
    assert model_calls == 0
    policy = result["evaluation_metadata"]["policy_result"]
    assert policy["permission_basis"] == "read_only_static"
    assert policy["evidence_quality"] == "deterministic"
    assert result["evaluation_metadata"]["static_facts"]["effects"] == ["read"]


@pytest.mark.parametrize(
    "case,changes",
    [
        ("undeclared", {"declared": False}),
        ("mismatched declaration", {"declared": "mismatch"}),
        ("write operation", {"mutation_kind": "update"}),
        ("executor tool", {"source": "executor"}),
        ("mcp tool", {"source": "mcp"}),
        ("unselected operation", {"selection": "unknown"}),
        (
            "path argument",
            {"args": {**BUILTIN_ARGS, "path": "/home/user/.ssh/id_ed25519"}},
        ),
    ],
)
def test_builtin_read_claims_need_a_matching_controller_declaration(
    workflow, case, changes
):
    result, model_calls = evaluate_builtin(workflow, **changes)
    policy = result["evaluation_metadata"].get("policy_result") or {}
    assert policy.get("permission_basis") != "read_only_static", case
    assert model_calls >= 1, case


def test_declared_session_cannot_disable_enforcement(workflow):
    sessions, _, _, _, sid, _ = workflow
    install(workflow)
    with pytest.raises(HybridError) as exc:
        sessions.update_session(
            sid, user_id="race-owner", policy={"evaluation_policy_version": "legacy"}
        )
    assert exc.value.code == "workflow_step_conflict"
    assert (
        sessions.get(sid, user_id="race-owner")["policy"]["evaluation_policy_version"]
        == "hybrid_v1"
    )


def test_human_checkpoint_is_not_a_configured_permission(workflow):
    from intaris.hybrid import Rule

    install(workflow)
    state = from_observations(
        {"tool": "read", "effects": ["read"], "mutation": "read_only"}
    )
    state = state.model_copy(update={"workflow_scope": match(workflow)})
    rule = Rule.model_validate(
        {
            "id": "human.checkpoint",
            "effect": "require_approval",
            "when": {"op": "tool_matches", "value": "read"},
        }
    )
    result = evaluate_policy([rule], state, assessed=True)
    assert (
        finalize_result(result, maximum="approve", minimum=None).decision == "escalate"
    )


def test_new_declaration_invalidates_already_admitted_dispatch(workflow):
    sessions, audit, _, _, sid, _ = workflow
    sessions.update_session(
        sid, user_id="race-owner", policy={"maximum_outcome": "approve"}
    )
    install(workflow)
    result = evaluate(workflow)
    assert result["decision"] == "approve"
    install(workflow, logical_call_id="call-2")
    with pytest.raises(HybridError) as exc:
        audit.mark_execution(
            result["call_id"], user_id="race-owner", state="dispatched"
        )
    assert exc.value.code == "workflow_step_conflict"


@pytest.mark.parametrize("projection", ["json", "lines"])
def test_matched_scope_reaches_source_context_assessment(
    workflow, monkeypatch, projection
):
    from unittest.mock import Mock

    from intaris import policy_assessment
    from intaris.evaluator import Evaluator
    from intaris.task_context import TaskContext
    from tests.test_task_context import append_reasoning, extractor
    from tools.l1_projection_sizes import valid_answer

    sessions, audit, llm, _, sid, _ = workflow
    append_reasoning(
        audit,
        user_id="race-owner",
        session_id=sid,
        content="User message: Inspect repository before proceeding.",
    )
    install(workflow, tool="opaque-tool")
    context = TaskContext(
        llm=extractor(),
        sessions=sessions,
        audit=audit,
        extractor_identity={"model": "local-test", "version": "1"},
    )
    evaluator = Evaluator(
        llm=llm,
        session_store=sessions,
        audit_store=audit,
        task_context=context,
        l1_input_projection=projection,
    )
    captured = []
    classify = policy_assessment.classify

    def capture(llm, jev, question):
        captured.append(question)
        llm.generate = Mock(return_value=json.dumps(valid_answer(question["schema"])))
        return classify(llm, jev, question)

    monkeypatch.setattr(policy_assessment, "classify", capture)
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=sid,
        agent_id="controller",
        logical_call_id="call-1",
        tool="opaque-tool",
        args=ARGS,
        controller_context=CONTEXT,
    )
    assert len(captured) == 1
    question = captured[0]
    assert question["projection"] == projection
    assert "\nVerified configured workflow scope" not in question["prompt"]
    assert "step-owned:opaque-tool" not in question["prompt"]
    assert "controller_workflow_step" not in json.dumps(question["schema"])
    assert result["evaluation_metadata"]["task_context"]["status"] == "ready"
    assert not any(
        row.get("origin") == "controller_workflow_step"
        for row in result["evaluation_metadata"]["task_context"]["originals"]
    )
