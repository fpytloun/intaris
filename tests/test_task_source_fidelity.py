import json

import pytest

from intaris.evaluation_context import source_view
from intaris.hybrid import evaluate_policy
from intaris.intention_profile import SYSTEM_PROMPT, VERSION, _authority_span
from intaris.judge_simulator import classify, request
from intaris.l1_projection import classifier_view, render
from intaris.policy_evidence import from_observations

TEXT = (
    "Load and run the daily-brief skill. "
    "Use verified Calendar, Todoist and personal Gmail for the briefing. "
    "Do not directly read ExampleCorp Slack, calendar or documents."
)


def artifact(first_seq=None, second_seq=2, second_text=TEXT):
    return {
        "originals": [
            {
                "ref": "initial",
                "origin": "recorded_user_event",
                "content": TEXT,
                "source_audit_call_id": "audit_request",
                "source_event_seq": first_seq,
            },
            {
                "ref": "latest_user_message",
                "origin": "recorded_user_event",
                "content": second_text,
                "source_audit_call_id": "audit_request",
                "source_event_seq": second_seq,
            },
        ],
        "items": [
            {
                "kind": "request",
                "source_ref": "latest_user_message",
                "quote": TEXT,
                "status": "active",
            }
        ],
    }


def state():
    return from_observations(
        {
            "tool_name": "mcp_googleworkspace__get_gmail_thread_content",
            "arguments": {"thread_id": "thread_personal"},
            "runtime_context": {
                "loaded_skills": {
                    "coverage": "complete",
                    "skills": [
                        {
                            "id": "brief",
                            "version_id": "brief_v1",
                            "summary": "Verify personal Gmail for the briefing.",
                            "authority": "loaded_procedure_not_policy",
                        }
                    ],
                }
            },
        }
    )


@pytest.mark.parametrize("seqs", [(None, 2), (2, None), (2, 2)])
def test_known_event_alias_has_one_body_and_retains_sequence(seqs):
    view = source_view(artifact(*seqs))
    assert len(view["sources"]) == 1
    row = view["sources"][0]
    assert row["source_event_seq"] == 2
    assert row["aliases"] == ["latest_user_message"]
    assert row["content"] == TEXT


@pytest.mark.parametrize("args", [(2, 3, TEXT), (None, 2, "Do not read any Gmail.")])
def test_conflicting_identity_or_body_remains_separate(args):
    assert len(source_view(artifact(*args))["sources"]) == 2


def test_three_aliases_do_not_bridge_conflicting_known_sequences():
    value = artifact(None, 2)
    value["originals"].append(
        {
            **value["originals"][1],
            "ref": "later",
            "source_event_seq": 3,
        }
    )
    assert len(source_view(value)["sources"]) == 2


def test_skill_context_reaches_both_l1_formats_and_independent_judge():
    value = artifact()
    evidence = state().model_copy(
        update={"assessment": {"relationship": "unrelated", "risk": "critical"}}
    )
    view = classifier_view(value, evidence)
    assert view["loaded_skills"]["skills"][0]["version_id"] == "brief_v1"
    assert "Verify personal Gmail" in render(value, evidence, {})
    packet = request(
        record={"tool": "mcp_googleworkspace__get_gmail_thread_content"},
        session={},
        state=evidence,
        result=evaluate_policy([], evidence),
        artifact=value,
        latest_user=TEXT,
    )
    body = json.loads(packet["messages"][1]["content"])
    assert body["loaded_skills"]["skills"][0]["version_id"] == "brief_v1"
    assert "l1" not in body
    assert len(body["originals"]) == 1
    assert body["conversation_tail"]["user_reply"]["source_ref"] == "initial"
    assert body["current_request_sources"] == [1]
    assert "intention_items" not in body
    assert "personal Gmail" in body["originals"][0]
    assert "Do not directly read ExampleCorp" in body["originals"][0]
    assert body["reference_ids"]["sources"] == {"1": "initial"}
    assert packet["citation_sources"][0]["ref"] == "initial"


def test_extraction_contract_preserves_supporting_sources_without_inventing_grants():
    assert VERSION == "task-context-single-v15"
    assert "supporting operations and named sources" in SYSTEM_PROMPT
    assert "Never transfer scope across sources" in SYSTEM_PROMPT
    assert "Keep unaffected restrictions current" in SYSTEM_PROMPT
    clause = "Use verified Calendar, Todoist and personal Gmail for the briefing."
    start = TEXT.index(clause)
    _authority_span(artifact()["originals"][0], start, start + len(clause))


def test_malformed_refusal_cannot_become_a_validated_judge_denial():
    from unittest.mock import Mock

    question = request(
        record={"tool": "mcp_googleworkspace__get_gmail_thread_content"},
        session={},
        state=state(),
        result=evaluate_policy([], state()),
        artifact=artifact(),
        latest_user=TEXT,
    )
    transport = Mock()
    transport.judge_response_mode = "json_object"
    transport.generate.return_value = json.dumps(
        {
            "user_would": "refuse",
            "action_summary": "Read a Gmail thread",
            "explanation": "No permission.",
            "restriction_conflict": True,
        }
    )
    result, details = classify(transport, question)
    assert result.basis == "review_unavailable"
    assert result.user_would == "ask"
    assert details["response_origin"] == "assessment_failed"
    assert transport.generate.call_count == 2
    first = transport.generate.call_args_list[0]
    assert first.kwargs["max_tokens"] == 16000
    assert first.kwargs["single_attempt"] is True
    assert first.kwargs["response_mode"] == "json_object"
    assert "Return JSON conforming to:" in first.args[0][0]["content"]
