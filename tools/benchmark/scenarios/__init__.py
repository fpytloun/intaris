"""Scenario registry and discovery.

Imports all scenario modules and provides lookup, filtering,
and listing functions for the benchmark runner.
"""

from __future__ import annotations

from tools.benchmark.models import Scenario, ScenarioSet
from tools.benchmark.scenarios import (
    adversarial,
    coding,
    cross_session,
    gold,
    hierarchical,
    infra,
    research,
    social,
    workplace,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# All individual scenarios (flat list).
ALL_SCENARIOS: list[Scenario] = [
    *coding.SCENARIOS,
    *social.SCENARIOS,
    *workplace.SCENARIOS,
    *infra.SCENARIOS,
    *research.SCENARIOS,
    *adversarial.SCENARIOS,
    *hierarchical.SCENARIOS,
    *gold.SCENARIOS,
]

# All scenario sets (cross-session patterns).
ALL_SCENARIO_SETS: list[ScenarioSet] = cross_session.SCENARIO_SETS

# Index by name for O(1) lookup.
_BY_NAME: dict[str, Scenario] = {s.name: s for s in ALL_SCENARIOS}
_SETS_BY_NAME: dict[str, ScenarioSet] = {ss.name: ss for ss in ALL_SCENARIO_SETS}

# Also index set member scenarios by name so get_scenario() works for them.
for _ss in ALL_SCENARIO_SETS:
    for _s in _ss.scenarios:
        _BY_NAME[_s.name] = _s


# ---------------------------------------------------------------------------
# Lookup functions
# ---------------------------------------------------------------------------


def get_scenario(name: str) -> Scenario:
    """Get a scenario by name. Raises KeyError if not found."""
    return _BY_NAME[name]


def get_scenario_set(name: str) -> ScenarioSet:
    """Get a scenario set by name. Raises KeyError if not found."""
    return _SETS_BY_NAME[name]


def get_all_scenarios() -> list[Scenario]:
    """Get all individual scenarios (not including cross-session set members)."""
    return list(ALL_SCENARIOS)


def get_all_scenario_sets() -> list[ScenarioSet]:
    """Get all scenario sets."""
    return list(ALL_SCENARIO_SETS)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_scenarios(
    *,
    category: str | None = None,
    name: str | None = None,
) -> tuple[list[Scenario], list[ScenarioSet]]:
    """Filter scenarios and sets by category or name.

    Returns ``(matching_scenarios, matching_sets)``.
    If *name* is provided, returns the exact match (scenario or set).
    If *category* is provided, returns all in that category.
    """
    if name is not None:
        # Exact match — check individual scenarios first, then sets.
        if name in _BY_NAME:
            return [_BY_NAME[name]], []
        if name in _SETS_BY_NAME:
            return [], [_SETS_BY_NAME[name]]
        return [], []

    if category is not None:
        scenarios = [s for s in ALL_SCENARIOS if s.category == category]
        sets = [ss for ss in ALL_SCENARIO_SETS if ss.category == category]
        return scenarios, sets

    # No filter — return everything.
    return list(ALL_SCENARIOS), list(ALL_SCENARIO_SETS)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def list_scenarios(*, category: str | None = None, verbose: bool = False) -> str:
    """Return a formatted string listing available scenarios.

    If *verbose*, include description, intention, intensity, and category.
    Otherwise just name and short description.
    """
    scenarios, sets = filter_scenarios(category=category)
    lines: list[str] = []

    if scenarios:
        lines.append("Scenarios:")
        for s in scenarios:
            if verbose:
                lines.append(f"  {s.name}")
                lines.append(f"    category:    {s.category}")
                lines.append(f"    description: {s.description}")
                lines.append(f"    intention:   {s.intention}")
                lines.append(f"    intensity:   {s.intensity}")
            else:
                lines.append(f"  {s.name:40s} {s.description}")

    if sets:
        if lines:
            lines.append("")
        lines.append("Scenario sets (cross-session):")
        for ss in sets:
            if verbose:
                lines.append(f"  {ss.name}")
                lines.append(f"    category:    {ss.category}")
                lines.append(f"    description: {ss.description}")
                lines.append(f"    sessions:    {len(ss.scenarios)}")
                for s in ss.scenarios:
                    lines.append(f"      - {s.name} (intensity={s.intensity})")
            else:
                lines.append(
                    f"  {ss.name:40s} {ss.description} ({len(ss.scenarios)} sessions)"
                )

    if not lines:
        return "No scenarios found."

    return "\n".join(lines)


def get_categories() -> list[str]:
    """Return sorted list of all unique categories."""
    cats: set[str] = set()
    for s in ALL_SCENARIOS:
        cats.add(s.category)
    for ss in ALL_SCENARIO_SETS:
        cats.add(ss.category)
    return sorted(cats)
