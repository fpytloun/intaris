"""A git grep search pattern is text to find, not a secret being disclosed.

Regression for a read-only `git -C <repo> grep -E '...token...' origin/main --
<paths>` that was classified as secret disclosure (risk high) because the
pattern contained the word "token", and as not a static read because
`git grep` was not a recognised read and `-C <dir>` hid the subcommand.
"""

import pytest

from intaris.classifier import resolve_tool_paths, secret_argument_disclosure
from intaris.hybrid import Assessment
from intaris.hybrid_runtime import _observe_action
from tests.test_hybrid_runtime import stores  # noqa: F401

REPORTED = (
    "git -C ~/src/example/apps grep -n -i -E "
    "'crumb|csrf|api.?token|scc-api-token|seed-job|buildAuthorizationToken"
    "|tokenCredentialId' origin/main -- apps/jenkins/{base,prod-infra,dev-infra}"
)


def facts(command):
    return _observe_action("bash", {"command": command}, policy={})


def test_reported_git_grep_is_read_only_without_disclosure():
    assert secret_argument_disclosure(REPORTED) is False
    observed = facts(REPORTED)
    assert not observed.get("secret_argument")
    assert observed.get("risk") != "high"
    # Brace expansion still needs scope assessment; it is not a mutation.
    assert observed["mutation"] == "read_only"


@pytest.mark.parametrize(
    "command",
    [
        "git grep -n 'api_token' -- src",
        "git grep -e password -e token HEAD -- config",
        "git grep --regexp=secret_key",
        "git -C /repo --no-pager grep -i credential",
        "git grep -epassword",
    ],
)
def test_git_grep_patterns_are_not_disclosure(command):
    assert secret_argument_disclosure(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # Files whose contents are printed remain disclosure signals.
        "git show HEAD:config/token.pem",
        "git grep -n foo -- secrets/token.env",
        "git grep -f patterns/secret_tokens.txt -- src",
        "git grep -f patterns.txt secrets/token.env",
        "git grep -fpatterns.txt secrets/token.env",
        "cat ~/.config/api_token",
    ],
)
def test_secret_bearing_file_operands_still_disclose(command):
    assert secret_argument_disclosure(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git -C /repo grep -n needle",
        "git --no-pager blame -L 1,5 README.md",
        "git -C /repo --no-pager log --oneline -3",
        "git -C /repo branch --list",
    ],
)
def test_git_global_dir_and_pager_options_keep_read_subcommands(command):
    observed = facts(command)
    assert observed["static_read"] is True
    assert observed["mutation"] == "read_only"


@pytest.mark.parametrize(
    "command",
    [
        # `-c` can set settings that execute commands; never a static read.
        "git -c core.fsmonitor=/tmp/x status",
        # `grep -O` opens matches with an arbitrary pager command.
        "git grep -Ovim needle",
        "git grep --open-files-in-pager=vim needle",
        "git grep -nOvim needle",
        "git grep --open-files=vim needle",
        # A non-read subcommand after -C stays a write.
        "git -C /repo commit -m x",
    ],
)
def test_execution_capable_git_forms_are_not_static_reads(command):
    assert facts(command)["static_read"] is not True
    assert facts(command)["mutation"] != "read_only"


@pytest.mark.parametrize(
    "prefix",
    [
        "git -C /repo/apps",
        "git -C apps",
        "git -C /repo -C apps",
        "/usr/bin/git -C /repo/apps",
        "/bin/git -C apps",
        "/usr/local/bin/git -C /repo -C apps",
    ],
)
def test_git_directory_applies_to_path_denies(prefix):
    command = prefix + " grep . -- private/data.txt"
    path = "/repo/apps/private/data.txt"
    assert path in resolve_tool_paths("bash", {"command": command}, "/repo")
    observed = _observe_action(
        "bash",
        {"command": command},
        working_directory="/repo",
        policy={"deny_paths": [path]},
    )
    assert observed["configured_deny"] is True


def test_reported_search_approves_through_balanced_evaluator(stores):  # noqa: F811
    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="search",
        intention="Inspect Jenkins configuration in the apps repository read-only",
    )
    llm.generate.return_value = Assessment(
        relationship="supports_intent",
        risk="low",
        operation="read",
        authorization="explicit",
        authorization_source="session_intention",
        authorization_scope="within_scope",
        environment="development",
        effects=["read"],
    ).model_dump_json()
    result = evaluator.evaluate(
        user_id="owner",
        session_id="search",
        agent_id=None,
        tool="bash",
        args={"command": REPORTED},
    )
    assert result["decision"] == "approve"
    assert result["risk"] == "low"


@pytest.mark.parametrize(
    "git", ["git", "/usr/bin/git", "/bin/git", "/usr/local/bin/git"]
)
def test_git_directory_deny_wins_through_evaluator(stores, git):  # noqa: F811
    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="search",
        intention="Inspect repository files",
        policy={"deny_paths": ["/repo/apps/private/data.txt"]},
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id="search",
        agent_id=None,
        tool="bash",
        args={"command": f"{git} -C apps grep . -- private/data.txt"},
        context={"working_directory": "/repo"},
    )
    assert result["decision"] == "deny"
    llm.generate.assert_not_called()
