"""Z.AI GLM Coding Plan quota adapter."""
from __future__ import annotations

import re
from datetime import datetime

from ..http import TransportError
from ..models import (
    AVAILABLE,
    UNAVAILABLE,
    ProviderError,
    ProviderSnapshot,
    QuotaWindow,
    bounded_int,
    vetted_label,
)
from . import common

ID = "zai"
HOST = "api.z.ai"
PATH = "/api/monitor/usage/quota/limit"
RELOGIN_ACTION = "check your ZAI_API_KEY (GLM Coding Plan key)"

_PERIOD_BY_UNIT = {3: 5 * 3600, 6: 7 * 86400, 5: 30 * 86400}

# Used only to build a distinguishing suffix for unrecognized rawType keys
# (see _window_kind's fallback); not a vetting/allowlist regex like
# common.PLAN_VALUE. QuotaWindow.__post_init__ still sanitizes/bounds the key.
_KEY_SLUG_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def _label_slug(label_text: str) -> str:
    """Collapse a raw label into a short lowercase key suffix, or ''."""
    return _KEY_SLUG_NOT_ALNUM.sub("_", label_text.strip().lower()).strip("_")[:20]


def _plain_headers(token: str) -> dict[str, str]:
    # Z.AI's official usage plugin sends the raw key as the Authorization
    # value (no Bearer prefix); mirror that first.
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": common.USER_AGENT,
    }


def _bearer_headers(token: str) -> dict[str, str]:
    headers = _plain_headers(token)
    headers["Authorization"] = f"Bearer {token}"
    return headers


_KIND_TOKENS = ("TOKENS_LIMIT", "CREDIT_LIMIT", "TIME_LIMIT")


def _window_kind(entry: dict) -> tuple[str, str, int | None]:
    """Classify one ``limits[]`` entry into ``(key, label, period_seconds)``.

    The kind token is ``entry["rawType"]`` when that is a string (the shape
    every recorded fixture used before llmits-o03.11). Some accounts omit
    ``rawType`` entirely and instead put the same enum value directly in
    ``type`` (`TOKENS_LIMIT`, `CREDIT_LIMIT`, `TIME_LIMIT`); when ``rawType``
    is absent (or not a string) and ``type`` is exactly one of those three
    tokens, ``type`` is consumed as the kind token instead — but only as the
    kind token, never also as the free-text label/period hint, so the
    unit-based branches below decide the period exactly as they do for a
    ``rawType`` entry (unit 3 -> 5h, unit 6 -> weekly, unit 5 -> monthly).
    Any other shape (free-text ``type``, no recognizable kind at all) falls
    through to the vetted-label fallback unchanged.
    """
    raw = entry.get("rawType")
    type_value = entry.get("type")
    type_str = type_value if isinstance(type_value, str) else ""
    kind: str
    label_text: str
    if isinstance(raw, str):
        kind = raw
        label_text = type_str
    elif type_str in _KIND_TOKENS:
        kind = type_str
        label_text = ""
    else:
        # A non-string rawType never equals any kind token; treat it as an
        # unrecognized kind and keep only the (possibly empty) label text.
        kind = ""
        label_text = type_str
    lowered = label_text.lower()
    unit = entry.get("unit")
    if kind == "TOKENS_LIMIT":
        if unit == 3 or "5h" in lowered or "5 hour" in lowered:
            return "5h", "5h", _PERIOD_BY_UNIT.get(3)
        if unit == 6 or "week" in lowered:
            return "weekly", "7d", _PERIOD_BY_UNIT.get(6)
        return "tokens", "token quota", None
    if kind == "CREDIT_LIMIT":
        if unit == 3 or "5h" in lowered or "5 hour" in lowered:
            return "5h_credits", "5h", _PERIOD_BY_UNIT.get(3)
        if unit == 6 or "week" in lowered:
            return "weekly_credits", "7d", _PERIOD_BY_UNIT.get(6)
        # Distinct from tui.py's "credits" key (codex.py's cents-valued
        # credit-balance window): an unrecognised-period CREDIT_LIMIT entry
        # here carries a raw remaining unit count, not pre-multiplied cents,
        # so it must never collide with that key.
        return "credits_other", "credits quota", None
    if kind == "TIME_LIMIT":
        return "monthly_mcp", "MCP", _PERIOD_BY_UNIT.get(5)
    # Unknown kind (no rawType string, and type is either absent or free
    # text rather than one of the three exact tokens above): fold in a slug
    # of the label so two differently-labeled unknown entries get distinct
    # keys (and thus both survive parse_usage's de-dup) while an exact
    # repeat still collapses to one window. The label itself is never the
    # raw provider string: it must pass the vetted_label allowlist, else it
    # falls back to the key's ASCII slug, else "quota".
    slug = _label_slug(label_text)
    key = f"other:{slug}" if slug else "other"
    label = vetted_label(label_text) or slug.replace("_", " ") or "quota"
    return key, label, _PERIOD_BY_UNIT.get(unit) if isinstance(unit, int) else None


def parse_usage(payload: dict) -> tuple[str, tuple[QuotaWindow, ...]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return "Z.AI Coding Plan", ()
    level = common.vetted_plan_value(data.get("level"))
    plan_name = f"Z.AI {level}" if level else "Z.AI Coding Plan"

    limits = data.get("limits")
    if not isinstance(limits, list):
        return plan_name, ()

    windows: list[QuotaWindow] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        key, label, period = _window_kind(entry)
        usage = entry.get("usage")
        current = entry.get("currentValue")
        remaining = entry.get("remaining")
        used_value = limit_value = remaining_value = None
        percent_source = None
        if isinstance(usage, (int, float)) and isinstance(current, (int, float)):
            # bounded_int also rejects non-finite values (1e999) that would
            # otherwise raise OverflowError at int()/round().
            used_value, limit_value = bounded_int(current), bounded_int(usage)
            remaining_value = (
                bounded_int(remaining)
                if isinstance(remaining, (int, float))
                else max(limit_value - used_value, 0)
            )
            if limit_value > 0:
                percent_source = used_value / limit_value * 100
        reset_at = common.epoch_to_datetime(entry.get("nextResetTime"), millis=True)
        if percent_source is None:
            percentage = entry.get("percentage")
            percent_source = percentage if isinstance(percentage, (int, float)) else 0
        window = QuotaWindow.from_percent(
            key=key,
            label=label,
            percent=percent_source,
            reset_at=reset_at,
            period_seconds=period,
            used_value=used_value,
            limit_value=limit_value,
            remaining_value=remaining_value,
        )
        if not any(w.key == window.key for w in windows):
            windows.append(window)
    return plan_name, tuple(windows)


def _provider_message() -> str:
    """Fixed, local error text — provider response bodies never reach output."""
    return "Z.AI reported an error for the coding plan"


def fetch(token: str, transport, now: datetime | None = None) -> ProviderSnapshot:
    now = now or common.utcnow()
    try:
        response = transport.get(HOST, PATH, _plain_headers(token))
        if response.status in (401, 403):
            # Same fixed host and path; only the header style changes.
            response = transport.get(HOST, PATH, _bearer_headers(token))
    except TransportError as exc:
        return common.transport_error_snapshot(ID, exc, now)
    if response.status != 200:
        return common.status_error_snapshot(
            ID, response.status, response.headers, now, RELOGIN_ACTION
        )
    payload = common.decode_json_object(ID, "Z.AI", response.body, now)
    if isinstance(payload, ProviderSnapshot):
        return payload
    if payload.get("code") != 200:
        error = ProviderError(
            code=UNAVAILABLE,
            message=_provider_message(),
            action="check your GLM Coding Plan subscription status",
        )
        return ProviderSnapshot(
            provider=ID, status=UNAVAILABLE, plan_name=None, fetched_at=now, error=error
        )
    plan_name, windows = parse_usage(payload)
    if not windows:
        return common.parse_error_snapshot(ID, "no quota limits in Z.AI response", now)
    return ProviderSnapshot(
        provider=ID,
        status=AVAILABLE,
        plan_name=plan_name,
        fetched_at=now,
        windows=windows,
    )
