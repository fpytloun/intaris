"""Argparse CLI for the Intaris benchmark system.

Subcommands:
  run       — Execute benchmark scenarios against a live Intaris instance
  evaluate  — Score a completed run with an evaluator LLM
  compare   — Diff two runs for regression detection
  list      — List available scenarios
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("benchmark")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _handle_run(args: argparse.Namespace) -> None:
    """Execute benchmark scenarios."""
    from tools.benchmark.models import RunConfig

    config = RunConfig(
        intaris_url=args.url,
        intaris_api_key=args.api_key,
        user_id=args.user,
        agent_id=args.agent,
        llm_api_key=args.llm_api_key,
        llm_base_url=args.llm_base_url or None,
        llm_model=args.llm_model,
        intensity=args.intensity,
        auto_resolve=args.auto_resolve,
        delay_range=tuple(args.delay),
        max_turns=args.max_turns,
        max_calls=args.max_calls,
        skip_analysis=args.skip_analysis,
        dry_run=args.dry_run,
        verbose=args.verbose,
        output_dir=args.output,
    )

    if not config.intaris_api_key:
        logger.error("--api-key or $INTARIS_API_KEY is required")
        sys.exit(1)

    if not config.llm_api_key:
        logger.error("--llm-api-key or $LLM_API_KEY is required")
        sys.exit(1)

    mode: str | None = args.mode
    scenario: str | None = args.scenario
    category: str | None = args.category

    if mode == "showcase":
        from tools.benchmark.runner import run_showcase

        logger.info("Starting showcase run against %s", config.intaris_url)
        report = run_showcase(config)
    else:
        from tools.benchmark.runner import run_filtered

        logger.info(
            "Starting benchmark run against %s (scenario=%s, category=%s)",
            config.intaris_url,
            scenario or "*",
            category or "*",
        )
        report = run_filtered(config, scenario=scenario, category=category)

    # Auto-evaluate after run completes
    if args.evaluate and not config.dry_run and report.run_id:
        from pathlib import Path

        from tools.benchmark.evaluator import evaluate_run
        from tools.benchmark.report import (
            format_benchmark_report,
            generate_markdown_report,
        )
        from tools.benchmark.store import RunStore

        run_dir = str(Path(config.output_dir) / report.run_id)
        logger.info("Auto-evaluating run %s ...", report.run_id)
        try:
            benchmark = evaluate_run(
                run_dir=run_dir,
                llm_api_key=config.llm_api_key,
                llm_base_url=config.llm_base_url,
                llm_model=args.eval_model,
                intaris_url=config.intaris_url,
                intaris_api_key=config.intaris_api_key,
            )
            print(format_benchmark_report(benchmark))  # noqa: T201
            store = RunStore(config.output_dir, report.run_id)
            store.save_report(generate_markdown_report(benchmark))
        except Exception:
            logger.exception(
                "Auto-evaluation failed (run data is saved, evaluate manually)"
            )


def _handle_evaluate(args: argparse.Namespace) -> None:
    """Score a completed run."""
    run_dir: str = args.run

    if not os.path.isdir(run_dir):
        logger.error("Run directory does not exist: %s", run_dir)
        sys.exit(1)

    llm_api_key: str = args.llm_api_key
    if not llm_api_key:
        logger.error("--llm-api-key or $LLM_API_KEY is required")
        sys.exit(1)

    from pathlib import Path

    from tools.benchmark.evaluator import evaluate_run
    from tools.benchmark.report import format_benchmark_report, generate_markdown_report
    from tools.benchmark.store import RunStore

    logger.info("Evaluating run: %s", run_dir)
    benchmark = evaluate_run(
        run_dir=run_dir,
        llm_api_key=llm_api_key,
        llm_base_url=args.llm_base_url or None,
        llm_model=args.llm_model,
        intaris_url=args.url,
        intaris_api_key=args.api_key,
        offline=args.offline,
    )

    # Generate and display report
    print(format_benchmark_report(benchmark))  # noqa: T201

    # Save markdown report
    run_path = Path(run_dir)
    store = RunStore(str(run_path.parent), run_path.name)
    store.save_report(generate_markdown_report(benchmark))


def _handle_compare(args: argparse.Namespace) -> None:
    """Diff two runs for regression detection."""
    baseline: str = args.baseline
    new: str = args.new

    for label, path in [("Baseline", baseline), ("New", new)]:
        if not os.path.isdir(path):
            logger.error("%s directory does not exist: %s", label, path)
            sys.exit(1)

    from tools.benchmark.report import format_comparison_report
    from tools.benchmark.scorer import compare_runs

    logger.info("Comparing: %s (baseline) vs %s (new)", baseline, new)
    result = compare_runs(baseline, new)
    print(format_comparison_report(result))  # noqa: T201


def _handle_list(args: argparse.Namespace) -> None:
    """List available scenarios."""
    from tools.benchmark.scenarios import list_scenarios

    output = list_scenarios(category=args.category, verbose=args.verbose)
    print(output)  # noqa: T201


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="benchmark",
        description="Intaris Benchmark — guardrails evaluation system",
    )

    # -- Global flags -------------------------------------------------------
    parser.add_argument(
        "--url",
        default=os.environ.get("INTARIS_URL", "http://localhost:8060"),
        help="Intaris server URL (default: $INTARIS_URL or http://localhost:8060)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("INTARIS_API_KEY", ""),
        help="Intaris API key (default: $INTARIS_API_KEY)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- run ----------------------------------------------------------------
    run_parser = subparsers.add_parser(
        "run",
        help="Execute benchmark scenarios against a live Intaris instance",
    )
    run_parser.add_argument(
        "mode",
        nargs="?",
        default=None,
        choices=["showcase"],
        help="Run mode: 'showcase' for full demo, omit for filtered run",
    )
    run_parser.add_argument(
        "--scenario",
        default=None,
        help="Run a specific scenario by name",
    )
    run_parser.add_argument(
        "--category",
        default=None,
        help="Filter scenarios by category",
    )
    run_parser.add_argument(
        "--user",
        default="",
        help="User ID (default: auto-generate per run)",
    )
    run_parser.add_argument(
        "--agent",
        default="opencode",
        help="Agent ID (default: opencode)",
    )
    run_parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("LLM_API_KEY", ""),
        help="LLM API key for the agent (default: $LLM_API_KEY)",
    )
    run_parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("LLM_BASE_URL", ""),
        help="LLM base URL for the agent (default: $LLM_BASE_URL)",
    )
    run_parser.add_argument(
        "--llm-model",
        default="gpt-5.4-nano",
        help="LLM model for the agent (default: gpt-5.4-nano)",
    )
    run_parser.add_argument(
        "--intensity",
        type=float,
        default=None,
        help="Override intensity 0.0-1.0 (default: use scenario default)",
    )
    run_parser.add_argument(
        "--auto-resolve",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-resolve escalations (default: --auto-resolve)",
    )
    run_parser.add_argument(
        "--delay",
        type=float,
        nargs=2,
        default=[1.0, 5.0],
        metavar=("MIN", "MAX"),
        help="Delay range in seconds between calls (default: 1.0 5.0)",
    )
    run_parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Override max turns per scenario",
    )
    run_parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="Override max total calls per scenario",
    )
    run_parser.add_argument(
        "--skip-analysis",
        action="store_true",
        default=False,
        help="Skip waiting for L2/L3 analysis after run",
    )
    run_parser.add_argument(
        "--output",
        default="./runs",
        help="Output directory for run data (default: ./runs)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log planned calls without executing",
    )
    run_parser.add_argument(
        "--evaluate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-evaluate after run completes (default: --evaluate)",
    )
    run_parser.add_argument(
        "--eval-model",
        default="gpt-5.4",
        help="LLM model for the evaluator (default: gpt-5.4)",
    )

    # -- evaluate -----------------------------------------------------------
    eval_parser = subparsers.add_parser(
        "evaluate",
        help="Score a completed run with an evaluator LLM",
    )
    eval_parser.add_argument(
        "--run",
        required=True,
        help="Path to run directory",
    )
    eval_parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("LLM_API_KEY", ""),
        help="LLM API key for the evaluator (default: $LLM_API_KEY)",
    )
    eval_parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("LLM_BASE_URL", ""),
        help="LLM base URL for the evaluator (default: $LLM_BASE_URL)",
    )
    eval_parser.add_argument(
        "--llm-model",
        default="gpt-5.4",
        help="LLM model for the evaluator (default: gpt-5.4)",
    )
    eval_parser.add_argument(
        "--url",
        default=None,
        help="Intaris URL for fetching L2/L3 data (optional)",
    )
    eval_parser.add_argument(
        "--api-key",
        default=None,
        help="Intaris API key for fetching L2/L3 data (optional)",
    )
    eval_parser.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Score from local data only (no Intaris API calls)",
    )

    # -- compare ------------------------------------------------------------
    cmp_parser = subparsers.add_parser(
        "compare",
        help="Diff two runs for regression detection",
    )
    cmp_parser.add_argument(
        "baseline",
        help="Path to baseline run directory",
    )
    cmp_parser.add_argument(
        "new",
        help="Path to new run directory",
    )

    # -- list ---------------------------------------------------------------
    list_parser = subparsers.add_parser(
        "list",
        help="List available scenarios",
    )
    list_parser.add_argument(
        "--category",
        default=None,
        help="Filter by category",
    )
    list_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Show full scenario details",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "run": _handle_run,
    "evaluate": _handle_evaluate,
    "compare": _handle_compare,
    "list": _handle_list,
}


def main() -> None:
    """Parse arguments, configure logging, and dispatch to subcommand."""
    parser = _build_parser()
    args = parser.parse_args()

    # -- Logging setup ------------------------------------------------------
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    handler = _HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        handler(args)
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting")
        sys.exit(130)
