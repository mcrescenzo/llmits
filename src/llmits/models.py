"""Immutable normalized result types shared by providers, service, and UIs.

Raw provider payloads never cross the provider-adapter seam; everything
downstream consumes these normalized types only. All provider-derived text
is sanitized at this boundary: C0/DEL/C1 control characters, Unicode format
characters (bidi overrides and isolates, zero-width joiners and spaces, BOM,
tag characters) and lone surrogates are stripped, whitespace is collapsed
to single spaces, and the result is length-capped.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProviderStatus(StrEnum):
    """The closed set of provider/error status codes.

    Members compare, format, and JSON-serialize as their plain string value
    (``ProviderStatus.AVAILABLE == "available"``), so every existing
    ``status == AVAILABLE`` comparison, f-string, and ``json.dumps`` call
    site keeps working unchanged. The module-level names below remain the
    stable import surface (``from .models import AVAILABLE``).
    """

    AVAILABLE = "available"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"


AVAILABLE = ProviderStatus.AVAILABLE
AUTH_REQUIRED = ProviderStatus.AUTH_REQUIRED
UNAVAILABLE = ProviderStatus.UNAVAILABLE
RATE_LIMITED = ProviderStatus.RATE_LIMITED
NETWORK_ERROR = ProviderStatus.NETWORK_ERROR
PARSE_ERROR = ProviderStatus.PARSE_ERROR

STATUSES = tuple(ProviderStatus)
ERROR_CODES = tuple(code for code in STATUSES if code != AVAILABLE)

MAX_TEXT = 200
MAX_KEY = 64
MAX_LABEL = 80
MAX_PLAN_NAME = 80
# C0 controls, DEL, and C1 controls (including the ESC used by terminal
# escape sequences) never survive into user-visible text.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Unicode general categories dropped outright (no replacement space):
# Cf (format) covers the bidi overrides/isolates (U+202A-U+202E,
# U+2066-U+2069), zero-width space/joiners and marks (U+200B-U+200F),
# BOM (U+FEFF) and the tag characters (U+E0000 block) that enable
# Trojan-Source-style visual spoofing and invisible-text smuggling in a
# terminal or a JSON consumer; Cs (lone surrogates, reachable through a
# JSON "\\ud800" escape) cannot be encoded for output at all.
_DROPPED_CATEGORIES = frozenset({"Cf", "Cs"})

# Conservative allowlist for provider-derived *display* labels (model names,
# quota type names): printable ASCII letters/digits/space and a few
# punctuation marks, starting with an alphanumeric, bounded length. Anything
# else is rejected by vetted_label() so the caller falls back to key-derived
# or fixed text instead of echoing raw provider strings.
MAX_VETTED_LABEL = 40
_LABEL_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+()/-]*")


def _drop_invisible(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) not in _DROPPED_CATEGORIES)


def sanitize_text(value, limit: int = MAX_TEXT) -> str:
    """Single-line, control-free, format-character-free, length-capped text.

    Every user-facing field passes through here. C0/DEL/C1 controls become a
    space (then collapse), Unicode format characters (category Cf: bidi
    controls, zero-width characters, BOM, tag characters) and lone surrogates
    (category Cs) are removed entirely, runs of whitespace collapse to one
    space, and the result is stripped and cut to ``limit`` characters.
    """
    text = _CONTROL_CHARS.sub(" ", str(value))
    text = _drop_invisible(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def vetted_label(value, limit: int = MAX_VETTED_LABEL) -> str | None:
    """Return a provider display string that passes the label allowlist, else None.

    The value is sanitized first (so a stray BOM or zero-width character does
    not disqualify an otherwise benign label) and must then be printable
    ASCII text matching ``_LABEL_SHAPE`` of at most ``limit`` characters.
    Provider adapters use this for any label built from raw provider text
    (Claude model display names, Z.AI quota type names) and fall back to a
    key-derived or fixed label when it returns None; the TUI reuses it for
    the same purpose.
    """
    if not isinstance(value, str):
        return None
    text = sanitize_text(value, limit + 1)
    if len(text) > limit or not _LABEL_SHAPE.fullmatch(text):
        return None
    return text


def _finite_number(value) -> float | None:
    """Coerce ``value`` to a finite float, or None.

    Shared preamble for ``bounded_percent`` and ``bounded_int``: both reject
    the same inputs (non-numeric, and numeric-but-non-finite like NaN or a
    JSON ``1e999``) the same way before diverging on how they clamp the
    result.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def bounded_percent(value) -> int:
    """Convert a provider percentage to a rounded int clamped to 0..100.

    Non-numeric, NaN, and infinite values (e.g. JSON ``1e999``) become 0
    instead of raising.
    """
    number = _finite_number(value)
    if number is None:
        return 0
    percent = round(number)
    if percent < 0:
        return 0
    if percent > 100:
        return 100
    return percent


def bounded_int(value) -> int:
    """Convert a provider count/metric to a clamped non-negative int.

    Non-numeric, NaN, and infinite values become 0 instead of raising
    (``round(inf)`` and ``int(inf)`` raise OverflowError otherwise).
    """
    number = _finite_number(value)
    if number is None:
        return 0
    return max(round(number), 0)


@dataclass(frozen=True)
class QuotaWindow:
    """One usage window normalized to a percentage plus optional raw values."""

    key: str
    label: str
    used_percent: int
    remaining_percent: int
    reset_at: datetime | None = None
    period_seconds: int | None = None
    used_value: int | None = None
    limit_value: int | None = None
    remaining_value: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", sanitize_text(self.key, MAX_KEY) or "window")
        object.__setattr__(self, "label", sanitize_text(self.label, MAX_LABEL) or self.key)

    @classmethod
    def from_percent(cls, *, key: str, label: str, percent, **fields) -> QuotaWindow:
        """Build a window from a raw percent value, deriving the complement.

        ``percent`` is passed through ``bounded_percent`` so callers can hand
        it an unvalidated provider value directly; ``remaining_percent`` is
        always set to the complement of the resulting ``used_percent``. This
        replaces the ``used = bounded_percent(x); remaining_percent = 100 -
        used`` idiom that was repeated at every adapter call site. Any other
        ``QuotaWindow`` field (``reset_at``, ``period_seconds``,
        ``used_value``, ...) passes straight through via ``fields``.
        """
        used = bounded_percent(percent)
        return cls(key=key, label=label, used_percent=used, remaining_percent=100 - used, **fields)


@dataclass(frozen=True)
class ProviderError:
    """A bounded, actionable error attached to a provider snapshot."""

    code: str
    message: str
    action: str = ""

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"invalid error code: {self.code!r}")
        object.__setattr__(self, "message", sanitize_text(self.message))
        object.__setattr__(self, "action", sanitize_text(self.action))


@dataclass(frozen=True)
class ProviderSnapshot:
    """The complete normalized state of one provider at one fetch attempt."""

    provider: str
    status: str
    plan_name: str | None
    fetched_at: datetime
    windows: tuple[QuotaWindow, ...] = ()
    stale: bool = False
    error: ProviderError | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid status: {self.status!r}")
        object.__setattr__(self, "windows", tuple(self.windows))
        if self.plan_name is not None:
            object.__setattr__(
                self, "plan_name", sanitize_text(self.plan_name, MAX_PLAN_NAME) or None
            )
