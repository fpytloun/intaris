# Intention repair isolation

Internal profile-validation feedback is part of the generation instructions,
not a new user message. A bounded repair keeps the original user/source payload
unchanged so the latest source message remains the latest actual task.

If the resulting profile is unavailable, Intaris preserves the previous
intention and intention source, and clears the derived profile using the existing
state-revision and user-message freshness guards. New raw instructions remain
available independently; preserving a summary does not preserve stale grants.
Successful extraction can still update the intention normally.

A nonempty generated display title can initialize an untitled session even when
its authority profile or intention summary is unusable. Existing titles are
preserved on this path. The same state-revision, latest-user-message and
generation freshness guards protect this write; a stale completion cannot
replace a manual title or a newer generation. A title-only `session_updated`
notification is emitted only after the guarded write succeeds. It is not an
intention update, does not trigger alignment rechecks, and never grants authority.
An empty or malformed generation still cannot invent a title.

Parent alignment is unchanged. This prevents a failed JSON repair from replacing
a valid child assignment with a task such as “Regenerate JSON.” It does not
retroactively rewrite already-corrupted sessions or approve their blocked calls.
Those sessions need source-based regeneration and normal alignment checking.

Regression coverage includes unchanged source payloads, successful repair,
failed repair, new raw restrictions with old grants cleared, and a concurrent
generation whose newer profile must not be cleared.

## Embedded diagnostic evidence

The single-call extractor receives labelled source segments whenever a message
contains fenced code, quotations or structured data. Segment offsets and text
refer to the unchanged captured original; the same boundary function validates
authority quotations. Ordinary prose is an instruction candidate, not an
automatic permission grant. Embedded material can explain a debugging request
but cannot independently authorize an action.

Extract a request heading separately from its following diagnostic block, retain
the block as context with its surrounding disclaimer, and preserve restrictions
on both sides. Only a complete prose segment identifying requested work may
end in a colon before embedded context. Partial approvals, restrictions and
revocations do not receive this exception. Fences match delimiter type and length;
structured diagnostic contents do not swallow prose after a closing fence.

An authority quotation overlapping embedded material receives one bounded repair
in both JSON transport modes. Feedback explains the boundary error without
changing the source payload or logging source text. Invalid items are never
silently trimmed or promoted to authority. A boundary repair with remaining
validation errors cannot persist a partially valid profile. Failed repair retains the existing
raw-source fallback and freshness guards. This does not change title adoption
or retroactively regenerate existing sessions.

## Unpunctuated request paragraphs

An entire ordinary-prose request paragraph may end without punctuation before
diagnostic paragraphs. Paragraph boundaries come from the unchanged original
source, including blank lines and CRLF; a single wrapped line is not a paragraph
boundary. This exception identifies requested work, not approvals, restrictions
or revocations. Embedded-data rejection, truncated-source checks, and the
original contextual envelope remain in force. Cut clauses and wrapped qualifiers
still fail. A `partial_statement` error receives one bounded actionable repair
in either JSON mode; an unsuccessful boundary repair retains raw-source fallback.
