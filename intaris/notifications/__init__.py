"""Per-user notification system for escalation alerts.

Provides a pluggable notification framework that sends alerts to users
when tool call evaluations result in escalation decisions. Each user
configures their own notification channels (webhook, Pushover, Slack)
via the REST API or management UI.

Independent of the system-level Cognis webhook (webhook.py) — both
fire on escalation. The Cognis webhook is for approval workflow
automation; notifications are for alerting the human operator.
"""

from __future__ import annotations

from intaris.notifications.dispatcher import Notification, NotificationDispatcher
from intaris.notifications.providers import PROVIDERS

__all__ = [
    "Notification",
    "NotificationDispatcher",
    "PROVIDERS",
]
