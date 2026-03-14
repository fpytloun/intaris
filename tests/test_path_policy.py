"""Tests for the approved path prefix cache and path policy flow.

Tests the evaluator's path approval learning mechanism:
- First out-of-project read → WRITE (LLM evaluates)
- After LLM/user approval → path prefix cached
- Subsequent reads under same prefix → READ (fast path)
- deny_paths always overrides cache
- Cache lifecycle (cleanup on session end, FIFO eviction)
- Prefix computation (sibling vs distant paths)
- Prefix merging (sibling directories → common ancestor)
- Learning from user-approved escalations
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intaris.evaluator import Evaluator, _compute_path_prefix, _try_merge_prefix


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

    def test_approve_merges_sibling_prefixes(self, evaluator: Evaluator):
        """Adding sibling prefixes with deep common ancestor merges them."""
        evaluator._approve_path_prefix(
            "user1",
            "session1",
            "/Users/foo/.cache/opencode/node_modules/@opencode-ai/sdk/dist",
        )
        evaluator._approve_path_prefix(
            "user1",
            "session1",
            "/Users/foo/.cache/opencode/node_modules/@opencode-ai/plugin/dist",
        )
        key = ("user1", "session1")
        # Should have merged into a single prefix
        assert len(evaluator._approved_paths[key]) == 1
        merged = evaluator._approved_paths[key][0]
        # The common ancestor should cover both
        assert "/Users/foo/.cache/opencode/node_modules/@opencode-ai" in merged

    def test_approve_does_not_merge_shallow_prefixes(self, evaluator: Evaluator):
        """Prefixes with shallow common ancestor are NOT merged."""
        evaluator._approve_path_prefix("user1", "session1", "/var/log")
        evaluator._approve_path_prefix("user1", "session1", "/tmp/data")
        key = ("user1", "session1")
        # Should remain separate (common ancestor is / which is too shallow)
        assert len(evaluator._approved_paths[key]) == 2

    def test_approve_skips_already_covered(self, evaluator: Evaluator):
        """Adding a prefix already covered by a broader one is a no-op."""
        evaluator._approve_path_prefix(
            "user1",
            "session1",
            "/Users/foo/.cache/opencode/node_modules/@opencode-ai",
        )
        evaluator._approve_path_prefix(
            "user1",
            "session1",
            "/Users/foo/.cache/opencode/node_modules/@opencode-ai/sdk/dist",
        )
        key = ("user1", "session1")
        # The narrower prefix should not be added
        assert len(evaluator._approved_paths[key]) == 1


class TestTryMergePrefix:
    """Test the prefix merging heuristic."""

    def test_merge_sibling_npm_packages(self):
        """Sibling npm packages under same scope merge to scope dir."""
        prefixes = ["/Users/foo/.cache/opencode/node_modules/@opencode-ai/sdk/dist"]
        result = _try_merge_prefix(
            "/Users/foo/.cache/opencode/node_modules/@opencode-ai/plugin/dist",
            prefixes,
        )
        assert result is True
        assert len(prefixes) == 1
        assert prefixes[0] == "/Users/foo/.cache/opencode/node_modules/@opencode-ai"

    def test_no_merge_shallow_common_ancestor(self):
        """Prefixes with shallow common ancestor are not merged."""
        prefixes = ["/var/log"]
        result = _try_merge_prefix("/tmp/data", prefixes)
        assert result is False
        assert prefixes == ["/var/log"]

    def test_no_merge_empty_list(self):
        """Empty prefix list → no merge possible."""
        prefixes: list[str] = []
        result = _try_merge_prefix("/var/log", prefixes)
        assert result is False

    def test_merge_replaces_existing(self):
        """Merged prefix replaces the existing one in-place."""
        prefixes = [
            "/Users/foo/.cache/pkg/a/lib",
            "/other/prefix",
        ]
        result = _try_merge_prefix(
            "/Users/foo/.cache/pkg/b/lib",
            prefixes,
        )
        assert result is True
        assert prefixes[0] == "/Users/foo/.cache/pkg"
        assert prefixes[1] == "/other/prefix"

    def test_merge_depth_boundary(self):
        """Exactly at MIN_MERGE_DEPTH (4) should merge."""
        # Common: /a/b/c/d → 4 components (including root '')
        prefixes = ["/a/b/c/d/e"]
        result = _try_merge_prefix("/a/b/c/d/f", prefixes)
        assert result is True
        assert prefixes[0] == "/a/b/c/d"

    def test_no_merge_below_depth_boundary(self):
        """Below MIN_MERGE_DEPTH should not merge."""
        # Common: /a/b/c → 3 components (including root '')
        prefixes = ["/a/b/c/x"]
        result = _try_merge_prefix("/a/b/d/y", prefixes)
        # Common is ['', 'a', 'b'] = 3 components, below threshold of 4
        assert result is False


class TestLearnFromApprovedEscalation:
    """Test learning path prefixes from user-approved escalations."""

    @pytest.fixture()
    def evaluator(self):
        """Create an Evaluator with mock dependencies for testing."""

        class MockLLM:
            pass

        mock_sessions = MagicMock()
        mock_sessions.get.return_value = {
            "session_id": "session1",
            "user_id": "user1",
            "details": {"working_directory": "/Users/foo/src/intaris"},
        }

        class MockAuditStore:
            pass

        return Evaluator(
            llm=MockLLM(),  # type: ignore[arg-type]
            session_store=mock_sessions,  # type: ignore[arg-type]
            audit_store=MockAuditStore(),  # type: ignore[arg-type]
        )

    def test_learns_prefix_from_read_approval(self, evaluator: Evaluator):
        """Approving a read escalation caches the path prefix."""
        record = {
            "tool": "read",
            "args_redacted": {
                "filePath": "/Users/foo/.cache/opencode/node_modules/pkg/index.js"
            },
            "user_id": "user1",
            "session_id": "session1",
        }
        evaluator.learn_from_approved_escalation(record)

        # Should have cached a prefix
        key = ("user1", "session1")
        assert key in evaluator._approved_paths
        assert len(evaluator._approved_paths[key]) == 1

    def test_ignores_write_tools(self, evaluator: Evaluator):
        """Write tools are not cached even if approved."""
        record = {
            "tool": "write",
            "args_redacted": {"filePath": "/tmp/output.txt", "content": "data"},
            "user_id": "user1",
            "session_id": "session1",
        }
        evaluator.learn_from_approved_escalation(record)

        key = ("user1", "session1")
        assert key not in evaluator._approved_paths

    def test_ignores_in_project_paths(self, evaluator: Evaluator):
        """Paths within working_directory are not cached (already fast-pathed)."""
        record = {
            "tool": "read",
            "args_redacted": {"filePath": "/Users/foo/src/intaris/intaris/server.py"},
            "user_id": "user1",
            "session_id": "session1",
        }
        evaluator.learn_from_approved_escalation(record)

        key = ("user1", "session1")
        assert key not in evaluator._approved_paths

    def test_ignores_missing_args(self, evaluator: Evaluator):
        """Records without args_redacted are ignored."""
        record = {
            "tool": "read",
            "args_redacted": None,
            "user_id": "user1",
            "session_id": "session1",
        }
        evaluator.learn_from_approved_escalation(record)

        key = ("user1", "session1")
        assert key not in evaluator._approved_paths

    def test_ignores_no_working_directory(self, evaluator: Evaluator):
        """Sessions without working_directory are ignored."""
        evaluator._sessions.get.return_value = {  # type: ignore[attr-defined]
            "session_id": "session1",
            "user_id": "user1",
            "details": {},
        }
        record = {
            "tool": "read",
            "args_redacted": {"filePath": "/tmp/file.txt"},
            "user_id": "user1",
            "session_id": "session1",
        }
        evaluator.learn_from_approved_escalation(record)

        key = ("user1", "session1")
        assert key not in evaluator._approved_paths


class TestHistoryUserDecisions:
    """Test that user decisions on escalations are rendered in LLM history."""

    def test_escalation_with_user_approve(self):
        """Resolved escalation shows [escalate→user:approve]."""
        from intaris.prompts import build_evaluation_user_prompt

        history = [
            {
                "decision": "escalate",
                "user_decision": "approve",
                "tool": "read",
                "args_redacted": {"filePath": "/tmp/file.txt"},
                "reasoning": "Out of project scope",
            }
        ]
        prompt = build_evaluation_user_prompt(
            intention="Test intention",
            policy=None,
            recent_history=history,
            session_stats={"total_calls": 1},
            tool="read",
            args={"filePath": "/tmp/other.txt"},
            agent_id=None,
        )
        assert "[escalate\u2192user:approve]" in prompt

    def test_escalation_with_user_deny(self):
        """Resolved escalation shows [escalate→user:deny]."""
        from intaris.prompts import build_evaluation_user_prompt

        history = [
            {
                "decision": "escalate",
                "user_decision": "deny",
                "tool": "bash",
                "args_redacted": {"command": "rm -rf /tmp/data"},
                "reasoning": "Dangerous operation",
            }
        ]
        prompt = build_evaluation_user_prompt(
            intention="Test intention",
            policy=None,
            recent_history=history,
            session_stats={"total_calls": 1},
            tool="bash",
            args={"command": "rm -rf /tmp/other"},
            agent_id=None,
        )
        assert "[escalate\u2192user:deny]" in prompt

    def test_unresolved_escalation_shows_plain(self):
        """Unresolved escalation shows plain [escalate]."""
        from intaris.prompts import build_evaluation_user_prompt

        history = [
            {
                "decision": "escalate",
                "user_decision": None,
                "tool": "read",
                "args_redacted": {"filePath": "/tmp/file.txt"},
                "reasoning": "Pending review",
            }
        ]
        prompt = build_evaluation_user_prompt(
            intention="Test intention",
            policy=None,
            recent_history=history,
            session_stats={"total_calls": 1},
            tool="read",
            args={"filePath": "/tmp/other.txt"},
            agent_id=None,
        )
        assert "[escalate]" in prompt
        assert "\u2192user:" not in prompt

    def test_non_escalation_decisions_unchanged(self):
        """Approve and deny decisions are not modified."""
        from intaris.prompts import build_evaluation_user_prompt

        history = [
            {
                "decision": "approve",
                "user_decision": None,
                "tool": "read",
                "args_redacted": {"filePath": "src/main.py"},
                "reasoning": "Read-only",
            },
            {
                "decision": "deny",
                "user_decision": None,
                "tool": "bash",
                "args_redacted": {"command": "rm -rf /"},
                "reasoning": "Critical",
            },
        ]
        prompt = build_evaluation_user_prompt(
            intention="Test intention",
            policy=None,
            recent_history=history,
            session_stats={"total_calls": 2},
            tool="read",
            args={"filePath": "src/other.py"},
            agent_id=None,
        )
        assert "[approve]" in prompt
        assert "[deny]" in prompt
        assert "\u2192user:" not in prompt
