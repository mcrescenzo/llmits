"""Codex / ChatGPT subscription usage adapter."""
from __future__ import annotations

from datetime import datetime

from ..http import TransportError
from ..models import AVAILABLE, ProviderSnapshot, QuotaWindow, bounded_int
from . import common

ID = "codex"
HOST = "chatgpt.com"
PATH = "/backend-api/wham/usage"
RELOGIN_ACTION = "log in again with the Codex CLI (codex login)"


def request_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": common.USER_AGENT,
    }


def _duration_label(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "window"
    seconds = int(seconds)
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


# Spec section 3 names exactly one additional_rate_limits prefix explicitly
# ("spk/7d" -> "Spark"). Raw upstream limit names are treated as untrusted
# text: a name is *detected* against this table (compared for equality
# after lowering and dropping non-alphanumerics) and never incorporated
# into a key, prefix, or label. Any unrecognized name — including a second
# distinct upstream limit that collides with an already-emitted key — gets
# a collision-safe ordinal prefix ("x1", "x2", ... -> "Limit 1",
# "Limit 2", ...) that contains no provider-derived characters.
_PREFIX_LABELS = {"spk": "Spark"}


def _known_prefix(name: str) -> str | None:
    """Fixed key prefix for a recognized upstream limit name, else None."""
    tail = "".join(char for char in name.rsplit("-", 1)[-1].lower() if char.isalnum())
    return "spk" if tail == "spark" else None


def _windows_from_rate_limit(rate_limit, prefix: str, label: str) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    if not isinstance(rate_limit, dict):
        return windows
    for field, default_period in (
        ("primary_window", 18000),
        ("secondary_window", 604800),
    ):
        entry = rate_limit.get(field)
        if not isinstance(entry, dict):
            continue
        used = entry.get("used_percent")
        if not isinstance(used, (int, float)):
            continue
        period = entry.get("limit_window_seconds")
        period_int = bounded_int(period) if isinstance(period, (int, float)) else 0
        period_seconds = period_int if period_int > 0 else default_period
        base = _duration_label(period_seconds)
        key = f"{prefix}/{base}" if prefix else base
        window_label = label if prefix else base
        reset_at = common.epoch_to_datetime(entry.get("reset_at"))
        windows.append(
            QuotaWindow.from_percent(
                key=key,
                label=window_label,
                percent=used,
                reset_at=reset_at,
                period_seconds=period_seconds,
            )
        )
    return windows


def _is_floatable(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def parse_usage(payload: dict) -> tuple[str, tuple[QuotaWindow, ...]]:
    """Return (plan_name, windows).

    An account is treated as unlimited-credits only on a strict ``True``
    (never a truthy string like ``"false"``, which is not unlimited); when
    unlimited, no credit-balance window is emitted regardless of balance.
    """
    plan_type = common.vetted_plan_value(payload.get("plan_type"))
    plan_name = f"Codex {plan_type}" if plan_type else "Codex"

    credits = payload.get("credits")
    unlimited = isinstance(credits, dict) and credits.get("unlimited") is True

    windows: list[QuotaWindow] = []
    seen: set[str] = set()

    def push_all(new_windows) -> None:
        for window in new_windows:
            if window.key not in seen:
                seen.add(window.key)
                windows.append(window)

    push_all(_windows_from_rate_limit(payload.get("rate_limit"), "", ""))

    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        next_ordinal = 0
        # raw name -> (prefix, label) already handed out in this payload, so
        # an exact repeat of a limit_name reuses its window (and dedupes)
        # instead of allocating a fresh ordinal. This is local bookkeeping
        # only; the raw name itself is never emitted anywhere.
        allocated: dict[str, tuple[str, str]] = {}
        for entry in additional:
            if not isinstance(entry, dict):
                continue
            name = entry.get("limit_name") or entry.get("metered_feature")
            name_str = name if isinstance(name, str) else ""
            if not name_str:
                prefix, label = "limit", "Limit"
            elif name_str in allocated:
                # Exact repeat of a seen name: reuse its allocation;
                # push_all below dedupes the identical key.
                prefix, label = allocated[name_str]
                push_all(_windows_from_rate_limit(entry.get("rate_limit"), prefix, label))
                continue
            else:
                known = _known_prefix(name_str)
                if known is not None:
                    prefix, label = known, _PREFIX_LABELS[known]
                else:
                    next_ordinal += 1
                    prefix, label = f"x{next_ordinal}", f"Limit {next_ordinal}"
            candidate = _windows_from_rate_limit(entry.get("rate_limit"), prefix, label)
            if name_str and any(window.key in seen for window in candidate):
                # A first-seen limit whose fixed prefix already collides
                # with an emitted key (e.g. two distinct upstream limits
                # both ending in "-Spark"): reallocate with the next
                # collision-safe ordinal prefix instead of deriving
                # anything from the raw upstream name, so unrelated limits
                # never collapse into the same window.
                next_ordinal += 1
                prefix = f"x{next_ordinal}"
                label = f"Limit {next_ordinal}"
                candidate = _windows_from_rate_limit(entry.get("rate_limit"), prefix, label)
            if name_str:
                allocated[name_str] = (prefix, label)
            push_all(candidate)

    if isinstance(credits, dict) and not unlimited:
        balance = credits.get("balance")
        if isinstance(balance, str):
            cents = bounded_int(float(balance) * 100) if _is_floatable(balance) else 0
            if cents > 0:
                windows.append(
                    QuotaWindow(
                        key="credits",
                        label="credits",
                        used_percent=0,
                        remaining_percent=100,
                        remaining_value=cents,
                    )
                )

    resets = payload.get("rate_limit_reset_credits")
    if isinstance(resets, dict):
        count = resets.get("available_count")
        # bool is an int subclass; exclude it explicitly so a stray
        # ``true``/``false`` never leaks into the int|null JSON field.
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            windows.append(
                QuotaWindow(
                    key="reset_credits",
                    label="banked",
                    used_percent=0,
                    remaining_percent=100,
                    remaining_value=count,
                )
            )

    return plan_name, tuple(windows)


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
    payload = common.decode_json_object(ID, "Codex", response.body, now)
    if isinstance(payload, ProviderSnapshot):
        return payload
    has_signal = any(
        key in payload for key in ("plan_type", "rate_limit", "credits", "rate_limit_reset_credits")
    )
    if not has_signal:
        return common.parse_error_snapshot(ID, "no usage data in Codex response", now)
    plan_name, windows = parse_usage(payload)
    if not windows:
        return common.parse_error_snapshot(ID, "no usage data in Codex response", now)
    return ProviderSnapshot(
        provider=ID,
        status=AVAILABLE,
        plan_name=plan_name,
        fetched_at=now,
        windows=windows,
    )
