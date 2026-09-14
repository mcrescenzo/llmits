"""Versioned, secret-free JSON serialization of provider snapshots."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import AVAILABLE, ProviderSnapshot

SCHEMA_VERSION = 1


def _rfc3339(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_dict(window) -> dict:
    return {
        "key": window.key,
        "label": window.label,
        "used_percent": window.used_percent,
        "remaining_percent": window.remaining_percent,
        "reset_at": _rfc3339(window.reset_at),
        "period_seconds": window.period_seconds,
        "used_value": window.used_value,
        "limit_value": window.limit_value,
        "remaining_value": window.remaining_value,
    }


def _provider_dict(snapshot: ProviderSnapshot) -> dict:
    return {
        "provider": snapshot.provider,
        "status": snapshot.status,
        "plan_name": snapshot.plan_name,
        "fetched_at": _rfc3339(snapshot.fetched_at),
        "stale": snapshot.stale,
        "windows": [_window_dict(window) for window in snapshot.windows],
        "error": (
            {
                "code": snapshot.error.code,
                "message": snapshot.error.message,
                "action": snapshot.error.action,
            }
            if snapshot.error
            else None
        ),
    }


def to_document(snapshots, generated_at: datetime | None = None) -> str:
    """Serialize snapshots to the stable schema_version 1 JSON document."""
    moment = generated_at or datetime.now(timezone.utc)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _rfc3339(moment),
        "providers": [_provider_dict(snapshot) for snapshot in snapshots],
    }
    return json.dumps(document, indent=2)


def all_available(snapshots) -> bool:
    return all(snapshot.status == AVAILABLE for snapshot in snapshots)


def used_percent_at_least(snapshots, threshold: int) -> bool:
    """True when any available snapshot's window is used at least ``threshold`` percent.

    The threshold comparison is inclusive (``>=``), so ``--fail-used-percent 0``
    fires for any provider that reported at least one window. Only available
    snapshots participate: a provider failure is the exit-1 condition, not a
    threshold breach.
    """
    return any(
        window.used_percent >= threshold
        for snapshot in snapshots
        if snapshot.status == AVAILABLE
        for window in snapshot.windows
    )
