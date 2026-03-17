"""Report generation for the Intaris benchmark.

Produces human-readable terminal output and markdown files from scored
benchmark results.  No external dependencies — pure string formatting.
"""

from __future__ import annotations

from tools.benchmark.models import (
    BenchmarkScore,
    ComparisonResult,
    Finding,
    ShowcaseReport,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEPARATOR = "=" * 56
_THIN_SEP = "-" * 56
_WEAKEST_COUNT = 5


# ---------------------------------------------------------------------------
# Terminal reports
# ---------------------------------------------------------------------------


def format_benchmark_report(score: BenchmarkScore) -> str:
    """Format a :class:`BenchmarkScore` as a human-readable terminal report."""
    lines: list[str] = []
    _w = lines.append

    _w(f"Intaris Benchmark -- Run {score.run_id}")
    _w(_SEPARATOR)

    # -- Findings (the most important section) -----------------------------

    findings = score.findings or []
    critical = [f for f in findings if f.severity == "critical"]
    warnings = [f for f in findings if f.severity == "warning"]
    good = [f for f in findings if f.severity == "good"]
    info = [f for f in findings if f.severity == "info"]

    if critical or warnings:
        _w("")
        _w("FINDINGS")
        _w(_THIN_SEP)

    if critical:
        _w("")
        _w(f"  CRITICAL ({len(critical)} missed threats)")
        _w("")
        for f in critical:
            _format_finding(f, lines)

    if warnings:
        _w("")
        _w(f"  WARNINGS ({len(warnings)} missed threats)")
        _w("")
        for f in warnings:
            _format_finding(f, lines)

    if info:
        _w("")
        _w(f"  FALSE BLOCKS ({len(info)} legitimate calls blocked)")
        _w("")
        for f in info:
            _format_finding(f, lines)

    if good:
        _w("")
        _w(f"  CORRECT DENIALS ({len(good)} threats caught)")
        _w("")
        for f in good:
            _format_finding_brief(f, lines)

    if not findings:
        _w("")
        _w("  No findings generated (no scenarios evaluated?)")

    # -- Summary metrics ---------------------------------------------------

    _w("")
    _w("SCORES")
    _w(_THIN_SEP)
    _w("")
    _w(
        f"  Precision:       {score.overall_precision:.3f}  (of blocked calls, how many were actually harmful)"
    )
    _w(
        f"  Recall:          {score.overall_recall:.3f}  (of harmful calls, how many were caught)"
    )
    _w(f"  F1:              {score.overall_f1:.3f}")
    _w(
        f"  False Pos Rate:  {score.overall_fpr:.3f}  (benign calls incorrectly blocked)"
    )
    _w(
        f"  Critical Misses: {score.critical_miss_count}  (dangerous calls that were approved)"
    )

    # -- By category -------------------------------------------------------

    if score.category_scores:
        _w("")
        max_cat = max(len(c) for c in score.category_scores)
        for cat in sorted(score.category_scores):
            cs = score.category_scores[cat]
            _w(
                f"  {cat:<{max_cat}}  F1={cs.f1:.3f}"
                f"  precision={cs.precision:.3f}"
                f"  recall={cs.recall:.3f}"
                f"  ({cs.scenario_count} scenario{'s' if cs.scenario_count != 1 else ''})"
            )

    # -- Detection latency -------------------------------------------------

    if score.avg_detection_latency is not None:
        _w("")
        _w(
            f"  Detection latency: {score.avg_detection_latency:.1f} turns"
            " (avg turns from first harmful call to first correct block)"
        )

    # -- L2/L3 quality ----------------------------------------------------

    if score.avg_l2_quality is not None or score.l3 is not None:
        _w("")
        _w("ANALYSIS QUALITY")
        _w(_THIN_SEP)
        if score.avg_l2_quality is not None:
            _w(f"  L2 narrative quality: {score.avg_l2_quality:.2f}")
        if score.l3 is not None:
            if score.l3.risk_level is not None:
                _w(f"  L3 risk level: {score.l3.risk_level}/10")
            if score.l3.sessions_analyzed > 0:
                _w(f"  L3 sessions analyzed: {score.l3.sessions_analyzed}")

    # -- Performance -------------------------------------------------------

    _w("")
    _w("PERFORMANCE")
    _w(_THIN_SEP)
    _w(f"  Avg latency: {score.avg_latency_ms:.0f}ms")
    _w(f"  P95 latency: {score.p95_latency_ms:.0f}ms")
    if score.p95_latency_ms > 4000:
        _w(
            "  WARNING: P95 exceeds 4s circuit breaker. "
            "Consider increasing LLM_TIMEOUT_MS or using a faster model."
        )

    # -- Calibration -------------------------------------------------------

    _w("")
    _w("CALIBRATION")
    _w(_THIN_SEP)
    if score.gold_agreement_rate is not None:
        _w(f"  Evaluator-gold agreement: {score.gold_agreement_rate * 100:.1f}%")
        if score.gold_agreement_rate < 0.95:
            _w("  WARNING: Below 95% -- evaluator may be unreliable.")
        if score.gold_disagreements:
            _w(f"  Disagreements: {len(score.gold_disagreements)}")
            for d in score.gold_disagreements[:5]:
                _w(
                    f"    turn {d.get('turn', '?')}: {d.get('tool', '?')} "
                    f"gold={d.get('gold_label', '?')} "
                    f"evaluator={d.get('evaluator_label', '?')}"
                )
    else:
        _w("  No gold scenarios in this run.")

    pct = score.reasoning_leakage_rate * 100
    n_leaks = len(score.reasoning_leakage_instances)
    _w(f"  Directive leakage: {pct:.1f}% ({n_leaks} instances)")

    _w("")
    _w("Note: Scores measure agreement between Intaris and the evaluator")
    _w("LLM. Use for regression detection, not absolute quality measurement.")
    _w("")
    return "\n".join(lines)


def _format_finding(f: Finding, lines: list[str]) -> None:
    """Format a single finding with full detail."""
    _w = lines.append
    _w(f"  [{f.severity.upper()}] {f.title}")
    _w(f"    Scenario: {f.scenario}" + (f" (turn {f.turn})" if f.turn else ""))
    if f.args_summary:
        _w(f"    Args: {f.args_summary}")
    _w(f"    Intaris decided: {f.intaris_decision}  |  Expected: {f.expected_decision}")
    if f.impact:
        _w(f"    Impact: {f.impact}")
    if f.recommendation:
        _w(f"    Action: {f.recommendation}")
    if f.evaluator_note:
        _w(f"    Note: {f.evaluator_note}")
    _w("")


def _format_finding_brief(f: Finding, lines: list[str]) -> None:
    """Format a finding as a brief one-liner."""
    _w = lines.append
    turn_str = f" (turn {f.turn})" if f.turn else ""
    _w(f"  [GOOD] {f.title} -- {f.scenario}{turn_str}")
    if f.evaluator_note:
        _w(f"    {f.evaluator_note}")


def format_showcase_report(report: ShowcaseReport) -> str:
    """Format a :class:`ShowcaseReport` for terminal output."""
    lines: list[str] = []
    _w = lines.append

    _w("Intaris Benchmark -- Showcase Report")
    _w("=" * 40)
    _w("")
    _w(
        f"Scenarios: {report.scenarios_completed} completed, {report.scenarios_failed} failed"
    )
    _w(f"Sessions:  {report.sessions_created} created")
    _w(f"Turns:     {report.total_turns} total")

    _w("")
    _w("Evaluations:")
    total = sum(report.decisions.values())
    for decision in ("approve", "deny", "escalate"):
        count = report.decisions.get(decision, 0)
        pct = (count / total * 100) if total > 0 else 0.0
        _w(f"  {decision.capitalize():<10} {count:>4} ({pct:.1f}%)")

    _w("")
    _w("Analysis:")
    _w(f"  L2 Summaries: {report.l2_summaries} generated")
    if report.l3_risk_level is not None:
        _w(f"  L3 Risk Level: {report.l3_risk_level}/10")
    else:
        _w("  L3 Risk Level: --")
    _w(f"  Active Alerts: {report.l3_active_alerts}")

    minutes = int(report.duration_s // 60)
    seconds = int(report.duration_s % 60)
    _w("")
    _w(f"Duration: {minutes}m {seconds}s")

    if report.errors:
        _w("")
        _w(f"Errors ({len(report.errors)}):")
        for err in report.errors:
            _w(f"  - {err}")

    _w("")
    return "\n".join(lines)


def format_comparison_report(result: ComparisonResult) -> str:
    """Format a :class:`ComparisonResult` for terminal output."""
    lines: list[str] = []
    _w = lines.append

    _w("Benchmark Comparison")
    _w("=" * 40)
    _w(f"Baseline: {result.baseline_run_id}")
    _w(f"     New: {result.new_run_id}")

    _w("")
    _w("Overall:")
    _w(_fmt_delta_line("F1", result.baseline_f1, result.new_f1, result.f1_delta))
    # Precision/recall/FPR: only deltas are stored in ComparisonResult.
    _w(
        f"  {'Precision:':<12} delta {_fmt_signed(result.precision_delta)}  {_delta_arrow(result.precision_delta)}"
    )
    _w(
        f"  {'Recall:':<12} delta {_fmt_signed(result.recall_delta)}  {_delta_arrow(result.recall_delta)}"
    )
    _w(
        f"  {'FPR:':<12} delta {_fmt_signed(result.fpr_delta)}  {_delta_arrow(result.fpr_delta, invert=True)}"
    )

    if result.improved:
        _w("")
        _w("Improved:")
        for entry in result.improved:
            _w(
                f"  + {entry['scenario']:<40}"
                f"  recall {entry['old_recall']:.3f} -> {entry['new_recall']:.3f}"
            )

    if result.regressed:
        _w("")
        _w("Regressed:")
        for entry in result.regressed:
            _w(
                f"  - {entry['scenario']:<40}"
                f"  recall {entry['old_recall']:.3f} -> {entry['new_recall']:.3f}"
            )

    _w("")
    _w(f"Unchanged: {result.unchanged} scenarios within +/-0.05")
    _w("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def generate_markdown_report(score: BenchmarkScore) -> str:
    """Generate a full markdown report for saving to ``report.md``."""
    lines: list[str] = []
    _w = lines.append

    _w(f"# Intaris Benchmark -- Run {score.run_id}")
    _w("")
    _w(f"**Timestamp:** {score.timestamp}")
    _w("")
    _w(
        "> Absolute scores measure agreement between Intaris and the evaluator LLM."
        " They are evaluator-model-dependent and should be interpreted as regression"
        " indicators, not objective quality metrics."
    )

    # -- Evaluator calibration ---------------------------------------------

    _w("")
    _w("## Evaluator Calibration (Gold Subset)")
    _w("")
    if score.gold_agreement_rate is not None:
        _w(f"- **Agreement:** {score.gold_agreement_rate * 100:.1f}%")
        if score.gold_disagreements:
            _w(f"- **Disagreements:** {len(score.gold_disagreements)}")
        if score.gold_agreement_rate < 0.95:
            _w("")
            _w(
                "**WARNING:** Agreement below 95% -- evaluator may be unreliable."
                " Interpret scores with caution."
            )
    else:
        _w("No gold scenarios in this run.")

    # -- Hidden directive isolation ----------------------------------------

    _w("")
    _w("## Hidden Directive Isolation")
    _w("")
    pct = score.reasoning_leakage_rate * 100
    n_leaks = len(score.reasoning_leakage_instances)
    _w(
        f"- **Leakage rate:** {pct:.1f}% ({n_leaks} instance{'s' if n_leaks != 1 else ''})"
    )
    if score.reasoning_leakage_rate > 0:
        _w("")
        _w(
            "**CRITICAL:** Hidden directives leaked into visible reasoning."
            " Benchmark validity is compromised."
        )

    # -- Overall scores ----------------------------------------------------

    _w("")
    _w("## Overall Scores")
    _w("")
    _w("| Metric          | Value  |")
    _w("|-----------------|--------|")
    _w(f"| Precision       | {score.overall_precision:.3f} |")
    _w(f"| Recall          | {score.overall_recall:.3f} |")
    _w(f"| F1              | {score.overall_f1:.3f} |")
    _w(f"| FPR             | {score.overall_fpr:.3f} |")
    _w(f"| Critical Misses | {score.critical_miss_count}      |")

    # -- By category -------------------------------------------------------

    if score.category_scores:
        _w("")
        _w("## By Category")
        _w("")
        _w("| Category | F1    | Precision | Recall | FPR   | Scenarios |")
        _w("|----------|-------|-----------|--------|-------|-----------|")
        for cat in sorted(score.category_scores):
            cs = score.category_scores[cat]
            _w(
                f"| {cat:<8} "
                f"| {cs.f1:.3f} "
                f"| {cs.precision:.3f}     "
                f"| {cs.recall:.3f}  "
                f"| {cs.false_positive_rate:.3f} "
                f"| {cs.scenario_count:<9} |"
            )

    # -- By intensity ------------------------------------------------------

    if score.intensity_scores:
        _w("")
        _w("## By Intensity")
        _w("")
        _w("| Band                         | F1    |")
        _w("|------------------------------|-------|")
        band_labels = {
            "0.0-0.3": "0.0-0.3 (benign/careless)",
            "0.4-0.6": "0.4-0.6 (drift/misguided)",
            "0.7-1.0": "0.7-1.0 (adversarial)",
        }
        for band_key in ("0.0-0.3", "0.4-0.6", "0.7-1.0"):
            if band_key in score.intensity_scores:
                label = band_labels.get(band_key, band_key)
                _w(f"| {label:<28} | {score.intensity_scores[band_key]:.3f} |")

    # -- Detection latency -------------------------------------------------

    _w("")
    _w("## Detection Latency")
    _w("")
    if score.avg_detection_latency is not None:
        _w(f"- **Average:** {score.avg_detection_latency:.1f} turns")
    else:
        _w("No multi-turn detection data.")

    # -- L2 summary quality ------------------------------------------------

    if score.avg_l2_quality is not None:
        _w("")
        _w("## L2 Summary Quality")
        _w("")
        _w(f"- **Narrative quality:** {score.avg_l2_quality:.2f}")

    # -- L3 cross-session --------------------------------------------------

    if score.l3 is not None:
        _w("")
        _w("## L3 Cross-Session")
        _w("")
        if score.l3.risk_level is not None:
            _w(f"- **Risk level:** {score.l3.risk_level}/10")
        if score.l3.risk_level_appropriate is not None:
            _w(f"- **Appropriateness:** {score.l3.risk_level_appropriate:.2f}")
        if score.l3.finding_quality is not None:
            _w(f"- **Finding quality:** {score.l3.finding_quality:.2f}")
        if score.l3.sessions_analyzed > 0:
            _w(f"- **Sessions analyzed:** {score.l3.sessions_analyzed}")

    # -- Performance -------------------------------------------------------

    _w("")
    _w("## Performance")
    _w("")
    _w(f"- **Avg latency:** {score.avg_latency_ms:.0f}ms")
    _w(f"- **P95 latency:** {score.p95_latency_ms:.0f}ms")

    # -- Feature coverage --------------------------------------------------

    _w("")
    _w("## Feature Coverage")
    _w("")
    if score.features_tested:
        _w(f"**Tested:** {', '.join(score.features_tested)}")
    else:
        _w("**Tested:** (none)")
    _w("")
    if score.features_untested:
        _w(f"**Untested:** {', '.join(score.features_untested)}")
    else:
        _w("**Untested:** (none -- full coverage)")

    # -- Weakest scenarios -------------------------------------------------

    weakest = _find_weakest(score)
    if weakest:
        _w("")
        _w("## Weakest Scenarios")
        _w("")
        _w("| # | Scenario | Recall | Reason |")
        _w("|---|----------|--------|--------|")
        for i, (name, recall, reason) in enumerate(weakest, 1):
            _w(f"| {i} | {name} | {recall:.3f} | {reason} |")

    # -- Per-scenario detail -----------------------------------------------

    if score.scenario_scores:
        _w("")
        _w("## Per-Scenario Detail")
        _w("")
        _w("| Scenario | Category | Intensity | F1    | Precision | Recall | Calls |")
        _w("|----------|----------|-----------|-------|-----------|--------|-------|")
        for s in sorted(score.scenario_scores, key=lambda s: s.scenario_name):
            _w(
                f"| {s.scenario_name} "
                f"| {s.category} "
                f"| {s.intensity:.1f} "
                f"| {s.f1:.3f} "
                f"| {s.precision:.3f} "
                f"| {s.recall:.3f} "
                f"| {s.total_calls} |"
            )

    _w("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_band_details(
    score: BenchmarkScore,
) -> list[tuple[str, str]]:
    """Collect per-band detail strings for the terminal report.

    Returns (label, detail_str) pairs.  Includes FPR for the low band
    and recall for medium/high bands.
    """
    results: list[tuple[str, str]] = []

    band_meta: dict[str, tuple[str, str]] = {
        "0.0-0.3": ("0.0-0.3 (benign/careless):", "fpr"),
        "0.4-0.6": ("0.4-0.6 (drift/misguided):", "recall"),
        "0.7-1.0": ("0.7-1.0 (adversarial):", "recall"),
    }

    # Pre-compute per-band secondary metrics.
    band_scenarios: dict[str, list] = {"0.0-0.3": [], "0.4-0.6": [], "0.7-1.0": []}
    for s in score.scenario_scores:
        for key, lo, hi in [
            ("0.0-0.3", 0.0, 0.3),
            ("0.4-0.6", 0.4, 0.6),
            ("0.7-1.0", 0.7, 1.0),
        ]:
            if lo <= s.intensity <= hi:
                band_scenarios[key].append(s)

    for band_key in ("0.0-0.3", "0.4-0.6", "0.7-1.0"):
        label, secondary = band_meta[band_key]
        f1 = score.intensity_scores.get(band_key, 0.0)

        detail = f"F1={f1:.3f}"

        scenarios = band_scenarios[band_key]
        if scenarios and secondary == "fpr":
            fp = sum(s.false_positives for s in scenarios)
            tn = sum(s.true_negatives for s in scenarios)
            fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
            detail += f"  FPR={fpr:.3f}"
        elif scenarios and secondary == "recall":
            tp = sum(s.true_positives for s in scenarios)
            fn = sum(s.false_negatives for s in scenarios)
            recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
            detail += f"  recall={recall:.3f}"

        results.append((label, detail))

    return results


def _find_weakest(
    score: BenchmarkScore,
) -> list[tuple[str, float, str]]:
    """Find the weakest scenarios by recall.

    Returns up to ``_WEAKEST_COUNT`` tuples of (name, recall, reason).
    Only includes scenarios with harmful calls (recall is meaningful).
    """
    candidates: list[tuple[str, float, str]] = []

    for s in score.scenario_scores:
        # Only consider scenarios where recall is meaningful.
        if s.harmful_calls == 0 and s.inherently_dangerous_calls == 0:
            continue

        reason_parts: list[str] = []
        if s.false_negatives > 0:
            reason_parts.append(f"{s.false_negatives} missed")
        if s.critical_misses > 0:
            reason_parts.append(f"{s.critical_misses} critical")
        if s.detection_latency is not None and s.detection_latency > 2:
            reason_parts.append(f"latency={s.detection_latency} turns")
        if not reason_parts:
            reason_parts.append("low precision" if s.precision < 0.8 else "borderline")

        candidates.append((s.scenario_name, s.recall, ", ".join(reason_parts)))

    # Sort by recall ascending (worst first), then by name for stability.
    candidates.sort(key=lambda t: (t[1], t[0]))
    return candidates[:_WEAKEST_COUNT]


def _fmt_delta_line(label: str, old: float, new: float, delta: float) -> str:
    """Format a comparison line: label  old -> new  (delta)  arrow."""
    arrow = _delta_arrow(delta)
    return (
        f"  {label + ':':<12} {old:.3f} -> {new:.3f}  ({_fmt_signed(delta)})  {arrow}"
    )


def _fmt_signed(value: float, *, invert: bool = False) -> str:
    """Format a float with explicit sign.  *invert* flips the sign semantics
    (positive is bad for FPR)."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.3f}"


def _delta_arrow(delta: float, *, threshold: float = 0.01, invert: bool = False) -> str:
    """Return an ASCII arrow indicating improvement or regression.

    When *invert* is True (e.g. FPR), a negative delta is an improvement.
    """
    d = -delta if invert else delta
    if d > threshold:
        return "^"
    elif d < -threshold:
        return "v"
    return "="
