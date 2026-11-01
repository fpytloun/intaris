"""Prose intention must follow the structured profile; stale prose is repaired.

Reconstructed example of the failure shape where a parent's prose intention kept
describing an earlier report request after two later messages authorized and
resumed implementation, which made every implementing child session look
incompatible to the alignment check.
"""

import json
from unittest.mock import MagicMock, Mock

from intaris import alignment, classifier, intention_profile, judge_simulator
from intaris.intention_profile import stale_prose
from tests.test_single_intention import item, record, run
from tests.test_single_intention import stores as stores  # noqa: F401

REPORT = (
    "Review the batch processor proposal and return a concise readiness verdict, "
    "the proposed worker behavior and lifecycle, retry patterns and blockers."
)
AUTHORIZE = (
    "The operator explicitly authorizes implementation of the agreed batch processor."
)
RESUME = "Resume work. Continue the approved batch processor implementation."

STALE_PROSE = (
    "Provide a concise readiness verdict, specify the batch processor worker "
    "behavior and lifecycle, list retry patterns, and note any "
    "remaining implementation blockers."
)
FRESH_PROSE = (
    "Implement the agreed batch processor design, continuing the "
    "approved implementation and integration work."
)


def items(*rows):
    """Items in model output order; each row names the source it came from."""
    return [
        {"kind": k, "status": s, "quote": q, "source_ref": f"message:{src}"}
        for (k, s, q, src) in rows
    ]


ORDER = ["message:0", "message:1", "message:2"]


def check(prose, profile, current=()):
    return stale_prose(prose, profile, list(current), source_order=ORDER)


def test_stale_prose_detected_when_only_a_later_approval_is_current():
    profile = items(
        ("request", "done", REPORT, 0), ("approval", "current", AUTHORIZE, 1)
    )
    found = check(STALE_PROSE, profile)
    assert found is not None
    assert found["code"] == "stale_prose_intention"
    assert found["done_request"] == 0
    assert found["current_approval_source"] == 1
    assert found["overlap"] >= 0.6


def test_fresh_prose_passes_with_the_same_profile():
    profile = items(
        ("request", "done", REPORT, 0), ("approval", "current", AUTHORIZE, 1)
    )
    assert check(FRESH_PROSE, profile) is None


def test_prose_describing_approved_work_that_restates_the_request_is_kept():
    """A superset of the done request is not stale when approved work dominates."""
    superset = (
        FRESH_PROSE
        + " As part of this, keep the readiness verdict, worker behavior, lifecycle, "
        "retry patterns and blockers documented for the batch processor runtime, "
        "job dispatch, progress indicators and the queue integration."
    )
    profile = items(
        ("request", "done", REPORT, 0), ("approval", "current", AUTHORIZE, 1)
    )
    assert check(superset, profile) is None


def test_no_diagnostic_when_a_request_is_current():
    profile = items(
        ("request", "current", REPORT, 0), ("approval", "current", AUTHORIZE, 1)
    )
    assert check(STALE_PROSE, profile, current=[0]) is None


def test_chronology_comes_from_sources_not_item_position():
    """Item order is model output order; source refs decide what is later."""
    # Approval listed FIRST but from the LATER source: still stale.
    reordered = items(
        ("approval", "current", AUTHORIZE, 1), ("request", "done", REPORT, 0)
    )
    assert check(STALE_PROSE, reordered) is not None
    # Approval listed LAST but from the EARLIER source: not stale.
    earlier = items(
        ("request", "done", REPORT, 1), ("approval", "current", AUTHORIZE, 0)
    )
    assert check(STALE_PROSE, earlier) is None


def test_multiple_approvals_use_the_latest_later_one():
    profile = items(
        ("approval", "current", "Earlier ok.", 0),
        ("request", "done", REPORT, 1),
        ("approval", "current", AUTHORIZE, 2),
    )
    found = check(STALE_PROSE, profile)
    assert found is not None and found["current_approval_source"] == 2


def test_no_diagnostic_without_any_current_approval_or_without_order():
    profile = items(("request", "done", REPORT, 0), ("approval", "done", AUTHORIZE, 1))
    assert check(STALE_PROSE, profile) is None
    live = items(("request", "done", REPORT, 0), ("approval", "current", AUTHORIZE, 1))
    assert stale_prose(STALE_PROSE, live, [], source_order=None) is None


def test_parse_profile_surfaces_stale_prose_as_a_drop_reason():
    """The diagnostic rides the existing validation channel into the repair."""
    captured = {
        "profile_sources": [
            {"ref": "message:0", "origin": "client_user_claim", "content": REPORT},
            {"ref": "message:1", "origin": "client_user_claim", "content": AUTHORIZE},
        ],
        "originals": [],
        "watermarks": {"decisions": 0, "latest_user": 1},
        "profile": None,
    }

    def raw(prose):
        return json.dumps(
            {
                "title": "Batch processor",
                "intention": prose,
                "items": [
                    {
                        "kind": "request",
                        "msg": 1,
                        "quote": REPORT,
                        "status": "done",
                        "confirmation": None,
                    },
                    {
                        "kind": "approval",
                        "msg": 2,
                        "quote": AUTHORIZE,
                        "status": "current",
                        "confirmation": None,
                    },
                ],
                "current": [],
            }
        )

    stale = intention_profile.parse_profile(raw(STALE_PROSE), captured, STALE_PROSE)
    assert stale is not None
    assert [(i["kind"], i["status"]) for i in stale["items"]] == [
        ("request", "done"),
        ("approval", "current"),
    ]
    assert "stale_prose_intention" in [
        r.get("code") for r in stale["validation"]["drop_reasons"]
    ]
    fresh = intention_profile.parse_profile(raw(FRESH_PROSE), captured, FRESH_PROSE)
    assert fresh["validation"]["drop_reasons"] == []


def test_generator_prompt_states_the_prose_rule():
    assert "A later approval of proposed work makes it active" in (
        intention_profile.SYSTEM_PROMPT
    )
    assert "the prose must describe it" in intention_profile.SYSTEM_PROMPT


def test_alignment_ranking_is_shared_by_both_reviewer_paths():
    """Jev and the LLM reviewer must receive the same ranking rule."""
    jev = Mock()
    jev.check_alignment.return_value = (True, "")
    backend = Mock()
    backend.select.return_value = jev
    alignment.check_intention_alignment(
        llm=None,
        evaluator_backend=backend,
        parent_intention=STALE_PROSE,
        child_intention="Implement the batch processor module and its tests.",
        parent_user_evidence=RESUME,
        fail_closed=False,
    )
    rules = jev.check_alignment.call_args.kwargs["evaluation_rules"]
    assert alignment.EVIDENCE_RANKING in rules
    assert "latest user update outranks the generated parent intention" in rules

    llm = Mock()
    llm.generate.return_value = (
        '{"aligned": true, "reasoning": "ok", "contradicted_instruction": ""}'
    )
    alignment.check_intention_alignment(
        llm=llm,
        evaluator_backend=None,
        parent_intention=STALE_PROSE,
        child_intention="Implement the batch processor module and its tests.",
        parent_user_evidence=RESUME,
        fail_closed=False,
    )
    prompt = llm.generate.call_args.args[0][1]["content"]
    assert alignment.EVIDENCE_RANKING in prompt
    assert RESUME in prompt


def test_no_digest_is_inferred_from_a_path_component():
    """A 64-hex directory in an asset path is a version id, not the file hash.

    Reporting it as a digest produced a false mismatch against the clause's
    real content hash in production. The classifier must emit no digest
    observation of any kind from a path.
    """
    version_dir = "c" * 64
    script = f"/opt/assets/skill/ver/{version_dir}/tool.py"
    facts = classifier.observe_hybrid(
        "bash",
        {"command": f"'/opt/assets/skill/ver/python-env/bin/python' -B '{script}'"},
        policy={},
    )
    assert not any("digest" in key for key in facts)
    assert not hasattr(classifier, "content_addressed_digest")


def test_path_alone_cannot_satisfy_a_hash_grant():
    """Authorization invariant, not wording.

    With no executor-verified digest, a grant conditioned on a SHA-256 is
    neither violated nor satisfied by the path. Judge's approval on that grant
    is rejected by the answer validator; a repeated approval ends as a safe
    ask. Neither a forged `/tmp` directory nor the genuine asset path can
    approve on the hash from the path alone.
    """
    from unittest.mock import Mock

    from intaris.hybrid import evaluate_policy
    from intaris.judge_simulator import Answer, classify, request
    from tests.test_user_simulator import state

    digest = "d" * 64
    forged = f"/tmp/scratch/{digest}/tool.py"
    assert "A directory name in a path is not a file's hash" in judge_simulator.SYSTEM
    assert "never approve on that grant from the path alone" in judge_simulator.SYSTEM

    grant = (
        f"May run only tool.py published in the asset store, SHA256 {digest}, "
        "at its manifest path with the managed interpreter."
    )
    s = state()
    q = request(
        {"tool": "bash", "args_redacted": {"command": f"python3 {forged}"}},
        {},
        s,
        evaluate_policy([], s, "balanced", assessed=True),
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
        latest_assistant="Running the published tool.",
    )
    index = next(
        row["index"] for row in q["citation_sources"] if row["ref"] == "message:grant"
    )
    assert index in q["hash_conditioned"]
    approve = Answer.model_validate(
        {
            "user_would": "approve",
            "risk": "low",
            "confidence": "high",
            "basis": "scoped_approval",
            "cites": [index],
            "restriction_quote": "",
            "action_summary": "Run tool.py.",
            "explanation": "The path's digest matches the grant.",
            "question": "",
        }
    )
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = approve.model_dump_json()
    answer, diagnostics = classify(llm, q)
    assert answer.user_would != "approve"
    assert diagnostics["validation_errors"]
    assert any(
        d[0]["type"] == "hash_conditioned_grant_unverified"
        for d in diagnostics["validation_details"]
    )
    # A refusal that invents a mismatch is also rejected: no hash fact exists.
    refuse = approve.model_copy(
        update={
            "user_would": "refuse",
            "basis": "restriction_conflict",
            "restriction_quote": "does not match the allowed hash",
        }
    )
    llm.generate.return_value = refuse.model_dump_json()
    answer, diagnostics = classify(llm, q)
    assert answer.user_would != "refuse" or answer.basis != "restriction_conflict"
    # A low-risk ask on explicit_request citing the hash grant must NOT be
    # upgraded to approve by the routine ask-upgrade: the grant's condition
    # is the thing being asked about.
    ask = approve.model_copy(
        update={
            "user_would": "ask",
            "basis": "explicit_request",
            "question": "Confirm the file matches the published digest?",
        }
    )
    q_local = {**q, "consequential": False, "ask_upgrade_blocked": False}
    llm.generate.return_value = ask.model_dump_json()
    answer, diagnostics = classify(llm, q_local)
    assert answer.user_would == "ask"
    assert diagnostics.get("upgrade_reason") is None
    # The positive control for the upgrade lives in test_judge_ask_upgrade.py.


def completion(prose, items):
    return json.dumps(
        {"title": "Batch processor", "intention": prose, "items": items, "current": []}
    )


def stored_profile(stores):
    value = stores[1].get("session", user_id="owner")["intention_profile"]
    return json.loads(value) if isinstance(value, str) else value


def test_prose_repair_keeps_structured_items_when_the_retry_drops_one(stores):
    """Through the real generator and bounded repair.

    First completion: correct items (report done, restriction current, approval
    current), stale prose. Retry: fresh prose but the restriction is missing.
    The stored profile must keep the restriction from the first validated
    structure and take only the repaired prose. Exactly two model calls.
    """
    restriction = "Do not push, merge, or open pull requests."
    record(stores, REPORT)
    record(stores, restriction)
    record(stores, AUTHORIZE)
    base = [
        item(REPORT, msg=1, status="done"),
        item(restriction, "restriction", msg=2),
        item(AUTHORIZE, "approval", msg=3),
    ]
    llm = MagicMock()
    llm.generate.side_effect = [
        completion(STALE_PROSE, base),
        completion(FRESH_PROSE, [base[0], base[2]]),  # retry drops the restriction
    ]
    result, _ = run(stores, llm=llm, response_mode="json_object")
    assert llm.generate.call_count == 2
    repair_system = llm.generate.call_args_list[1].args[0][0]["content"]
    assert "stale_prose_intention" in repair_system
    assert result == FRESH_PROSE
    profile = stored_profile(stores)
    assert profile["intention"] == FRESH_PROSE
    assert sorted((i["kind"], i["status"]) for i in profile["items"]) == [
        ("approval", "current"),
        ("request", "done"),
        ("restriction", "current"),
    ]
    assert profile["current"] == []


def test_fresh_prose_on_first_completion_makes_exactly_one_call(stores):
    record(stores, REPORT)
    record(stores, AUTHORIZE)
    llm = MagicMock()
    llm.generate.side_effect = [
        completion(
            FRESH_PROSE,
            [item(REPORT, msg=1, status="done"), item(AUTHORIZE, "approval", msg=2)],
        )
    ]
    result, _ = run(stores, llm=llm, response_mode="json_object")
    assert llm.generate.call_count == 1
    assert result == FRESH_PROSE


def test_failed_prose_repair_keeps_first_structure(stores):
    """If the retry is still stale, structure stays and nothing is invented."""
    record(stores, REPORT)
    record(stores, AUTHORIZE)
    base = [item(REPORT, msg=1, status="done"), item(AUTHORIZE, "approval", msg=2)]
    llm = MagicMock()
    llm.generate.side_effect = [completion(STALE_PROSE, base)] * 2
    result, _ = run(stores, llm=llm, response_mode="json_object")
    assert llm.generate.call_count == 2
    profile = stored_profile(stores)
    assert profile is not None
    assert [(i["kind"], i["status"]) for i in profile["items"]] == [
        ("request", "done"),
        ("approval", "current"),
    ]
    assert result == STALE_PROSE
    assert any(
        r.get("code") == "stale_prose_intention"
        for r in profile["validation"]["drop_reasons"]
    )
