"""Kimi Coding Plan usage adapter.

Endpoint and payload shape follow the official kimi-cli ``/usage`` command:
``GET https://api.kimi.com/coding/v1/usages`` with a static Bearer key
(the staff-shared "experimental endpoint" kimi-cli itself calls). The
payload is undocumented, so this parser recognizes only the evidenced
windows — the weekly membership quota (``usage``) and the rolling 5-hour
window (``limits[]`` with a 300-minute duration) — and reports
``parse_error`` instead of guessing when neither is present. Numbers arrive
as JSON strings or numbers and reset timestamps as ``resetTime``/
``reset_at``/``resets_at`` in RFC 3339 or epoch form; all variants are
accepted.

Deferred payload concepts (dropped, not guessed): the ``user`` object
(account identifiers), ``totalQuota`` (monthly membership-freeze flag;
verified shape unavailable), ``parallel`` (a concurrency cap, not a usage
window), and the Extra Usage wallet balance (whether ``/usages`` exposes it
is unverified). ``~/.kimi-code/credentials/`` OAuth tokens are also not
read: kimi-cli owns their refresh and the file shape is unverified.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from ..http import TransportError
from ..models import (
    AVAILABLE,
    ProviderSnapshot,
    QuotaWindow,
    bounded_int,
    bounded_percent,
)
from . import common

ID = "kimi"
HOST = "api.kimi.com"
PATH = "/coding/v1/usages"
PLAN_NAME = "Kimi Coding Plan"
RELOGIN_ACTION = "configure kimi-cli or Pi with a Kimi Coding Plan key"

_FIVE_HOURS_SECONDS = 5 * 3600
_WEEK_SECONDS = 7 * 86400
# Seconds per one ``duration`` unit. A token is matched exactly (after
# stripping the documented ``TIME_UNIT_`` prefix, case-insensitively):
# substring matching would alias ``TIME_UNIT_NANOSECOND`` to SECOND and
# turn a non-5h entry into a 5-hour window.
_UNIT_SECONDS = {
    "MILLISECOND": 0.001,
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3600,
    "DAY": 86400,
}


def request_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": common.USER_AGENT,
    }


def _numeric(value) -> float | None:
    """A finite number, accepting the payload's string-typed numbers too."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value.strip()) if isinstance(value, str) else float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value) -> datetime | None:
    """An RFC 3339 string, an epoch-seconds number, or a numeric string; else None."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None:
            try:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    return common.epoch_to_datetime(_numeric(value))


def _unit_seconds(unit) -> float | None:
    if not isinstance(unit, str):
        return None
    token = unit.strip().upper().removeprefix("TIME_UNIT_")
    return _UNIT_SECONDS.get(token)


def _window_from_pair(key: str, label: str, entry, period_seconds: int) -> QuotaWindow | None:
    """One window from a ``{limit, remaining[, used]}`` object, or None.

    Kimi reports remaining counts; ``used`` is derived as ``limit -
    remaining`` unless the payload states it explicitly. Anything without a
    finite positive limit yields no window rather than a guessed percent.
    """
    if not isinstance(entry, dict):
        return None
    limit = _numeric(entry.get("limit"))
    remaining = _numeric(entry.get("remaining"))
    used = _numeric(entry.get("used"))
    if limit is None or limit <= 0:
        return None
    if used is None:
        if remaining is None:
            return None
        used = limit - remaining
    used = max(used, 0.0)
    raw_reset = entry.get("resetTime", entry.get("reset_at", entry.get("resets_at")))
    return QuotaWindow(
        key=key,
        label=label,
        used_percent=bounded_percent(used / limit * 100),
        remaining_percent=bounded_percent((limit - used) / limit * 100),
        reset_at=_parse_time(raw_reset),
        period_seconds=period_seconds,
        used_value=bounded_int(used),
        limit_value=bounded_int(limit),
        remaining_value=bounded_int(limit - used),
    )


def _five_hour_window(limits) -> QuotaWindow | None:
    """The documented 300-minute ``limits[]`` entry, if any.

    Entries whose duration/unit pair does not describe exactly five hours
    are skipped (not guessed at): only the 5-hour rolling window is
    evidenced. The counts live in the entry's ``detail`` object when
    present, otherwise on the entry itself.
    """
    if not isinstance(limits, list):
        return None
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        duration = _numeric(entry.get("duration"))
        unit_seconds = _unit_seconds(entry.get("timeUnit", entry.get("time_unit")))
        if duration is None or unit_seconds is None:
            continue
        if round(duration * unit_seconds) != _FIVE_HOURS_SECONDS:
            continue
        detail = entry.get("detail")
        source = detail if isinstance(detail, dict) else entry
        window = _window_from_pair("5h", "5h", source, _FIVE_HOURS_SECONDS)
        if window is not None:
            return window
    return None


def parse_usage(payload: dict) -> tuple[QuotaWindow, ...]:
    windows: list[QuotaWindow] = []
    five_hour = _five_hour_window(payload.get("limits"))
    if five_hour is not None:
        windows.append(five_hour)
    weekly = _window_from_pair("weekly", "7d", payload.get("usage"), _WEEK_SECONDS)
    if weekly is not None:
        windows.append(weekly)
    return tuple(windows)


def fetch(token: str, transport, now: datetime | None = None) -> ProviderSnapshot:
    now = now or common.utcnow()
    try:
        response = transport.get(HOST, PATH, request_headers(token))
    except TransportError as exc:
        return common.transport_error_snapshot(ID, exc, now)
    if response.status != 200:
        return common.status_error_snapshot(
            ID, response.status, response.headers, now, RELOGIN_ACTION
        )
    payload = common.decode_json_object(ID, "Kimi", response.body, now)
    if isinstance(payload, ProviderSnapshot):
        return payload
    windows = parse_usage(payload)
    if not windows:
        return common.parse_error_snapshot(ID, "no usage data in Kimi response", now)
    return ProviderSnapshot(
        provider=ID,
        status=AVAILABLE,
        plan_name=PLAN_NAME,
        fetched_at=now,
        windows=windows,
    )
