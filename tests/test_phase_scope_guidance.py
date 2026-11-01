"""Source/status transport tests; scripted extraction is not live-model proof."""

import pytest

from intaris import intention_profile, judge_simulator, l1_projection
from intaris.hybrid import evaluate_policy
from intaris.policy_evidence import from_observations
from intaris.task_support import PHASE_SCOPE_GUIDANCE
from tests.test_single_intention import capture, item, record, run
from tests.test_single_intention import stores as stores


@pytest.mark.parametrize("phase_approved", [True, False])
def test_phase_status_preserves_independent_limits_and_originals(
    stores, phase_approved
):
    plan = "PLAN only. No code edits this turn. Do not push or edit secrets."
    build = (
        "Plan approved. BUILD the agreed correction, but do not edit migration files."
        if phase_approved
        else "Continue explaining the plan. Do not implement yet."
    )
    record(stores, plan)
    record(stores, build)
    status = "superseded" if phase_approved else "current"
    _, llm = run(
        stores,
        [
            item("PLAN only.", status=status),
            item("No code edits this turn.", "restriction", status=status),
            item("Do not push or edit secrets.", "restriction"),
            item(build, msg=2),
        ],
    )
    artifact = capture(stores)[1]
    assert artifact["status"] == "ready"
    by_quote = {row["quote"]: row for row in artifact["items"]}
    assert by_quote["No code edits this turn."]["status"] == status
    assert by_quote["Do not push or edit secrets."]["status"] == "current"
    schema = llm.generate.call_args.kwargs["json_schema"]["schema"]
    assert (
        PHASE_SCOPE_GUIDANCE
        in schema["$defs"]["CompactItem"]["properties"]["status"]["description"]
    )
    state = from_observations({"tool": "apply_patch", "args": {"patch": "task edit"}})
    l1 = l1_projection.render(artifact, state, {})
    judge = judge_simulator.request(
        {"tool": "apply_patch"},
        {"intention": build, "policy": {"profile": "balanced"}},
        state,
        evaluate_policy([], state),
        artifact,
    )
    for payload in (l1, str(judge["messages"])):
        assert "Do not push or edit secrets." in payload
        assert build in payload
    assert plan in str(artifact["originals"])


def test_all_three_boundaries_share_phase_scope_without_blanket_override():
    for prompt in (
        intention_profile.CompactItem.model_fields["status"].description,
        l1_projection.SYSTEM,
        judge_simulator.SYSTEM,
    ):
        assert PHASE_SCOPE_GUIDANCE in prompt
        assert "phase-bound no-edits clause" in prompt
        assert "ongoing read-only prohibition" in prompt
        assert "nor an unrelated newer request" in prompt
        assert "latest message wins globally" in prompt
    assert intention_profile.VERSION == "task-context-single-v15"
