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
