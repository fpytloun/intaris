"""Session participation gates; never authorization or historical erasure.

Mode revisions fence disable/re-enable (ABA), independently of policy revisions.
Only short persistence transactions lock source rows. Inference runs without a
DB transaction; a dispatched request cannot be recalled, but its result is fenced.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Iterator

from intaris.analytics_evidence import metadata


class ParticipationRevoked(Exception):
    """A queued or running operation no longer has participation consent."""


class AnalyticsResult(dict):
    """Carry task-local fencing to queue completion without persisting it."""

    def __init__(self, result: dict, scope: AnalyticsScope) -> None:
        super().__init__(result)
        self.scope = scope


_dispatch_check: ContextVar[Callable[[], None] | None] = ContextVar(
    "session_dispatch_check", default=None
)
_analytics_scope: ContextVar[AnalyticsScope | None] = ContextVar(
    "session_analytics_scope", default=None
)


def check_dispatch() -> None:
    """Recheck participation before every provider attempt, including retries."""
    check = _dispatch_check.get()
    if check is not None:
        check()


@contextmanager
def dispatch_scope(check: Callable[[], None]) -> Iterator[None]:
    """Install a call-local gate without changing shared model clients."""
    token = _dispatch_check.set(check)
    try:
        yield
    finally:
        _dispatch_check.reset(token)


class GuardedLLM:
    """Gate logical calls too, including injected/non-OpenAI model clients."""

    def __init__(self, llm: Any, check: Callable[[], None]) -> None:
        self._llm = llm
        self._check = check

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        with dispatch_scope(self._check):
            result = self._llm.generate(*args, **kwargs)
        self._check()
        return result


def guardrails_enabled(db: Any, user_id: str, session_id: str) -> bool:
    """Read only the participation flag; missing sessions keep legacy waits."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT guardrails_enabled FROM sessions WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = cur.fetchone()
    return row is None or bool(row["guardrails_enabled"])


class GuardrailsFence:
    """Fence proactive work only; explicit evaluation never installs this gate."""

    def __init__(self, db: Any, user_id: str, session_id: str) -> None:
        self.db, self.user_id, self.session_id = db, user_id, session_id
        with db.cursor() as cur:
            cur.execute(
                "SELECT guardrails_enabled, guardrails_revision FROM sessions "
                "WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            row = cur.fetchone()
        if row is None or not row["guardrails_enabled"]:
            raise ParticipationRevoked("guardrails_disabled")
        self.revision = row["guardrails_revision"]

    def check(self) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT guardrails_enabled, guardrails_revision FROM sessions "
                "WHERE user_id = ? AND session_id = ?",
                (self.user_id, self.session_id),
            )
            row = cur.fetchone()
        if (
            not row
            or not row["guardrails_enabled"]
            or row["guardrails_revision"] != self.revision
        ):
            raise ParticipationRevoked("guardrails_disabled")


def proactive_intention(
    function: Callable[..., str | None],
) -> Callable[..., str | None]:
    """Keep the generation algorithm unchanged behind a per-session gate."""

    @wraps(function)
    def guarded(**kwargs: Any) -> str | None:
        try:
            fence = GuardrailsFence(
                kwargs["db"], kwargs["user_id"], kwargs["session_id"]
            )
            expected = kwargs.get("expected_guardrails_revision")
            if expected is not None and expected != fence.revision:
                raise ParticipationRevoked("guardrails_disabled")
            kwargs["llm"] = GuardedLLM(kwargs["llm"], fence.check)
            kwargs["expected_guardrails_revision"] = fence.revision
            with dispatch_scope(fence.check):
                result = function(**kwargs)
            fence.check()
            return result
        except ParticipationRevoked:
            return None

    return guarded


class AnalyticsScope:
    """One task's session lineage and mode snapshot, with bounded source fences.

    Legacy aggregates do not identify their prior recap dependencies. If an
    owner has excluded descendants, reuse only summaries explicitly generated
    with those descendants excluded. No provenance graph or backfill is needed.
    """

    def __init__(self, db: Any, user_id: str, task: dict | None = None) -> None:
        self.db, self.user_id, self.task = db, user_id, task or {}
        with db.cursor() as cur:
            cur.execute(
                "SELECT session_id, parent_session_id, analytics_enabled, analytics_revision "
                "FROM sessions WHERE user_id = ?",
                (user_id,),
            )
            self.sessions = {row["session_id"]: dict(row) for row in cur.fetchall()}
        self._children: dict[str, set[str]] = {}
        for sid, row in self.sessions.items():
            if row["parent_session_id"] is not None:
                self._children.setdefault(row["parent_session_id"], set()).add(sid)
        self.watched: set[str] = set()

    def descendants(self, session_id: str) -> set[str]:
        result, frontier = set(), {session_id}
        while frontier:
            children = {
                sid
                for parent in frontier
                for sid in self._children.get(parent, ())
                if sid not in result and sid != session_id
            }
            result.update(children)
            frontier = children
        return result

    def enabled(self, session_id: str) -> bool:
        row = self.sessions.get(session_id)
        return bool(row and row["analytics_enabled"])

    def exclusions(self, session_id: str) -> set[str]:
        return {sid for sid in self.descendants(session_id) if not self.enabled(sid)}

    def include(self, session_id: str) -> None:
        if not self.enabled(session_id):
            raise ParticipationRevoked("analytics_disabled")
        self.watched.update({session_id} | self.descendants(session_id))

    def usable_summary(self, row: dict) -> bool:
        sid = row["session_id"]
        if not self.enabled(sid):
            return False
        evidence = metadata(row.get("evidence_metadata"))
        known_children = evidence.get("children", {})
        if any(not self.enabled(child) for child in known_children):
            return False
        return self.exclusions(sid) <= set(
            evidence.get("analytics_excluded_sessions", [])
        )

    def validate(
        self, cur: Any, *, lock: bool = False, check_claim: bool = True
    ) -> None:
        # Lock in stable order, before any derived write. PATCH takes the same
        # session row locks before revoking task claims, avoiding lock inversion.
        for sid in sorted(self.watched):
            if lock:
                cur.execute(
                    "UPDATE sessions SET analytics_revision = analytics_revision "
                    "WHERE user_id = ? AND session_id = ?",
                    (self.user_id, sid),
                )
            cur.execute(
                "SELECT analytics_revision FROM sessions WHERE user_id = ? AND session_id = ?",
                (self.user_id, sid),
            )
            row = cur.fetchone()
            if (
                row is None
                or row["analytics_revision"] != self.sessions[sid]["analytics_revision"]
            ):
                raise ParticipationRevoked("analytics_disabled")
        if check_claim and self.task.get("claim_token"):
            cur.execute(
                "SELECT id FROM analysis_tasks WHERE id = ? AND status = 'running' AND claim_token = ?",
                (self.task["id"], self.task["claim_token"]),
            )
            if cur.fetchone() is None:
                raise ParticipationRevoked("claim_revoked")

    def check(self, *, check_claim: bool = True) -> None:
        with self.db.cursor() as cur:
            self.validate(cur, check_claim=check_claim)


def analytics_scope(db: Any, user_id: str) -> AnalyticsScope:
    """Use the task snapshot, or a read-only selection snapshot outside tasks."""
    return _analytics_scope.get() or AnalyticsScope(db, user_id)


@contextmanager
def analytics_write(db: Any) -> Iterator[Any]:
    """Serialize derived persistence with opt-out, never with external I/O."""
    with db.cursor() as cur:
        scope = _analytics_scope.get()
        if scope is not None:
            scope.validate(cur, lock=True)
        yield cur


def analytics_task(function: Callable[..., dict]) -> Callable[..., dict]:
    """Fence L2/L3 entry, logical model calls, and task-local derived writes."""

    @wraps(function)
    def guarded(db: Any, llm: Any, task: dict, *args: Any, **kwargs: Any) -> dict:
        scope = AnalyticsScope(db, task.get("user_id", ""), task)
        token = _analytics_scope.set(scope)
        try:
            sid = task.get("session_id")
            if sid and sid in scope.sessions:
                scope.include(sid)
                expected = (task.get("payload") or {}).get("analytics_revision")
                if (
                    expected is not None
                    and scope.sessions[sid]["analytics_revision"] != expected
                ):
                    raise ParticipationRevoked("analytics_disabled")
            scope.check()
            guarded_llm = GuardedLLM(llm, scope.check) if llm is not None else None
            result = function(db, guarded_llm, task, *args, **kwargs)
            scope.check()
            return AnalyticsResult(result, scope)
        except ParticipationRevoked as exc:
            return {"status": "skipped", "reason": str(exc)}
        finally:
            _analytics_scope.reset(token)

    return guarded
