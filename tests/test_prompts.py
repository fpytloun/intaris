"""Tests for English language enforcement across all LLM system prompts.

Verifies that every system prompt and JSON schema in intaris instructs
the LLM to respond in English, regardless of the language of the input
data.  This prevents reasoning, intention, summary, and analysis outputs
from being generated in the language of the session conversation (e.g.,
Czech) instead of English.
"""

from __future__ import annotations

from intaris.judge import JUDGE_EVALUATION_SCHEMA, JUDGE_SYSTEM_PROMPT
from intaris.prompts import (
    ALIGNMENT_CHECK_SCHEMA,
    ALIGNMENT_CHECK_SYSTEM_PROMPT,
    SAFETY_EVALUATION_SCHEMA,
    SAFETY_EVALUATION_SYSTEM_PROMPT,
    build_evaluation_user_prompt,
    render_user_decisions_section,
)
from intaris.prompts_analysis import (
    BEHAVIORAL_ANALYSIS_SCHEMA,
    BEHAVIORAL_ANALYSIS_SYSTEM_PROMPT,
    SESSION_COMPACTION_SYSTEM_PROMPT,
    SESSION_SUMMARY_SCHEMA,
    SESSION_SUMMARY_SYSTEM_PROMPT,
    SESSION_SUMMARY_SYSTEM_PROMPT_4STREAM,
)


class TestEnglishEnforcementInSystemPrompts:
    """All system prompts must contain an English language instruction."""

    def test_safety_evaluation_prompt_enforces_english(self):
        assert "Always respond in English" in SAFETY_EVALUATION_SYSTEM_PROMPT

    def test_alignment_check_prompt_enforces_english(self):
        assert "Always respond in English" in ALIGNMENT_CHECK_SYSTEM_PROMPT

    def test_session_summary_prompt_enforces_english(self):
        assert "Always respond in English" in SESSION_SUMMARY_SYSTEM_PROMPT

    def test_session_summary_4stream_prompt_enforces_english(self):
        assert "Always respond in English" in SESSION_SUMMARY_SYSTEM_PROMPT_4STREAM

    def test_session_compaction_prompt_enforces_english(self):
        assert "Always respond in English" in SESSION_COMPACTION_SYSTEM_PROMPT

    def test_behavioral_analysis_prompt_enforces_english(self):
        assert "Always respond in English" in BEHAVIORAL_ANALYSIS_SYSTEM_PROMPT

    def test_judge_prompt_enforces_english(self):
        assert "Always respond in English" in JUDGE_SYSTEM_PROMPT


class TestEnglishEnforcementInSchemas:
    """JSON schema description fields for free-text outputs must include
    'in English' to reinforce the language instruction via structured output."""

    def test_safety_evaluation_reasoning_schema(self):
        desc = SAFETY_EVALUATION_SCHEMA["schema"]["properties"]["reasoning"][
            "description"
        ]
        assert "in English" in desc

    def test_alignment_check_reasoning_schema(self):
        desc = ALIGNMENT_CHECK_SCHEMA["schema"]["properties"]["reasoning"][
            "description"
        ]
        assert "in English" in desc

    def test_judge_reasoning_schema(self):
        desc = JUDGE_EVALUATION_SCHEMA["schema"]["properties"]["reasoning"][
            "description"
        ]
        assert "in English" in desc

    def test_session_summary_schema(self):
        desc = SESSION_SUMMARY_SCHEMA["schema"]["properties"]["summary"]["description"]
        assert "in English" in desc

    def test_session_summary_risk_indicator_detail_schema(self):
        detail_desc = SESSION_SUMMARY_SCHEMA["schema"]["properties"]["risk_indicators"][
            "items"
        ]["properties"]["detail"]["description"]
        assert "in English" in detail_desc

    def test_behavioral_analysis_finding_detail_schema(self):
        detail_desc = BEHAVIORAL_ANALYSIS_SCHEMA["schema"]["properties"]["findings"][
            "items"
        ]["properties"]["detail"]["description"]
        assert "in English" in detail_desc

    def test_behavioral_analysis_rationale_schema(self):
        rationale_desc = BEHAVIORAL_ANALYSIS_SCHEMA["schema"]["properties"][
            "recommendations"
        ]["items"]["properties"]["rationale"]["description"]
        assert "in English" in rationale_desc

    def test_behavioral_analysis_context_summary_schema(self):
        desc = BEHAVIORAL_ANALYSIS_SCHEMA["schema"]["properties"]["context_summary"][
            "description"
        ]
        assert "in English" in desc


class TestUserDecisionsPromptRendering:
    def test_render_user_decisions_section_wraps_and_truncates(self):
        section = render_user_decisions_section(
            [
                {
                    "tool": "web_search",
                    "args_redacted": {"query": "x" * 500},
                    "user_decision": "approve",
                    "user_note": "allow this\nfor research",
                }
            ]
        )

        assert section is not None
        assert "## User Decisions" in section
        assert "⟨user_decisions⟩" in section
        assert "allow this for research" in section
        assert len(section) < 900

    def test_build_prompt_includes_user_decisions_section(self):
        prompt = build_evaluation_user_prompt(
            intention="Research a bug",
            policy=None,
            recent_history=[],
            user_decisions=[
                {
                    "tool": "web_search",
                    "args_redacted": {"query": "daily brief"},
                    "user_decision": "approve",
                    "user_note": "this is fine and aligned",
                }
            ],
            session_stats={
                "total_calls": 1,
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 1,
            },
            tool="web_search",
            args={"query": "weather"},
            agent_id="agent-1",
        )

        assert "## User Decisions" in prompt
        assert "this is fine and aligned" in prompt

    def test_build_prompt_omits_user_decisions_when_empty(self):
        prompt = build_evaluation_user_prompt(
            intention="Research a bug",
            policy=None,
            recent_history=[],
            user_decisions=[],
            session_stats={
                "total_calls": 0,
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 0,
            },
            tool="web_search",
            args={"query": "weather"},
            agent_id=None,
        )

        assert "## User Decisions" not in prompt

    def test_approval_without_note_does_not_render_prior_reasoning(self):
        section = render_user_decisions_section(
            [
                {
                    "tool": "web_search",
                    "args_redacted": {"query": "daily brief"},
                    "user_decision": "approve",
                    "reasoning": "Original evaluator said not aligned",
                }
            ]
        )

        assert section is not None
        assert "prior_reasoning" not in section
