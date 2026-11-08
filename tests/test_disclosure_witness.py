import json
import subprocess
from unittest.mock import Mock

import pytest

from intaris import judge_simulator as simulator
from intaris.classifier import observe_hybrid, secret_argument_disclosure
from intaris.config import JudgeConfig
from intaris.hybrid import evaluate_policy
from intaris.judge import JudgeReviewer
from intaris.l1_projection import render
from intaris.policy_evidence import from_observations
from tests.test_intent_first_defaults import model_response
from tests.test_policy_evidence_runtime import runtime as runtime
from tests.test_user_simulator import answer, state


@pytest.mark.parametrize(
    "command,expected",
    [
        ("printenv ORBIT_SERVICE_TOKEN", True),
        ("cat .env", True),
        ("echo $AWS_SECRET_ACCESS_KEY", True),
        ("printenv PATH", False),
        ("cat README.md", False),
        ("head /tmp/private_key.pem", True),
        ("tail credentials.txt", True),
        ("less id_rsa", True),
        ("more passwd.txt", True),
        ("env API_KEY", False),
        ("env API_KEY=synthetic-only", True),
        ("env -u PYTHONPATH uv run ruff check src/credentials.py", False),
        ("env -u API_TOKEN uv run pytest tests/test_credentials.py", False),
        ("env -u PYTHONPATH cat .env", True),
        ("env -u PYTHONPATH printenv API_TOKEN", True),
        ("env -S 'printenv API_TOKEN'", True),
        ("env -u PYTHONPATH env -u TERM cat credentials.txt", True),
        ("env -u PYTHONPATH sh -c 'cat credentials.json'", True),
        ("cat PaSsWoRd.txt", True),
        ("printf hello", False),
        ("grep cat TOKEN", False),
        ("pwd && /usr/bin/printenv ORBIT_SERVICE_TOKEN", True),
        ('echo ";" "$AWS_SECRET_ACCESS_KEY"', True),
        ("pwd\nprintenv ORBIT_SERVICE_TOKEN", True),
        (
            "git worktree add -b feat/station-credentials ../station-credentials origin/main",
            False,
        ),
        ("git -C /repo worktree add ../credentials origin/main", False),
        ("git switch -c feat/token-storage", False),
        ('git commit -m "fix: credential handling"', False),
        (
            "git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push origin HEAD",
            False,
        ),
        ("git config credential.helper manager", False),
        ("git status --short -- credentials", False),
        ("git diff --name-status origin/main -- credentials.json", False),
        ("git log --oneline -- credentials.json", False),
        ("git show --format=credential HEAD:README.md", False),
        ("git diff -S credential HEAD -- README.md", False),
        ("git show HEAD:.env", True),
        ("git show --pretty credentials.json", True),
        ("git -C /repo -c credential.helper= show HEAD:credentials.json", True),
        ("git diff HEAD -- credentials.json", True),
        ("git diff --check -- credentials.json", True),
        ("git diff HEAD -- --name-only credentials.json", True),
        ("git diff -S --name-only HEAD -- credentials.json", True),
        ("git log -p -- credentials.json", True),
        ("git log --patch-with-stat -- credentials.json", True),
        ("git reflog --patch-with-raw -- credentials.json", True),
        ("git log -c -- credentials.json", True),
        ("git log --cc -- credentials.json", True),
        ("git log -U1 -- credentials.json", True),
        ("git log --unified=1 -- credentials.json", True),
        ("git log --word-diff -- credentials.json", True),
        ("git log -L 1,2:credentials.json", True),
        ("git log -L1,2:credentials.json", True),
        ("git blame credentials.json", True),
        ("git cat-file -p HEAD:.env", True),
        ("git archive HEAD credentials.json", True),
        ("git config --get remote.origin.token", True),
        ("git config --get-urlmatch credential https://example.com", True),
        ("git config remote.origin.token", True),
        ("git config --global remote.origin.token", True),
        ("git config --file settings remote.origin.token", True),
        ("git credential fill", True),
        ("git worktree add ../credentials; cat .env", True),
        ("git -c credential.helper= push origin HEAD && printenv API_TOKEN", True),
    ],
)
def test_secret_argument_observation(command, expected):
    assert secret_argument_disclosure(command) is expected
    facts = observe_hybrid("bash", {"command": command}, policy={})
    if expected:
        assert facts["static_read"] is False
        assert facts["operation"] == "disclose"
        assert facts["risk"] in {"high", "critical"}
        assert "disclose" in facts["effects"]
        evidence = from_observations(facts)
        assert evidence.observations["effects"].source == "secret_argument"
    else:
        assert "secret_argument" not in facts


@pytest.mark.parametrize("profile", ["balanced", "bounded_autonomy"])
@pytest.mark.parametrize(
    "command",
    [
        "git fetch origin; git status --short; git worktree add -b feat/station-credentials ../station-credentials origin/main",
        "git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push origin HEAD",
        "env -u PYTHONPATH uv run ruff check --fix src/api/credentials.py; env -u PYTHONPATH uv run ruff format src/domain/credentials.py; env -u PYTHONPATH uv run python scripts/openapi.py --write; env -u PYTHONPATH uv run pytest tests/test_credentials.py",
    ],
)
def test_credential_named_development_approves_without_disclosure_gate(
    runtime, profile, command
):
    sessions, audit, _, evaluator, sid = runtime
    sessions.update_session(sid, user_id="owner", policy={"profile": profile})
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert result["decision"] == "approve"
    metadata = audit.get_by_call_id(result["call_id"], user_id="owner")[
        "evaluation_metadata"
    ]
    assert not metadata["policy_evidence"]["observations"].get("secret_argument")
    assert not metadata["policy_result"]["outstanding_obligations"]


def test_disclosure_prompt_explains_remaining_gate_despite_judge_approval():
    vote = answer().model_copy(
        update={
            "explanation": "The user already authorized this task.",
            "action_summary": "Print the configured credential",
        }
    )
    message = simulator.user_explanation(
        vote,
        {"status": "ready"},
        "escalate",
        record={"tool": "bash"},
        triggers=["disclosure.authorization"],
    )
    assert "Authorization for that disclosure remains unverified" in message
    assert "reviewer recommended approval" in message
    assert "already authorized" not in message


def test_diff_check_can_print_a_secret_bearing_line(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "credentials.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "credentials.json"], check=True)
    (tmp_path / "credentials.json").write_text('{"password": "synthetic-only"} \n')
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--check", "--", "credentials.json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "synthetic-only" in result.stdout
    assert secret_argument_disclosure("git diff --check -- credentials.json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,approved",
    [
        (
            "git worktree add -b feat/station-credentials ../station-credentials origin/main",
            True,
        ),
        ("git show HEAD:.env", False),
        ("git log --patch-with-stat -- credentials.json", False),
        ("git reflog --patch-with-raw -- credentials.json", False),
        ("git log -c -- credentials.json", False),
        ("git log --cc -- credentials.json", False),
        ("git log -U1 -- credentials.json", False),
        ("git log -L1,2:credentials.json", False),
        ("git config --get-urlmatch credential https://example.com", False),
        ("env -u PYTHONPATH sh -c 'cat credentials.json'", False),
    ],
)
async def test_judge_resolution_keeps_only_real_disclosure_gate(
    runtime, command, approved
):
    sessions, audit, llm, evaluator, sid = runtime
    sessions.update_session(
        sid, user_id="owner", policy={"profile": "bounded_autonomy"}
    )
    # Force an independent model-severity review so this exercises resolution,
    # rather than the ordinary low-risk approval covered above.
    llm.generate.side_effect = lambda *a, **kw: model_response(kw, risk="high")
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert result["decision"] == "escalate"
    llm.generate.side_effect = None
    llm.generate.return_value = answer().model_dump_json()
    reviewer = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="auto"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    )
    await reviewer.review_and_resolve(
        call_id=result["call_id"],
        user_id="owner",
        session_id=sid,
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert (record["user_decision"] == "approve") is approved
    obligations = record["evaluation_metadata"]["policy_result"][
        "outstanding_obligations"
    ]
    assert (
        "disclosure.authorization" in [o["id"] for o in obligations]
    ) is not approved
    # Removing a false disclosure witness never bypasses an explicit tool deny.
    sessions.update_session(
        sid,
        user_id="owner",
        policy={"profile": "bounded_autonomy", "deny_tools": ["bash"]},
    )
    denied = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert denied["decision"] == "deny"


@pytest.mark.parametrize(
    "confidence,risk,disclose,expected",
    [
        ("medium", "low", False, "ask"),
        ("high", "medium", False, "ask"),
        ("high", "low", False, "approve"),
        ("high", "low", True, "ask"),
    ],
)
def test_model_conflict_approval_gate(confidence, risk, disclose, expected):
    evidence = state()
    evidence = evidence.model_copy(
        update={
            "assessment": {
                **evidence.assessment,
                "contextual_restrictions": "conflicts",
            },
        }
    )
    if disclose:
        facts = observe_hybrid(
            "bash", {"command": "printenv ORBIT_SERVICE_TOKEN"}, policy={}
        )
        evidence = evidence.model_copy(
            update={
                "observations": from_observations(facts).observations,
                # state() grants an inbox deletion. That action-specific grant
                # cannot be reused as authorization to print a credential.
                "assessment": {
                    **evidence.assessment,
                    "authorization": "absent",
                    "authorization_source": "none",
                    "authorization_scope": "unclear",
                },
            }
        )
    prior = evaluate_policy([], evidence, "bounded_autonomy")
    assert any(o.id == "model.denial" for o in prior.outstanding_obligations)
    artifact = {
        "status": "ready",
        "source_metadata": [],
        "items": [],
        "originals": [
            {
                "ref": "message:one",
                "origin": "client_user_claim",
                "content": "Use the configured runtime credential reference.",
            }
        ],
    }
    question = simulator.request({"tool": "bash"}, {}, evidence, prior, artifact)
    vote = answer(risk=risk).model_copy(update={"confidence": confidence})
    mapped, _ = simulator.classify(
        Mock(generate=Mock(return_value=vote.model_dump_json())), question
    )
    assert mapped.user_would == "approve"
    reviewed = evidence.model_copy(
        update={
            "review": simulator.receipt(evidence, prior, mapped, question["citable"]),
        }
    )
    result = evaluate_policy([], reviewed, "bounded_autonomy")
    # The validated Judge decision resolves this model-derived conflict. It
    # does not erase observed credential disclosure/risk requirements.
    assert not any(o.id == "model.denial" for o in result.outstanding_obligations)
    assert result.outcome == ("escalate" if disclose else "approve")
    if disclose:
        payload = json.loads(question["messages"][1]["content"])
        assert "disclose" in payload["controller_facts"]["effects"]
        assert payload["controller_facts"]["secret_argument"]
        projected = render(artifact, evidence, {})
        assert "Observed.effects: disclose" in projected
        assert "Observed.secret_argument: True" in projected


@pytest.mark.parametrize(
    "command",
    [
        "printenv ORBIT_SERVICE_TOKEN",
        'echo ";" "$AWS_SECRET_ACCESS_KEY"',
        "pwd\nprintenv ORBIT_SERVICE_TOKEN",
    ],
)
def test_disclosure_survives_runtime_observers_and_persistence(runtime, command):
    _, audit, _, evaluator, sid = runtime
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert result["decision"] == "escalate"
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    observations = record["evaluation_metadata"]["policy_evidence"]["observations"]
    assert "disclose" in observations["effects"]["value"]
    assert observations["effects"]["source"] == "secret_argument"
    assert observations["static_read"]["value"] is False
    assert observations["risk"]["value"] == "high"
    from intaris.policy_evidence import Evidence

    evidence = Evidence.model_validate(record["evaluation_metadata"]["policy_evidence"])
    artifact = {
        "originals": [
            {
                "ref": "message:one",
                "origin": "client_user_claim",
                "content": "Inspect the configuration.",
            }
        ]
    }
    q = simulator.request(
        record,
        {},
        evidence,
        evaluate_policy([], evidence, "bounded_autonomy"),
        artifact,
    )
    vote, _ = simulator.classify(
        Mock(generate=Mock(return_value=answer().model_dump_json())), q
    )
    reviewed = evidence.model_copy(
        update={
            "review": simulator.receipt(
                evidence,
                evaluate_policy([], evidence, "bounded_autonomy"),
                vote,
                q["citable"],
            ),
        }
    )
    assert evaluate_policy([], reviewed, "bounded_autonomy").outcome != "approve"
    assert reviewed.observations["risk"].value == "high"
    assert "disclose" in reviewed.value("effects")
