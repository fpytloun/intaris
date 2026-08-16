"""Regression tests for the OpenCode integration source."""

from __future__ import annotations

from pathlib import Path


def test_opencode_policy_includes_temp_allow_paths() -> None:
    """OpenCode should match other integrations by allowing OS temp dirs."""
    source = Path("integrations/opencode/intaris.ts").read_text()

    assert 'globPatternsFor("/tmp")' in source
    assert 'globPatternsFor("/var/tmp")' in source
    assert "process.env.TMPDIR" in source
    assert "transient scratch" in source


def test_opencode_policy_resolves_macos_private_symlinks() -> None:
    """/tmp, /var/tmp and $TMPDIR are symlinks into /private/... on macOS —
    a literal "/tmp/*" pattern never matches an already-resolved real path.
    globPatternsFor must resolve each built-in temp dir with realpathSync
    and include the result too, or reads/writes under the resolved path
    (e.g. Claude Code's own tool scratchpad) get flagged as out-of-policy.
    """
    source = Path("integrations/opencode/intaris.ts").read_text()

    assert "realpathSync" in source
    assert "globPatternsFor(tmpDir)" in source
