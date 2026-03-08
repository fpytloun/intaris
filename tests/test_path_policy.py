"""Tests for the approved path prefix cache and path policy flow.

Tests the evaluator's path approval learning mechanism:
- First out-of-project read → WRITE (LLM evaluates)
- After LLM approval → path prefix cached
- Subsequent reads under same prefix → READ (fast path)
- deny_paths always overrides cache
- Cache lifecycle (cleanup on session end, FIFO eviction)
- Prefix computation (sibling vs distant paths)
"""

from __future__ import annotations

import pytest

from intaris.evaluator import Evaluator, _compute_path_prefix


class TestComputePathPrefix:
    """Test the path prefix computation heuristic."""

    def test_sibling_project(self):
        """Sibling projects share a deep common ancestor → sibling prefix."""
        prefix = _compute_path_prefix(
            "/Users/foo/src/intaris/intaris/classifier.py",
            "/Users/foo/src/mnemory",
        )
        assert prefix == "/Users/foo/src/intaris"

    def test_sibling_project_deeper_file(self):
        """Deeper file in sibling project → same sibling prefix."""
        prefix = _compute_path_prefix(
            "/Users/foo/src/intaris/tests/test_classifier.py",
            "/Users/foo/src/mnemory",
        )
        assert prefix == "/Users/foo/src/intaris"

    def test_distant_path_narrow_prefix(self):
        """Distant path (shallow common ancestor) → exact parent directory."""
        prefix = _compute_path_prefix(
            "/var/log/app.log",
            "/Users/foo/src/mnemory",
        )
        assert prefix == "/var/log"

    def test_distant_path_etc(self):
        """Path in /etc → narrow prefix, not /etc."""
        prefix = _compute_path_prefix(
            "/etc/hosts",
            "/Users/foo/src/mnemory",
        )
        assert prefix == "/etc"

    def test_distant_path_deep(self):
        """Deep distant path → exact parent directory."""
        prefix = _compute_path_prefix(
            "/var/lib/docker/containers/abc/config.json",
            "/Users/foo/src/mnemory",
        )
        assert prefix == "/var/lib/docker/containers/abc"

    def test_same_parent_different_child(self):
        """Different child under same parent → sibling prefix."""
        prefix = _compute_path_prefix(
            "/Users/foo/src/k8s-manifests/deploy.yaml",
            "/Users/foo/src/mnemory",
        )
        assert prefix == "/Users/foo/src/k8s-manifests"

    def test_approving_var_log_does_not_approve_var_run(self):
        """Regression: /var/log/app.log prefix must NOT cover /var/run/."""
        prefix = _compute_path_prefix(
            "/var/log/app.log",
            "/Users/foo/src/mnemory",
        )
        # Prefix should be /var/log, not /var/
        assert not "/var/run/secret".startswith(prefix + "/")
        assert not "/var/run/secret".startswith(prefix)


class TestApprovedPathsCache:
    """Test the evaluator's approved path prefix cache."""

    @pytest.fixture()
    def evaluator(self):
        """Create a minimal Evaluator for cache testing.

        Uses mock objects for dependencies that aren't needed for
        cache operations.
        """

        class MockLLM:
            pass

        class MockSessionStore:
            pass

        class MockAuditStore:
            pass

        return Evaluator(
            llm=MockLLM(),  # type: ignore[arg-type]
            session_store=MockSessionStore(),  # type: ignore[arg-type]
            audit_store=MockAuditStore(),  # type: ignore[arg-type]
        )

    def test_empty_cache_returns_false(self, evaluator: Evaluator):
        """No approved paths → check returns False."""
        assert not evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/var/log/app.log"],
            "/home/user/project",
        )

    def test_approve_and_check(self, evaluator: Evaluator):
        """After approving a prefix, paths under it are approved."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        assert evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/var/log/app.log"],
            "/home/user/project",
        )

    def test_different_prefix_not_approved(self, evaluator: Evaluator):
        """Approving /var/log does not approve /var/run."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        assert not evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/var/run/secret"],
            "/home/user/project",
        )

    def test_in_project_paths_always_approved(self, evaluator: Evaluator):
        """Paths within working_directory are always approved."""
        assert evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/home/user/project/src/main.py"],
            "/home/user/project",
        )

    def test_mixed_paths_all_must_pass(self, evaluator: Evaluator):
        """All paths must be approved for the check to pass."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        # One approved, one not
        assert not evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/var/log/app.log", "/etc/shadow"],
            "/home/user/project",
        )

    def test_clear_approved_paths(self, evaluator: Evaluator):
        """Clearing removes all approved paths for a session."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        evaluator.clear_approved_paths("user1", "session1")
        assert not evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/var/log/app.log"],
            "/home/user/project",
        )

    def test_session_isolation(self, evaluator: Evaluator):
        """Approved paths are session-scoped."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        # Different session should not have the approval
        assert not evaluator._check_approved_paths(
            "user1",
            "session2",
            ["/var/log/app.log"],
            "/home/user/project",
        )

    def test_user_isolation(self, evaluator: Evaluator):
        """Approved paths are user-scoped."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        assert not evaluator._check_approved_paths(
            "user2",
            "session1",
            ["/var/log/app.log"],
            "/home/user/project",
        )

    def test_fifo_eviction(self, evaluator: Evaluator):
        """Oldest prefixes are evicted when over the limit."""
        # Add more than the max
        for i in range(55):
            evaluator._approve_path_prefix("user1", "session1", f"/prefix/{i}")

        # The first few should have been evicted
        assert not evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/prefix/0/file.txt"],
            "/home/user/project",
        )
        # The latest should still be there
        assert evaluator._check_approved_paths(
            "user1",
            "session1",
            ["/prefix/54/file.txt"],
            "/home/user/project",
        )

    def test_duplicate_prefix_not_added(self, evaluator: Evaluator):
        """Adding the same prefix twice doesn't create duplicates."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        key = ("user1", "session1")
        assert len(evaluator._approved_paths[key]) == 1

    def test_sweep_removes_stale(self, evaluator: Evaluator):
        """Sweep removes entries for sessions not in active set."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        evaluator._approve_path_prefix("user1", "session2", "/tmp")
        removed = evaluator.sweep_approved_paths({("user1", "session1")})
        assert removed == 1
        assert ("user1", "session2") not in evaluator._approved_paths
        assert ("user1", "session1") in evaluator._approved_paths

    def test_sweep_empty_active_set(self, evaluator: Evaluator):
        """Sweep with empty active set removes everything."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        removed = evaluator.sweep_approved_paths(set())
        assert removed == 1
        assert len(evaluator._approved_paths) == 0
