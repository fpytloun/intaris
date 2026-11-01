"""Source-context comparison preparation and paired execution boundary.

No model credentials, provider calls or fixture commands are used by freeze.
Execution requires reviewed runtime hooks; no source-role/approval fallback is
implemented using text prefixes or internal approval flags.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import re
import secrets
from collections import Counter
from pathlib import Path
from typing import Protocol

from tools.benchmark.matched import MODEL_SETTINGS, digest, write_json

# Current public fixture identities, not the pre-reconstruction model evidence.
BASELINE_HASH = "bf7af507b6ea391385c4f7ccf30a9749106a906ebcfd11b5fdc0719e5eb5d3f3"
BASE_REVISION = "92828d6517daff3f7665fa20eac21af65aabd1ee"
GIT_HASH = "149aa6ea3c18ffbc69254b2fd14de28ddc302944ec1a5de89c89f1c2a6626e3d"
FIXTURE = Path(__file__).parent / "fixtures/source-context-focused-v1.json"


def focused_packet(case: dict) -> dict:
    """Allowlisted public source/event content only, identical for both arms."""
    events = []
    for event in case["events"]:
        if event["role"] == "decision":
            events.append(
                {
                    "role": "decision",
                    "decision": event["decision"],
                    "mechanism": "public_human_resolution",
                    "action": copy.deepcopy(event["action"]),
                }
            )
        else:
            events.append({"role": event["role"], "text": event["text"]})
    return {
        "intention": case["intention"],
        "details": {"working_directory": "/workspace/harbor"},
        "policy": None,
        "events": events,
        "steps": [{"tool": case["tool"], "args": copy.deepcopy(case["args"])}],
    }


def original_packet(scenario: dict) -> dict:
    """Preserve original user-facing fields; labels/names never cross the hook."""
    steps = []
    for step in scenario["steps"]:
        steps.append(
            {
                key: copy.deepcopy(step[key])
                for key in (
                    "tool",
                    "args",
                    "context",
                    "reasoning",
                    "latest_user_message",
                    "minimum_outcome",
                )
                if key in step
            }
        )
    return {
        "intention": scenario["intention"],
        "details": copy.deepcopy(scenario.get("details")),
        "policy": copy.deepcopy(scenario.get("policy")),
        "events": [],
        "steps": steps,
    }


def eligible_source_signature(packet: dict) -> str:
    """Evaluator-side invariant oracle, not a runtime cache-key implementation."""
    return digest(
        {
            "intention": packet["intention"],
            "policy_clues": packet["policy"],
            "events": [
                event
                for event in packet["events"]
                if event["role"] in {"user", "decision"}
            ],
        }
    )


def freeze(baseline: dict, git: dict, contract: Path) -> dict:
    if (
        baseline["sha256"] != BASELINE_HASH
        or digest({k: v for k, v in baseline.items() if k != "sha256"}) != BASELINE_HASH
    ):
        raise ValueError("Original47 identity mismatch")
    if (
        git["sha256"] != GIT_HASH
        or digest({k: v for k, v in git.items() if k != "sha256"}) != GIT_HASH
    ):
        raise ValueError("Original Git manifest changed")
    focused = json.loads(FIXTURE.read_text())
    cases = focused["cases"]
    counts = Counter(case["pair"] for case in cases)
    if len(cases) != 24 or len(counts) != 12 or set(counts.values()) != {2}:
        raise ValueError("Focused24/12 pair inventory changed")
    value = {
        "version": 1,
        "cohort": "source_context_comparison_v1",
        "base_revision": BASE_REVISION,
        "baseline_manifest_sha256": baseline["sha256"],
        "git_manifest_sha256": git["sha256"],
        "focused_literal_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "budget_authorization": "Main preregistered cumulative4000-attempt/$10 authorization",
        "budget": {
            "provider_attempts": 4000,
            "known_price_usd": 10.0,
            "consecutive_upstream_failures": 3,
        },
        "trials": 3,
        "seed": 2026100609,
        "arms": ["baseline", "source_context"],
        "backends": ["llm", "jev"],
        "judges": ["disabled", "advisory"],
        "models": MODEL_SETTINGS,
        "jev": {"minimum_posterior": 0.6, "minimum_margin": 0.1},
        "lanes": {
            "original47": copy.deepcopy(baseline["scenarios"]),
            "git": copy.deepcopy(git["actions"]),
            "focused": copy.deepcopy(cases),
        },
        "human_auto_approval": False,
        "render_contract": {
            "baseline": "unchanged_mixed",
            "treatment_l1": "compact_rich_intention_action_policy_reference_metadata",
            "treatment_l1_full_history": False,
            "judge": "captured_originals_history_previous_classification",
            "extractor": "eligible_sources_only_action_independent",
            "fallback": "partial_sidecar_and_existing_compact_intention",
            "human_origin_filter": "resolved_by=user",
            "routing": "unchanged",
        },
        "compiler_expected_calls": 0,
        "live_ready": False,
        "required_hooks": [
            "approved_clean_candidate_and_off_unit_evidence",
            "single_opt_in_switch_only",
            "public_role_recording_without_text_role_inference",
            "public_decision_resolution_with_original_scope",
            "equal_raw_policy_clues_without_compiler",
            "source_collection_and_extraction_telemetry",
            "pinned120b_extraction_route_and_price_upper_bound",
            "compact_l1_captured_originals_judge_rendering",
            "source_availability_coverage_and_artifact_hash",
        ],
    }
    value["sha256"] = digest(value)
    return value


def verify(manifest: dict) -> None:
    if (
        digest({k: v for k, v in manifest.items() if k != "sha256"})
        != manifest["sha256"]
    ):
        raise ValueError("Frozen source-context manifest changed")
    if manifest["baseline_manifest_sha256"] != BASELINE_HASH:
        raise ValueError("Original47 mismatch")
    if manifest["git_manifest_sha256"] != GIT_HASH:
        raise ValueError("Original Git inputs mismatch")
    if manifest["models"] != MODEL_SETTINGS:
        raise ValueError("Pinned model settings changed")
    if manifest["budget"] != {
        "provider_attempts": 4000,
        "known_price_usd": 10.0,
        "consecutive_upstream_failures": 3,
    }:
        raise ValueError("Approved cumulative budget changed")
    if manifest["trials"] != 3 or manifest["jev"] != {
        "minimum_posterior": 0.6,
        "minimum_margin": 0.1,
    }:
        raise ValueError("Fixed trials/calibration changed")
    if sum(len(s["steps"]) for s in manifest["lanes"]["original47"]) != 47:
        raise ValueError("Original47 count changed")


def schedule(manifest: dict) -> list[dict]:
    """Scenario blocks preserve chronology; each backend/Judge pairs arm order."""
    verify(manifest)
    jobs = []
    for trial in range(1, manifest["trials"] + 1):
        rng = random.Random(manifest["seed"] + trial)
        cases = [
            (lane, case) for lane, cases in manifest["lanes"].items() for case in cases
        ]
        rng.shuffle(cases)
        for lane, case in cases:
            for judge in manifest["judges"]:
                for backend in manifest["backends"]:
                    arms = list(manifest["arms"])
                    rng.shuffle(arms)
                    packet = (
                        focused_packet(case)
                        if lane == "focused"
                        else original_packet(case)
                    )
                    for arm in arms:
                        jobs.append(
                            {
                                "lane": lane,
                                "case_id": case["id"],
                                "trial": trial,
                                "backend": backend,
                                "judge": judge,
                                "arm": arm,
                                "packet": copy.deepcopy(packet),
                                "packet_sha256": digest(packet),
                                "expected_calls": len(packet["steps"]),
                            }
                        )
    return jobs


def _extraction_check(packet: dict, telemetry: dict | None) -> dict:
    """Validate provided statements against actual recorded source text, not gold."""
    if not telemetry:
        return {"state": "unverifiable", "quote_checks": []}
    if telemetry.get("status") != "ready":
        return {
            "state": "fallback_or_unavailable",
            "status": telemetry.get("status"),
            "quote_checks": [],
            "extraction_passed": False,
        }
    canonical = "originals" in telemetry
    sources = (
        [
            {
                "id": item["ref"],
                "origin": item["origin"],
                "text": item["content"],
                "scope_refs": item.get("scope_refs", []),
                "truncated": item.get("truncated", False),
            }
            for item in telemetry.get("originals", [])
        ]
        if canonical
        else telemetry.get("sources") or []
    )
    public_texts = [
        packet["intention"],
        *(e["text"] for e in packet["events"] if e["role"] == "user"),
        *(
            s["latest_user_message"]
            for s in packet["steps"]
            if s.get("latest_user_message")
        ),
    ]
    results = []
    source_map = {s["id"]: s for s in sources}
    statements = (
        telemetry.get("items", []) if canonical else telemetry.get("statements", [])
    )
    for statement in statements:
        if canonical:
            statement = {
                **statement,
                "source_id": statement["source_ref"],
            }
        source = source_map.get(statement.get("source_id"))
        quote = statement.get("quote")
        span_matches = True
        if canonical:
            start, end = statement.get("start"), statement.get("end")
            span_matches = bool(
                source
                and isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and 0 <= start < end <= len(source["text"])
                and source["text"][start:end] == quote
                and statement.get("scope_refs") == source["scope_refs"]
            )
        valid = bool(
            source
            and source.get("origin")
            in {
                "client_user",
                "explicit_client_assignment",
                "client_assignment",
                "client_user_claim",
                "recorded_user_event",
            }
            and source.get("text") in public_texts
            and isinstance(quote, str)
            and quote
            and quote in source["text"]
            and span_matches
        )
        results.append({"source_id": statement.get("source_id"), "valid": valid})
    # Decision statements require audit-record validation, not fabricated text.
    return {
        "state": "observed",
        "quote_checks": results,
        "duplicate_source_ids": len(source_map) != len(sources),
        "all_quotes_valid": bool(results) and all(item["valid"] for item in results),
        "extraction_passed": bool(results)
        and all(item["valid"] for item in results)
        and len(source_map) == len(sources),
        "decision_quote_validation": "requires_public_decision_audit_hook",
        "canonical_artifact": canonical,
        "coverage": copy.deepcopy(telemetry.get("coverage")),
        "source_digest": telemetry.get("source_digest"),
        "artifact_digest": telemetry.get("artifact_digest"),
    }


def extraction_check(packet: dict, telemetry: dict | None) -> dict:
    """Malformed metadata is retained as invalid, never allowed to discard a row."""
    try:
        return _extraction_check(packet, telemetry)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {
            "state": "invalid_telemetry",
            "quote_checks": [],
            "extraction_passed": False,
        }


class RuntimeHooks(Protocol):
    """Trusted harness hooks supplied only after core/public schema review."""

    def preflight(self, config: dict) -> dict: ...
    def replay(
        self,
        *,
        packet: dict,
        session_id: str,
        arm: str,
        backend: str,
        judge: str,
        trial: int,
        relay,
    ) -> list[dict]: ...


def execute(
    manifest: dict, hooks: RuntimeHooks | None, relay, output: Path, *, scope=None
) -> None:
    """One relay/ledger for the entire matrix; no per-arm counter reset."""
    verify(manifest)
    from tools.benchmark.source_scope import attempt_cap

    cap = attempt_cap(scope, manifest["sha256"])
    if hooks is None:
        raise RuntimeError("Reviewed source-context runtime hooks not ready")
    config = {
        key: copy.deepcopy(manifest[key])
        for key in (
            "sha256",
            "base_revision",
            "trials",
            "seed",
            "models",
            "jev",
            "budget",
            "render_contract",
            "human_auto_approval",
        )
    }
    config["execution_scope"] = copy.deepcopy(scope)
    proof = hooks.preflight(config)
    required = (
        "approved_revision",
        "clean_runtime",
        "baseline_off_unit_evidence",
        "single_switch_verified",
        "role_recording_verified",
        "public_approval_verified",
        "equal_policy_clues_verified",
        "extraction_telemetry_verified",
        "human_resolved_by_user_filter_verified",
        "compact_l1_rendering_verified",
        "judge_captured_originals_verified",
        "source_artifact_hash_verified",
        "pinned_config_verified",
        "price_bound_verified",
    )
    revision = proof.get("approved_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Concrete reviewed runtime revision required")
    if not all(
        isinstance(proof.get(key), dict)
        and proof[key].get("passed") is True
        and isinstance(proof[key].get("reference"), str)
        and bool(proof[key]["reference"])
        and isinstance(proof[key].get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", proof[key]["sha256"])
        for key in required
        if key != "approved_revision"
    ):
        raise RuntimeError("Source-context runtime preflight incomplete")
    if relay.budget is None or relay.max_requests != cap:
        raise RuntimeError("Shared cumulative budget required")
    if not relay.forbid_compiler:
        raise RuntimeError("Compiler-zero guard required")
    if (
        relay.budget.max_attempts != cap
        or relay.budget.max_usd != 10
        or relay.budget.snapshot()["attempts"] != 0
    ):
        raise RuntimeError("One fresh cumulative ledger for the entire matrix required")
    output.mkdir(exist_ok=False, parents=True)
    jobs = schedule(manifest)
    planned = sum(job["expected_calls"] for job in jobs)
    info = {
        "manifest_sha256": manifest["sha256"],
        "planned_calls": planned,
        "recorded_calls": 0,
        "missing_calls": planned,
        "completed": False,
        "runtime_proof": proof,
        "execution_scope": copy.deepcopy(scope),
    }
    write_json(output / "manifest.json", manifest)
    write_json(output / "run.json", info)
    try:
        with (output / "results.jsonl").open("w") as stream:
            for job in jobs:
                relay.context = {
                    k: v
                    for k, v in job.items()
                    if k not in {"packet", "expected_calls"}
                }
                try:
                    batch = hooks.replay(
                        packet=copy.deepcopy(job["packet"]),
                        session_id=secrets.token_hex(16),
                        arm=job["arm"],
                        backend=job["backend"],
                        judge=job["judge"],
                        trial=job["trial"],
                        relay=relay,
                    )
                except Exception as exc:
                    batch = [{"error": type(exc).__name__, "state": "hook_failure"}]
                for index, row in enumerate(batch):
                    telemetry = row.get("source_context") or row.get("task_context")
                    recorded = {**relay.context, "step_index": index, "result": row}
                    recorded["extraction_validation"] = extraction_check(
                        job["packet"], telemetry
                    )
                    recorded["telemetry_missing"] = [
                        key
                        for key in (
                            "source_availability",
                            "input_coverage",
                            "source_artifact_sha256",
                            "l1_input_sha256",
                            "judge_input_sha256",
                            "extraction_cache",
                            "negative_or_ambiguous",
                            "actually_entered_judge",
                        )
                        if key not in row
                    ]
                    stream.write(json.dumps(recorded, sort_keys=True) + "\n")
                    stream.flush()
                info["recorded_calls"] += len(batch)
                info["missing_calls"] = planned - info["recorded_calls"]
                write_json(output / "run.json", info)
                relay.drain()
                if len(batch) != job["expected_calls"] or any(
                    r.get("state") == "hook_failure" for r in batch
                ):
                    raise RuntimeError(
                        "Replay hook failed or changed original scenario count"
                    )
                if relay.validation_errors:
                    raise RuntimeError(
                        "Provider/schema boundary violation; rows retained"
                    )
                if relay.stopped:
                    raise RuntimeError("Cumulative budget/provider stop; rows retained")
        info["completed"] = True
    finally:
        relay.drain()
        write_json(output / "provider-attempts.json", relay.records)
        write_json(output / "provider-validation-errors.json", relay.validation_errors)
        write_json(output / "budget.json", relay.budget.snapshot())
        write_json(output / "run.json", info)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--git-manifest", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Freeze into a fresh output; previous manifests are immutable")
    manifest = freeze(
        json.loads(args.baseline.read_text()),
        json.loads(args.git_manifest.read_text()),
        args.contract,
    )
    write_json(args.output, manifest)
    print(f"Frozen focused24 + unchanged47/Git: {manifest['sha256']}")


if __name__ == "__main__":
    main()
