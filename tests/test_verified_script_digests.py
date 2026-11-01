"""Executor-verified script digests: bound to the action, satisfy hash grants.

The controller hashes the files a bash command will run, on the executor, at
dispatch. Intaris binds that evidence to the exact action and user/session,
promotes it to a controller fact, and lets a grant that names the same hash
be an approval basis. A path-declared digest never does; a verified digest
that differs from the clause never does.
"""

from unittest.mock import Mock

import pytest

from intaris import judge_simulator
from intaris.evaluation_context import normalize, runtime_view
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import _observe_action
from intaris.judge_simulator import Answer, classify, request
from intaris.policy_evidence import Observation
from tests.test_user_simulator import state

SHA = "b" * 64
OTHER = "c" * 64
SCRIPT = "/opt/assets/skill/ver/" + "d" * 64 + "/tool.py"
COMMAND = f"'/opt/assets/skill/ver/python-env/bin/python' -B '{SCRIPT}'"


def evidence(**updates):
    return {
        "coverage": "complete",
        "source": "executor_file_digest",
        "authority": "context_only",
        "user_id": "owner",
        "session_id": "session",
        "executor_id": "executor_a",
        "paths": [SCRIPT],
        "verified": [{"path": SCRIPT, "sha256": SHA, "size_bytes": 10}],
        "unverified": [],
        **updates,
    }


def test_binding_keeps_digests_only_for_the_same_action_and_identity():
    good = normalize(
        {"executed_scripts": evidence()},
        user_id="owner",
        session_id="session",
        evaluated_tool="bash",
        args={"command": COMMAND},
    )["executed_scripts"]
    assert good["verified"] == [{"path": SCRIPT, "sha256": SHA}]
    assert good["authority"] == "context_only"
    for bad in (
        dict(user_id="intruder"),
        dict(session_id="other"),
        dict(source="model_claim"),
    ):
        view = normalize(
            {"executed_scripts": evidence(**bad)},
            user_id="owner",
            session_id="session",
            evaluated_tool="bash",
            args={"command": COMMAND},
        )
        assert view["executed_scripts"] == {
            "coverage": "unknown",
            "reason": "identity_mismatch",
        }
    # Wrong tool, or a command that does not name the digested path.
    assert (
        normalize(
            {"executed_scripts": evidence()},
            user_id="owner",
            session_id="session",
            evaluated_tool="read",
            args={"file_path": SCRIPT},
        )["executed_scripts"]["coverage"]
        == "unknown"
    )
    assert (
        normalize(
            {"executed_scripts": evidence()},
            user_id="owner",
            session_id="session",
            evaluated_tool="bash",
            args={"command": "python3 /tmp/other.py"},
        )["executed_scripts"]["verified"]
        == []
    )


def test_verified_digest_becomes_a_controller_fact_and_nothing_is_inferred():
    context = normalize(
        {"executed_scripts": evidence()},
        user_id="owner",
        session_id="session",
        evaluated_tool="bash",
        args={"command": COMMAND},
    )
    facts = _observe_action("bash", {"command": COMMAND}, policy={}, context=context)
    assert facts["verified_asset_digests"] == {SCRIPT: SHA}
    assert not any("declared" in key for key in facts)
    assert "executed_scripts" in runtime_view(context)
    without = _observe_action("bash", {"command": COMMAND}, policy={}, context={})
    assert "verified_asset_digests" not in without


def packet(
    verified: dict | None, grant_sha: str, *, grant: str | None = None, command=COMMAND
):
    s = state()
    observations = dict(state().observations)
    if verified is not None:
        observations["verified_asset_digests"] = Observation(
            value=verified, source="static_observation", coverage="complete"
        )
    s = s.model_copy(update={"observations": observations})
    grant = grant or (
        f"May run only tool.py published in the asset store, SHA256 {grant_sha}, "
        "at its manifest path with the managed interpreter."
    )
    return request(
        {"tool": "bash", "args_redacted": {"command": command}},
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


def approval(q):
    index = next(
        r["index"] for r in q["citation_sources"] if r["ref"] == "message:grant"
    )
    return index, Answer.model_validate(
        {
            "user_would": "approve",
            "risk": "low",
            "confidence": "high",
            "basis": "scoped_approval",
            "cites": [index],
            "restriction_quote": "",
            "action_summary": "Run tool.py.",
            "explanation": "The executor-verified digest matches the grant.",
            "question": "",
        }
    )


def test_matching_verified_digest_satisfies_the_hash_grant():
    q = packet({SCRIPT: SHA}, SHA)
    index, answer = approval(q)
    assert index in q["hash_conditioned"] and index in q["hash_satisfied"]
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    got, diagnostics = classify(llm, q)
    assert got.user_would == "approve"
    assert got.basis == "scoped_approval"
    assert diagnostics["response_origin"] == "model"
    assert not diagnostics["validation_errors"]


def test_mismatched_verified_digest_cannot_satisfy_the_hash_grant():
    q = packet({SCRIPT: OTHER}, SHA)
    index, answer = approval(q)
    assert index in q["hash_conditioned"] and index not in q["hash_satisfied"]
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    got, diagnostics = classify(llm, q)
    assert got.user_would != "approve"
    assert any(
        d[0]["type"] == "hash_conditioned_grant_unverified"
        for d in diagnostics["validation_details"]
    )


def test_path_alone_cannot_satisfy_the_hash_grant():
    q = packet(None, "d" * 64)  # grant names the path's version-dir string
    index, answer = approval(q)
    assert index in q["hash_conditioned"] and index not in q["hash_satisfied"]
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    got, _ = classify(llm, q)
    assert got.user_would != "approve"


def test_prompt_states_verified_digest_is_the_hash_and_path_is_not():
    assert "verified_asset_digests" in judge_simulator.SYSTEM
    assert "That IS the" in judge_simulator.SYSTEM
    assert "A directory name in a path is not a file's hash" in judge_simulator.SYSTEM


SCRIPT_B = "/opt/assets/skill/ver/" + "d" * 64 + "/helper.py"
H1, H2, WRONG = "1" * 64, "2" * 64, "f" * 64
TWO = f"python3 {SCRIPT}; python3 {SCRIPT_B}"


def test_multi_hash_clause_needs_every_hash_verified():
    """A/H1 + B/wrong does not satisfy a clause naming H1 and H2."""
    grant = f"May run tool.py SHA256 {H1} and helper.py SHA256 {H2} only."
    partial = packet({SCRIPT: H1, SCRIPT_B: WRONG}, H1, grant=grant, command=TWO)
    index, answer = approval(partial)
    assert index in partial["hash_conditioned"]
    assert index not in partial["hash_satisfied"]
    assert partial["hash_contradicted"] is True
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    got, _ = classify(llm, partial)
    assert got.user_would != "approve"
    full = packet({SCRIPT: H1, SCRIPT_B: H2}, H1, grant=grant, command=TWO)
    assert next(iter(full["hash_satisfied"])) == index
    assert full["hash_contradicted"] is False


def test_swapped_digests_do_not_satisfy_a_two_file_clause():
    """tool.py/H2 + helper.py/H1 has equal hash SETS but every pairing is wrong."""
    grant = f"May run tool.py SHA256 {H1} and helper.py SHA256 {H2} only."
    swapped = packet({SCRIPT: H2, SCRIPT_B: H1}, H1, grant=grant, command=TWO)
    index, answer = approval(swapped)
    assert index in swapped["hash_conditioned"]
    assert index not in swapped["hash_satisfied"]
    assert swapped["hash_contradicted"] is True
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    got, _ = classify(llm, swapped)
    assert got.user_would != "approve"


def test_hex_version_directory_in_a_quoted_path_is_not_a_named_hash():
    """The original production bug, from the clause side this time."""
    grant = (
        f"May run tool.py SHA256 {SHA} at "
        f"'/opt/assets/skill/ver/{'d' * 64}/tool.py' with the managed interpreter."
    )
    assert judge_simulator.hash_conditions(grant) == ([("tool.py", SHA)], False)
    q = packet({SCRIPT: SHA}, SHA, grant=grant)
    index, answer = approval(q)
    assert index in q["hash_satisfied"]
    assert q["hash_contradicted"] is False
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    got, _ = classify(llm, q)
    assert got.user_would == "approve"


def test_stated_absolute_path_applies_only_to_that_path():
    """A grant for /approved/tool.py says nothing about /other/tool.py."""
    approved = "/approved/tool.py"
    other = "/other/tool.py"
    grant = f"May run {approved} SHA256 {H1} only."
    assert judge_simulator.hash_conditions(grant) == ([(approved, H1)], False)
    # Running the other file with a different digest: not applicable, no
    # contradiction, and an independent routine approval is allowed.
    q = packet({other: H2}, H1, grant=grant, command=f"python3 {other}")
    index, _ = approval(q)
    assert index in q["hash_conditioned"]
    assert index not in q["hash_satisfied"]
    assert q["hash_contradicted"] is False
    # Running the approved path with the stated digest: met.
    ok = packet({approved: H1}, H1, grant=grant, command=f"python3 {approved}")
    assert index in ok["hash_satisfied"]
    # Running the approved path with a different digest: contradicted.
    bad = packet({approved: H2}, H1, grant=grant, command=f"python3 {approved}")
    assert bad["hash_contradicted"] is True


def test_root_level_absolute_path_keeps_its_identity():
    """A grant for /tool.py must not become a grant for every tool.py."""
    root = "/tool.py"
    other = "/other/tool.py"
    grant = f"May run {root} SHA256 {H1} only."
    assert judge_simulator.hash_conditions(grant) == ([(root, H1)], False)
    q = packet({other: H2}, H1, grant=grant, command=f"python3 {other}")
    index, _ = approval(q)
    assert index not in q["hash_satisfied"]
    assert q["hash_contradicted"] is False
    ok = packet({root: H1}, H1, grant=grant, command=f"python3 {root}")
    assert index in ok["hash_satisfied"]


def test_unsupported_path_syntax_does_not_degrade_to_a_basename():
    """A path with spaces yields no script token; the condition is never met."""
    spaced = "/my dir/tool.py"
    grant = f"May run '{spaced}' SHA256 {H1} only."
    # The lookbehind rejects `dir/tool.py` (preceded by a space inside a
    # path) and the bare-name branch cannot start after `/`, so no script
    # token is found: the condition anchors to "" and is unverifiable.
    assert judge_simulator.hash_conditions(grant) == ([("", H1)], False)
    q = packet(
        {"/other/tool.py": H2}, H1, grant=grant, command="python3 /other/tool.py"
    )
    index, _ = approval(q)
    assert index in q["hash_conditioned"]
    assert index not in q["hash_satisfied"]
    assert q["hash_contradicted"] is False
    # Even the "right" basename with the right hash cannot satisfy it.
    same = packet({"/x/tool.py": H1}, H1, grant=grant, command="python3 /x/tool.py")
    assert index not in same["hash_satisfied"]


def test_shared_label_second_condition_is_ambiguous_never_satisfied():
    """'tool.py SHA256 H1 and helper.py H2' must not satisfy on tool.py alone."""
    grant = f"May run tool.py SHA256 {H1} and helper.py {H2} only."
    conditions, ambiguous = judge_simulator.hash_conditions(grant)
    assert conditions == [("tool.py", H1)]
    assert ambiguous is True
    q = packet({SCRIPT: H1, SCRIPT_B: WRONG}, H1, grant=grant, command=TWO)
    index, answer = approval(q)
    assert index in q["hash_conditioned"]
    assert index not in q["hash_satisfied"]
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    got, _ = classify(llm, q)
    assert got.user_would != "approve"
    # Even with both files correct, an ambiguous source is never satisfied.
    both = packet({SCRIPT: H1, SCRIPT_B: H2}, H1, grant=grant, command=TWO)
    assert index not in both["hash_satisfied"]


def test_a_grant_for_another_file_is_not_a_prohibition_on_this_one():
    """A conditional grant for tool.py says nothing about helper.py."""
    grant = f"May run tool.py SHA256 {H1}."
    only_b = f"python3 {SCRIPT_B}"
    q = packet({SCRIPT_B: H2}, H1, grant=grant, command=only_b)
    index, _ = approval(q)
    assert index in q["hash_conditioned"]
    assert index not in q["hash_satisfied"]  # not applicable, not met
    assert q["hash_contradicted"] is False  # and not contradicted
    # A routine approval of helper.py is therefore not blocked by it.
    routine = Answer.model_validate(
        {
            "user_would": "approve",
            "risk": "low",
            "confidence": "high",
            "basis": "routine_within_task",
            "cites": [],
            "restriction_quote": "",
            "action_summary": "Run helper.py.",
            "explanation": "Routine helper within the task.",
            "question": "",
        }
    )
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = routine.model_dump_json()
    got, _ = classify(llm, q)
    assert got.user_would == "approve"


def test_satisfaction_is_per_stated_file_not_per_action():
    """A clause about tool.py is met by tool.py/H1 regardless of other scripts.

    helper.py running alongside with another digest is outside what the
    clause states; it neither satisfies nor contradicts it. Whether helper.py
    may run is decided on its own evidence, not borrowed from tool.py's grant.
    """
    grant = f"May run tool.py SHA256 {H1} only."
    q = packet({SCRIPT: H1, SCRIPT_B: WRONG}, H1, grant=grant, command=TWO)
    index, _ = approval(q)
    assert index in q["hash_satisfied"]
    assert q["hash_contradicted"] is False
    # And a clause naming a different hash for a file that does run is
    # contradicted even when the other executed file is fine.
    grant_b = f"May run helper.py SHA256 {H2} only."
    q2 = packet({SCRIPT: H1, SCRIPT_B: WRONG}, H2, grant=grant_b, command=TWO)
    assert q2["hash_contradicted"] is True


def test_verified_mismatch_blocks_routine_basis_and_the_ask_upgrade():
    """A contradicting controller fact routes to review on every basis."""
    q = packet({SCRIPT: WRONG}, SHA)
    assert q["hash_contradicted"] is True
    index, answer = approval(q)
    routine = answer.model_copy(update={"basis": "routine_within_task", "cites": []})
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = routine.model_dump_json()
    got, diagnostics = classify(llm, q)
    assert got.user_would != "approve"
    assert any(
        d[0]["type"] == "hash_conditioned_grant_unverified"
        for d in diagnostics["validation_details"]
    )
    ask = routine.model_copy(
        update={"user_would": "ask", "question": "Confirm the helper?"}
    )
    q_local = {**q, "consequential": False, "ask_upgrade_blocked": False}
    llm.generate.return_value = ask.model_dump_json()
    got, diagnostics = classify(llm, q_local)
    assert got.user_would == "ask"
    assert diagnostics.get("upgrade_reason") is None
    # Control: with the matching digest the same routine answer approves.
    ok = packet({SCRIPT: SHA}, SHA)
    assert ok["hash_contradicted"] is False
    llm.generate.return_value = routine.model_dump_json()
    got, _ = classify(llm, ok)
    assert got.user_would == "approve"


@pytest.mark.parametrize(
    "text,expected,ambiguous",
    [
        (f"tool.py SHA256 {SHA}", [("tool.py", SHA)], False),
        (f"tool.py sha256:{SHA}", [("tool.py", SHA)], False),
        (f"tool.py SHA-256 {SHA.upper()}", [("tool.py", SHA)], False),
        (f"tool.py sha256 `{SHA}`", [("tool.py", SHA)], False),
        (
            f"tool.py SHA256 {H1} and helper.py SHA256 {H2}",
            [("tool.py", H1), ("helper.py", H2)],
            False,
        ),
        # An absolute path is kept whole; a bare name is a basename.
        (f"/a/b/tool.py sha256 {H1}", [("/a/b/tool.py", H1)], False),
        # Unlabelled trailing hex is a possible second condition: ambiguous.
        (f"tool.py sha256 {H1} and {H2}", [("tool.py", H1)], True),
        (f"tool.py sha256 {H1} and helper.py {H2}", [("tool.py", H1)], True),
        # Hex inside a stated path is the path, not a condition.
        (f"tool.py sha256 {H1} at /x/{'d' * 64}/tool.py", [("tool.py", H1)], False),
        # A digest with no preceding filename is anchored to '' (never met).
        (f"SHA256 {SHA}", [("", SHA)], False),
        (f"no label here {SHA}", [], False),
        ("SHA256 only, no digest", [], False),
    ],
)
def test_hash_condition_extraction_formats(text, expected, ambiguous):
    assert judge_simulator.hash_conditions(text) == (expected, ambiguous)


def test_binding_requires_the_exact_executed_path_not_a_substring():
    """A digest for /a/b.py must not bind to a command running /a/b.py.bak."""
    bak = SCRIPT + ".bak"
    for cmd in (f"python3 {bak}", f"python3 /x{SCRIPT}"):
        view = normalize(
            {"executed_scripts": evidence()},
            user_id="owner",
            session_id="session",
            evaluated_tool="bash",
            args={"command": cmd},
        )
        assert view["executed_scripts"]["verified"] == []
