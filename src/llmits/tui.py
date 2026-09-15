"""Terminal UI: pure rendering helpers plus a curses event loop.

The rendering layer is pure (no curses dependency) so tests can verify
layout at any terminal size. The controller owns refresh scheduling state
with all network work happening off the curses loop thread.
"""
from __future__ import annotations

import contextlib
import curses
import locale
import sys
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import __version__
from .models import (
    AUTH_REQUIRED,
    AVAILABLE,
    NETWORK_ERROR,
    PARSE_ERROR,
    RATE_LIMITED,
    UNAVAILABLE,
    ProviderSnapshot,
    QuotaWindow,
)
from .providers import display_name
from .service import RefreshService

MIN_COLS = 60
MIN_ROWS = 15
COMPACT_COLS = 80
WARN_PERCENT = 70
HIGH_PERCENT = 90
LABEL_MIN = 5
LABEL_MAX = 12
BAR_MIN = 10
BAR_MAX = 40
# ncurses waits this long after a lone ESC before reporting it (default 1000 ms).
ESC_DELAY_MS = 25

STYLE_NORMAL = "normal"
STYLE_DIM = "dim"
STYLE_ACCENT = "accent"
STYLE_OK = "ok"
STYLE_WARN = "warn"
STYLE_HIGH = "high"
STYLE_TRACK = "track"


@dataclass(frozen=True)
class Segment:
    text: str
    style: str = STYLE_NORMAL


@dataclass(frozen=True)
class Glyphs:
    """One glyph set: unicode line-drawing, or a plain-ASCII fallback.

    ``run_tui`` picks between the two based on the stdout encoding; the
    pure renderer below never decides this itself, it only consumes
    whichever set it is given.
    """

    fill: str
    track: str
    dot: str
    hollow: str
    sep: str
    ellipsis: str
    dash: str


UNICODE_GLYPHS = Glyphs(fill="━", track="─", dot="●", hollow="○", sep="·", ellipsis="…", dash="—")
ASCII_GLYPHS = Glyphs(fill="=", track="-", dot="*", hollow="o", sep="-", ellipsis="...", dash="-")


@dataclass(frozen=True)
class TuiView:
    provider_ids: tuple[str, ...]
    snapshots: tuple[ProviderSnapshot, ...] | None
    loading: bool
    last_refresh: datetime | None
    refresh_seconds: int
    version: str
    now: datetime
    last_attempt: datetime | None = None
    scroll: int = 0
    last_error: str | None = None
    # Session-local card state: providers hidden with ``h`` (excluded from
    # rendering and from ``r`` refreshes), providers collapsed with ``c``
    # (header-only cards), the focused provider id (``tab``), and whether
    # the next render should bring the focused card on screen. All default
    # to "nothing hidden/collapsed/focused" so older callers are unchanged.
    hidden: frozenset[str] = frozenset()
    collapsed: frozenset[str] = frozenset()
    focus: str | None = None
    reveal_focus: bool = False


@dataclass(frozen=True)
class Frame:
    """One rendered screen: the visible lines plus how far scroll can go.

    Behaves like the ``list[list[Segment]]`` ``render()`` used to return —
    iterable, sized, indexable and sliceable over ``lines`` — so a caller
    that only ever consumed the lines (``_draw``, ``tests/test_security.py``)
    keeps working unchanged; new callers use ``.lines``, ``.max_scroll``,
    and ``.offset`` explicitly.
    """

    lines: list[list[Segment]]
    max_scroll: int
    # The scroll offset this frame actually applied: the clamped
    # ``view.scroll``, nudged when ``view.reveal_focus`` pulled the focused
    # card's header into view. Callers that own scroll state should adopt
    # this after drawing so their state matches what is on screen.
    offset: int = 0

    def __iter__(self):
        return iter(self.lines)

    def __len__(self):
        return len(self.lines)

    def __getitem__(self, index):
        return self.lines[index]


def _format_age(now: datetime, then: datetime) -> str:
    """``Ns ago`` / ``Nm ago`` / ``Nh ago`` / ``Nd ago``.

    Returns the full phrase; callers must not append a second "ago".
    Negative deltas (clock skew) clamp to 0 rather than going negative.
    """
    total = max(0, int((now - then).total_seconds()))
    if total < 60:
        return f"{total}s ago"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _countdown(total_seconds: int) -> str:
    """``{d}d {h}h`` / ``{h}h {m}m`` / ``{m}m`` for a positive duration."""
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _reset_text(reset_at: datetime | None, now: datetime, wide: bool, glyphs: Glyphs) -> str:
    """Empty when unset, "resets now" at/after the instant, else a countdown.

    ``wide`` appends the local wall-clock time (``%H:%M`` today, else
    ``{month} {day} {%H:%M}`` with no zero-padded day) — never for "resets
    now", since there is no useful clock to show for an instant that has
    already passed.
    """
    if reset_at is None:
        return ""
    total = int((reset_at - now).total_seconds())
    if total <= 0:
        return "resets now"
    text = f"resets {_countdown(total)}"
    if wide:
        local = reset_at.astimezone()
        if local.date() == now.astimezone().date():
            clock = local.strftime("%H:%M")
        else:
            clock = f"{local.strftime('%b')} {local.day} {local.strftime('%H:%M')}"
        text += f" {glyphs.sep} {clock}"
    return text


def _value_note(window: QuotaWindow) -> str:
    # "credits" is codex.py's credit-balance window key specifically: its
    # remaining_value is pre-multiplied by 100 (cents), which is what the
    # division below undoes. zai.py's own fallback keys are "credits_other"
    # and "other:<slug>" (raw unit counts, not cents) and must never be
    # renamed to collide with this key.
    if window.key == "credits" and window.remaining_value is not None:
        return f"${window.remaining_value / 100:.2f} balance"
    if window.key == "reset_credits" and window.remaining_value is not None:
        return f"{window.remaining_value} available"
    if window.used_value is not None and window.limit_value:
        return f"{window.used_value:,}/{window.limit_value:,}"
    return ""


def _is_value_only(window: QuotaWindow) -> bool:
    """True for a window with only a remaining count: no bar, no percent."""
    return (
        window.reset_at is None
        and window.limit_value is None
        and window.used_value is None
        and window.remaining_value is not None
    )


def _meta_text(window: QuotaWindow, now: datetime, compact: bool, glyphs: Glyphs) -> str:
    """The DIM trailer on a percent row: mode-dependent join of note/reset."""
    reset = _reset_text(window.reset_at, now, wide=not compact, glyphs=glyphs)
    if compact:
        return reset
    note = _value_note(window)
    return f" {glyphs.sep} ".join(part for part in (note, reset) if part)


def _threshold_style(used_percent: int) -> str:
    if used_percent >= HIGH_PERCENT:
        return STYLE_HIGH
    if used_percent >= WARN_PERCENT:
        return STYLE_WARN
    return STYLE_OK


def _rendered_windows(snapshots):
    """Windows from the cards that actually show them: available or stale."""
    for snapshot in snapshots:
        if snapshot.status == AVAILABLE or snapshot.stale:
            yield from snapshot.windows


def _label_width(snapshots) -> int:
    """``L``: clamp(longest label in the view, 5, 12), shared by every row."""
    longest = max((len(window.label) for window in _rendered_windows(snapshots)), default=0)
    return max(LABEL_MIN, min(LABEL_MAX, longest))


def _max_meta_len(snapshots, now: datetime, compact: bool, glyphs: Glyphs) -> int:
    lengths = [
        len(_meta_text(window, now, compact, glyphs))
        for window in _rendered_windows(snapshots)
        if not _is_value_only(window)
    ]
    return max(lengths, default=0)


def _bar_width(width: int, label_width: int, max_meta_len: int) -> int:
    """``W``: the one bar width every percent row in the frame shares."""
    fixed = label_width + 11  # indent, label, gap, gap, " NNN%", gap
    raw = width - 1 - fixed - max_meta_len
    return max(BAR_MIN, min(BAR_MAX, raw))


def _window_line(
    window: QuotaWindow,
    glyphs: Glyphs,
    now: datetime,
    label_width: int,
    bar_width: int,
    compact: bool,
) -> list[Segment]:
    # The label is DIM: it is metadata identifying the row, not the data
    # itself (the bar/percent/value is what the eye should land on first).
    label_segment = Segment(f"  {window.label[:label_width]:<{label_width}} ", STYLE_DIM)
    if _is_value_only(window):
        return [label_segment, Segment(_value_note(window), STYLE_NORMAL)]
    style = _threshold_style(window.used_percent)
    filled = max(0, min(bar_width, round(window.used_percent / 100 * bar_width)))
    segments = [
        label_segment,
        Segment(glyphs.fill * filled, style),
        Segment(glyphs.track * (bar_width - filled), STYLE_TRACK),
        Segment(f"  {window.used_percent:>3}%", style),
    ]
    meta = _meta_text(window, now, compact, glyphs)
    if meta:
        segments.append(Segment(f"  {meta}", STYLE_DIM))
    return segments


# Status -> status-group word (HIGH-styled, dot + word) for every non-stale,
# non-available status. A table, not an if/elif chain, over the closed set
# of error codes in models.STATUSES. The ProviderStatus members are StrEnum
# values (plain str subclasses), so the table is typed dict[str, str] and a
# snapshot's `status: str` field (models.ProviderSnapshot) looks up directly.
_STATUS_WORDS: dict[str, str] = {
    AUTH_REQUIRED: "auth required",
    RATE_LIMITED: "rate limited",
    NETWORK_ERROR: "network error",
    PARSE_ERROR: "response changed",
    UNAVAILABLE: "unavailable",
}

# Status -> the card's HIGH-styled message line, same closed set as above.
_STATUS_MESSAGES: dict[str, str] = {
    AUTH_REQUIRED: "  credentials missing or rejected",
    RATE_LIMITED: "  rate limited by provider",
    NETWORK_ERROR: "  network problem",
    PARSE_ERROR: "  provider response changed",
    UNAVAILABLE: "  unavailable",
}


def _status_segments(snapshot: ProviderSnapshot, glyphs: Glyphs, now: datetime) -> list[Segment]:
    """The card header's status group: dot plus whatever spec §2.1 pairs with it.

    Healthy cards deliberately carry no status word (dot + age is the
    signal); every non-healthy state carries a word so a monochrome
    terminal still reads correctly.
    """
    if snapshot.stale:
        return [
            Segment(f"{glyphs.dot} stale", STYLE_WARN),
            Segment(f" {glyphs.sep} {_format_age(now, snapshot.fetched_at)}", STYLE_DIM),
        ]
    if snapshot.status == AVAILABLE:
        return [
            Segment(glyphs.dot, STYLE_OK),
            Segment(f" {_format_age(now, snapshot.fetched_at)}", STYLE_DIM),
        ]
    word = _STATUS_WORDS.get(snapshot.status, _STATUS_WORDS[UNAVAILABLE])
    return [Segment(f"{glyphs.dot} {word}", STYLE_HIGH)]


def _card_header(
    name: str, plan_name: str | None, status_segments: list[Segment], *, focused: bool = False
) -> list[Segment]:
    """``{> }{Name}  [{plan}]  {status group}``; the bracket is omitted when plan_name is None.

    The focused card's plain-ASCII ``"> "`` prefix is a session-local marker
    (never provider-derived text), so it survives ``ASCII_GLYPHS`` mode and
    the codepoint-clipping below unchanged.
    """
    segments = [Segment("> ", STYLE_ACCENT)] if focused else []
    segments.append(Segment(name, STYLE_ACCENT))
    if plan_name:
        segments.append(Segment(f"  [{plan_name}]", STYLE_DIM))
    segments.append(Segment("  "))
    segments.extend(status_segments)
    return segments


def _card_lines(
    snapshot: ProviderSnapshot,
    glyphs: Glyphs,
    now: datetime,
    label_width: int,
    bar_width: int,
    compact: bool,
    *,
    collapsed: bool = False,
    focused: bool = False,
) -> list[list[Segment]]:
    name = display_name(snapshot.provider)
    lines = [
        _card_header(
            name, snapshot.plan_name, _status_segments(snapshot, glyphs, now), focused=focused
        )
    ]
    if collapsed:
        # A collapsed card keeps its header (status still readable) but
        # shows no window or error rows.
        return lines

    if snapshot.status == AVAILABLE or snapshot.stale:
        for window in snapshot.windows:
            lines.append(_window_line(window, glyphs, now, label_width, bar_width, compact))
        if snapshot.stale and snapshot.error is not None:
            detail = snapshot.error.message
            if snapshot.error.action:
                detail += f" {glyphs.dash} {snapshot.error.action}"
            lines.append([Segment(f"  last update failed: {detail}", STYLE_WARN)])
        return lines

    message = _STATUS_MESSAGES.get(snapshot.status, _STATUS_MESSAGES[UNAVAILABLE])
    lines.append([Segment(message, STYLE_HIGH)])
    if snapshot.error is not None:
        detail = snapshot.error.message
        if snapshot.error.action:
            detail += f" {glyphs.dash} {snapshot.error.action}"
        lines.append([Segment(f"  {detail}", STYLE_DIM)])
    return lines


def _join_wide(parts, glyphs: Glyphs) -> str:
    """Header/footer separator: two spaces, the glyph, two spaces."""
    return f"  {glyphs.sep}  ".join(parts)


def _header_text(view: TuiView, glyphs: Glyphs) -> str:
    """Row 0: one of the five variants in spec §1, chosen from loading/last_refresh."""
    parts = [f"llmits v{view.version}"]
    if view.last_refresh is not None:
        parts.append(f"updated {_format_age(view.now, view.last_refresh)}")
        if view.loading:
            parts.append(f"refreshing{glyphs.ellipsis}")
        elif view.refresh_seconds > 0:
            attempt_at = view.last_attempt or view.last_refresh
            next_at = attempt_at + timedelta(seconds=view.refresh_seconds)
            remaining = int((next_at - view.now).total_seconds())
            if remaining > 0:
                minutes, seconds = divmod(remaining, 60)
                parts.append(f"next refresh {minutes}:{seconds:02d}")
        else:
            parts.append("auto-refresh off")
    elif view.loading:
        parts.append(f"refreshing{glyphs.ellipsis}")
    return _join_wide(parts, glyphs)


def _footer_text(view: TuiView, glyphs: Glyphs, *, scrollable: bool) -> str:
    """Row H-1, pinned: only currently actionable key hints.

    ``j/k scroll`` is omitted when the card stack fits in the viewport, so
    the footer never promises movement that cannot occur. The newest keys
    go last so a narrow terminal clips them — not ``q/esc quit`` — while
    80 columns still show through ``c collapse``.
    """
    parts = []
    if view.loading:
        parts.append(f"refreshing{glyphs.ellipsis}")
    parts.append("r/R refresh")
    if scrollable:
        parts.append("j/k scroll")
    parts.extend(
        [
            "q/esc quit",
            "tab focus",
            "h hide",
            "c collapse",
            "a show all",
        ]
    )
    return _join_wide(parts, glyphs)


def _clip(segments: list[Segment], limit: int) -> list[Segment]:
    """Trim a line to at most ``limit`` codepoints, cutting the last segment.

    Plain codepoint counting (one codepoint = one column): a double-width
    CJK or emoji label can still overflow a real terminal by up to 2x, but
    this never raises and always keeps ``sum(len(s.text)) <= limit`` — the
    safety property this renderer promises.
    """
    if limit <= 0:
        return []
    out: list[Segment] = []
    used = 0
    for segment in segments:
        remaining = limit - used
        if remaining <= 0:
            break
        if len(segment.text) <= remaining:
            out.append(segment)
            used += len(segment.text)
        else:
            out.append(Segment(segment.text[:remaining], segment.style))
            break
    return out


def render(view: TuiView, width: int, height: int, glyphs: Glyphs = UNICODE_GLYPHS) -> Frame:
    """Render one frame; never raises regardless of terminal size or content.

    Row 0 is the DIM header, row 1 is blank, the footer is pinned to the
    last row, and everything between is the scrollable card stack — sliced
    by the clamped ``view.scroll`` offset so every provider stays reachable
    at the documented 60x15 minimum. Hidden providers (``view.hidden``)
    drop out of the stack entirely; collapsed ones (``view.collapsed``)
    keep only their header; the focused provider's header carries the
    ``"> "`` marker. When ``view.reveal_focus`` is set, the applied offset
    is nudged so the focused card's header lands inside the viewport.
    Every returned line is clipped to ``width - 1`` columns; the returned
    ``Frame`` also carries ``max_scroll`` and the effective ``offset`` so a
    caller can keep its own scroll state in sync with what was drawn.
    """
    limit = max(0, width - 1)
    if width < MIN_COLS or height < MIN_ROWS:
        message = f"terminal too small: need {MIN_COLS}x{MIN_ROWS}, have {width}x{height}"
        return Frame(lines=[_clip([Segment(message, STYLE_HIGH)], limit)], max_scroll=0)

    compact = width <= COMPACT_COLS
    visible_ids = tuple(pid for pid in view.provider_ids if pid not in view.hidden)
    focus_row: int | None = None
    content: list[list[Segment]] = []
    if view.last_error is not None:
        content.append([Segment(view.last_error, STYLE_HIGH)])
        content.append([])
    if not visible_ids:
        # Fixed local text: provider ids are compile-time constants, so no
        # provider-derived string can reach this line.
        content.append(
            [Segment(f"all providers hidden {glyphs.sep} press a to restore", STYLE_DIM)]
        )
    elif view.snapshots is None:
        for provider_id in visible_ids:
            state = f"fetching{glyphs.ellipsis}" if view.loading else "no data"
            status = [Segment(f"{glyphs.hollow} {state}", STYLE_DIM)]
            focused = provider_id == view.focus
            if focused:
                focus_row = len(content)
            content.append(_card_header(display_name(provider_id), None, status, focused=focused))
            content.append([])
    else:
        visible_snapshots = tuple(s for s in view.snapshots if s.provider not in view.hidden)
        label_width = _label_width(visible_snapshots)
        max_meta_len = _max_meta_len(visible_snapshots, view.now, compact, glyphs)
        bar_width = _bar_width(width, label_width, max_meta_len)
        for snapshot in visible_snapshots:
            focused = snapshot.provider == view.focus
            if focused:
                focus_row = len(content)
            content.extend(
                _card_lines(
                    snapshot,
                    glyphs,
                    view.now,
                    label_width,
                    bar_width,
                    compact,
                    collapsed=snapshot.provider in view.collapsed,
                    focused=focused,
                )
            )
            content.append([])

    body_height = max(0, height - 3)
    max_scroll = max(0, len(content) - body_height)
    offset = min(max(view.scroll, 0), max_scroll)
    if view.reveal_focus and focus_row is not None and body_height > 0:
        if focus_row < offset:
            offset = focus_row
        elif focus_row >= offset + body_height:
            offset = min(max(0, focus_row - body_height + 1), max_scroll)
    visible = content[offset : offset + body_height]

    lines = [_clip([Segment(_header_text(view, glyphs), STYLE_DIM)], limit), []]
    lines.extend(_clip(line, limit) for line in visible)
    while len(lines) < height - 1:
        lines.append([])
    lines.append(
        _clip(
            [Segment(_footer_text(view, glyphs, scrollable=max_scroll > 0), STYLE_DIM)],
            limit,
        )
    )
    return Frame(lines=lines, max_scroll=max_scroll, offset=offset)


class AppController:
    """Refresh scheduling state machine; owns no curses objects.

    Refresh work runs off the curses loop thread. Each refresh spawns its
    own short-lived daemon ``threading.Thread`` rather than reusing a
    pooled ``concurrent.futures.ThreadPoolExecutor``: that pool registers
    every worker thread with an interpreter-exit hook that joins it
    unconditionally, even when the thread is marked daemon, which would
    keep the process alive until any in-flight HTTP call returns (up to
    the transport's timeout). A plain daemon thread carries no such hook,
    so ``close()`` can abandon an in-flight refresh and let the
    interpreter exit immediately instead of waiting for it to finish.
    """

    def __init__(self, service: RefreshService, refresh_seconds: int, now_fn=None):
        self._service = service
        self.refresh_seconds = refresh_seconds
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._future: Future | None = None
        self._refresh_ids: tuple[str, ...] = ()  # ids the in-flight refresh covers
        self._restore_pending = False  # `a` pressed while a refresh was in flight
        self._deferred_refresh_ids: tuple[str, ...] | None = None
        self.snapshots: tuple[ProviderSnapshot, ...] | None = None
        self.loading = False
        self.last_refresh: datetime | None = None
        self.last_attempt: datetime | None = None
        self.last_error: str | None = None
        self.quit = False
        self.scroll = 0
        self._scroll_cap = 512
        # Session-local card state; mutated only on the curses loop thread,
        # like scroll, so no locking is needed.
        self.hidden: set[str] = set()
        self.collapsed: set[str] = set()
        self.focus: str | None = self.provider_ids[0]
        self._reveal_focus = False

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return self._service.provider_ids

    @property
    def visible_ids(self) -> tuple[str, ...]:
        """Configured provider ids in order, minus those hidden with ``h``."""
        return tuple(pid for pid in self.provider_ids if pid not in self.hidden)

    def start(self) -> None:
        self._submit(self.visible_ids)

    def request_refresh(self) -> None:
        """Refresh every visible (non-hidden) provider."""
        self._submit(self.visible_ids)

    def request_focused_refresh(self) -> None:
        """Refresh only the focused provider (``R``)."""
        if self.focus is not None and self.focus in self.visible_ids:
            self._submit((self.focus,))

    def cycle_focus(self, backward: bool = False) -> None:
        """Move focus to the next (or previous) visible provider, wrapping."""
        visible = self.visible_ids
        if not visible:
            return
        if self.focus not in visible:
            self.focus = visible[-1] if backward else visible[0]
        else:
            index = visible.index(self.focus)
            step = -1 if backward else 1
            self.focus = visible[(index + step) % len(visible)]
        self._reveal_focus = True

    def hide_focused(self) -> None:
        """Hide the focused card (``h``); focus moves to the next visible one."""
        visible = self.visible_ids
        if self.focus is None or self.focus not in visible:
            return
        index = visible.index(self.focus)
        self.hidden.add(self.focus)
        remaining = self.visible_ids
        if not remaining:
            self.focus = None
        else:
            self.focus = remaining[min(index, len(remaining) - 1)]
            self._reveal_focus = True

    def show_all(self) -> None:
        """Restore every hidden and collapsed card (``a``) and refresh them all.

        Unlike ``r``/``R``, the post-restore full refresh is never silently
        dropped while another refresh is in flight: it is deferred and
        submitted as soon as ``poll()`` adopts the in-flight one, so every
        restored provider really is refreshed.
        """
        self.hidden.clear()
        self.collapsed.clear()
        ids = self.visible_ids
        if self.focus not in ids:
            self.focus = ids[0] if ids else None
            self._reveal_focus = self.focus is not None
        if not ids:
            return
        if self._future is not None:
            self._restore_pending = True
            return
        self._submit(ids)

    def toggle_collapse_focused(self) -> None:
        """Collapse/expand the focused card to/from a header-only card (``c``)."""
        if self.focus is not None and self.focus in self.visible_ids:
            if self.focus in self.collapsed:
                self.collapsed.discard(self.focus)
            else:
                self.collapsed.add(self.focus)

    def _submit(self, provider_ids) -> None:
        ids = tuple(provider_ids)
        if not ids:
            return  # nothing rendered -> nothing to refresh
        if self._future is not None:
            if self._future.done():
                # Preserve a keypress that lands after completion but before
                # the curses loop has polled and adopted the result.
                self._deferred_refresh_ids = ids
            return
        self.loading = True
        self._refresh_ids = ids
        future: Future = Future()
        self._future = future
        threading.Thread(
            target=self._run_refresh,
            args=(future, ids),
            name="llmits-tui-refresh",
            daemon=True,
        ).start()

    def _run_refresh(self, future: Future, provider_ids) -> None:
        """Run one refresh on a daemon thread and resolve ``future`` with it.

        Never lets an exception escape the thread: any failure from
        ``RefreshService.refresh()`` is attached to ``future`` via
        ``set_exception`` so ``poll()`` observes and surfaces it instead of
        it being silently lost off the curses loop thread.
        """
        try:
            result = self._service.refresh(provider_ids)
        except Exception as exc:  # defensive: a refresh failure must not crash the app
            future.set_exception(exc)
        else:
            future.set_result(result)

    def poll(self, now: datetime | None = None) -> None:
        now = now or self._now_fn()
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            covered, self._refresh_ids = self._refresh_ids, ()
            deferred, self._deferred_refresh_ids = self._deferred_refresh_ids, None
            self.loading = False
            self.last_attempt = now
            try:
                snapshots = future.result()
            except Exception as exc:
                # Surface the failure rather than swallowing it: previously
                # adopted snapshots (if any) are left untouched so the last
                # good data stays on screen.
                self.last_error = f"refresh failed: {type(exc).__name__}"
            else:
                # Merge, not replace: a subset refresh (``R`` refreshes only
                # the focused provider) returns snapshots for exactly the
                # ids it covered, so every other provider keeps its last
                # adopted snapshot and the merged list stays in the
                # configured provider order.
                covered_set = set(covered)
                fresh = {snapshot.provider: snapshot for snapshot in snapshots}
                previous = {snapshot.provider: snapshot for snapshot in self.snapshots or ()}
                merged = []
                for provider_id in self.provider_ids:
                    if provider_id in fresh:
                        merged.append(fresh[provider_id])
                    elif provider_id in previous and provider_id not in covered_set:
                        merged.append(previous[provider_id])
                self.snapshots = tuple(merged)
                self.last_refresh = now
                self.last_error = None
            restore, self._restore_pending = self._restore_pending, False
            if restore:
                # A restore pressed while this refresh was in flight: its
                # full refresh was deferred, not dropped — submit it now.
                self._submit(self.visible_ids)
            elif deferred is not None:
                self._submit(deferred)
        if (
            not self.loading
            and self.refresh_seconds > 0
            and self.last_attempt is not None
            and now >= self.last_attempt + timedelta(seconds=self.refresh_seconds)
        ):
            self._submit(self.visible_ids)

    def handle_key(self, key: int) -> None:
        if key in (ord("q"), ord("Q"), 27):
            self.quit = True
        elif key in (ord("\t"), curses.KEY_BTAB):
            self.cycle_focus(backward=key == curses.KEY_BTAB)
        elif key == ord("r"):
            self.request_refresh()
        elif key == ord("R"):
            self.request_focused_refresh()
        elif key in (ord("h"), ord("H")):
            self.hide_focused()
        elif key in (ord("a"), ord("A")):
            self.show_all()
        elif key in (ord("c"), ord("C")):
            self.toggle_collapse_focused()
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scroll = min(self.scroll + 1, self._scroll_cap)
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll = max(self.scroll - 1, 0)

    def view(self, now: datetime | None = None) -> TuiView:
        """Build the next render input, consuming the pending reveal request.

        ``reveal_focus`` is one-shot: the first ``view()`` after a focus move
        asks ``render`` to bring the focused card on screen, later views do
        not, so manual scrolling stays free once the focus is visible.
        """
        now = now or self._now_fn()
        reveal = self._reveal_focus
        self._reveal_focus = False
        return TuiView(
            provider_ids=self.provider_ids,
            snapshots=self.snapshots,
            loading=self.loading,
            last_refresh=self.last_refresh,
            refresh_seconds=self.refresh_seconds,
            version=__version__,
            now=now,
            last_attempt=self.last_attempt,
            scroll=self.scroll,
            last_error=self.last_error,
            hidden=frozenset(self.hidden),
            collapsed=frozenset(self.collapsed),
            focus=self.focus,
            reveal_focus=reveal,
        )

    def close(self) -> None:
        """Abandon any in-flight refresh; never waits for it to finish.

        There is no worker pool to shut down — each refresh runs on its own
        short-lived daemon thread (see ``_run_refresh``) — so this only
        drops the controller's reference to the in-flight future. The
        thread itself keeps running until the HTTP call returns, but being
        daemon it cannot delay interpreter exit; see the class docstring.
        """
        self._future = None
        self._deferred_refresh_ids = None
        self._restore_pending = False


def _terminal_codeset(stream=None) -> str | None:
    """The codeset ncurses draws under, else the stream's declared encoding.

    ``cli._run_tui`` calls ``locale.setlocale(LC_ALL, "")`` before curses, so
    ``locale.nl_langinfo(locale.CODESET)`` reports the C-library locale that
    ncurses uses for character widths. ``sys.stdout.encoding`` is NOT that
    signal: CPython's UTF-8 mode (PEP 540) reports ``utf-8`` for the C locale
    even when ``LC_ALL=C`` blocked PEP 538 coercion and the C locale is still
    ASCII, and drawing Unicode glyphs there corrupts the screen. The stream
    encoding is only the fallback for platforms without ``nl_langinfo``.
    """
    nl_langinfo = getattr(locale, "nl_langinfo", None)
    codeset = getattr(locale, "CODESET", None)
    if nl_langinfo is not None and codeset is not None:
        try:
            value = nl_langinfo(codeset)
        except (ValueError, TypeError, OSError):
            value = None
        if value:
            return value
    stream = sys.stdout if stream is None else stream
    return getattr(stream, "encoding", None)


def _select_glyphs(encoding: str | None) -> Glyphs:
    """UNICODE_GLYPHS when ``encoding`` names UTF-8, else ASCII_GLYPHS.

    Matched case-insensitively, with or without the hyphen/underscore
    (``'utf-8'``, ``'UTF8'``, ``'utf_8'`` all match); ``None`` — a stream
    with no declared encoding, e.g. stdout redirected to a pipe on some
    platforms — also falls back to ASCII rather than guessing.
    """
    if encoding is None:
        return ASCII_GLYPHS
    normalized = encoding.lower().replace("-", "").replace("_", "")
    return UNICODE_GLYPHS if normalized == "utf8" else ASCII_GLYPHS


# Spec section 6: style -> (foreground colour, bold?) for the two colour
# tiers. Dict order fixes the curses colour-pair numbers _build_attrs
# allocates (1..N); NORMAL, and every style in the no-colour tier, resolve
# to plain attribute flags instead, so neither table lists it.
_TIER_256 = {
    STYLE_ACCENT: (215, True),
    STYLE_OK: (114, False),
    STYLE_WARN: (221, False),
    STYLE_HIGH: (203, True),
    STYLE_DIM: (245, False),
    STYLE_TRACK: (239, False),
}

_TIER_8 = {
    STYLE_ACCENT: (curses.COLOR_YELLOW, True),
    STYLE_OK: (curses.COLOR_GREEN, False),
    STYLE_WARN: (curses.COLOR_YELLOW, False),
    STYLE_HIGH: (curses.COLOR_RED, True),
}


def _build_attrs(has_colors: bool, color_count: int) -> dict[str, int]:
    """Build the style -> curses attribute table once, per spec section 6.

    Three tiers, chosen by the caller's ``curses.has_colors()`` /
    ``curses.COLORS``: 256-colour (``color_count >= 256``), 8-colour
    fallback (``has_colors`` but fewer than 256 colours), and no-colour
    (attribute flags only, no color pairs). Backgrounds are never painted —
    every ``init_pair`` call below uses ``-1`` so ``use_default_colors()``
    keeps the terminal's own background. This is the only place
    ``curses.init_pair`` is ever called: ``run_tui`` calls it once per run,
    and ``_draw`` only looks the finished table up.
    """
    attrs: dict[str, int] = {STYLE_NORMAL: 0}
    if not has_colors:
        attrs[STYLE_ACCENT] = curses.A_BOLD
        attrs[STYLE_OK] = 0
        attrs[STYLE_WARN] = curses.A_BOLD
        attrs[STYLE_HIGH] = curses.A_BOLD
        attrs[STYLE_DIM] = curses.A_DIM
        attrs[STYLE_TRACK] = curses.A_DIM
        return attrs
    tier = _TIER_256 if color_count >= 256 else _TIER_8
    for pair, (style, (fg, bold)) in enumerate(tier.items(), start=1):
        curses.init_pair(pair, fg, -1)
        attrs[style] = curses.color_pair(pair) | (curses.A_BOLD if bold else 0)
    if tier is _TIER_8:
        attrs[STYLE_DIM] = curses.A_DIM
        attrs[STYLE_TRACK] = curses.A_DIM
    return attrs


def _draw(screen, lines, width: int, height: int, attrs: dict[str, int]) -> None:
    screen.erase()
    for row, segments in enumerate(lines[:height]):
        column = 0
        for segment in segments:
            if column >= width:
                break
            # The bottom-right cell of a real terminal cannot be written
            # without the cursor needing to advance past the edge of the
            # screen, which curses refuses; every other segment still gets
            # its own attempt.
            with contextlib.suppress(curses.error):
                screen.addnstr(
                    row, column, segment.text, max(0, width - column), attrs.get(segment.style, 0)
                )
            column += len(segment.text)
    screen.refresh()


def run_tui(screen, service: RefreshService, refresh_seconds: int) -> int:
    """Curses entry point; `curses.wrapper` passes stdscr as the first arg."""
    controller = AppController(service, refresh_seconds)
    controller.start()
    try:
        screen.timeout(250)
        screen.keypad(True)
        # Absent or unsupported on some terminals; never worth aborting over.
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        has_colors = curses.has_colors()
        if has_colors:
            curses.start_color()
            curses.use_default_colors()
        color_count = curses.COLORS if has_colors else 0
        # Built once for the whole run, not per frame: previously
        # curses.init_pair ran on every draw.
        attrs = _build_attrs(has_colors, color_count)
        # A lone ESC must quit as promptly as q: ncurses' default escape
        # delay (1000 ms) is the getch() timeout that disambiguates ESC from
        # the start of an escape sequence. Guarded: absent on old curses
        # builds, and never worth aborting startup over.
        set_escdelay = getattr(curses, "set_escdelay", None)
        if set_escdelay is not None:
            with contextlib.suppress(curses.error):
                set_escdelay(ESC_DELAY_MS)
        glyphs = _select_glyphs(_terminal_codeset())
        while True:
            height, width = screen.getmaxyx()
            now = datetime.now(timezone.utc)
            frame = render(controller.view(now), width, height, glyphs)
            _draw(screen, frame, width, height, attrs)
            # Adopt the offset actually rendered: the clamped scroll, plus
            # any focus-reveal nudge. This keeps the controller's scroll in
            # sync with the screen (spec section 7) so a single k responds
            # immediately after an over-scroll or a focus jump.
            controller.scroll = frame.offset
            key = screen.getch()
            controller.handle_key(key)
            controller.poll()
            if controller.quit:
                return 0
    finally:
        controller.close()
