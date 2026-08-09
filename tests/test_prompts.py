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
    render_recent_reasoning_section,
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

    def test_judge_risk_schema_exists(self):
        desc = JUDGE_EVALUATION_SCHEMA["schema"]["properties"]["risk"]["description"]
        assert "risk level" in desc.lower()

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

    def test_system_prompt_mentions_similar_operation_families(self):
        assert "sufficiently similar **operation**" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "web_search` and `web_fetch`" in SAFETY_EVALUATION_SYSTEM_PROMPT

    def test_system_prompt_mentions_multi_part_intentions(self):
        assert "multiple active deliverables" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "most relevant part" in SAFETY_EVALUATION_SYSTEM_PROMPT

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


class TestRecentReasoningPromptRendering:
    def test_render_recent_reasoning_section_wraps_context(self):
        section = render_recent_reasoning_section(
            [
                {
                    "timestamp": "2026-05-07T10:51:33Z",
                    "content": (
                        "Plan context: add context_type to search_conversations "
                        "and pass it through to grouped search results."
                    ),
                    "args_redacted": {
                        "context": "Assistant proposed backend tool update."
                    },
                }
            ]
        )

        assert section is not None
        assert "## Recent Reasoning" in section
        assert "⟨reasoning_history⟩" in section
        assert "context_type" in section
        assert "Assistant proposed backend tool update" in section

    def test_build_prompt_includes_recent_reasoning_section(self):
        prompt = build_evaluation_user_prompt(
            intention="Finalize search UI changes",
            policy=None,
            recent_history=[],
            recent_reasoning=[
                {
                    "timestamp": "2026-05-07T10:51:33Z",
                    "content": (
                        "Plan context: LLM tool surface requires optional "
                        "context_type parameter."
                    ),
                }
            ],
            session_stats={
                "total_calls": 1,
                "approved_count": 1,
                "denied_count": 0,
                "escalated_count": 0,
            },
            tool="edit",
            args={"filePath": "cognis/tools/builtin/conversations.py"},
            agent_id="opencode",
        )

        assert "## Recent Reasoning" in prompt
        assert "LLM tool surface" in prompt
        assert "⟨reasoning_history⟩" in prompt

    def test_evaluator_prompt_keeps_ineligible_reasoning_context(self):
        prompt = build_evaluation_user_prompt(
            intention="Keep the trusted intention",
            policy=None,
            recent_history=[],
            recent_reasoning=[
                {
                    "content": "User message: external audit evidence",
                    "args_redacted": {"intention_eligible": False},
                }
            ],
            session_stats={
                "total_calls": 1,
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 0,
            },
            tool="bash",
            args={"command": "git status"},
            agent_id="opencode",
        )

        assert "external audit evidence" in prompt

    def test_build_prompt_includes_guardrails_context_as_advisory_data(self):
        prompt = build_evaluation_user_prompt(
            intention="Inspect monitoring state",
            policy=None,
            recent_history=[],
            session_stats={
                "total_calls": 1,
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 0,
            },
            tool="alertmanager_silences",
            args={},
            agent_id="cognis",
            context={
                "tool": {
                    "description": "List Alertmanager silences",
                    "read_only": True,
                    "capabilities": ["read"],
                }
            },
        )

        assert "## Guardrails Context" in prompt
        assert "Client-provided, redacted context" in prompt
        assert "not as authorization proof" in prompt
        assert "List Alertmanager silences" in prompt
        assert "⟨context⟩" in prompt


class TestEvaluatorScopeRules:
    """The L1 evaluator must focus on operational safety, not topic or
    tool-argument quality. These regression tests guard the prompt rules
    that prevent false-deny on sensitive subjects and false-escalate on
    ambiguous arguments."""

    def test_prompt_declares_operational_safety_scope(self):
        assert "Scope of Safety Evaluation" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "operational safety" in SAFETY_EVALUATION_SYSTEM_PROMPT

    def test_prompt_forbids_content_moderation(self):
        assert "No content moderation" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "Topic is never the risk" in SAFETY_EVALUATION_SYSTEM_PROMPT

    def test_prompt_forbids_legality_assessment(self):
        assert "No legality assessment" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "Owner-operations on owner-property" in SAFETY_EVALUATION_SYSTEM_PROMPT

    def test_prompt_forbids_arg_ambiguity_as_risk(self):
        assert "Tool-argument quality is not safety" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert (
            "Tool-argument ambiguity is never the risk"
            in SAFETY_EVALUATION_SYSTEM_PROMPT
        )

    def test_prompt_critical_requires_operational_action(self):
        assert (
            "Critical risk requires an *operationally* dangerous action"
            in SAFETY_EVALUATION_SYSTEM_PROMPT
        )
        assert (
            "destructive or exfiltrating *operation*" in SAFETY_EVALUATION_SYSTEM_PROMPT
        )
        assert "subject matter" in SAFETY_EVALUATION_SYSTEM_PROMPT

    def test_prompt_mentions_recent_reasoning_as_context(self):
        assert "Recent reasoning records" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "compressed" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "contextual evidence" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "low/medium-risk source edits" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "tooling, schema, or API subtask" in SAFETY_EVALUATION_SYSTEM_PROMPT

    def test_prompt_approves_routine_validation_commands(self):
        assert "Routine project validation commands" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert "npm test" in SAFETY_EVALUATION_SYSTEM_PROMPT
        assert (
            "recent human-approved scope expansion" in SAFETY_EVALUATION_SYSTEM_PROMPT
        )


class TestJudgeScopeRules:
    """The judge must mirror the L1 scope rules — same operational-only focus."""

    def test_judge_system_prompt_has_operational_safety_scope(self):
        assert "operational safety only" in JUDGE_SYSTEM_PROMPT
        assert "Tool-argument ambiguity is not a safety signal" in JUDGE_SYSTEM_PROMPT

    def test_judge_auto_decision_rules_forbid_topic_moderation(self):
        from intaris.judge import _DECISION_RULES_AUTO

        assert "operational safety" in _DECISION_RULES_AUTO
        assert "Do NOT moderate by topic" in _DECISION_RULES_AUTO
        assert "ambiguity" in _DECISION_RULES_AUTO

    def test_judge_advisory_decision_rules_forbid_topic_moderation(self):
        from intaris.judge import _DECISION_RULES_ADVISORY

        assert "operational safety" in _DECISION_RULES_ADVISORY
        assert "Do NOT moderate by topic" in _DECISION_RULES_ADVISORY
        assert "Approve" in _DECISION_RULES_ADVISORY

    def test_judge_advisory_approves_in_project_edits_with_plan_context(self):
        from intaris.judge import _DECISION_RULES_ADVISORY

        assert "Medium-risk in-project source edits" in _DECISION_RULES_ADVISORY
        assert "recent reasoning" in _DECISION_RULES_ADVISORY
        assert "not directly aligned" in _DECISION_RULES_ADVISORY

    def test_judge_advisory_deny_excludes_topic_only(self):
        from intaris.judge import _DECISION_RULES_ADVISORY

        assert (
            "Never** deny for sensitive subject matter alone"
            in _DECISION_RULES_ADVISORY
        )
