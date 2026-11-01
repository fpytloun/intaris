import pytest

from intaris.classifier import Classification, classify, verified_read_only_bash
from tests.test_hybrid_races import race_store as race_store


@pytest.mark.parametrize(
    "command",
    [
        "git diff",
        "git show HEAD:README",
        "git log",
        "git log --format=%GG -1",
        "git log --format %GS -1",
        "git log --show-signature",
        "git diff --textconv",
        "git diff --ext-diff",
        "git config --get core.pager",
        "git config --list",
        "git -C /other status",
        "git log --oneline | head -1; git merge-base HEAD origin/main",
    ],
)
def test_trusted_git_reads_skip_inference(race_store, command):
    _, _, llm, evaluator, sid = race_store
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert verified_read_only_bash({"command": command})
    assert result["decision"] == "approve"
    assert (
        result["evaluation_metadata"]["policy_result"]["permission_basis"]
        == "read_only_static"
    )
    llm.generate.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        "git -c gpg.program=sh log",
        "git --config-env=gpg.program=PROGRAM log",
        "git --exec-path=/tmp log",
        "git log --exec-path=/tmp",
        "git fetch --upload-pack=sh origin",
        "git push --receive-pack=sh origin",
        "git ls-files -u",
        "git show ext::sh",
        "git show ssh://host/repo",
        "git show user@host:repo",
        "git --git-dir=/other log",
        "git --work-tree=/other diff",
        "git -C /other commit -m x",
        "GIT_EXTERNAL_DIFF=sh git diff --ext-diff",
        "GIT_CONFIG_COUNT=1 git log",
        "PAGER=sh git log",
        "EDITOR=sh git log",
        "env GIT_PAGER=sh git show",
        "git diff --output=file",
        "git log -o file",
        "git diff --out=file",
        "git log > file",
        "git log $(touch file)",
        "git log `touch file`",
        "git config core.pager sh",
        "git config --unset core.pager",
        "git config --list --edit",
        "git add file",
        "git reset --hard",
        "git clean -fd",
        "git checkout file",
        "git apply patch",
    ],
)
def test_command_line_injection_or_mutation_is_not_read(command):
    assert not verified_read_only_bash({"command": command})


@pytest.mark.parametrize("tool", ["write", "edit", "apply_patch"])
@pytest.mark.parametrize(
    "path", [".git/config", ".gitconfig", ".git/hooks/pre-commit", ".gitattributes"]
)
def test_config_writes_remain_writes(tool, path):
    assert (
        classify(tool, {"file_path": path, "path": path, "content": "change"})
        != Classification.READ
    )
    from intaris.read_only import observe

    assert observe(tool, {"file_path": path}) != "read_only"


@pytest.mark.parametrize("denied", [False, True])
def test_routine_glob_reads_keep_explicit_target_restrictions(race_store, denied):
    sessions, _, llm, evaluator, sid = race_store
    if denied:
        sessions.update_session(
            sid,
            user_id="race-owner",
            policy={"deny_paths": ["/repo/src/restricted.py"]},
        )
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": "git diff -- src/*.py"},
        context={"working_directory": "/repo"},
    )
    fast = (
        result["evaluation_metadata"]["policy_result"]["permission_basis"]
        == "read_only_static"
    )
    assert fast is not denied
    if not denied:
        llm.generate.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        "git show HEAD:.{env,env.local}",
        "cat /repo/.{env,env.local}",
    ],
)
def test_brace_secret_targets_require_disclosure_assessment(race_store, command):
    _, _, _, evaluator, sid = race_store
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert (
        result["evaluation_metadata"]["policy_result"]["permission_basis"]
        != "read_only_static"
    )
