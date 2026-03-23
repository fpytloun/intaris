"""LLM client wrapper for intaris.

Provides a thin, cached OpenAI-compatible chat completions client with
structured output support (json_schema) and automatic fallback to JSON mode.

Forked from mnemory's LLM client with identical behavior.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import BadRequestError, OpenAI

from intaris.config import LLMConfig

logger = logging.getLogger(__name__)


class LLMClient:
    """Cached OpenAI-compatible LLM client.

    Reuses a single HTTP connection pool across calls. Supports structured
    outputs (json_schema) with automatic fallback to plain JSON mode for
    providers that don't support it.
    """

    def __init__(self, config: LLMConfig):
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_ms / 1000.0,
        )
        self._model = config.model
        self._temperature = config.temperature
        self._reasoning_effort = config.reasoning_effort
        self._timeout_ms = config.timeout_ms
        self._supports_structured: bool | None = None
        # Tracks unsupported parameters for this model/provider.
        # Maps param name -> fix action. Populated on first BadRequestError.
        self._param_fixes: dict[str, str] = {}

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int = 16384,
        reasoning_effort: str | None = None,
    ) -> str:
        """Generate a chat completion, returning the content string.

        Args:
            messages: Chat messages (system + user).
            json_schema: If provided, attempts structured output first
                         (json_schema mode), falling back to json_object mode.
            temperature: Override default temperature.
            max_tokens: Maximum tokens to generate.
            reasoning_effort: Override instance-level reasoning effort.

        Returns:
            The raw content string from the LLM response.
        """
        temp = temperature if temperature is not None else self._temperature

        if json_schema and self._supports_structured is not False:
            try:
                result = self._call(
                    messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": json_schema,
                    },
                    temperature=temp,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
                self._supports_structured = True
                return result
            except Exception:
                if self._supports_structured is None:
                    logger.warning(
                        "Structured outputs not supported by provider, "
                        "falling back to JSON mode. Schema enum constraints "
                        "will NOT be enforced — prompt injection resistance "
                        "is reduced."
                    )
                    self._supports_structured = False
                else:
                    raise

        # JSON mode fallback (or no schema requested)
        response_format = {"type": "json_object"} if json_schema else None
        return self._call(
            messages,
            response_format=response_format,
            temperature=temp,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    def _call(
        self,
        messages: list[dict[str, str]],
        response_format: dict | None,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> str:
        """Execute a single chat completion call.

        Handles parameter incompatibilities across model versions and
        providers. When a model rejects a parameter, the fix is cached
        so subsequent calls skip the unsupported parameter.

        Also retries on empty or truncated responses.
        """
        params = self._build_params(
            messages,
            response_format,
            temperature,
            max_tokens,
            reasoning_effort=reasoning_effort,
        )

        # Retry loop for parameter incompatibilities
        max_retries = 3
        response = None
        for attempt in range(1 + max_retries):
            try:
                response = self._client.chat.completions.create(**params)
                break
            except BadRequestError as e:
                if attempt < max_retries and self._try_fix_params(
                    e, params, max_tokens
                ):
                    continue
                raise

        assert response is not None

        # Retry on empty content.
        # When finish_reason=length and content is empty, the model ran out
        # of tokens before producing any output (all budget consumed by
        # reasoning). Double max_tokens on each retry to give it more room.
        content_retries = 2
        for retry in range(content_retries):
            choice = response.choices[0]
            content = choice.message.content or ""

            if content.strip():
                if choice.finish_reason == "length":
                    logger.warning(
                        "LLM response truncated (finish_reason=length, content_len=%d)",
                        len(content),
                    )
                break

            refusal = getattr(choice.message, "refusal", None)

            # If the model hit the token limit, double max_tokens for retry
            if choice.finish_reason == "length":
                old_max = params.get(
                    "max_completion_tokens", params.get("max_tokens", max_tokens)
                )
                new_max = old_max * 2
                if "max_completion_tokens" in params:
                    params["max_completion_tokens"] = new_max
                elif "max_tokens" in params:
                    params["max_tokens"] = new_max
                logger.warning(
                    "LLM returned empty content (finish_reason=length), "
                    "retrying with increased max_tokens %d→%d (%d/%d)",
                    old_max,
                    new_max,
                    retry + 1,
                    content_retries,
                )
            else:
                logger.warning(
                    "LLM returned empty content (finish_reason=%s, refusal=%s), "
                    "retrying (%d/%d)",
                    choice.finish_reason,
                    refusal,
                    retry + 1,
                    content_retries,
                )

            response = self._client.chat.completions.create(**params)
        else:
            choice = response.choices[0]
            content = choice.message.content or ""
            if not content.strip():
                refusal = getattr(choice.message, "refusal", None)
                logger.warning(
                    "LLM returned empty content after %d retries "
                    "(finish_reason=%s, refusal=%s)",
                    content_retries,
                    choice.finish_reason,
                    refusal,
                )

        return _clean_response(content)

    def _build_params(
        self,
        messages: list[dict[str, str]],
        response_format: dict | None,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Build API call parameters, applying any cached fixes."""
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }

        if "temperature" not in self._param_fixes:
            params["temperature"] = temperature

        if "max_tokens" in self._param_fixes:
            params["max_completion_tokens"] = max_tokens
        else:
            params["max_tokens"] = max_tokens

        if response_format:
            params["response_format"] = response_format

        effective_effort = reasoning_effort or self._reasoning_effort
        if effective_effort and "reasoning_effort" not in self._param_fixes:
            params["reasoning_effort"] = effective_effort

        return params

    def _try_fix_params(
        self,
        error: BadRequestError,
        params: dict[str, Any],
        max_tokens: int,
    ) -> bool:
        """Try to fix params based on a BadRequestError.

        Returns True if a fix was applied and the call should be retried.
        """
        error_body = getattr(error, "body", None)
        if not isinstance(error_body, dict):
            return False

        param = error_body.get("param", "")
        code = error_body.get("code", "")

        if not param or code not in (
            "unsupported_parameter",
            "unsupported_value",
        ):
            return False

        if param in self._param_fixes:
            return False

        if param == "max_tokens":
            logger.info(
                "Model %s requires max_completion_tokens — adapting",
                self._model,
            )
            self._param_fixes["max_tokens"] = "use_max_completion_tokens"
            params.pop("max_tokens", None)
            params["max_completion_tokens"] = max_tokens
            return True

        if param == "temperature":
            logger.info(
                "Model %s does not support custom temperature — omitting",
                self._model,
            )
            self._param_fixes["temperature"] = "omit"
            params.pop("temperature", None)
            return True

        if param in params:
            logger.info(
                "Model %s does not support parameter '%s' — omitting",
                self._model,
                param,
            )
            self._param_fixes[param] = "omit"
            params.pop(param, None)
            return True

        return False


def _clean_response(text: str) -> str:
    """Strip markdown code fences and <think> blocks from LLM output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    return text.strip()


def parse_json_response(
    text: str,
    *,
    expected_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Parse a JSON response from the LLM, with fallback extraction.

    Args:
        text: Raw LLM response text.
        expected_keys: If provided, validates that the parsed dict contains
            only these keys. Extra keys are logged and stripped as a
            defense against prompt injection that adds unexpected fields.

    Raises:
        ValueError: If no valid JSON can be extracted.
    """
    text = _clean_response(text)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return _validate_keys(result, expected_keys)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, dict):
                return _validate_keys(result, expected_keys)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from LLM response: {text[:200]}")


# Common LLM key hallucinations when structured output is not enforced.
# Maps hallucinated key → list of possible expected key targets.
# Disambiguation is context-aware: only remap when the target is missing.
_KEY_ALIASES: dict[str, list[str]] = {
    # Safety evaluation / alignment check
    "compatible": ["aligned"],
    "alignment": ["aligned", "intent_alignment"],
    "reason": ["reasoning"],
    "notes": ["reasoning"],
    # Session summary / compaction
    "narrative": ["summary"],
    "overall_alignment": ["intent_alignment"],
    "trajectory": ["intent_alignment"],
    "delegated_work_alignment": ["intent_alignment"],
    "tools": ["tools_used"],
    "indicators": ["risk_indicators"],
    # Judge evaluation
    "verdict": ["decision"],
    "explanation": ["reasoning"],
    "certainty": ["confidence"],
    # Behavioral analysis
    "risk": ["risk_level"],
    "risk_score": ["risk_level"],
    "summary": ["context_summary"],
}


def _validate_keys(
    result: dict[str, Any],
    expected_keys: set[str] | None,
) -> dict[str, Any]:
    """Validate, remap, and strip keys from parsed JSON response.

    Defense-in-depth for JSON mode fallback (no schema enforcement):

    1. **Alias remapping**: When the LLM returns a hallucinated key name
       (e.g., ``narrative`` instead of ``summary``), attempt to remap it
       to the expected key — but only when the expected key is missing.
       This recovers the LLM's actual output instead of discarding it.

    2. **Extra key stripping**: Remove any keys not in ``expected_keys``
       (defense against prompt injection adding unexpected fields).

    3. **Missing key detection**: After remapping and stripping, raise
       ``ValueError`` if any expected keys are still missing. This lets
       the caller (typically a task queue) retry the LLM call.
    """
    if expected_keys is None:
        return result

    # Phase 1: Alias remapping for missing keys
    present = set(result.keys())
    missing = expected_keys - present
    extra = present - expected_keys

    if missing and extra:
        remapped: dict[str, str] = {}  # hallucinated_key → expected_key
        for key in list(extra):
            targets = _KEY_ALIASES.get(key)
            if not targets:
                continue
            for target in targets:
                if target in missing:
                    remapped[key] = target
                    missing.discard(target)
                    extra.discard(key)
                    break

        if remapped:
            logger.info(
                "Remapped hallucinated LLM keys: %s",
                {k: v for k, v in remapped.items()},
            )
            for old_key, new_key in remapped.items():
                result[new_key] = result.pop(old_key)

    # Phase 2: Strip remaining extra keys
    extra = set(result.keys()) - expected_keys
    if extra:
        logger.warning(
            "Stripped unexpected keys from LLM JSON response: %s",
            extra,
        )
        result = {k: v for k, v in result.items() if k in expected_keys}

    # Phase 3: Check for missing required keys
    still_missing = expected_keys - set(result.keys())
    if still_missing:
        raise ValueError(
            f"LLM JSON response missing required keys after alias "
            f"remapping: {still_missing}"
        )

    return result
