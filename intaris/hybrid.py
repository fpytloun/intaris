"""Versioned hybrid policy contracts and pure, three-valued rule evaluation.

Transport, model calls and persistence deliberately live outside this module.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intaris.effect_scope import requires_explicit_grant
from intaris.policy_evidence import (
    AUTHORITY_FIELDS,
    Evidence,
    Obligation,
    fields_for,
    from_observations,
    ground,
)

VERSION = "hybrid_v1"
BUNDLE_VERSION = "builtin-v10"
CLASSIFICATION_VERSION = "action-intention-v2"

Relationship = Literal[
    "directly_serves_intent",
    "supports_intent",
    "unrelated",
    "conflicts_with_intent",
    "unclear",
]
Profile = Literal["strict", "balanced", "bounded_autonomy"]
Outcome = Literal["approve", "escalate", "deny"]

ENUM_FIELDS = {
    "relationship": {
        "directly_serves_intent",
        "supports_intent",
        "unrelated",
        "conflicts_with_intent",
        "unclear",
    },
    "risk": {"low", "medium", "high", "critical", "unknown"},
    "authorization": {"explicit", "implicit", "absent", "unclear"},
    "operation": {"read", "write", "unknown"},
    "environment": {"development", "production", "unknown"},
    "reversibility": {"reversible", "irreversible", "unknown"},
    "side_effect_scope": {"bounded", "unbounded", "unknown"},
    "mutation": {"read_only", "mutating", "unclear"},
    "authorization_source": {
        "session_intention",
        "latest_user_message",
        "parent_intention",
        "none",
    },
    "authorization_scope": {"within_scope", "outside_scope", "unclear"},
}
SET_FIELDS = {"effects"}
EFFECTS = {"read", "write", "execute", "transmit", "delete", "unknown"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assessment(StrictModel):
    mutation: Literal["read_only", "mutating", "unclear"] = "unclear"
    relationship: Relationship = "unclear"
    risk: Literal["low", "medium", "high", "critical", "unknown"] = "unknown"
    authorization: Literal["explicit", "implicit", "absent", "unclear"] = "unclear"
    operation: Literal["read", "write", "unknown"] = "unknown"
    environment: Literal["development", "production", "unknown"] = "unknown"
    reversibility: Literal["reversible", "irreversible", "unknown"] = "unknown"
    side_effect_scope: Literal["bounded", "unbounded", "unknown"] = "unknown"
    authorization_source: Literal[
        "session_intention", "latest_user_message", "parent_intention", "none"
    ] = "none"
    authorization_scope: Literal["within_scope", "outside_scope", "unclear"] = "unclear"
    effects: list[
        Literal["read", "write", "execute", "transmit", "delete", "unknown"]
    ] = Field(default_factory=lambda: ["unknown"])


class Predicate(StrictModel):
    op: Literal[
        "all",
        "any",
        "not",
        "eq",
        "in",
        "contains",
        "tool_matches",
        "command_matches",
        "path_matches",
    ]
    children: list[Predicate] | None = None
    field: str | None = None
    value: Any = None
    quantifier: Literal["any", "all"] | None = None

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        # The runtime shape stays stable; its wire schema must express the same
        # op/field/value correlations instead of inviting arbitrary strings.
        base = handler(core_schema)
        child_ref = base["properties"]["children"]["anyOf"][0]["items"]
        branches = []

        def branch(op, properties, required):
            shape = {"op": {"const": op}, **properties}
            for name in {"children", "field", "value", "quantifier"} - shape.keys():
                shape[name] = {"type": "null"}
            branches.append(
                {
                    "type": "object",
                    "properties": shape,
                    "required": ["op", *required],
                    "additionalProperties": False,
                }
            )

        for op in ("all", "any", "not"):
            children = {"type": "array", "items": child_ref, "minItems": 1}
            if op == "not":
                children["maxItems"] = 1
            branch(op, {"children": children}, ["children"])
        for op in ("tool_matches", "command_matches", "path_matches"):
            properties = {
                "value": {"type": "string", "minLength": 1, "maxLength": 1024}
            }
            if op == "path_matches":
                properties["quantifier"] = {"enum": ["any", "all", None]}
            branch(
                op,
                properties,
                ["value"],
            )
        branch(
            "contains",
            {
                "field": {"const": "effects"},
                "value": {"enum": sorted(EFFECTS)},
            },
            ["field", "value"],
        )
        for field, values in ENUM_FIELDS.items():
            for op in ("eq", "in"):
                value = {"enum": sorted(values)}
                if op == "in":
                    value = {"type": "array", "items": value, "minItems": 1}
                branch(
                    op, {"field": {"const": field}, "value": value}, ["field", "value"]
                )
        return {"oneOf": branches, "title": base.get("title", "Predicate")}

    @model_validator(mode="after")
    def validate_operation(self) -> Predicate:
        if self.quantifier is not None and self.op != "path_matches":
            raise ValueError("Only path_matches accepts a quantifier")
        if self.op in {"all", "any", "not"}:
            if not self.children or self.field is not None or self.value is not None:
                raise ValueError("Boolean predicates require only nonempty children")
            if self.op == "not" and len(self.children) != 1:
                raise ValueError("not requires exactly one child")
        else:
            if self.children is not None:
                raise ValueError("Leaf predicates cannot have children")
            if self.op.endswith("_matches"):
                if self.field is not None or not isinstance(self.value, str):
                    raise ValueError("Matchers require only a string value")
                if not self.value or len(self.value) > 1024:
                    raise ValueError("Matcher length must be 1..1024")
            elif self.op == "contains":
                if (
                    self.field not in SET_FIELDS
                    or not isinstance(self.value, str)
                    or self.value not in EFFECTS
                ):
                    raise ValueError("contains requires an effects member")
            else:
                allowed = ENUM_FIELDS.get(self.field or "")
                values = self.value if self.op == "in" else [self.value]
                if (
                    allowed is None
                    or not isinstance(values, list)
                    or not values
                    or any(not isinstance(v, str) or v not in allowed for v in values)
                ):
                    raise ValueError("Invalid enum field or value")
        return self


class Rule(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    effect: Literal["allow", "deny", "require_approval"]
    when: Predicate
    description: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def bounded(self) -> Rule:
        count = 0

        def walk(node: Predicate, depth: int) -> None:
            nonlocal count
            count += 1
            if count > 32 or depth > 6:
                raise ValueError("Predicate exceeds 32 nodes or depth 6")
            for child in node.children or []:
                walk(child, depth + 1)

        walk(self.when, 1)
        return self


def validate_rules(raw: list[dict[str, Any]]) -> list[Rule]:
    if len(raw) > 64:
        raise ValueError("At most 64 custom rules are supported")
    rules = [Rule.model_validate(rule) for rule in raw]
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError("Rule identities must be unique")
    return rules


def digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def action_digest(
    user_id: str,
    session_id: str,
    tool: str,
    args: dict,
    *,
    mode_identity: str | None = None,
) -> str:
    return digest(
        {
            "version": VERSION,
            "user_id": user_id,
            "session_id": session_id,
            "tool": tool,
            "args": args,
            **({"mode_identity": mode_identity} if mode_identity else {}),
        }
    )


class Truth(Enum):
    FALSE = 0
    TRUE = 1
    UNKNOWN = 2


def test_predicate(node: Predicate, facts: dict[str, Any]) -> Truth:
    """Evaluate partial evidence without turning missing information into false."""
    if node.op in {"all", "any", "not"}:
        children = [test_predicate(child, facts) for child in node.children or []]
        if node.op == "not":
            return {
                Truth.FALSE: Truth.TRUE,
                Truth.TRUE: Truth.FALSE,
                Truth.UNKNOWN: Truth.UNKNOWN,
            }[children[0]]
        decisive = Truth.FALSE if node.op == "all" else Truth.TRUE
        if decisive in children:
            return decisive
        if Truth.UNKNOWN in children:
            return Truth.UNKNOWN
        return Truth.TRUE if node.op == "all" else Truth.FALSE
    if node.op.endswith("_matches"):
        field = {
            "tool_matches": "tool",
            "command_matches": "command",
            "path_matches": "paths",
        }[node.op]
        value = facts.get(field)
        if value is None:
            return Truth.UNKNOWN
        if field == "paths":
            if not value:
                return Truth.FALSE if facts.get("paths_complete") else Truth.UNKNOWN
            if node.quantifier == "all":
                if any(not fnmatchcase(path, node.value) for path in value):
                    return Truth.FALSE
                return Truth.TRUE if facts.get("paths_complete") else Truth.UNKNOWN
            if any(fnmatchcase(path, node.value) for path in value):
                return Truth.TRUE
            return Truth.FALSE if facts.get("paths_complete") else Truth.UNKNOWN
        return Truth.TRUE if fnmatchcase(value, node.value) else Truth.FALSE
    value = facts.get(node.field)
    if value is None or value in ("unknown", "unclear"):
        return Truth.UNKNOWN
    if node.op == "contains":
        if node.value in value:
            return Truth.TRUE
        return Truth.UNKNOWN if "unknown" in value else Truth.FALSE
    matches = value == node.value if node.op == "eq" else value in node.value
    return Truth.TRUE if matches else Truth.FALSE


# Prevent pytest collecting the public pure evaluator when imported by tests.
test_predicate.__test__ = False


class PolicyResult(StrictModel):
    workflow_scope: dict | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    outcome: Literal["approve", "deny", "escalate", "needs_assessment"]
    matched_rules: list[str] = Field(default_factory=list)
    mandatory_rule_ids: list[str] = Field(default_factory=list)
    unresolved_rules: list[str] = Field(default_factory=list)
    outstanding_obligations: list[Obligation] = Field(default_factory=list)
    constraint_matches: list[dict] = Field(default_factory=list)
    permission_basis: str | None = None
    evidence_quality: Literal["deterministic", "model_assessed", "unresolved"] = (
        "unresolved"
    )
    # Verification of model-asserted conflicts and which ungrounded opinions
    # the active profile declined to turn into obligations. Diagnostic only.
    grounding: dict[str, str] = Field(default_factory=dict)
    ignored_opinions: list[str] = Field(default_factory=list)


def bounded_effects(state: Evidence) -> bool:
    """Controller facts alone establish a bounded, recoverable local action.

    Model risk labels cannot escalate such an action by themselves. Unknown or
    hazardous effects, external/irreversible hints, secret operands and
    incomplete path coverage all fail this test.
    """
    values = state.comparison()
    if values.get("secret_argument") or values.get("external_or_irreversible"):
        return False
    observed_risk = state.observations.get("risk")
    if observed_risk is not None and observed_risk.value in {"high", "critical"}:
        return False
    effects = set(state.value("effects", ["unknown"]))
    if not effects or effects & {"unknown", "transmit", "delete"}:
        return False
    if values.get("static_read") and state.value("mutation") == "read_only":
        return True
    return bool(
        (state.workflow_scope or {}).get("matched") is True
        and values.get("paths_complete") is True
        and values.get("paths")
        and state.known("effects")
        and effects <= {"read", "write"}
    )


# Recorded P(critical) below which an abstained risk still admits the explicit
# user grant; the risk label itself stays unknown for every other rule.
_GRANT_CRITICAL_CEILING = 0.2


def _abstained_risk_below_critical(state: Evidence) -> bool:
    """True when a risk label abstained to unknown on thresholds (not a
    confident unknown) and the recorded P(critical) is below the ceiling.

    Only Jev records posteriors; without them (the LLM path) this is False.
    """
    if state.value("risk", "unknown") != "unknown":
        return False
    row = state.confidences.get("risk")
    if not isinstance(row, dict) or row.get("threshold_abstained") is not True:
        return False
    critical = (row.get("probabilities") or {}).get("critical")
    if isinstance(critical, bool) or not isinstance(critical, (int, float)):
        return False
    return critical < _GRANT_CRITICAL_CEILING


def evaluate_policy(
    rules: list[Rule],
    facts: dict[str, Any] | Evidence,
    profile: Profile = "balanced",
    *,
    assessed: bool = False,
) -> PolicyResult:
    """Canonical table; observations, constraints and inferences stay separate."""
    state = facts if isinstance(facts, Evidence) else from_observations(facts)
    values = state.comparison()
    observations = {key: item.value for key, item in state.observations.items()}
    if state.review is not None and state.review.obligation_ids and state.review.risk:
        observations["risk"] = state.value("risk")
    matched = {"deny": [], "allow": [], "require_approval": []}
    unresolved, obligations, records = [], [], []
    for rule in rules:
        fields = fields_for(rule.when)
        static_truth = test_predicate(rule.when, observations)
        truth = (
            static_truth
            if static_truth is not Truth.UNKNOWN
            else test_predicate(rule.when, values)
        )
        records.append(
            {
                "rule_id": rule.id,
                "effect": rule.effect,
                "applicability": truth.value,
                "comparison": "deterministic"
                if static_truth is not Truth.UNKNOWN
                else "model_assessed",
                "references": sorted(
                    {
                        ref
                        for key in fields
                        if key in state.observations
                        for ref in state.observations[key].references
                    }
                ),
            }
        )
        if truth is Truth.TRUE:
            matched[rule.effect].append(rule.id)
        elif truth is Truth.UNKNOWN:
            unresolved.append(rule.id)
            if rule.effect != "allow":
                unknown_fields = {field for field in fields if not state.known(field)}
                action_fields = {
                    field
                    for field in unknown_fields - AUTHORITY_FIELDS
                    if state.value(field) in (None, "unknown", "unclear", ["unknown"])
                    or field not in state.assessment
                }
                obligations.append(
                    Obligation(
                        id=f"rule:{rule.id}",
                        basis=rule.id,
                        fields=tuple(
                            sorted(
                                action_fields
                                or unknown_fields & AUTHORITY_FIELDS
                                or unknown_fields
                            )
                        ),
                        requirement="independent_review"
                        if not action_fields and unknown_fields & AUTHORITY_FIELDS
                        else "assessment",
                        evidence=("action",),
                        protected=rule.effect == "require_approval",
                    )
                )
                if rule.effect == "require_approval":
                    matched["require_approval"].append(rule.id)
    if values.get("configured_review"):
        matched["require_approval"].append("tool_preference.escalate")
    base = dict(
        matched_rules=sum(matched.values(), []),
        unresolved_rules=unresolved,
        mandatory_rule_ids=matched["require_approval"],
        constraint_matches=records,
    )
    # Policy-local interpretation only. Stored L1 evidence and configured rule
    # witnesses above are unchanged; a vote/basis is not corrected scope.
    review = state.review
    reviewed_scope_ids = {
        "model.denial",
        "intent.scope",
        "intent.unrelated",
        "process.current_restrictions",
        "alignment.compatibility",
    }
    judge_scope_approval = bool(
        review is not None
        and review.user_would == "approve"
        and review.basis
        in {"routine_within_task", "explicit_request", "scoped_approval"}
        and review.risk in {"low", "medium", "high"}
        and reviewed_scope_ids.intersection(review.obligation_ids)
        and not any(
            observations.get(key) == conflict
            for key, conflict in (
                ("relationship", "conflicts_with_intent"),
                ("contextual_restrictions", "conflicts"),
                ("target_scope", "conflicts"),
            )
        )
    )
    corrections = {}
    if review is not None:
        ids = set(review.obligation_ids)
        if review.corrected_assessment is not None and ids & {
            "model.denial",
            "intent.unrelated",
            "intent.scope",
            "process.current_restrictions",
            "alignment.compatibility",
            "risk.high",
            "risk.unknown",
            "model.risk.critical",
        }:
            for group in review.corrected_assessment.validated_groups(
                state, review.evidence_refs
            ):
                corrections.update(group)
        authority_rules = {
            f"rule:{rule.id}"
            for rule in rules
            if fields_for(rule.when) & AUTHORITY_FIELDS
        }
        if review.corrected_assessment is not None and ids & (
            authority_rules | {"disclosure.authorization"}
        ):
            for group in review.corrected_assessment.validated_groups(
                state, review.evidence_refs
            ):
                if set(group) <= AUTHORITY_FIELDS:
                    corrections.update(group)
        if (
            ids & {"risk.high", "risk.unknown", "model.risk.critical"}
            and review.risk is not None
        ):
            corrections["risk"] = review.risk
        elif ids & {
            "model.denial",
            "intent.scope",
            "intent.unrelated",
            "process.current_restrictions",
            "alignment.compatibility",
        } and (review.risk in {"high", "critical"} or judge_scope_approval):
            # Scope adjudication examines the actual action as well as intent.
            # Its validated severity must not leave an unreviewed model or
            # heuristic label to veto admission.
            corrections["risk"] = review.risk
        if corrections:
            changed = any(
                state.assessment.get(key) != value for key, value in corrections.items()
            )
            state = state.model_copy(
                update={"assessment": {**state.assessment, **corrections}}
            )
            if changed:
                # Recompute configured predicates too, not only built-in scope.
                return evaluate_policy(rules, state, profile, assessed=assessed)
            values = state.comparison()

    grounding = ground(state)
    ignored: list[str] = []

    def opinion_blocks(key: str) -> bool:
        """Whether a model-asserted conflict creates an obligation here.

        Grounded conflicts bind in every profile. Ungrounded ones bind unless
        the profile is bounded autonomy, which only acts on evidence of harm
        or a genuine contradiction and records the ignored opinion.
        """
        # Lack of a citation does not disprove an explicit prohibition. The
        # typed classifier can omit references even for genuine bans, so this
        # finding remains reviewable; task-relevance opinions are different.
        if key == "restriction_conflict":
            return True
        if grounding.get(key) in {"grounded", "unavailable"}:
            return True
        if profile == "bounded_autonomy":
            ignored.append(key)
            return False
        return True

    def result(outcome, basis=None, quality="unresolved", blocking=()):
        remaining = list(obligations)
        review = state.review
        if outcome != "deny" and review is not None and review.user_would is not None:
            if review.user_would == "ask":
                # A severity correction is not a completed scope review. Keep
                # deferral across recursive reassessment even if lowering risk
                # removes the obligation that originally triggered review.
                remaining.append(
                    Obligation(
                        id="review.deferred",
                        basis="review.deferred",
                        fields=(),
                        evidence=tuple(review.evidence_refs),
                        requirement="human",
                    )
                )
            elif review.user_would == "refuse":
                return PolicyResult(
                    workflow_scope=state.workflow_scope,
                    outcome="deny",
                    blocking_reasons=[*blocking, "review.refused"],
                    outstanding_obligations=remaining,
                    permission_basis=None,
                    evidence_quality=quality,
                    grounding=grounding,
                    ignored_opinions=ignored,
                    **base,
                )
            # Scope/conflicts are recomputed from corrected factual fields.
            # Only targeted model severity is discharged by the risk receipt.
            if review.risk != "critical":
                remaining = [
                    item
                    for item in remaining
                    if item.requirement == "human"
                    or item.id == "mode.read_only"
                    or item.id.startswith("rule:")
                    or item.id
                    not in {"risk.high", "risk.unknown", "model.risk.critical"}
                    or item.id not in review.obligation_ids
                    or review.risk not in {"low", "medium", "high"}
                    or (
                        review.corrected_assessment is None
                        and review.user_would != "approve"
                    )
                ]
                if judge_scope_approval:
                    # Judge owns independent adjudication against the original
                    # instructions. Its approval is an admission basis, not a
                    # fabricated correction or a grant to configured rules.
                    remaining = [
                        item
                        for item in remaining
                        if item.id not in reviewed_scope_ids
                        or item.id not in review.obligation_ids
                        or item.requirement != "independent_review"
                    ]
                outcome = "escalate" if remaining else "approve"
                basis = (
                    "independent_scope_review"
                    if judge_scope_approval and not remaining
                    else basis or "reviewed_assessment"
                )
            else:
                outcome = "deny"
        return PolicyResult(
            workflow_scope=state.workflow_scope,
            outcome=outcome,
            blocking_reasons=list(blocking),
            outstanding_obligations=remaining,
            permission_basis=basis,
            evidence_quality=quality,
            grounding=grounding,
            ignored_opinions=ignored,
            **base,
        )

    def need(identifier, fields, *, kind="assessment", protected=False):
        obligations.append(
            Obligation(
                id=identifier,
                basis=identifier,
                fields=tuple(fields),
                evidence=tuple(state.sources) or ("action",),
                requirement=kind,
                protected=protected,
            )
        )

    mutation = state.value("mutation", "unclear")
    if state.read_only_required:
        if mutation == "mutating":
            return result("deny", blocking=("request.read_only_required",))
        if mutation != "read_only":
            need("mode.read_only", ["mutation", "effects", "risk"], protected=True)
    if matched["deny"] or values.get("configured_deny"):
        return result("deny")
    if values.get("risk") == "critical":
        # Model and heuristic risk labels are fallible. Judge can correct them;
        # exact configured prohibitions and actual action identity still bind.
        need("model.risk.critical", [], kind="independent_review", protected=True)
    if matched["require_approval"] and not state.human_approved:
        need("human.checkpoint", [], kind="human", protected=True)
    if state.assessment_failed and not state.human_approved:
        need("assessment.invalid_response", [], kind="human", protected=True)
    alignment = state.parent_alignment or {}
    # A controller-proven read of non-sensitive targets cannot advance an
    # incompatible child task; parent compatibility is reviewed where the child
    # acts. Sensitive paths and secret operands never count as a static read.
    safe_read = bool(
        values.get("static_read")
        and mutation == "read_only"
        and not values.get("secret_argument")
        and set(state.value("effects", ["unknown"])) <= {"read"}
    )
    alignment_read_exempt = safe_read and (
        profile == "bounded_autonomy"
        or (
            profile == "balanced"
            and grounding.get("alignment_conflict") == "ungrounded"
        )
    )
    if (
        alignment.get("state") in {"incompatible", "unavailable"}
        and not alignment.get("overridden")
        and not alignment_read_exempt
        and opinion_blocks("alignment_conflict")
    ):
        # Parent/child compatibility is a classification like any other: it
        # is reviewed through the same pipeline, never a pre-policy hard stop.
        need("alignment.compatibility", [], kind="independent_review", protected=True)
    # Advisory clauses are L1 context only: they never route a call to
    # assessment or create obligations. Only a conflict that L1 already
    # classified against one, through contextual_restrictions, is reviewed,
    # including on a static read that reached L1 for another reason.
    compatibility = state.value("contextual_restrictions", "unresolved")
    if (
        profile == "strict"
        and any(
            source.get("interpretation_missing") for source in state.sources.values()
        )
        and not (
            review is not None
            and "intent.scope" in review.obligation_ids
            and review.user_would == "approve"
            and review.evidence_refs
        )
    ):
        need("intent.scope", [], kind="independent_review", protected=True)
    if (
        profile == "strict"
        and state.assessment
        and compatibility == "unresolved"
        and any(
            source.get("constraint_kinds")
            or any(
                item.get("status") == "current"
                and item.get("kind") in {"restriction", "disapproval", "uncertainty"}
                for item in source.get("constraint_items", [])
            )
            for source in state.sources.values()
        )
    ):
        # A supplied prohibition with unresolved applicability is different from
        # mere absence of permission. It must be reviewed before any grant.
        need(
            "intent.scope",
            ["contextual_restrictions", "restriction_reference"],
            kind="independent_review",
            protected=True,
        )
    if compatibility == "conflicts":
        # A model-classified conflict with operator prose is reviewed, never a
        # deterministic deny; exact compiled rules own hard prohibitions.
        if (
            corrections.get("contextual_restrictions") == "conflicts"
            and grounding.get("confirmed_restriction_conflict") == "grounded"
            and not judge_scope_approval
        ):
            return result("deny")
        if opinion_blocks("restriction_conflict"):
            need("model.denial", [], kind="independent_review", protected=True)
    static_read_proven = (
        values.get("static_read")
        and mutation == "read_only"
        and state.known("effects")
        and set(state.value("effects", [])) <= {"read"}
        and not state.value("external_or_irreversible", False)
        and not state.value("secret_argument")
        and state.assessment.get("relationship") != "conflicts_with_intent"
        and not (
            profile == "balanced"
            and state.assessment.get("relationship") == "unrelated"
            and state.value("risk") == "medium"
        )
        and state.assessment.get("target_scope") != "conflicts"
        and state.assessment.get("risk") not in {"high", "critical"}
    )
    if static_read_proven and [item.id for item in obligations] == [
        "alignment.compatibility"
    ]:
        # A read cannot act outside the parent's scope by effect, so the
        # compatibility question is moot for it in every profile. Any other
        # outstanding obligation keeps its own route below.
        return result("approve", "read_only_static", "deterministic")
    if static_read_proven and profile in {"balanced", "bounded_autonomy"}:
        if not obligations:
            return result("approve", "read_only_static", "deterministic")
        if all(item.requirement == "independent_review" for item in obligations):
            # Facts already prove this is a read. Missing original authority
            # belongs to Judge, not a redundant L1 permission classification.
            return result("escalate")
    target_scope = state.value("target_scope", "unresolved")
    lifecycle = state.process_lifecycle or {}
    if lifecycle.get("authenticated") is True and lifecycle.get("matched") is True:
        # Scope is established by the admitted spawn and authenticated registry,
        # not inferred again from a task summary. Current restrictions still win.
        if not state.assessment:
            need("process.current_restrictions", ["contextual_restrictions", "risk"])
            return result("escalate")
        if compatibility == "conflicts" and not any(
            item.id == "model.denial" for item in obligations
        ):
            if opinion_blocks("restriction_conflict"):
                need("process.current_restrictions", [], kind="independent_review")
        elif compatibility == "unresolved" and profile == "strict":
            need("process.current_restrictions", [], kind="independent_review")
        risk = state.value("risk", "unknown")
        if risk == "high":
            need("risk.high", [], kind="independent_review")
        elif risk == "unknown":
            # Registry ownership is permission to manage a process, not proof
            # that stopping it has bounded operational impact.
            need("risk.unknown", [], kind="independent_review")
        # This assessment deliberately omits task relationship and target:
        # authenticated spawn scope already owns those questions. Never fall
        # through into the generic intent.assessment path after a partial L1.
        return result(
            "escalate" if obligations else "approve",
            "task_process_lifecycle",
            "deterministic",
        )
    target_ref = state.assessment.get("target_reference")
    admissible_targets = {"action"} | {
        ref
        for observation in state.observations.values()
        for ref in observation.references
    }
    # Permission bases are alternatives. Failure to establish a scoped grant
    # cannot veto a separate semantic basis; prohibitions/checkpoints above are
    # global obligations and remain binding on every alternative.
    covered_allows = {
        rule.id
        for rule in rules
        if rule.id in matched["allow"]
        and (
            "paths" not in fields_for(rule.when)
            or (
                state.value("paths_complete") is True
                and bool(values.get("paths"))
                # Check membership for each target, preserving boolean/negation
                # semantics instead of rewriting existential leaves under NOT.
                and all(
                    test_predicate(rule.when, {**values, "paths": [path]}) is Truth.TRUE
                    for path in values["paths"]
                )
            )
        )
    }
    configured_grant = bool(
        (state.workflow_scope or {}).get("matched")
        or values.get("configured_allow")
        or (
            profile != "strict"
            and values.get("static_read")
            and mutation == "read_only"
        )
    )
    effects_known = state.value("effects") is not None and "unknown" not in state.value(
        "effects", ["unknown"]
    )
    grant = effects_known and bool(covered_allows or configured_grant)
    deterministic_allows = {
        item["rule_id"]
        for item in records
        if item["rule_id"] in covered_allows and item["comparison"] == "deterministic"
    }
    deterministic_grant = (
        grant
        and state.known("effects")
        and bool(deterministic_allows or configured_grant)
    )
    if (
        profile == "strict"
        and not deterministic_grant
        and not state.human_approved
        and (
            "transmit" in state.value("effects", [])
            or (
                observations.get("mutation") != "read_only"
                and (target_scope == "unresolved" or compatibility == "unresolved")
            )
        )
    ):
        need("intent.scope", [], kind="independent_review")
    source = state.sources.get(state.assessment.get("authorization_source"), {})
    authority = (
        (
            state.assessment.get("authorization") in {"explicit", "implicit"}
            or (source.get("kind") == "approval" and source.get("approves"))
        )
        and state.assessment.get("authorization_scope") == "within_scope"
        and source.get("independent_grant") is True
    )
    # An explicit instruction or confirmation of this action,
    # cited from an independent user source, authorizes it in every mode,
    # including strict where it is the evidence. Risk must be assessed and
    # below critical, or a Jev threshold abstention whose recorded P(critical)
    # is below 0.2 (the label stays unknown elsewhere); configured denials, human checkpoints and restriction
    # conflicts above still bind; session min/max caps apply at finalization.
    # The cited source must be the user's own words (a user message or human
    # decision), not a session summary or the broad initial assignment.
    user_words = source.get("origin") in {
        "recorded_client_user_message",
        "client_user_claim",
        "recorded_user_event",
        "human_resolution",
    }
    if (
        authority
        and user_words
        and state.assessment.get("authorization") == "explicit"
        and compatibility != "conflicts"
        and target_scope != "conflicts"
        and (
            state.value("risk", "unknown") in {"low", "medium", "high"}
            or _abstained_risk_below_critical(state)
        )
        and not obligations
    ):
        return result("approve", "explicit_user_authorization", "model_assessed")

    def granted():
        return result(
            "approve",
            sorted(deterministic_allows)[0]
            if deterministic_allows
            else "controller_workflow_step"
            if (state.workflow_scope or {}).get("matched")
            else "configured_or_static",
            "deterministic",
        )

    if (
        state.value("secret_argument") is True
        and "disclose" in state.value("effects", [])
        and not (
            authority
            and user_words
            and state.assessment.get("authorization") == "explicit"
        )
        and not deterministic_grant
        and not state.human_approved
    ):
        # Lower severity is not evidence that the user authorized disclosure.
        # This survives generic risk/scope review until a scoped grant exists.
        need(
            "disclosure.authorization",
            sorted(AUTHORITY_FIELDS),
            kind="independent_review",
        )

    # Common factual assessment, progressively broader scope defaults.
    if "relationship" not in state.assessment:
        if deterministic_grant and not obligations and not state.sources:
            return granted()
        need(
            "intent.assessment",
            [
                "relationship",
                "risk",
                "mutation",
                "effects",
                "contextual_restrictions",
                "restriction_reference",
                "target_scope",
                "target_reference",
            ],
        )
        return result("escalate" if assessed else "needs_assessment")
    relationship = state.assessment["relationship"]
    risk = state.value("risk", "unknown")
    real_target_conflict = target_scope == "conflicts" and opinion_blocks(
        "target_conflict"
    )
    relationship_conflict = relationship == "conflicts_with_intent" and opinion_blocks(
        "relationship_conflict"
    )
    if not judge_scope_approval and (
        (
            real_target_conflict
            and target_ref in admissible_targets
            and corrections.get("target_scope") == "conflicts"
        )
        or (
            corrections.get("relationship") in {"unrelated", "conflicts_with_intent"}
            and corrections.get("risk") in {"high", "critical"}
        )
    ):
        if (
            review is not None
            and review.user_would == "ask"
            and review.basis in {"outside_task", "uncertain"}
            and risk in {"low", "medium"}
        ):
            # Model-derived scope corrections are not binding controls, which
            # retain their earlier precedence. Asking is not refusing.
            need("intent.scope", review.evidence_refs, kind="independent_review")
            return result("escalate")
        return result("deny")
    if (real_target_conflict or relationship_conflict) and not any(
        item.id == "model.denial" for item in obligations
    ):
        need("model.denial", [], kind="independent_review", protected=True)
    if (
        profile == "strict"
        and relationship not in {"directly_serves_intent", "supports_intent"}
        and not grant
    ):
        need("intent.scope", [], kind="independent_review")
    elif profile == "balanced" and relationship == "unrelated" and risk == "medium":
        need("intent.unrelated", [], kind="independent_review")
    if risk == "high" and (
        profile != "bounded_autonomy"
        or state.value("external_or_irreversible") is True
        or state.value("secret_argument") is True
    ):
        need("risk.high", [], kind="independent_review")
    # A label recovered from a risk abstention resolves label ambiguity only.
    # It cannot discharge a consequential boundary (external or irreversible
    # observation, delete/disclose effects, irreversible reversibility, or a
    # production mutation): treat such an action as still unassessed unless a
    # deterministic grant or a human receipt for this action already covers it.
    recovered_risk = bool(
        (state.confidences.get("risk") or {}).get("abstention_fallback")
    )
    if (
        recovered_risk
        and requires_explicit_grant(state)
        and not deterministic_grant
        and not state.human_approved
    ):
        risk = "unknown"
    if (
        risk == "unknown"
        and profile != "bounded_autonomy"
        and mutation != "read_only"
        and not deterministic_grant
        and not (profile == "balanced" and bounded_effects(state))
    ):
        # Intent alignment is not a completed risk assessment. Preserve the
        # earlier explicit-source/configured-grant paths, but do not convert
        # an abstention on a mutating action into semantic permission. When
        # controller facts already bound the effects, a missing model label
        # is not evidence of harm (balanced); strict still reviews it.
        need("risk.unknown", [], kind="independent_review")
    if obligations:
        return result("escalate")
    if deterministic_grant:
        return granted()
    return result("approve", "intent_aligned", "model_assessed")
