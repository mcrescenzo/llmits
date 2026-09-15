"""Claude Pro/Max subscription usage adapter."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TypeGuard

from ..http import TransportError
from ..models import (
    AVAILABLE,
    ProviderSnapshot,
    QuotaWindow,
    bounded_int,
    bounded_percent,
    vetted_label,
)
from . import common

ID = "claude"
HOST = "api.anthropic.com"
PATH = "/api/oauth/usage"
PLAN_NAME = "Claude Pro/Max"
RELOGIN_ACTION = "log in again with the Claude Code CLI (claude login)"


def request_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": common.USER_AGENT,
    }


def _parse_rfc3339(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Python 3.11+ parses a trailing 'Z' UTC designator natively.
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _slugify(name: str) -> str:
    """Lowercase ASCII ``[a-z0-9_]`` slug of a provider model name, or ''.

    Only ASCII alphanumerics survive (``str.isalnum`` alone would admit
    Cyrillic homoglyphs and other non-ASCII letters into window keys), so
    the slug is safe both as a stable JSON key suffix and as the fallback
    label text when the display name fails ``vetted_label``.
    """
    parts = []
    current = ""
    for char in name:
        if char.isascii() and char.isalnum():
            current += char.lower()
        elif current:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return "_".join(parts)


def _model_label(name: str, slug: str) -> str:
    """Build the short per-model label from vetted text, never raw provider text.

    ``name`` is the provider-supplied model text (a ``seven_day_<model>``
    key suffix or ``limits[].scope.model.display_name``); ``slug`` is its
    ``_slugify`` result. The display text is used only when it passes the
    ``vetted_label`` allowlist; otherwise the label falls back to the
    ASCII-only slug (the same text that forms the window key). Title-cased so
    both shapes render as the spec's short model label ("Opus", "Sonnet").
    """
    text = vetted_label(name) or slug.replace("_", " ")
    return text.title()


def _is_finite_number(value) -> TypeGuard[float]:
    """Narrow ``value`` to a finite non-boolean number for runtime and mypy."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _window_from_rate_limit(
    key: str, label: str, entry: dict, period_seconds: int
) -> QuotaWindow | None:
    utilization = entry.get("utilization")
    if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
        return None
    return QuotaWindow.from_percent(
        key=key,
        label=label,
        percent=utilization,
        reset_at=_parse_rfc3339(entry.get("resets_at")),
        period_seconds=period_seconds,
    )


def parse_usage(payload: dict) -> tuple[QuotaWindow, ...]:
    windows: list[QuotaWindow] = []
    seen: set[str] = set()

    def push(window: QuotaWindow | None) -> None:
        if window is not None and window.key not in seen:
            seen.add(window.key)
            windows.append(window)

    five_hour = payload.get("five_hour")
    if isinstance(five_hour, dict):
        push(_window_from_rate_limit("5h", "5h", five_hour, 5 * 3600))
    seven_day = payload.get("seven_day")
    if isinstance(seven_day, dict):
        push(_window_from_rate_limit("weekly", "7d", seven_day, 7 * 86400))

    for key, value in payload.items():
        if not key.startswith("seven_day_"):
            continue
        model = key.removeprefix("seven_day_")
        if not model or not isinstance(value, dict):
            continue
        slug = _slugify(model)
        if slug:
            push(
                _window_from_rate_limit(
                    f"weekly_{slug}", _model_label(model.replace("_", " "), slug), value, 7 * 86400
                )
            )

    limits = payload.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict) or entry.get("group") != "weekly":
                continue
            scope = entry.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            display = None
            if isinstance(model, dict):
                display = model.get("display_name") or model.get("id")
            if not isinstance(display, str):
                continue
            slug = _slugify(display)
            percent = entry.get("percent")
            if (
                not slug
                or isinstance(percent, bool)
                or not isinstance(percent, (int, float))
            ):
                continue
            push(
                QuotaWindow.from_percent(
                    key=f"weekly_{slug}",
                    label=_model_label(display, slug),
                    percent=percent,
                    reset_at=_parse_rfc3339(entry.get("resets_at")),
                    period_seconds=7 * 86400,
                )
            )

    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        limit = extra.get("monthly_limit")
        used = extra.get("used_credits")
        if _is_finite_number(limit) and limit > 0 and _is_finite_number(used):
            used_i = bounded_int(used)
            limit_i = bounded_int(limit)
            push(
                QuotaWindow(
                    key="extra_credits",
                    label="extra",
                    used_percent=bounded_percent(used / limit * 100),
                    remaining_percent=bounded_percent((limit - used) / limit * 100),
                    used_value=used_i,
                    limit_value=limit_i,
                    remaining_value=max(limit_i - used_i, 0),
                    period_seconds=30 * 86400,
                )
            )

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
    payload = common.decode_json_object(ID, "Claude", response.body, now)
    if isinstance(payload, ProviderSnapshot):
        return payload
    windows = parse_usage(payload)
    if not windows:
        return common.parse_error_snapshot(ID, "no usage data in Claude response", now)
    return ProviderSnapshot(
        provider=ID,
        status=AVAILABLE,
        plan_name=PLAN_NAME,
        fetched_at=now,
        windows=windows,
    )
