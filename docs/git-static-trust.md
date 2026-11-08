# Static Git trust boundary

This is the canonical Git classification threat model.

Intaris guards against agent drift from user intention and harmful behavior over
short and long horizons. It is not an antivirus or an adversarial-agent sandbox.
Filesystem/network isolation and limited tools provide containment. Static reads
are a pragmatic fast path, not a proof against every weaponizable construction.
Review findings should concern plausible drift, real mutation, explicit
command-line/environment injection, disclosure or regressions—not hypothetical
helper chains. Scoring/bypass-attempt detection is outside this change.

Existing repository and global Git configuration is trusted, including implicit
external diff/textconv drivers, signature-verification helpers, pager and
attribute-selected drivers. Formats (including `%G*`), `--show-signature`,
`--textconv` and `--ext-diff` do not by themselves remove read-only proof.
The classifier does not execute or inspect these helpers. Existing configured
aliases are trusted infrastructure, but unknown subcommands still need assessment
because their effects cannot be inferred from their names.

Command-line or environment overrides are untrusted. Static proof excludes:

- Config/executable injection: `-c`, `--config-env`, `--exec-path`,
  `--upload-pack`, `--receive-pack`, `-u` and transports/URLs.
- Any environment-assignment prefix, including `GIT_*`, `PAGER`, `EDITOR`;
  shell wrappers (`env`, `sh -c`, etc.) cannot evade this.
- Output options (`--output`, abbreviations and `-o`), redirects, substitutions,
  backticks, unsupported shell syntax and executable filters.
- Global repository relocation (`--git-dir`, `--work-tree`): these are
  conservatively not proved, including inside-target forms. `-C <dir>` and
  `--no-pager` preserve a read subcommand's mutation classification; target
  and deny-path checks still apply. `git rev-parse
  --git-dir` is an observation, not global relocation.
- Mutating subcommands and options. `git config` is observational only with
  `--get` or `--list`; unknown config options are not proved.

All config changes go through evaluated writes: `git config` setters and direct
writes/edits to `.git/config`, `.gitconfig`, `.git/hooks/*` and `.gitattributes`.
The write is assessed under ordinary policy; a read grant is never reused for it.

Target/disclosure guards still apply. Git blob operands and literal filenames
after `--` feed deny-path checks. Ordinary glob/pathspec reads are permitted;
expansion with an explicit deny-path policy still needs assessment, as do broad
hidden-file reads. The blanket no-expansion rule and exhaustive formatting-option
allowlist have been removed. Existing shell substitution, wrapper and filter
rules remain because they introduce plausible command/effect ambiguity.
Brace alternatives remain assessed because they can spell explicit credential
filenames absent from the literal disclosure check (`HEAD:.{env,env.local}`).
Trusted config does not imply permission to disclose credentials or bypass
explicit denies, applicable restrictions, floors or human checkpoints.

`git grep` and `git blame` are observational. Grep patterns (positional or
`-e`/`--regexp`) are search text, not secret-bearing file operands; pattern
files (`-f`) and paths still feed disclosure checks. Grep's `-O` /
`--open-files-in-pager` can execute a command and are not static reads.

Secret-word disclosure evidence is operation-aware. Content reads such as
`git show HEAD:.env`, diffs of credential files, and credential/config getters
still require disclosure assessment. Branch/worktree names, commit messages,
authentication settings used by a push, and filename-only diff output are not
secret values. They do not establish disclosure merely because they contain
`credentials` or `token`. This distinction grants no permission to mutate or
publish: writes, unknown operations, explicit denies, and task restrictions
continue through ordinary evaluation.

An `env` wrapper with a child command executes that command instead of printing
environment values. Disclosure inspection follows the child (`env -u PYTHONPATH
cat .env` still exposes a secret file); credential-related source filenames
passed to a formatter or test runner are not themselves disclosure evidence.
