# Session participation

Sessions persist two independent boolean settings, both enabled by default:

| Setting | When disabled |
|---|---|
| `analytics_enabled` | No automatic or requested L2 generation/compaction; the session is excluded from new L3 inputs. |
| `guardrails_enabled` | No proactive intention generation/bootstrap or child-alignment checks. Pending proactive work cannot apply stale results. |

Set these top-level fields on `POST /api/v1/intention` when creating a session.
The response acknowledges persisted values. Session reads and listings expose
both fields. `PATCH /api/v1/session/{id}` changes either setting independently;
an omitted or `null` field means no change. Creation requires actual booleans
when a setting is provided, not `null`, strings, or numbers.

These settings are participation controls, not authorization grants. An
explicit `/evaluate` still enforces session/parent lifecycle, inherited policy,
evaluation bounds, the configured evaluator (including Jev), and Judge behavior.
Disabling proactive alignment does not bypass mandatory per-call evaluation.

## Independent modes

| Guardrails | Analytics | Behavior |
|---|---|---|
| Enabled | Enabled | Existing proactive and behavioral processing. |
| Enabled | Disabled | Proactive intention/alignment and evaluation remain; no L2/L3 participation. |
| Disabled | Enabled | Behavioral analysis of recorded events/audit remains; no proactive intention/alignment. |
| Disabled | Disabled | Recording and retrieval remain; neither proactive pipeline runs. |

Global service settings remain applicable. Search and embedding settings are
separate; these flags do not disable or purge search.

## Opt-out and retained history

Opt-out prevents new eligible queue admission and fences execution and
persistence. Each provider attempt rechecks participation, including retries.
Already-dispatched requests may finish: they cannot be recalled. Their revoked
results cannot create summaries, update behavioral analyses/profiles, publish a
generation-success event, or schedule follow-up work. Mode revisions also fence
disable/re-enable races and cancelled queue claims. No transaction spans model
inference.

Sessions, events, audit, source evidence, checkpoints, and client-submitted
summaries remain recorded. Historical derived summaries/analyses stay readable;
existing behavioral profiles are not retroactively erased. Re-enabling makes
retained session history eligible again, but does not resurrect revoked claims.

New cohorts exclude disabled sessions' direct summaries and known child
contributions. Legacy aggregates can contain prior recaps without complete
dependency metadata. If their owner has disabled descendants, these aggregates
are conservatively skipped. New summaries record excluded descendant identities
so clean replacement summaries can be reused. This is session-source membership,
not text-level redaction of independently recorded content in another session.
There is no historical provenance backfill or privacy-purge operation.

## API results

- Manual summary generation for a disabled session returns `409` with
  `detail.code = "analytics_disabled"`.
- Summary reads still return retained artifacts and current `analytics_enabled`.
- Backfill reports excluded sessions in `skipped_disabled`, not `enqueued`.
- An L3 request with no participating sessions returns `enqueued = 0` and
  `reason = "no_eligible_sessions"`.
- Skipped/revoked work is not counted or announced as a generated analysis.

Clients resolve modes before creating each session, including rotation and
recovery. A delegate may inherit effective execution capabilities; a managed
conversation may use its target agent's configuration. Intaris does not infer
these client rules from the parent relationship. Duplicate creation must be
reconciled by reading the existing session; changing its modes requires PATCH.
WorkflowStep v1 remains declaration-only against an existing session and cannot
change participation settings.
