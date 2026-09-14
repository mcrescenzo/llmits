"""OpenCode Go usage adapter for the fixed Zen Go endpoint."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone

from ..http import TransportError
from ..models import (
    AVAILABLE,
    UNAVAILABLE,
    ProviderError,
    ProviderSnapshot,
    QuotaWindow,
    bounded_percent,
)
from .common import (
    USER_AGENT,
    decode_json_object,
    parse_error_snapshot,
    status_error_snapshot,
    transport_error_snapshot,
    utcnow,
)

HOST = "opencode.ai"
PATH = "/zen/go/v1/usage"
PLAN_NAME = "OpenCode Go"

_WINDOW_SPECS = (
    ("rolling", "5h", "5h", 5 * 3600),
    ("weekly", "weekly", "7d", 7 * 86400),
    ("monthly", "monthly", "month", None),
)
_ALLOWED_STATUSES = frozenset(("ok", "rate-limited"))
_RESET_AT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)


def _parse_reset_at(value) -> datetime:
    if not isinstance(value, str) or _RESET_AT.fullmatch(value) is None:
        raise ValueError("invalid OpenCode reset timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("invalid OpenCode reset timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid OpenCode reset timestamp")
    return parsed.astimezone(timezone.utc)


def parse_usage(payload) -> tuple[QuotaWindow, ...]:
    """Parse the evidenced OpenCode Go usage envelope, failing closed on drift."""
    if not isinstance(payload, Mapping) or set(payload) != {"usage"}:
        raise ValueError("invalid OpenCode usage payload")
    usage = payload.get("usage")
    expected_windows = {source_key for source_key, *_ in _WINDOW_SPECS}
    if not isinstance(usage, Mapping) or set(usage) != expected_windows:
        raise ValueError("invalid OpenCode usage payload")

    windows = []
    for source_key, key, label, period_seconds in _WINDOW_SPECS:
        item = usage.get(source_key)
        if not isinstance(item, Mapping) or set(item) != {
            "status",
            "percent",
            "resetsAt",
        }:
            raise ValueError("invalid OpenCode usage payload")
        status = item.get("status")
        if status not in _ALLOWED_STATUSES:
            raise ValueError("invalid OpenCode usage status")
        percent = item.get("percent")
        if (
            not isinstance(percent, int)
            or isinstance(percent, bool)
            or not 0 <= percent <= 100
        ):
            raise ValueError("invalid OpenCode usage percentage")
        if (status == "rate-limited") != (percent == 100):
            raise ValueError("inconsistent OpenCode usage status")
        reset_at = _parse_reset_at(item.get("resetsAt"))
        used = bounded_percent(percent)
        windows.append(
            QuotaWindow(
                key=key,
                label=label,
                used_percent=used,
                remaining_percent=100 - used,
                reset_at=reset_at,
                period_seconds=period_seconds,
            )
        )
    return tuple(windows)


def fetch(credential: str, transport, now=None) -> ProviderSnapshot:
    """Fetch and normalize one OpenCode Go usage snapshot."""
    now = now or utcnow()
    headers = {
        "Authorization": f"Bearer {credential}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    try:
        response = transport.get(HOST, PATH, headers)
    except TransportError as exc:
        return transport_error_snapshot("opencode", exc, now)

    if response.status == 403:
        error = ProviderError(
            code=UNAVAILABLE,
            message="OpenCode Go subscription is required",
            action="subscribe to OpenCode Go or use a Go-enabled credential",
        )
        return ProviderSnapshot(
            provider="opencode",
            status=UNAVAILABLE,
            plan_name=None,
            fetched_at=now,
            error=error,
        )
    if response.status != 200:
        return status_error_snapshot(
            "opencode",
            response.status,
            response.headers,
            now,
            "log in to OpenCode Go again",
        )

    payload = decode_json_object("opencode", "OpenCode", response.body, now)
    if isinstance(payload, ProviderSnapshot):
        return payload
    try:
        windows = parse_usage(payload)
    except (TypeError, ValueError):
        return parse_error_snapshot("opencode", "OpenCode returned an unexpected payload", now)

    return ProviderSnapshot(
        provider="opencode",
        status=AVAILABLE,
        plan_name=PLAN_NAME,
        fetched_at=now,
        windows=windows,
    )
