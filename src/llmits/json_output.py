"""Versioned, secret-free JSON serialization of provider snapshots."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import AVAILABLE, ProviderSnapshot, bounded_percent
from .providers import PROVIDER_IDS

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


def _max_used_percent(snapshot) -> int:
    """Highest window percentage of a snapshot, 0 when it has no windows.

    Each value is re-clamped through ``bounded_percent``: the model does
    not validate ``QuotaWindow.used_percent`` at construction, so a
    manually built snapshot could carry a non-numeric, non-finite, or
    out-of-range value. Clamping here keeps the overview line an ASCII
    integer 0..100 even for such a snapshot.
    """
    return max(
        (bounded_percent(window.used_percent) for window in snapshot.windows), default=0
    )


def overview_state(snapshot) -> str:
    """The one ASCII ``<provider>=<state>`` state token for a snapshot.

    Precedence: a failed snapshot renders its normalized fixed status; an
    available snapshot with windows renders its highest window usage as
    ``N%`` (``N%~`` when the numbers come from a retained last-good
    snapshot); an available snapshot without windows renders the literal
    ``available`` (or ``stale``). Plan names, window labels, error text, and
    timestamps never participate.
    """
    if snapshot.status != AVAILABLE:
        return str(snapshot.status)
    if snapshot.windows:
        suffix = "~" if snapshot.stale else ""
        return f"{_max_used_percent(snapshot)}%{suffix}"
    return "stale" if snapshot.stale else "available"


def _urgency_key(snapshot) -> tuple:
    """Sort key ranking snapshots from most to least urgent.

    Category 0 is a failed provider, 1 a stale one (windowed or not), 2 a
    fresh provider with windows ordered by highest usage descending (note
    the negated percentage), and 3 a fresh provider without windows.
    ``sorted()`` is stable, so equal keys keep their configured order.
    """
    if snapshot.status != AVAILABLE:
        return (0,)
    if snapshot.stale:
        return (1,)
    if snapshot.windows:
        return (2, -_max_used_percent(snapshot))
    return (3,)


def overview_line(snapshots, order: str = "configured") -> str:
    """The complete ``--overview`` line body (without its trailing newline).

    Tokens ``<provider>=<state>`` join with single ASCII spaces in the given
    snapshot order, which the CLI supplies already de-duplicated in
    configured order. ``order="urgency"`` re-sorts them by urgency instead.
    Only fixed provider ids render: ``ProviderSnapshot`` does not validate
    ``provider``, so a snapshot carrying anything outside ``PROVIDER_IDS``
    raises ``ValueError`` instead of injecting a newline, extra spaces, or
    non-ASCII text into the one-line grammar.
    """
    if order == "urgency":
        snapshots = sorted(snapshots, key=_urgency_key)
    elif order != "configured":
        raise ValueError(f"unknown overview order: {order!r}")
    tokens = []
    for snapshot in snapshots:
        if snapshot.provider not in PROVIDER_IDS:
            raise ValueError(f"unknown provider id: {snapshot.provider!r}")
        tokens.append(f"{snapshot.provider}={overview_state(snapshot)}")
    return " ".join(tokens)


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
