"""Explicit pre-inference scope expansion; original gold manifests stay intact."""

from tools.benchmark.matched import digest

PRIMARY = "b3712156ef25c08ddb5ee945c31d78fa9c91e601f96e37a68e3b0366b78a729d"
HISTORY = "6669348ef8799dfea731c6a8f4c52697a1ad3a65a81f9975ee1991370df2c886"


def expansion(history_sha256: str) -> dict:
    from tools.benchmark.source_history_manifest import manifest_sha256

    if history_sha256 != HISTORY or manifest_sha256() != HISTORY:
        raise ValueError("Expansion requires unchanged reviewed history bytes")
    value = {
        "version": 1,
        "primary_manifest_sha256": PRIMARY,
        "history_bytes_sha256": history_sha256,
        "original_attempt_cap": 4000,
        "authorized_attempt_cap": 6000,
        "known_price_usd": 10.0,
        "primary_evaluations": 1800,
        "history_checkpoints": 288,
        "no_retry_stage_envelope": 4746,
        "reason": "Owner authorized48 checkpoints x3 trials x2 arms before inference",
        "no_ledger_reset": True,
        "model_execution_authorized": False,
    }
    value["sha256"] = digest(value)
    return value


def attempt_cap(scope: dict | None, primary_sha256: str, history_sha256=None) -> int:
    if scope is None:
        return 4000
    if scope != expansion(scope.get("history_bytes_sha256", "")):
        raise ValueError("Unapproved expansion contents or digest")
    if primary_sha256 != PRIMARY:
        raise ValueError("Scope expansion belongs to frozen source-context matrix")
    if history_sha256 is not None and history_sha256 != scope["history_bytes_sha256"]:
        raise ValueError("History identity changed")
    return 6000
