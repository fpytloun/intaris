"""Local data store for benchmark run artifacts.

Handles reading and writing all benchmark data to the local filesystem.
Storage layout::

    runs/
      {run_id}/
        run.json
        scenarios/
          {scenario_name}/
            scenario.json
            conversation.jsonl
            calls.jsonl
            reasoning.jsonl
            session.json
        intaris/
          audit/{session_id}.json
          summaries/{session_id}.json
          analysis.json
          profile.json
        evaluation.json
        report.md
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict | list) -> None:
    """Write a JSON file with pretty-printing. Creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def _read_json(path: Path) -> dict:
    """Read a JSON file. Returns empty dict if missing."""
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a single JSON line to a .jsonl file. Creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    """Read all lines from a .jsonl file. Returns empty list if missing."""
    if not path.exists():
        return []
    records: list[dict] = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line %d in %s", i, path)
    return records


# ---------------------------------------------------------------------------
# RunStore
# ---------------------------------------------------------------------------


class RunStore:
    """Filesystem-backed store for a single benchmark run.

    All paths are derived from ``base_dir / run_id``.  Every write creates
    parent directories as needed so callers never need to pre-create the
    tree.
    """

    def __init__(self, base_dir: str | Path, run_id: str) -> None:
        self.run_dir = Path(base_dir) / run_id

    # -- path helpers ------------------------------------------------------

    def _scenario_dir(self, scenario_name: str) -> Path:
        return self.run_dir / "scenarios" / scenario_name

    def _intaris_dir(self) -> Path:
        return self.run_dir / "intaris"

    # -- run metadata ------------------------------------------------------

    def save_run_meta(self, meta: dict) -> None:
        """Save run metadata to ``run.json``."""
        _write_json(self.run_dir / "run.json", meta)

    def load_run_meta(self) -> dict:
        """Load run metadata from ``run.json``."""
        return _read_json(self.run_dir / "run.json")

    # -- scenario data -----------------------------------------------------

    def save_scenario(self, scenario_name: str, scenario: dict) -> None:
        """Save the full scenario definition (ground truth)."""
        _write_json(self._scenario_dir(scenario_name) / "scenario.json", scenario)

    def load_scenario(self, scenario_name: str) -> dict:
        """Load a scenario definition."""
        return _read_json(self._scenario_dir(scenario_name) / "scenario.json")

    # -- JSONL append operations (streaming writes during run) -------------

    def append_call(self, scenario_name: str, record: dict) -> None:
        """Append a call record to ``calls.jsonl``."""
        _append_jsonl(self._scenario_dir(scenario_name) / "calls.jsonl", record)

    def append_conversation(self, scenario_name: str, message: dict) -> None:
        """Append a conversation message to ``conversation.jsonl``."""
        _append_jsonl(self._scenario_dir(scenario_name) / "conversation.jsonl", message)

    def append_reasoning(self, scenario_name: str, record: dict) -> None:
        """Append a reasoning record to ``reasoning.jsonl``."""
        _append_jsonl(self._scenario_dir(scenario_name) / "reasoning.jsonl", record)

    # -- JSONL read operations ---------------------------------------------

    def load_calls(self, scenario_name: str) -> list[dict]:
        """Load all call records for a scenario."""
        return _read_jsonl(self._scenario_dir(scenario_name) / "calls.jsonl")

    def load_conversation(self, scenario_name: str) -> list[dict]:
        """Load all conversation messages for a scenario."""
        return _read_jsonl(self._scenario_dir(scenario_name) / "conversation.jsonl")

    def load_reasoning(self, scenario_name: str) -> list[dict]:
        """Load all reasoning records for a scenario."""
        return _read_jsonl(self._scenario_dir(scenario_name) / "reasoning.jsonl")

    # -- session state -----------------------------------------------------

    def save_session(self, scenario_name: str, session: dict) -> None:
        """Save Intaris session state at scenario completion."""
        _write_json(self._scenario_dir(scenario_name) / "session.json", session)

    def load_session(self, scenario_name: str) -> dict:
        """Load Intaris session state for a scenario."""
        return _read_json(self._scenario_dir(scenario_name) / "session.json")

    # -- Intaris data (fetched during evaluate phase) ----------------------

    def save_audit(self, session_id: str, audit: list[dict]) -> None:
        """Save audit log fetched from Intaris."""
        _write_json(self._intaris_dir() / "audit" / f"{session_id}.json", audit)

    def load_audit(self, session_id: str) -> list[dict]:
        """Load audit log for a session."""
        path = self._intaris_dir() / "audit" / f"{session_id}.json"
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def save_summaries(self, session_id: str, summaries: dict) -> None:
        """Save session summaries fetched from Intaris."""
        _write_json(self._intaris_dir() / "summaries" / f"{session_id}.json", summaries)

    def load_summaries(self, session_id: str) -> dict:
        """Load session summaries for a session."""
        return _read_json(self._intaris_dir() / "summaries" / f"{session_id}.json")

    def save_analysis(self, analysis: dict) -> None:
        """Save cross-session analysis fetched from Intaris."""
        _write_json(self._intaris_dir() / "analysis.json", analysis)

    def load_analysis(self) -> dict:
        """Load cross-session analysis."""
        return _read_json(self._intaris_dir() / "analysis.json")

    def save_profile(self, profile: dict) -> None:
        """Save behavioral profile fetched from Intaris."""
        _write_json(self._intaris_dir() / "profile.json", profile)

    def load_profile(self) -> dict:
        """Load behavioral profile."""
        return _read_json(self._intaris_dir() / "profile.json")

    # -- evaluation results ------------------------------------------------

    def save_evaluation(self, evaluation: dict) -> None:
        """Save evaluator output."""
        _write_json(self.run_dir / "evaluation.json", evaluation)

    def load_evaluation(self) -> dict:
        """Load evaluator output."""
        return _read_json(self.run_dir / "evaluation.json")

    # -- report ------------------------------------------------------------

    def save_report(self, content: str) -> None:
        """Save the human-readable markdown report."""
        path = self.run_dir / "report.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    # -- discovery ---------------------------------------------------------

    def list_scenarios(self) -> list[str]:
        """List scenario names in this run (sorted alphabetically)."""
        scenarios_dir = self.run_dir / "scenarios"
        if not scenarios_dir.is_dir():
            return []
        return sorted(d.name for d in scenarios_dir.iterdir() if d.is_dir())

    def exists(self) -> bool:
        """Whether this run directory exists."""
        return self.run_dir.is_dir()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def list_runs(base_dir: str | Path) -> list[str]:
    """List all run IDs in the base directory, sorted by modification time (newest first)."""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    runs = [d for d in base.iterdir() if d.is_dir() and (d / "run.json").exists()]
    runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [d.name for d in runs]
