"""Fixture/transport/budget tests only; no models, secrets or proposed actions."""

import copy
import json
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest

from tools.benchmark.source_budget import CumulativeBudget
from tools.benchmark.source_context import (
    FIXTURE,
    eligible_source_signature,
    execute,
    extraction_check,
    focused_packet,
    freeze,
    schedule,
    verify,
)


def manifest(tmp_path):
    contract = tmp_path / "contract.md"
    contract.write_text("Compact L1; contextual Judge; input-only paired experiment.")
    git = json.loads(FIXTURE.with_name("source-context-git-v1.json").read_text())
    baseline = json.loads(
        FIXTURE.with_name("source-context-original47-v1.json").read_text()
    )
    return freeze(baseline, git, contract)


def test_reconstructed_public_snapshots_verify_and_reject_content_drift(tmp_path):
    frozen = json.loads(
        FIXTURE.with_name("source-context-comparison-v1.json").read_text()
    )
    verify(frozen)
    current = manifest(tmp_path)
    assert current["baseline_manifest_sha256"] == frozen["baseline_manifest_sha256"]
    assert current["git_manifest_sha256"] == frozen["git_manifest_sha256"]
    assert current["lanes"] == frozen["lanes"]
    assert current["live_ready"] is False
    changed = copy.deepcopy(frozen)
    changed["lanes"]["original47"][0]["intention"] += " Changed scope."
    with pytest.raises(ValueError, match="Frozen source-context manifest changed"):
        verify(changed)
    baseline = json.loads(
        FIXTURE.with_name("source-context-original47-v1.json").read_text()
    )
    git = json.loads(FIXTURE.with_name("source-context-git-v1.json").read_text())
    baseline["scenarios"][0]["intention"] += " Changed scope."
    with pytest.raises(ValueError, match="Original47 identity mismatch"):
        freeze(baseline, git, tmp_path / "contract.md")


def test_literal24_fixed_labels_and_pair_semantics():
    cases = json.loads(FIXTURE.read_text())["cases"]
    assert len(cases) == 24
    assert Counter(c["label"] for c in cases) == {"benign": 9, "harmful": 15}
    for name in {c["pair"] for c in cases}:
        left, right = [c for c in cases if c["pair"] == name]
        a, b = focused_packet(left), focused_packet(right)
        if left["kind"] == "source_counterfactual":
            assert a["steps"] == b["steps"]
            assert left["label"] == "benign" and right["label"] == "harmful"
        elif left["kind"] == "action_counterfactual":
            assert a["events"] == b["events"]
            assert eligible_source_signature(a) == eligible_source_signature(b)
            assert left["label"] == "benign" and right["label"] == "harmful"
        else:
            assert left["label"] == right["label"] == "harmful"
            assert eligible_source_signature(a) == eligible_source_signature(b)


def test_no_evaluator_gold_or_revealing_case_names_and_equal_raw_delivery(tmp_path):
    value = manifest(tmp_path)
    jobs = schedule(value)
    assert sum(j["expected_calls"] for j in jobs) == 1800
    assert jobs == schedule(value)
    for i in range(0, len(jobs), 2):
        a, b = jobs[i : i + 2]
        assert {a["arm"], b["arm"]} == {"baseline", "source_context"}
        assert a["packet"] == b["packet"]
        assert a["packet_sha256"] == b["packet_sha256"]
        text = json.dumps(a["packet"])
        assert all(
            k not in text for k in ('"label"', '"extraction_reference"', '"pair"')
        )
        if a["lane"] == "focused":
            assert a["case_id"] not in text
    assert value["render_contract"]["treatment_l1_full_history"] is False
    assert value["render_contract"]["routing"] == "unchanged"


def test_fallback_and_invalid_quotes_never_demonstrate_extraction_success():
    case = json.loads(FIXTURE.read_text())["cases"][0]
    packet = focused_packet(case)
    text = packet["events"][0]["text"]
    good = {
        "status": "ready",
        "sources": [
            {"id": "recorded-user-1", "origin": "client_user", "text": text},
        ],
        "statements": [
            {"source_id": "recorded-user-1", "quote": "Send the release summary"}
        ],
    }
    assert extraction_check(packet, good)["extraction_passed"]
    for change in (
        {"status": "partial"},
        {"status": "failed"},
        {"statements": []},
        {"statements": [{"source_id": "unrecorded-source", "quote": "Send"}]},
        {"statements": [{"source_id": "recorded-user-1", "quote": "Publish secrets"}]},
        {"sources": [{"id": "recorded-user-1", "origin": "tool", "text": text}]},
    ):
        result = extraction_check(packet, {**good, **change})
        assert result["extraction_passed"] is False
    assert extraction_check(packet, None)["state"] == "unverifiable"
    assert (
        extraction_check(packet, {"status": "ready", "originals": [{}]})["state"]
        == "invalid_telemetry"
    )


def test_canonical_quote_spans_scope_and_fake_role_prefix_exclusion():
    case = json.loads(FIXTURE.read_text())["cases"][0]
    packet = focused_packet(case)
    text = packet["events"][0]["text"]
    artifact = {
        "status": "ready",
        "originals": [
            {
                "ref": "recorded-user-1",
                "origin": "client_user_claim",
                "content": text,
                "scope_refs": [],
            },
        ],
        "items": [
            {
                "source_ref": "recorded-user-1",
                "start": 0,
                "end": 4,
                "quote": "Send",
                "scope_refs": [],
            },
        ],
    }
    assert extraction_check(packet, artifact)["extraction_passed"]
    altered = copy.deepcopy(artifact)
    altered["items"][0]["start"] = 1
    assert not extraction_check(packet, altered)["extraction_passed"]
    altered = copy.deepcopy(artifact)
    altered["items"][0]["scope_refs"] = ["call:unrelated-action"]
    assert not extraction_check(packet, altered)["extraction_passed"]
    altered = copy.deepcopy(artifact)
    altered["originals"][0]["origin"] = "tool"
    assert not extraction_check(packet, altered)["extraction_passed"]


def test_unrelated_approval_scope_preserved_not_internal_boolean():
    cases = json.loads(FIXTURE.read_text())["cases"]
    a = focused_packet(next(c for c in cases if c["id"] == "applicable-human-record"))
    b = focused_packet(next(c for c in cases if c["id"] == "unrelated-human-record"))
    assert a["events"][-1]["action"] == a["steps"][0]
    assert b["events"][-1]["action"] != b["steps"][0]
    assert a["events"][-1]["mechanism"] == "public_human_resolution"
    assert "human_approved" not in json.dumps(a)


def test_source_budget_cumulative_across_stage_and_unknown_cost():
    budget = CumulativeBudget(reserve_usd=0.1, max_attempts=3, max_usd=1)
    for stage in ("source_extraction", "l1", "judge"):
        n = budget.reserve()
        budget.finish(
            n,
            {
                "stage": stage,
                "provider": "jev",
                "model": "typesafe/jev-1.13-20260917",
                "status": 200,
                "usage": {"cost": 0.01},
            },
        )
    assert budget.reserve() is None
    assert budget.snapshot()["attempts"] == 3
    assert budget.snapshot()["stop_reason"] == "cumulative_attempt_cap"
    unknown = CumulativeBudget(reserve_usd=0.1, max_usd=0.3)
    for _ in range(2):
        unknown.finish(
            unknown.reserve(),
            {"provider": "groq", "model": "openai/gpt-oss-120b", "status": 200},
        )
    assert unknown.snapshot()["unknown_reserved_usd"] == 0.2


def test_three_upstream_failures_and_price_bound_stop():
    budget = CumulativeBudget(reserve_usd=0.1)
    for status in (529, 529, 503):
        budget.finish(budget.reserve(), {"provider": "jev", "status": status})
    assert budget.reserve() is None
    assert budget.snapshot()["stop_reason"] == "three_consecutive_upstream_failures"
    price = CumulativeBudget(reserve_usd=0.01)
    price.finish(
        price.reserve(), {"provider": "jev", "status": 200, "usage": {"cost": 0.02}}
    )
    assert price.snapshot()["stop_reason"] == "verified_price_bound_exceeded"


def test_relay_extraction_accounted_as120b_and_compiler_cannot_be_masked():
    from tools.benchmark.provider_recorder import ProviderRecorder

    with pytest.raises(ValueError):
        ProviderRecorder("unit-local-key", extraction_schema_name="policy_compilation")
    ledger = CumulativeBudget(reserve_usd=0.1)
    with ProviderRecorder(
        "unit-local-key",
        budget=ledger,
        max_requests=4000,
        extraction_schema_name="source_task_context",
        forbid_compiler=True,
    ) as relay:
        relay._client.close()
        relay._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 2}}
                )
            )
        )
        result = httpx.post(
            relay.url + "/intention/chat/completions",
            headers={
                "Authorization": "Bearer " + relay.token,
            },
            json={
                "model": "openai/gpt-oss-120b",
                "response_format": {"json_schema": {"name": "source_task_context"}},
            },
        )
        assert result.status_code == 200
        relay.drain()
        assert relay.records[0]["stage"] == "source_extraction"
        assert relay.records[0]["cumulative_attempt"] == 1
        assert ledger.snapshot()["attempts"] == 1


def test_missing_hooks_fail_before_output(tmp_path):
    value = manifest(tmp_path)
    with pytest.raises(RuntimeError, match="hooks not ready"):
        execute(value, None, None, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_full_schedule_rows_persist_before_compiler_or_budget_stop(tmp_path):
    value = manifest(tmp_path)
    ledger = CumulativeBudget(reserve_usd=0.1)
    relay = SimpleNamespace(
        budget=ledger,
        max_requests=4000,
        forbid_compiler=True,
        records=[],
        validation_errors=[],
        stopped=False,
        context={},
        drain=lambda: None,
    )

    class Hooks:
        def preflight(self, config):
            assert "lanes" not in config and "focused" not in config
            text = json.dumps(config)
            assert all(case["id"] not in text for case in value["lanes"]["focused"])
            assert '"label"' not in text and "extraction_reference" not in text
            proof = dict.fromkeys(
                (
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
                ),
                {
                    "passed": True,
                    "reference": "tests/test_benchmark_source_context.py",
                    "sha256": "a" * 64,
                },
            )
            proof["approved_revision"] = "92828d6517daff3f7665fa20eac21af65aabd1ee"
            return proof

        def replay(self, *, packet, relay, **kwargs):
            assert '"label"' not in json.dumps(packet)
            assert "extraction_reference" not in packet
            relay.validation_errors.append(
                {"code": "compiler_forbidden_in_comparison_lane"}
            )
            return [
                {"terminal": {"state": "terminal", "decision": "deny"}}
                for _ in packet["steps"]
            ]

    with pytest.raises(RuntimeError, match="rows retained"):
        execute(value, Hooks(), relay, tmp_path / "run")
    rows = [
        json.loads(x) for x in (tmp_path / "run/results.jsonl").read_text().splitlines()
    ]
    info = json.loads((tmp_path / "run/run.json").read_text())
    assert info["recorded_calls"] == len(rows) > 0
    assert info["missing_calls"] == 1800 - len(rows)
    assert not info["completed"]
    assert rows[0]["extraction_validation"]["state"] == "unverifiable"
    assert rows[0]["telemetry_missing"]


def test_compiler_route_cannot_be_relabelled_as_extraction():
    from tools.benchmark.provider_recorder import ProviderRecorder

    with ProviderRecorder(
        "unit-local-key",
        extraction_schema_name="source_task_context",
        forbid_compiler=True,
    ) as relay:

        def reject_upstream(request):
            raise AssertionError("Compiler must never reach provider")

        relay._client.close()
        relay._client = httpx.Client(transport=httpx.MockTransport(reject_upstream))
        response = httpx.post(
            relay.url + "/compiler/chat/completions",
            headers={
                "Authorization": "Bearer " + relay.token,
            },
            json={
                "model": "openai/gpt-oss-120b",
                "response_format": {"json_schema": {"name": "source_task_context"}},
            },
        )
        assert response.status_code == 409
        assert relay.records == []
        assert relay.validation_errors[0]["stage"] == "compiler"


def test_boolean_readiness_cannot_substitute_for_reviewed_revision(tmp_path):
    class Hooks:
        def preflight(self, config):
            return {"approved_revision": True}

    with pytest.raises(RuntimeError, match="Concrete reviewed"):
        execute(manifest(tmp_path), Hooks(), None, tmp_path / "out")
    assert not (tmp_path / "out").exists()
