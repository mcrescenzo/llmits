"""Z.AI GLM Coding Plan quota adapter."""
from __future__ import annotations

import hashlib
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


def _unknown_key(label_text: str) -> str:
    """The single-label fallback key: ``other:{slug}``, or ``other`` when empty."""
    slug = _label_slug(label_text)
    return f"other:{slug}" if slug else "other"


def _label_digest(label_text: str) -> str:
    """A stable 12-hex-char digest of the *full* label, not its truncated slug.

    ``surrogatepass`` keeps lone-surrogate labels (JSON ``"\\ud800"``
    escapes) encodable instead of raising; the digest is hex-only either
    way, so it never needs sanitizing and never contains ``:`` or ``_``.
    """
    raw = label_text.encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()[:12]


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


def _kind_and_label(entry: dict) -> tuple[str, str]:
    """Extract the ``(kind token, free-text label)`` pair from one entry.

    The kind token is ``entry["rawType"]`` when that is a string (the shape
    every recorded fixture used before llmits-o03.11). Some accounts omit
    ``rawType`` entirely and instead put the same enum value directly in
    ``type`` (`TOKENS_LIMIT`, `CREDIT_LIMIT`, `TIME_LIMIT`); when ``rawType``
    is absent (or not a string) and ``type`` is exactly one of those three
    tokens, ``type`` is consumed as the kind token instead — but only as the
    kind token, never also as the free-text label/period hint. Any other
    shape (free-text ``type``, no recognizable kind at all) yields an empty
    kind token plus the (possibly empty) label text, which falls through to
    ``_window_kind``'s fallback branch.
    """
    raw = entry.get("rawType")
    type_value = entry.get("type")
    type_str = type_value if isinstance(type_value, str) else ""
    if isinstance(raw, str):
        return raw, type_str
    if type_str in _KIND_TOKENS:
        return type_str, ""
    # A non-string rawType never equals any kind token; treat it as an
    # unrecognized kind and keep only the (possibly empty) label text.
    return "", type_str


def _fallback_keys(entries: list[dict]) -> list[str | None]:
    """Unknown-kind fallback keys for ``entries``, index-aligned (``None`` = known kind).

    Uncontested labels keep the single-label key from ``_unknown_key``
    (``other:{slug}`` with a 20-char slug, or ``other`` when the label has
    no ``[a-z0-9]`` run), so already-unique keys such as
    ``other:weekly_token_budget`` never churn. Only when *distinct* labels
    collide on one such key — same first 20 slug characters, or both slugs
    empty — does each distinct label get that key plus a digest of the full
    label: ``other:{slug}:{digest}`` / ``other:{digest}``. Exact duplicate
    labels map to the same key, so parse_usage's de-dup still collapses
    them. If two distinct labels ever produced the same 12-hex digest
    prefix, a first-appearance ordinal (``_{n}``) keeps them apart — a
    digest never contains ``:`` or ``_``, so adjusted keys can collide with
    neither each other nor any single-label key. The longest possible key
    ("other:" + 20 + ":" + 12 + a short ordinal suffix) stays well under
    QuotaWindow's 64-char MAX_KEY, so sanitize_text can neither rewrite it
    nor truncate two keys into one. Keys depend only on the labels and
    their source order, never on dict/set iteration order.
    """
    records: list[tuple[str | None, str]] = []
    labels_by_key: dict[str, dict[str, None]] = {}
    for entry in entries:
        kind, label_text = _kind_and_label(entry)
        if kind in _KIND_TOKENS:
            records.append((None, ""))
            continue
        base = _unknown_key(label_text)
        records.append((base, label_text))
        # Dict keys double as an insertion-ordered set of distinct labels.
        labels_by_key.setdefault(base, {}).setdefault(label_text, None)
    adjusted: dict[str, dict[str, str]] = {}
    for base, distinct in labels_by_key.items():
        if len(distinct) < 2:
            continue
        taken: set[str] = set()
        mapping: dict[str, str] = {}
        for ordinal, label_text in enumerate(distinct, start=1):
            key = f"{base}:{_label_digest(label_text)}"
            if key in taken:
                # Only constructible deliberately (a 48-bit prefix match);
                # break the tie deterministically by first appearance.
                key = f"{key}_{ordinal}"
            taken.add(key)
            mapping[label_text] = key
        adjusted[base] = mapping
    return [
        None if base is None else adjusted.get(base, {}).get(label_text, base)
        for base, label_text in records
    ]


def _window_kind(entry: dict, fallback_key: str | None = None) -> tuple[str, str, int | None]:
    """Classify one ``limits[]`` entry into ``(key, label, period_seconds)``.

    The kind token and free-text label come from ``_kind_and_label``; the
    unit-based branches below then decide the period exactly as they always
    have for a ``rawType`` entry (unit 3 -> 5h, unit 6 -> weekly, unit 5 ->
    monthly). ``fallback_key`` is the payload-wide disambiguated fallback
    key from ``_fallback_keys`` (parse_usage passes it so distinct labels
    colliding on one fallback key keep distinct windows); ``None`` keeps
    the single-label key from ``_unknown_key``.
    """
    kind, label_text = _kind_and_label(entry)
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
    # of the label so differently-labeled unknown entries get distinct keys
    # while an exact repeat still collapses to one window. The slug alone
    # cannot keep that promise: it truncates to 20 characters and every
    # label without an [a-z0-9] run slugs to "", so distinct labels can
    # share one fallback key and parse_usage's de-dup would silently drop
    # the second window. parse_usage therefore pre-computes payload-wide
    # fallback keys (_fallback_keys) that append a digest of the full label
    # only when distinct labels actually collide, and passes the result in
    # as ``fallback_key``. The label itself is never the raw provider
    # string: it must pass the vetted_label allowlist, else it falls back
    # to the key's ASCII slug, else "quota".
    slug = _label_slug(label_text)
    key = fallback_key if fallback_key is not None else _unknown_key(label_text)
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

    entries = [entry for entry in limits if isinstance(entry, dict)]
    # Payload-wide fallback keys keep distinct colliding labels on distinct
    # windows (see _fallback_keys); known-kind entries get None and their
    # keys are untouched.
    fallback_keys = _fallback_keys(entries)
    windows: list[QuotaWindow] = []
    for entry, fallback_key in zip(entries, fallback_keys, strict=True):
        key, label, period = _window_kind(entry, fallback_key)
        usage = entry.get("usage")
        current = entry.get("currentValue")
        remaining = entry.get("remaining")
        used_value = limit_value = remaining_value = None
        percent_source = None
        if (
            isinstance(usage, (int, float))
            and not isinstance(usage, bool)
            and isinstance(current, (int, float))
            and not isinstance(current, bool)
        ):
            # bounded_int also rejects non-finite values (1e999) that would
            # otherwise raise OverflowError at int()/round().
            used_value, limit_value = bounded_int(current), bounded_int(usage)
            remaining_value = (
                bounded_int(remaining)
                if isinstance(remaining, (int, float)) and not isinstance(remaining, bool)
                else max(limit_value - used_value, 0)
            )
            if limit_value > 0:
                percent_source = used_value / limit_value * 100
        reset_at = common.epoch_to_datetime(entry.get("nextResetTime"), millis=True)
        if percent_source is None:
            percentage = entry.get("percentage")
            percent_source = (
                percentage
                if isinstance(percentage, (int, float)) and not isinstance(percentage, bool)
                else 0
            )
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
