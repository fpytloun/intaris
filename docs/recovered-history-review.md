# Recovered history and restriction-review provenance

Recovered event envelopes can retain sequence identity while lacking a usable
timestamp. Such events remain available through unfiltered and sequence-based
reads. Time-bounded reads exclude them because their membership in a historical
snapshot cannot be established. Readers never invent timestamps or delete these
events. Persisted filesystem, S3 and buffered reads use the same filter.

A Judge restriction claim, whether refusing or asking, must quote text present in the
cited original instruction or configured clause. Prior automated decisions and
assistant reports are not independent user restrictions. This verifies source
provenance, not the semantic correctness of the model's interpretation.

An unverifiable restriction claim gets the existing single repair attempt. If the repair
still cannot substantiate the restriction, the action remains unresolved for
human review. It does not become an approval, an automatic uncertainty denial,
or an infrastructure error. Actual model/transport failures retain their
separate failure contract.

The pending action is labeled as a review-validation failure, not missing user
permission. The unverified restriction is not repeated as an established fact.
The original risk assessment is preserved; when it is unknown, the explanation
states that it remains unknown rather than implying a validated harmful effect.
A genuine, validated missing-permission question retains its ordinary wording.
No approval or denial is manufactured from a failed review.

Validation: 291 tests passed, 14 skipped across event storage, review behavior,
source integrity, outcome finalization and the dedicated regressions. No paid
model calls or history repair operations were required.

The clarification-provenance and failure-diagnostics correction passed 331 tests
with 78 skipped across review, HTTP integration, source integrity, profile and
outcome-finalization suites. These are offline regression results, not a claim
that all model interpretations are correct.

## Risk abstention recovery and evidence-aware restriction retry

The hybrid risk fallback resolves an abstained label to `medium` when the
posterior is a low/medium split: hazardous (`high` + `critical`) mass below 0.2,
`unknown` mass below 0.5, and `low` + `medium` holding a majority. Diffuse
uncertainty between the two safe labels is label ambiguity, not evidence of
harm. The earlier rule refused whenever `unknown` reached 0.2, which sent routine
local commands to Judge, where they were approved as routine. Hazard mass at the
floor, dominant `unknown` mass, or an arg-max mismatch still keep the abstention.

A recovered label resolves label ambiguity only. Policy treats a recovered risk
as still unassessed whenever the action crosses a consequential boundary
(`requires_explicit_grant`: observed external or irreversible effect,
delete/disclose effects, irreversible reversibility, or a production mutation)
unless a deterministic grant or an action-bound human receipt already covers it.
Controller facts cannot be discharged by a model label.

When a Judge restriction claim fails quote verification, the retry depends on
whether any citable instruction text exists. If citable originals or configured
clauses exist, the restriction basis stays available and the retry names the
exact failure so a paraphrased real limit can be corrected to its verbatim words.
Absence of a restriction cannot be proven from text, so this check fails closed.
Only when nothing citable exists at all does the retry remove
`restriction_conflict` from the response contract; a repeated claim after that is
the same failure and stays pending for human review.

Offline replay of 72 recorded abstention escalations: 57 resolve to direct
approval, 15 remain reviewed, and none of the retained-review set carries a
consequential boundary that reaches approval. These are offline regression
results, not a claim about model interpretations.

## Alignment continuation and protected targets

Controller-proven static reads now bypass alignment-only review while retaining
other independent review obligations. Human approval of a hybrid alignment
obligation records the session override regardless of whether the evaluation
path is `fast` or `llm`; the legacy `alignment` path remains supported. Judge
guidance distinguishes the protected target of a read-only restriction from
scratch files and artifacts produced by the task.

Offline replay of 326 captured non-approval decisions changed 58 outcomes
(escalate to approve). Every changed record was a static read-only action
without secrets or external effects; no mutating action changed outcome.

## Executed scripts reach the classifier

When a shell command runs a file the agent wrote earlier in the session,
Intaris reconstructs that file from the session's recorded, successfully
executed writes and attaches it as `executed_script` runtime context. The
text is context only (`authority: none`): Intaris does not verify the file
on disk, and a path with no recorded full write stays unknown.

The reconstruction was previously shown only to Judge. The L1 classifier never
saw it, so an interpreter invocation was unlabelable (`effects=[unknown]`) and
every such run went to Judge. The L1 projection now carries `executed_script`
in both JSON and line modes under the same untrusted-context framing.

Reconstruction follows how files are actually built: the last full write
(`write` content or an `apply_patch` Add File section) is the base, and every
later recorded partial edit of that path (`edit`, `multiedit`, `apply_patch`
Update hunks) is replayed in order. Hunks resolve against the original lines
with a forward-only cursor and whole-line comparison, exact first and then
with per-line whitespace stripped, and must match uniquely under whichever
rule applies. Any edit that cannot be replayed exactly, or a Delete of the
path, leaves the file unknown rather than attaching stale or spliced text.

Tool names and argument keys are harness-neutral: lowercase `write`, `edit`,
`multiedit`, `apply_patch` (OpenCode, Cognis) and PascalCase `Write`, `Edit`,
`MultiEdit` (Claude Code), with `file_path`/`path`/`filePath` and
`old_string`/`oldString` style keys. PascalCase names previously never matched.

Offline replay against recorded session events for one day's interpreter
runs: 20 of 23 reconstruct (previously 7 current and 12 stale); the remaining
3 have no full write within the lookback window. Scripts that were never
written in the session, such as published skill assets, are out of scope here.

## Intention prose follows the structured profile

The structured intention profile is the source of truth; the prose intention
is a projection of it. A parent session's prose kept describing an earlier
"give a readiness verdict" request after two later user messages authorized
and then resumed implementation. The profile was right (that request `done`,
an `approval` item `current`, no request current) but the prose was not
re-derived, and the parent/child alignment check reads the prose. Every
implementing child was flagged incompatible and routine development commands
escalated for a night.

`parse_profile` now raises a `stale_prose_intention` diagnostic when no request
is current, an approval from a later source is current, and a finished request
dominates the prose in both directions (most of the request's words are in the
prose and most of the prose is that request). Chronology comes from captured
source order, never item position. The diagnostic triggers the existing bounded
repair with targeted guidance. When the first completion's only problem was
stale prose, its validated items and current selection are retained and only
the repaired prose is accepted; a retry that changes structure or stays stale
cannot drop a restriction or invent a request. The generator rule states that
a later approval of proposed work makes that work the active intention.

Alignment review in both reviewer paths ranks the parent's latest user update
above the generated parent intention when they disagree: a stale generated
intention is a projection, not a human-stated restriction.

## Verified script digests

Intaris cannot read files, so no digest is ever inferred from a path. A
64-character hex directory in a managed asset path is the store's version
identifier, not the file's hash; a short-lived observation that reported it as
one produced a false mismatch against a clause's real content hash and was
removed.

When the integrating client resolves the SHA-256 of each script a shell command
will run, on the executor, at dispatch, it attaches that as `executed_scripts`
runtime context. Intaris binds the evidence to the exact action: it must come
from the executor-digest source, carry the same user and session, be evaluated
for `bash`, and each digested path must appear in the command. Bound digests
become the `verified_asset_digests` controller fact. The digest is a snapshot
of the bytes at dispatch, not an attestation of what later executes.

A grant conditioned on a content hash is neither satisfied nor violated when
no verified digest is present: Judge is told never to refuse on a mismatch it
cannot see, the answer validator rejects an approval that rests on such a
grant, and the routine ask-to-approve upgrade is suppressed for it. When the
executor-verified digest of the executed script equals the hash the grant
names, the grant's hash condition is met and the grant may be an approval
basis; a verified digest that differs leaves the grant unmet.
