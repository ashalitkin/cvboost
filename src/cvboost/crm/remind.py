"""Telegram reminders for a contact's "next step" on its due date.

A reminder is a system alert (kind 'crm_reminder', one per contact and due date). The notifier
holds pending system alerts during quiet hours, so a reminder due at night goes out in the morning.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from cvboost.crm import store as S
from cvboost.notify import format as F
from cvboost.storage.db import Store


def queue_crm_reminders(store: Store, timezone: str, now: datetime) -> int:
    today = now.astimezone(ZoneInfo(timezone)).date().isoformat()
    n = 0
    for c in S.due_contacts(store.conn, today):
        name, step = F.safe_text(c["name"], 80), F.safe_text(c["next_step"], 200)
        text = f"CRM reminder: {name} — {step} (due {c['due_at']})."
        n += store.insert_system_alert("crm_reminder", f"{c['id']}:{c['due_at']}", text, now)
    return n
