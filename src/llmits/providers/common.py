"""Shared HTTP-status → snapshot mapping for provider adapters."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from .. import __version__
from ..http import RedirectedError
from ..models import (
    AUTH_REQUIRED,
    NETWORK_ERROR,
    PARSE_ERROR,
    RATE_LIMITED,
    UNAVAILABLE,
    ProviderError,
    ProviderSnapshot,
)

_RETRY_AFTER_MAX_SECONDS = 24 * 3600

# Sent by every adapter's request headers; centralized so it is derived once.
USER_AGENT = f"llmits/{__version__}"

# Provider-derived display strings (plan/level tokens) are vetted against this
# shape before use; anything else falls back to a generic label. The model
# layer sanitizes and bounds the final value as defense in depth. Shared by
# codex.py (plan_type) and zai.py (level) — the two adapters that expose a
# short plan/tier token in their payloads.
PLAN_VALUE = re.compile(r"^[a-z0-9][a-z0-9 _+.-]{0,23}$")


def vetted_plan_value(value) -> str | None:
    """Return a lowercase vetted plan/level token, or None."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if PLAN_VALUE.match(candidate):
        return candidate
    return None


def _retry_after_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    if text.isdigit():
        seconds = min(int(text), _RETRY_AFTER_MAX_SECONDS)
        if seconds > 0:
            return f"; retry in {seconds}s"
    return ""


def transport_error_snapshot(provider: str, exc: Exception, now: datetime) -> ProviderSnapshot:
    if isinstance(exc, RedirectedError):
        error = ProviderError(
            code=PARSE_ERROR,
            message="provider endpoint unexpectedly redirected",
            action="the provider API may have changed; check for a newer llmits release",
        )
    else:
        # The raw exception text is untrusted (it can embed URLs, hostnames,
        # or other transport-internal detail); only the exception *class
        # name* — a closed vocabulary from llmits' own exception hierarchy —
        # is retained for diagnostics.
        error = ProviderError(
            code=NETWORK_ERROR,
            message=f"network error ({type(exc).__name__})",
            action="check your connection and try again shortly",
        )
    return ProviderSnapshot(
        provider=provider, status=error.code, plan_name=None, fetched_at=now, error=error
    )


def status_error_snapshot(
    provider: str,
    status: int,
    headers: dict[str, str],
    now: datetime,
    relogin_action: str,
) -> ProviderSnapshot:
    if status in (401, 403):
        error = ProviderError(
            code=AUTH_REQUIRED,
            message="provider rejected the credentials",
            action=relogin_action,
        )
    elif status == 429:
        error = ProviderError(
            code=RATE_LIMITED,
            message=f"rate limited{_retry_after_text(headers.get('retry-after'))}",
            action="wait for the rate limit window to pass before refreshing",
        )
    elif status >= 500:
        error = ProviderError(
            code=NETWORK_ERROR,
            message=f"provider server error (HTTP {status})",
            action="try again shortly",
        )
    else:
        error = ProviderError(
            code=UNAVAILABLE,
            message=f"provider returned HTTP {status}",
            action="the plan or endpoint may be unavailable; try the provider console",
        )
    return ProviderSnapshot(
        provider=provider, status=error.code, plan_name=None, fetched_at=now, error=error
    )


def parse_error_snapshot(provider: str, message: str, now: datetime) -> ProviderSnapshot:
    error = ProviderError(
        code=PARSE_ERROR,
        message=message,
        action="the provider API may have changed; check for a newer llmits release",
    )
    return ProviderSnapshot(
        provider=provider, status=PARSE_ERROR, plan_name=None, fetched_at=now, error=error
    )


def decode_json_object(
    provider: str, display: str, body: bytes, now: datetime
) -> dict | ProviderSnapshot:
    """Decode ``body`` as a JSON object, or return a ``parse_error`` snapshot.

    Guards the "decode UTF-8 JSON, then require a dict" boundary duplicated
    verbatim across every adapter's ``fetch()``. ``display`` is the
    human-readable provider name used in the two failure messages (e.g.
    "Claude", "Codex", "Z.AI"); ``provider`` is the snapshot's provider id.
    Callers must check ``isinstance(result, ProviderSnapshot)`` and return it
    unchanged before treating the result as the payload dict.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return parse_error_snapshot(provider, f"{display} returned invalid JSON", now)
    if not isinstance(payload, dict):
        return parse_error_snapshot(provider, f"{display} returned an unexpected payload", now)
    return payload


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def epoch_to_datetime(value, *, millis: bool = False) -> datetime | None:
    """Convert a provider epoch value to a UTC ``datetime``, or ``None``.

    Guards the "coerce an optional numeric epoch, then build a UTC datetime"
    idiom duplicated across provider adapters: ``value`` is treated as
    seconds since the epoch, or milliseconds when ``millis=True``. Returns
    ``None`` instead of raising when ``value`` is non-numeric, non-finite
    (NaN/inf), non-positive, or too large/small for
    ``datetime.fromtimestamp`` to represent — mirroring the guarded-parse
    contract of ``bounded_int``/``bounded_percent`` in models.py and
    ``claude._parse_rfc3339``'s try/except around ``fromisoformat``.
    """
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if millis:
        number /= 1000
    if number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
