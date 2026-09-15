import curses
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llmits import __version__ as PACKAGE_VERSION
from llmits.models import (
    AVAILABLE,
    AUTH_REQUIRED,
    NETWORK_ERROR,
    PARSE_ERROR,
    RATE_LIMITED,
    UNAVAILABLE,
    ProviderError,
    ProviderSnapshot,
    QuotaWindow,
)
from llmits import tui

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def snapshot(provider="claude", status=AVAILABLE, windows=(), fetched_at=NOW, **kwargs):
    return ProviderSnapshot(
        provider=provider,
        status=status,
        plan_name=kwargs.pop("plan_name", "Plan X" if status == AVAILABLE else None),
        fetched_at=fetched_at,
        windows=windows,
        **kwargs,
    )


def full_window(used=42, reset_at=None, key="5h", label="5h"):
    return QuotaWindow(
        key=key,
        label=label,
        used_percent=used,
        remaining_percent=100 - used,
        reset_at=reset_at or (NOW + timedelta(hours=2, minutes=15)),
        period_seconds=18000,
    )


def pct_window(key, label, used, reset_at=None, used_value=None, limit_value=None, period=18000):
    return QuotaWindow(
        key=key,
        label=label,
        used_percent=used,
        remaining_percent=100 - used,
        reset_at=reset_at,
        period_seconds=period,
        used_value=used_value,
        limit_value=limit_value,
    )


def value_window(key, label, remaining):
    return QuotaWindow(
        key=key, label=label, used_percent=0, remaining_percent=100, remaining_value=remaining
    )


def view(
    snapshots=None,
    loading=False,
    refresh_seconds=300,
    last_refresh=None,
    ids=("claude", "codex", "zai"),
    scroll=0,
    now=NOW,
    last_attempt=None,
    version="0.2.0",
    last_error=None,
    hidden=frozenset(),
    collapsed=frozenset(),
    focus=None,
    reveal_focus=False,
):
    return tui.TuiView(
        provider_ids=ids,
        snapshots=snapshots,
        loading=loading,
        last_refresh=last_refresh,
        refresh_seconds=refresh_seconds,
        version=version,
        now=now,
        last_attempt=last_attempt,
        scroll=scroll,
        last_error=last_error,
        hidden=hidden,
        collapsed=collapsed,
        focus=focus,
        reveal_focus=reveal_focus,
    )


def text(lines) -> list[str]:
    return ["".join(segment.text for segment in line) for line in lines]


def flat(frame) -> str:
    return "\n".join(text(frame))


def four_card_fixture(now=NOW):
    """The spec section 2 mockup: Claude, Codex, a stale Z.AI, an errored Z.AI.

    Built directly from QuotaWindow objects with the short labels spec
    section 3 assigns (so this does not depend on the provider-adapter
    label rewrite landing first).
    """
    far = now + timedelta(days=6, hours=7)
    claude = snapshot(
        "claude",
        windows=(
            pct_window("5h", "5h", 42, reset_at=now),  # delta <= 0 -> "resets now"
            pct_window("7d", "7d", 10, reset_at=far, period=604800),
            pct_window("weekly_opus", "Opus", 5, reset_at=far, period=604800),
            pct_window("extra", "extra", 42, used_value=42, limit_value=100, period=2592000),
        ),
        plan_name="Claude Pro/Max",
        fetched_at=now - timedelta(seconds=12),
    )
    codex = snapshot(
        "codex",
        windows=(
            pct_window("5h", "5h", 23, reset_at=now + timedelta(days=5, hours=20)),
            value_window("credits", "credits", 4250),
            value_window("reset_credits", "banked", 2),
        ),
        plan_name="Codex plus",
        fetched_at=now - timedelta(seconds=12),
    )
    zai_stale = replace(
        snapshot(
            "zai",
            windows=(
                pct_window("5h", "5h", 7, reset_at=now, used_value=72000, limit_value=1_000_000),
            ),
            plan_name="Z.AI pro",
            fetched_at=now - timedelta(minutes=6),
        ),
        stale=True,
        error=ProviderError(code=NETWORK_ERROR, message="provider server error (HTTP 503)"),
    )
    zai_failed = snapshot(
        "zai",
        status=AUTH_REQUIRED,
        plan_name=None,
        windows=(),
        fetched_at=now,
        error=ProviderError(
            code=AUTH_REQUIRED,
            message="provider rejected the credentials",
            action="check your ZAI_API_KEY (GLM Coding Plan key)",
        ),
    )
    return (claude, codex, zai_stale, zai_failed)


FOUR_CARD_SIZES = ((60, 15), (80, 24), (100, 30), (120, 40))


def scroll_fixture(now=NOW):
    """A taller three-provider view: enough windows to overflow 60x15.

    Distinct from ``four_card_fixture`` (which is sized so the mockup fits
    *without* scrolling at every documented size) -- this one exists to
    prove scrolling itself: every provider must stay reachable even though
    the content does not fit on one screen.
    """
    far = now + timedelta(days=6, hours=7)
    claude = snapshot(
        "claude",
        windows=(
            pct_window("5h", "5h", 42, reset_at=far),
            pct_window("7d", "7d", 10, reset_at=far, period=604800),
            pct_window("weekly_opus", "Opus", 5, reset_at=far, period=604800),
            pct_window("weekly_sonnet", "Sonnet", 15, reset_at=far, period=604800),
            pct_window("weekly_fable", "Fable", 47, reset_at=far, period=604800),
            pct_window("extra", "extra", 43, used_value=43, limit_value=100, period=2592000),
        ),
        plan_name="Claude Pro/Max",
        fetched_at=now - timedelta(seconds=12),
    )
    codex = snapshot(
        "codex",
        windows=(
            pct_window("5h", "5h", 23, reset_at=far),
            pct_window("7d", "7d", 45, reset_at=far, period=604800),
            pct_window("spk/7d", "Spark", 12, reset_at=far, period=604800),
            value_window("credits", "credits", 4250),
            value_window("reset_credits", "banked", 2),
        ),
        plan_name="Codex plus",
        fetched_at=now - timedelta(seconds=12),
    )
    zai = snapshot(
        "zai",
        windows=(
            pct_window("5h", "5h", 7, reset_at=far),
            pct_window("weekly", "7d", 53, reset_at=far, period=604800),
            pct_window("monthly_mcp", "MCP", 4, reset_at=far, period=2592000),
        ),
        plan_name="Z.AI pro",
        fetched_at=now - timedelta(seconds=12),
    )
    return (claude, codex, zai)


class RenderTests(unittest.TestCase):
    # -- header: the five variants of spec section 1 -------------------------

    def test_header_first_load_no_data_shows_refreshing(self):
        header = tui._header_text(view(None, loading=True, last_refresh=None), tui.UNICODE_GLYPHS)
        self.assertEqual(header, "llmits v0.2.0  ·  refreshing…")

    def test_header_data_idle_shows_updated_and_countdown(self):
        last = NOW - timedelta(seconds=12)
        header = tui._header_text(
            view(loading=False, last_refresh=last, refresh_seconds=300), tui.UNICODE_GLYPHS
        )
        self.assertEqual(header, "llmits v0.2.0  ·  updated 12s ago  ·  next refresh 4:48")

    def test_header_countdown_uses_last_attempt_after_failed_refresh(self):
        last_success = NOW - timedelta(minutes=10)
        last_attempt = NOW - timedelta(seconds=12)
        header = tui._header_text(
            view(
                loading=False,
                last_refresh=last_success,
                last_attempt=last_attempt,
                refresh_seconds=300,
            ),
            tui.UNICODE_GLYPHS,
        )
        self.assertEqual(
            header,
            "llmits v0.2.0  ·  updated 10m ago  ·  next refresh 4:48",
        )

    def test_header_refresh_in_flight_with_data(self):
        last = NOW - timedelta(seconds=12)
        header = tui._header_text(
            view(loading=True, last_refresh=last, refresh_seconds=300), tui.UNICODE_GLYPHS
        )
        self.assertEqual(header, "llmits v0.2.0  ·  updated 12s ago  ·  refreshing…")

    def test_header_auto_refresh_disabled(self):
        last = NOW - timedelta(seconds=12)
        header = tui._header_text(
            view(loading=False, last_refresh=last, refresh_seconds=0), tui.UNICODE_GLYPHS
        )
        self.assertEqual(header, "llmits v0.2.0  ·  updated 12s ago  ·  auto-refresh off")

    def test_header_no_data_not_loading_is_bare(self):
        header = tui._header_text(
            view(loading=False, last_refresh=None, refresh_seconds=300), tui.UNICODE_GLYPHS
        )
        self.assertEqual(header, "llmits v0.2.0")

    def test_header_updated_text_present_in_full_render(self):
        # rank 27: the "updated HH:MM:SS"-successor text must actually be
        # asserted, not just the separately-gated countdown next to it.
        last = NOW - timedelta(seconds=45)
        frame = tui.render(view((snapshot(),), last_refresh=last), 120, 40)
        self.assertIn("updated 45s ago", flat(frame))

    def test_header_ascii_glyphs_swap_ellipsis(self):
        header = tui._header_text(view(None, loading=True), tui.ASCII_GLYPHS)
        self.assertEqual(header, "llmits v0.2.0  -  refreshing...")

    # -- footer: pinned, DIM, refreshing prefix -------------------------------

    def test_footer_idle_text(self):
        footer = tui._footer_text(view(loading=False), tui.UNICODE_GLYPHS, scrollable=True)
        self.assertEqual(
            footer,
            "r/R refresh  ·  j/k scroll  ·  q/esc quit  ·  tab focus  ·  h hide  ·  c collapse  ·  a show all",
        )

    def test_footer_refreshing_prefix(self):
        footer = tui._footer_text(view(loading=True), tui.UNICODE_GLYPHS, scrollable=True)
        self.assertEqual(
            footer,
            "refreshing…  ·  r/R refresh  ·  j/k scroll  ·  q/esc quit  ·  tab focus  ·  h hide  ·  c collapse  ·  a show all",
        )

    def test_footer_hides_scroll_hint_when_content_fits(self):
        frame = tui.render(view((snapshot(windows=(full_window(10),)),)), 120, 40)
        self.assertEqual(frame.max_scroll, 0)
        footer = text(frame.lines[-1:])[0]
        self.assertNotIn("j/k scroll", footer)

    def test_footer_shows_scroll_hint_when_content_overflows(self):
        frame = tui.render(view(scroll_fixture()), 60, 15)
        self.assertGreater(frame.max_scroll, 0)
        footer = text(frame.lines[-1:])[0]
        self.assertIn("j/k scroll", footer)

    def test_footer_is_last_row_and_dim(self):
        frame = tui.render(view((snapshot(windows=(full_window(10),)),)), 120, 40)
        last_line = frame.lines[-1]
        self.assertEqual(len(last_line), 1)
        self.assertEqual(last_line[0].style, tui.STYLE_DIM)
        self.assertIn("q/esc quit", last_line[0].text)

    def test_readme_sample_matches_fixture_at_80_columns(self):
        marker = "Sample render at 80 columns, from the test fixtures"
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        documented = readme.split(marker, 1)[1].split("```", 2)[1].strip("\n").splitlines()
        sample_view = view(
            four_card_fixture()[:3],
            loading=False,
            last_refresh=NOW - timedelta(seconds=12),
            refresh_seconds=300,
            ids=("claude", "codex", "zai"),
            version=PACKAGE_VERSION,
            focus="claude",
        )
        rendered = text(tui.render(sample_view, 80, 24).lines)
        self.assertEqual(documented, rendered[:17] + [rendered[-1]])
        self.assertTrue(all(len(line) <= 79 for line in documented))

    def test_footer_lists_both_q_and_esc_as_quit_keys(self):
        # rank 90: a monochrome or screen-reader-piped terminal must still
        # learn both quit keys from the footer text itself.
        footer = tui._footer_text(view(loading=False), tui.UNICODE_GLYPHS, scrollable=False)
        self.assertIn("q", footer)
        self.assertIn("esc", footer)

    # -- card header: name/plan/status group, spec section 2.1 ---------------

    def test_plan_bracket_shown_at_every_width_including_compact(self):
        # rank 19: the old compact-mode omission is dropped; assert the
        # bracket at both a compact width and a wide one.
        snap = snapshot(windows=(full_window(10),), plan_name="Claude Pro/Max")
        for width in (61, 80, 120):
            with self.subTest(width=width):
                frame = tui.render(view((snap,)), width, 24)
                self.assertIn("[Claude Pro/Max]", flat(frame))

    def test_plan_bracket_omitted_when_plan_name_none(self):
        snap = snapshot(status=AUTH_REQUIRED, plan_name=None)
        frame = tui.render(view((snap,)), 120, 40)
        self.assertNotIn("[", flat(frame))

    def test_card_header_styles_healthy(self):
        segments = tui._status_segments(snapshot(fetched_at=NOW - timedelta(seconds=12)), tui.UNICODE_GLYPHS, NOW)
        self.assertEqual(segments[0], tui.Segment("●", tui.STYLE_OK))
        self.assertEqual(segments[1], tui.Segment(" 12s ago", tui.STYLE_DIM))
        # Healthy cards carry no status word (rank 34 + decision #1).
        joined = "".join(s.text for s in segments)
        for word in ("available", "ok", "healthy"):
            self.assertNotIn(word, joined)

    def test_card_header_styles_stale(self):
        stale = replace(snapshot(fetched_at=NOW - timedelta(minutes=6)), stale=True)
        segments = tui._status_segments(stale, tui.UNICODE_GLYPHS, NOW)
        self.assertEqual(segments[0], tui.Segment("● stale", tui.STYLE_WARN))
        self.assertEqual(segments[1], tui.Segment(" · 6m ago", tui.STYLE_DIM))

    def test_card_header_styles_every_error_status(self):
        # rank 34: every state_style branch gets its style asserted, not
        # just its flattened text.
        cases = {
            AUTH_REQUIRED: "auth required",
            RATE_LIMITED: "rate limited",
            NETWORK_ERROR: "network error",
            PARSE_ERROR: "response changed",
            UNAVAILABLE: "unavailable",
        }
        for status, word in cases.items():
            with self.subTest(status=status):
                snap = snapshot(status=status, plan_name=None)
                segments = tui._status_segments(snap, tui.UNICODE_GLYPHS, NOW)
                self.assertEqual(len(segments), 1)
                self.assertEqual(segments[0].style, tui.STYLE_HIGH)
                self.assertEqual(segments[0].text, f"● {word}")

    def test_loading_card_hollow_dot_no_bracket(self):
        frame = tui.render(view(None, loading=True, ids=("claude",)), 120, 40)
        flattened = flat(frame)
        self.assertIn("○ fetching…", flattened)
        self.assertNotIn("[", flattened)
        # And it is DIM, not a card-status colour.
        loading_line = next(line for line in frame.lines if "fetching" in "".join(s.text for s in line))
        fetching_segment = next(s for s in loading_line if "fetching" in s.text)
        self.assertEqual(fetching_segment.style, tui.STYLE_DIM)

    def test_idle_without_snapshots_does_not_claim_fetching(self):
        frame = tui.render(
            view(None, loading=False, last_refresh=None, ids=("claude",)),
            120,
            40,
        )
        flattened = flat(frame)
        self.assertIn("○ no data", flattened)
        self.assertNotIn("fetching", flattened)

    def test_card_name_is_accent_styled(self):
        frame = tui.render(view((snapshot(windows=(full_window(10),)),)), 120, 40)
        name_segment = frame.lines[2][0]
        self.assertEqual(name_segment.text, "Claude")
        self.assertEqual(name_segment.style, tui.STYLE_ACCENT)

    # -- percent rows: spec section 2.2 ---------------------------------------

    def test_percent_row_exact_text_matches_template(self):
        # Guards rank 85 (the old "join non-blank pieces" predicate that
        # never actually dropped anything): every piece must appear, in the
        # documented order, with the documented spacing.
        # compact=True keeps this deterministic (no local-timezone-dependent
        # clock suffix); the wide/local-time behaviour has its own test.
        window = pct_window("5h", "5h", 42, reset_at=NOW + timedelta(hours=2, minutes=15))
        line = tui._window_line(window, tui.UNICODE_GLYPHS, NOW, label_width=7, bar_width=27, compact=True)
        joined = "".join(s.text for s in line)
        self.assertEqual(joined, "  5h      " + "━" * 11 + "─" * 16 + "   42%  resets 2h 15m")

    def test_percent_row_thresholds_ok_warn_high_at_boundaries(self):
        # OK/WARN/HIGH at 69/70/89/90.
        cases = [(69, tui.STYLE_OK), (70, tui.STYLE_WARN), (89, tui.STYLE_WARN), (90, tui.STYLE_HIGH)]
        for used, expected_style in cases:
            with self.subTest(used=used):
                window = pct_window("5h", "5h", used, reset_at=NOW + timedelta(hours=1))
                line = tui._window_line(window, tui.UNICODE_GLYPHS, NOW, label_width=5, bar_width=20, compact=False)
                fill_segment, track_segment, pct_segment = line[1], line[2], line[3]
                self.assertEqual(fill_segment.style, expected_style)
                self.assertEqual(pct_segment.style, expected_style)
                self.assertEqual(track_segment.style, tui.STYLE_TRACK)

    def test_percent_row_reset_now_branch(self):
        # rank 35: total <= 0 -> "resets now", previously unreached by any
        # fixture (all used a future reset_at).
        past_or_now = tui._reset_text(NOW, NOW, wide=True, glyphs=tui.UNICODE_GLYPHS)
        self.assertEqual(past_or_now, "resets now")
        past = tui._reset_text(NOW - timedelta(seconds=1), NOW, wide=True, glyphs=tui.UNICODE_GLYPHS)
        self.assertEqual(past, "resets now")

    def test_percent_row_reset_none_is_empty(self):
        self.assertEqual(tui._reset_text(None, NOW, wide=True, glyphs=tui.UNICODE_GLYPHS), "")

    def test_reset_wide_includes_local_time_compact_does_not(self):
        reset_at = NOW + timedelta(days=6, hours=7)
        wide = tui._reset_text(reset_at, NOW, wide=True, glyphs=tui.UNICODE_GLYPHS)
        compact = tui._reset_text(reset_at, NOW, wide=False, glyphs=tui.UNICODE_GLYPHS)
        self.assertEqual(compact, "resets 6d 7h")
        self.assertTrue(wide.startswith("resets 6d 7h · "))
        self.assertGreater(len(wide), len(compact))

    def test_compact_percent_row_drops_note_wide_keeps_it(self):
        # rank 19's sibling for row content: note is dropped in compact,
        # kept in wide, on percent rows specifically (value-only rows keep
        # their note either way -- see test_value_only_row_note_present).
        window = pct_window("extra", "extra", 42, used_value=42, limit_value=100)
        wide_meta = tui._meta_text(window, NOW, compact=False, glyphs=tui.UNICODE_GLYPHS)
        compact_meta = tui._meta_text(window, NOW, compact=True, glyphs=tui.UNICODE_GLYPHS)
        self.assertEqual(wide_meta, "42/100")
        self.assertEqual(compact_meta, "")

    def test_wide_meta_joins_note_then_reset(self):
        window = pct_window(
            "5h", "5h", 7, reset_at=NOW, used_value=72_000, limit_value=1_000_000
        )
        meta = tui._meta_text(window, NOW, compact=False, glyphs=tui.UNICODE_GLYPHS)
        self.assertEqual(meta, "72,000/1,000,000 · resets now")

    def test_percent_row_bar_uses_track_glyph_for_remainder(self):
        window = pct_window("5h", "5h", 0, reset_at=NOW + timedelta(hours=1))
        line = tui._window_line(window, tui.UNICODE_GLYPHS, NOW, label_width=5, bar_width=10, compact=False)
        self.assertEqual(line[1].text, "")
        self.assertEqual(line[2].text, "─" * 10)

    def test_percent_row_label_dim_and_cut_to_label_width(self):
        window = pct_window("weekly_sonnet", "Sonnet-overflow", 5, reset_at=NOW + timedelta(hours=1))
        line = tui._window_line(window, tui.UNICODE_GLYPHS, NOW, label_width=5, bar_width=10, compact=False)
        label_segment = line[0]
        self.assertEqual(label_segment.style, tui.STYLE_DIM)
        # "Sonnet-overflow"[:5] == "Sonne": a plain slice to L, per spec.
        self.assertEqual(label_segment.text, "  Sonne ")

    # -- value-only rows: spec section 2.2 ------------------------------------

    def test_value_only_row_has_no_bar_or_percent(self):
        credits = value_window("credits", "credits", 4250)
        line = tui._window_line(credits, tui.UNICODE_GLYPHS, NOW, label_width=7, bar_width=27, compact=False)
        joined = "".join(s.text for s in line)
        self.assertNotIn("%", joined)
        for glyph in (tui.UNICODE_GLYPHS.fill, tui.UNICODE_GLYPHS.track):
            self.assertNotIn(glyph, joined)
        self.assertEqual(joined, "  credits $42.50 balance")

    def test_value_only_row_note_style_normal(self):
        resets = value_window("reset_credits", "banked", 2)
        line = tui._window_line(resets, tui.UNICODE_GLYPHS, NOW, label_width=7, bar_width=27, compact=False)
        self.assertEqual(line[0].style, tui.STYLE_DIM)
        self.assertEqual(line[1], tui.Segment("2 available", tui.STYLE_NORMAL))

    def test_value_only_row_note_present_in_compact_too(self):
        # Compact mode drops the note on *percent* rows only; a value-only
        # row's note is its entire payload and must survive.
        credits = value_window("credits", "credits", 4250)
        snap = snapshot("codex", windows=(credits,))
        frame = tui.render(view((snap,)), 80, 24)
        self.assertIn("$42.50 balance", flat(frame))

    # -- stale and error cards: spec sections 2.1/2.4 -------------------------

    def test_stale_card_keeps_windows_and_shows_failure_line(self):
        stale = replace(
            snapshot(windows=(full_window(30),)),
            stale=True,
            error=ProviderError(code=NETWORK_ERROR, message="provider server error (HTTP 500)"),
        )
        frame = tui.render(view((stale,)), 120, 40)
        flattened = flat(frame)
        self.assertIn("stale", flattened)
        self.assertIn("30%", flattened)
        self.assertIn("last update failed: provider server error (HTTP 500)", flattened)

    def test_stale_card_shows_recovery_action(self):
        stale = replace(
            snapshot(windows=(full_window(30),)),
            stale=True,
            error=ProviderError(
                code=AUTH_REQUIRED,
                message="credentials rejected",
                action="run claude login",
            ),
        )
        rendered = flat(tui.render(view((stale,)), 120, 40))
        self.assertIn(
            "last update failed: credentials rejected — run claude login",
            rendered,
        )

    def test_stale_card_without_error_has_no_failure_line(self):
        stale = replace(snapshot(windows=(full_window(30),)), stale=True, error=None)
        frame = tui.render(view((stale,)), 120, 40)
        self.assertNotIn("last update failed", flat(frame))

    def test_error_card_status_message_table_covers_every_status(self):
        # rank 104: a table, not an if/elif chain -- and every branch,
        # not just auth_required, gets its exact message asserted.
        self.assertIsInstance(tui._STATUS_MESSAGES, dict)
        expected = {
            AUTH_REQUIRED: "credentials missing or rejected",
            RATE_LIMITED: "rate limited by provider",
            NETWORK_ERROR: "network problem",
            PARSE_ERROR: "provider response changed",
            UNAVAILABLE: "unavailable",
        }
        for status, message in expected.items():
            with self.subTest(status=status):
                snap = snapshot(status=status, plan_name=None, error=None)
                frame = tui.render(view((snap,)), 120, 40)
                self.assertIn(message, flat(frame))

    def test_error_card_action_joined_with_em_dash_when_present(self):
        failed = snapshot(
            "zai",
            status=AUTH_REQUIRED,
            error=ProviderError(
                code=AUTH_REQUIRED,
                message="Z.AI API key not set",
                action="export ZAI_API_KEY with your GLM Coding Plan key",
            ),
        )
        frame = tui.render(view((failed,)), 120, 40)
        self.assertIn("Z.AI API key not set — export ZAI_API_KEY", flat(frame))

    def test_error_card_omits_dash_when_action_empty(self):
        failed = snapshot("zai", status=AUTH_REQUIRED, error=ProviderError(code=AUTH_REQUIRED, message="bad token"))
        frame = tui.render(view((failed,)), 120, 40)
        flattened = flat(frame)
        self.assertIn("bad token", flattened)
        self.assertNotIn("bad token —", flattened)

    def test_error_card_no_detail_line_when_error_none(self):
        failed = snapshot("zai", status=UNAVAILABLE, error=None)
        lines = tui._card_lines(failed, tui.UNICODE_GLYPHS, NOW, label_width=5, bar_width=20, compact=False)
        # Header line + the status message only: no third (error detail) line.
        self.assertEqual(len(lines), 2)

    # -- widths, wrapping, ascii/control safety, and frame shape --------------

    def test_min_size_message_below_60x15(self):
        frame = tui.render(view((snapshot(),)), 59, 14)
        flattened = flat(frame)
        self.assertIn("terminal too small", flattened)
        self.assertIn("59x14", flattened)
        self.assertEqual(len(frame.lines), 1)
        self.assertEqual(frame.max_scroll, 0)

    def test_frame_exposes_max_scroll(self):
        frame = tui.render(view((snapshot(windows=(full_window(10),)),)), 120, 40)
        self.assertEqual(frame.max_scroll, 0)

    def test_frame_is_iterable_and_sliceable_like_a_line_list(self):
        # Backward-compat: tests/test_security.py and _draw both consume
        # render()'s result as a plain list of lines.
        frame = tui.render(view((snapshot(windows=(full_window(10),)),)), 120, 40)
        as_list = [line for line in frame]
        self.assertEqual(as_list, frame.lines)
        self.assertEqual(len(frame), len(frame.lines))
        self.assertEqual(frame[:3], frame.lines[:3])

    def test_glyphs_ascii_set_yields_only_ascii_glyphs(self):
        for value in (
            tui.ASCII_GLYPHS.fill,
            tui.ASCII_GLYPHS.track,
            tui.ASCII_GLYPHS.dot,
            tui.ASCII_GLYPHS.hollow,
            tui.ASCII_GLYPHS.sep,
            tui.ASCII_GLYPHS.ellipsis,
        ):
            self.assertTrue(value.isascii(), value)

    def test_ascii_glyphs_replace_unicode_glyphs_in_full_render(self):
        frame = tui.render(
            view((snapshot(windows=(full_window(42),), plan_name="Claude Pro/Max"), snapshot("zai", status=AUTH_REQUIRED)), loading=True),
            120,
            40,
            tui.ASCII_GLYPHS,
        )
        flattened = flat(frame)
        for glyph in (
            tui.UNICODE_GLYPHS.fill,
            tui.UNICODE_GLYPHS.track,
            tui.UNICODE_GLYPHS.dot,
            tui.UNICODE_GLYPHS.hollow,
            tui.UNICODE_GLYPHS.sep,
            tui.UNICODE_GLYPHS.ellipsis,
        ):
            self.assertNotIn(glyph, flattened)
        self.assertIn("=", flattened)  # ascii fill glyph shows up somewhere

    def test_control_characters_never_reach_output(self):
        dirty = snapshot(
            "codex",
            status=AUTH_REQUIRED,
            error=ProviderError(
                code=AUTH_REQUIRED,
                message="bad \x1b[31mtoken\x1b[0m value",
                action="run codex login",
            ),
        )
        frame = tui.render(view((dirty,)), 120, 40)
        flattened = flat(frame)
        self.assertNotIn("\x1b", flattened)
        self.assertNotIn("\x00", flattened)

    def test_hostile_wide_label_does_not_crash_and_is_clipped_to_label_width(self):
        # rank 121: card/window rendering keeps 1 codepoint == 1 column
        # (documented, deliberate) but must not crash and must still clip
        # to L on a double-width CJK or emoji label.
        wide_label = "日本語ラベルテキスト" * 3  # 30 codepoints, well past LABEL_MAX
        window = pct_window(wide_label[:8] or "w", wide_label, 50, reset_at=NOW + timedelta(hours=1))
        snap = snapshot(windows=(window,))
        frame = tui.render(view((snap,)), 60, 15)  # does not raise
        for line in text(frame.lines):
            self.assertLessEqual(len(line), 59)
        emoji_label = "🎉" * 20
        window2 = pct_window("emo", emoji_label, 10, reset_at=NOW + timedelta(hours=1))
        frame2 = tui.render(view((snapshot(windows=(window2,)),)), 60, 15)
        for line in text(frame2.lines):
            self.assertLessEqual(len(line), 59)

    def test_all_lines_fit_width_at_various_sizes(self):
        for width, height in ((120, 40), (80, 24), (61, 16)):
            frame = tui.render(
                view((snapshot(windows=(full_window(99),)), snapshot("codex", windows=(full_window(3),)))),
                width,
                height,
            )
            for line in text(frame.lines):
                self.assertLessEqual(len(line), width - 1, f"line overflow at {width}x{height}: {line!r}")

    # -- section 17/69: AppController.view() feeding render() directly -------

    def test_controller_view_feeds_render_directly(self):
        class OneShotService:
            provider_ids = ("claude",)

            def refresh(self, provider_ids=None):
                return (snapshot(),)

        controller = tui.AppController(OneShotService(), 0, now_fn=lambda: NOW)
        try:
            controller.start()
            controller._future.result(timeout=5)
            controller.poll(NOW)
            controller_view = controller.view(NOW)
            self.assertEqual(controller_view.version, PACKAGE_VERSION)
            frame = tui.render(controller_view, 120, 40)
            self.assertIn("Claude", flat(frame))
            self.assertIn(f"llmits v{PACKAGE_VERSION}", flat(frame))
        finally:
            controller.close()

    def test_refresh_failure_is_visible_without_hiding_prior_data(self):
        prior = snapshot(windows=(full_window(30),), plan_name="Claude Pro/Max")
        frame = tui.render(
            view(
                (prior,),
                last_refresh=NOW - timedelta(seconds=12),
                last_error="refresh failed: RuntimeError",
            ),
            120,
            40,
        )
        rendered = flat(frame)
        self.assertIn("refresh failed: RuntimeError", rendered)
        self.assertIn("Claude Pro/Max", rendered)
        self.assertIn("30%", rendered)
        error_segment = next(
            segment
            for line in frame.lines
            for segment in line
            if "refresh failed" in segment.text
        )
        self.assertEqual(error_segment.style, tui.STYLE_HIGH)

    def test_first_refresh_failure_is_visible_in_rendered_view(self):
        frame = tui.render(
            view(None, loading=False, last_error="refresh failed: RuntimeError"),
            120,
            40,
        )
        self.assertIn("refresh failed: RuntimeError", flat(frame))

    def test_version_wrapper_removed(self):
        # rank 69: the `_version()` one-line indirection is gone; the
        # module imports __version__ once at top level instead.
        self.assertFalse(hasattr(tui, "_version"))
        self.assertEqual(tui.__version__, PACKAGE_VERSION)

    def test_unused_field_import_removed(self):
        # rank 59: `field` was imported from dataclasses but never used.
        self.assertNotIn("field", vars(tui))

    # -- the four-card fixture at every documented size -----------------------

    def test_four_card_fixture_no_line_exceeds_width_at_each_size(self):
        v = view(four_card_fixture(), last_refresh=NOW - timedelta(seconds=12))
        for width, height in FOUR_CARD_SIZES:
            with self.subTest(size=(width, height)):
                frame = tui.render(v, width, height)
                for line in text(frame.lines):
                    self.assertLessEqual(len(line), width - 1)

    def test_four_card_fixture_footer_is_last_row_at_each_size(self):
        v = view(four_card_fixture(), last_refresh=NOW - timedelta(seconds=12))
        for width, height in FOUR_CARD_SIZES:
            with self.subTest(size=(width, height)):
                frame = tui.render(v, width, height)
                self.assertEqual(len(frame.lines), height)
                last = "".join(s.text for s in frame.lines[-1])
                self.assertIn("q/esc quit", last)

    def test_four_card_fixture_bar_width_uniform_at_each_size(self):
        v = view(four_card_fixture(), last_refresh=NOW - timedelta(seconds=12))
        for width, height in FOUR_CARD_SIZES:
            with self.subTest(size=(width, height)):
                frame = tui.render(v, width, height)
                bar_widths = {
                    len(line[1].text) + len(line[2].text)
                    for line in frame.lines
                    if len(line) >= 3
                    and line[1].style in (tui.STYLE_OK, tui.STYLE_WARN, tui.STYLE_HIGH)
                    and line[2].style == tui.STYLE_TRACK
                }
                self.assertEqual(len(bar_widths), 1, bar_widths)

    def test_four_card_fixture_compact_vs_wide_meta(self):
        v = view(four_card_fixture(), last_refresh=NOW - timedelta(seconds=12))
        compact = flat(tui.render(v, 80, 24))
        wide = flat(tui.render(v, 120, 40))
        self.assertIn("42/100", wide)  # extra's note, wide only
        self.assertNotIn("42/100", compact)


class FakeService:
    """Records every requested provider-id set; gates each refresh by hand.

    Mirrors RefreshService.refresh: an explicit id list returns snapshots
    for exactly those ids, so the controller's merge logic can be tested
    against the real subset-refresh contract.
    """

    def __init__(self):
        self.calls = 0
        self.requested = []
        self._gate = threading.Event()

    @property
    def provider_ids(self):
        return ("claude", "codex", "zai")

    def _snapshot_for(self, provider_id):
        return snapshot(provider_id, plan_name=f"Plan {provider_id} #{self.calls}")

    def refresh(self, provider_ids=None):
        self.calls += 1
        ids = tuple(self.provider_ids) if provider_ids is None else tuple(provider_ids)
        self.requested.append(ids)
        self._gate.wait(timeout=5)
        self._gate.clear()
        return tuple(self._snapshot_for(pid) for pid in dict.fromkeys(ids))


class FailingService:
    """Like FakeService, but refresh() always raises once released."""

    def __init__(self):
        self.calls = 0
        self._gate = threading.Event()

    @property
    def provider_ids(self):
        return ("claude", "codex", "zai")

    def refresh(self, provider_ids=None):
        self.calls += 1
        self._gate.wait(timeout=5)
        self._gate.clear()
        raise RuntimeError("simulated refresh failure")


class FlakyService:
    """Succeeds on the first refresh, then fails on every refresh after."""

    def __init__(self):
        self.calls = 0
        self._gate = threading.Event()

    @property
    def provider_ids(self):
        return ("claude", "codex", "zai")

    def refresh(self, provider_ids=None):
        self.calls += 1
        self._gate.wait(timeout=5)
        self._gate.clear()
        if self.calls == 1:
            ids = tuple(self.provider_ids) if provider_ids is None else tuple(provider_ids)
            return tuple(snapshot(pid) for pid in dict.fromkeys(ids))
        raise RuntimeError("simulated refresh failure")


class ControllerTests(unittest.TestCase):
    @staticmethod
    def settle(controller, timeout=5):
        future = controller._future
        if future is not None:
            future.result(timeout=timeout)

    def test_start_triggers_refresh_and_poll_adopts(self):
        svc = FakeService()
        controller = tui.AppController(svc, 0)
        controller.start()
        self.assertTrue(controller.loading)
        controller.poll(NOW)  # future not done yet
        self.assertTrue(controller.loading)
        self.assertIsNone(controller.snapshots)
        svc._gate.set()
        self.settle(controller)
        controller.poll(NOW + timedelta(seconds=1))
        self.assertFalse(controller.loading)
        self.assertIsNotNone(controller.snapshots)
        self.assertEqual(len(controller.snapshots), 3)
        controller.close()

    def test_overlapping_refresh_suppressed(self):
        svc = FakeService()
        controller = tui.AppController(svc, 0)
        controller.start()
        controller.request_refresh()
        controller.request_refresh()
        self.assertEqual(svc.calls, 1)
        svc._gate.set()
        self.settle(controller)
        controller.poll(NOW)
        controller.close()

    def test_quit_keys(self):
        controller = tui.AppController(FakeService(), 0)
        for key in (ord("q"), ord("Q"), 27):
            controller.quit = False
            controller.handle_key(key)
            self.assertTrue(controller.quit)
        controller.quit = False
        controller.handle_key(ord("x"))
        self.assertFalse(controller.quit)
        controller.close()

    def test_periodic_refresh_schedules_after_interval(self):
        svc = FakeService()
        controller = tui.AppController(svc, 300, now_fn=lambda: NOW)
        controller.start()
        svc._gate.set()
        self.settle(controller)
        controller.poll(NOW)
        self.assertEqual(svc.calls, 1)
        controller.poll(NOW + timedelta(seconds=299))
        self.assertEqual(svc.calls, 1)
        controller.poll(NOW + timedelta(seconds=301))
        svc._gate.set()
        self.settle(controller)
        self.assertEqual(svc.calls, 2)
        controller.poll(NOW + timedelta(seconds=302))
        controller.close()

    def test_disabled_periodic_refresh_never_reschedules(self):
        svc = FakeService()
        controller = tui.AppController(svc, 0, now_fn=lambda: NOW)
        controller.start()
        svc._gate.set()
        self.settle(controller)
        controller.poll(NOW)
        controller.poll(NOW + timedelta(hours=2))
        self.assertEqual(svc.calls, 1)
        controller.close()


class ControllerRefreshFailureTests(unittest.TestCase):
    """Covers the previously-silent `_safe_refresh` error boundary."""

    @staticmethod
    def settle_ignoring_failure(controller, timeout=5):
        future = controller._future
        if future is not None:
            try:
                future.result(timeout=timeout)
            except Exception:
                pass

    def test_first_refresh_failure_clears_loading_and_surfaces_error(self):
        svc = FailingService()
        controller = tui.AppController(svc, 300, now_fn=lambda: NOW)
        controller.start()
        self.assertTrue(controller.loading)
        self.assertIsNone(controller.last_attempt)
        svc._gate.set()
        self.settle_ignoring_failure(controller)
        controller.poll(NOW)
        self.assertFalse(controller.loading)
        self.assertIsNone(controller.snapshots)
        self.assertEqual(controller.last_attempt, NOW)
        self.assertIsNotNone(controller.last_error)
        self.assertIn("RuntimeError", controller.last_error)
        self.assertIsNotNone(controller.view(NOW).last_error)

        # Not stranded: the failed attempt still schedules the next
        # automatic refresh a full interval later. Previously the gate
        # read last_refresh, which a first failure never sets, so
        # auto-refresh never engaged at all.
        controller.poll(NOW + timedelta(seconds=299))
        self.assertEqual(svc.calls, 1)
        controller.poll(NOW + timedelta(seconds=301))
        self.assertEqual(svc.calls, 2)
        svc._gate.set()
        self.settle_ignoring_failure(controller)
        controller.poll(NOW + timedelta(seconds=302))
        controller.close()

    def test_refresh_failure_after_prior_success_keeps_snapshots_and_avoids_retry_storm(self):
        svc = FlakyService()
        controller = tui.AppController(svc, 60, now_fn=lambda: NOW)
        controller.start()
        svc._gate.set()
        ControllerTests.settle(controller)
        controller.poll(NOW)
        self.assertEqual(svc.calls, 1)
        first_snapshots = controller.snapshots
        self.assertIsNotNone(first_snapshots)
        self.assertEqual(controller.last_refresh, NOW)
        self.assertEqual(controller.last_attempt, NOW)

        # A full interval later: triggers the second, failing refresh.
        t1 = NOW + timedelta(seconds=61)
        controller.poll(t1)
        self.assertTrue(controller.loading)
        svc._gate.set()
        self.settle_ignoring_failure(controller)
        controller.poll(t1)
        self.assertEqual(svc.calls, 2)
        self.assertFalse(controller.loading)
        self.assertEqual(controller.snapshots, first_snapshots)  # last-good preserved
        self.assertEqual(controller.last_refresh, NOW)  # unchanged by the failure
        self.assertEqual(controller.last_attempt, t1)
        self.assertIsNotNone(controller.last_error)
        self.assertIsNotNone(controller.view(t1).last_error)

        # Rapid ticks (as the real ~250ms curses poll loop would produce)
        # before a full interval has passed since the *failed* attempt
        # must not resubmit. Gating on the stale last_refresh (the old
        # bug) would have resubmitted on every one of these.
        for tick in range(1, 5):
            controller.poll(t1 + timedelta(milliseconds=250 * tick))
        self.assertEqual(svc.calls, 2)

        # A full interval past the failed attempt: schedules the next one.
        t2 = t1 + timedelta(seconds=61)
        controller.poll(t2)
        self.assertTrue(controller.loading)
        svc._gate.set()
        self.settle_ignoring_failure(controller)
        controller.poll(t2)
        self.assertEqual(svc.calls, 3)
        controller.close()

    def test_close_returns_immediately_while_refresh_is_blocked(self):
        svc = FailingService()  # gate left unset: refresh() stays blocked
        controller = tui.AppController(svc, 0)
        controller.start()
        started = time.monotonic()
        controller.close()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        svc._gate.set()  # let the background thread unblock and finish quietly


class FocusToolkitRenderTests(unittest.TestCase):
    """Session-local focus/hide/collapse toolkit: pure render behavior."""

    BASE = dict(last_refresh=NOW - timedelta(seconds=12))

    def test_hidden_provider_card_absent_from_every_rendered_line(self):
        v = view(scroll_fixture(), hidden={"codex"}, **self.BASE)
        rendered = flat(tui.render(v, 120, 40))
        self.assertNotIn("Codex", rendered)
        self.assertNotIn("Spark", rendered)  # a codex-only window row
        self.assertNotIn("$42.50 balance", rendered)  # codex credits row
        self.assertIn("Claude", rendered)
        self.assertIn("Z.AI", rendered)

    def test_hidden_provider_absent_while_fetching(self):
        v = view(None, loading=True, hidden={"claude"})
        rendered = flat(tui.render(v, 80, 24))
        self.assertNotIn("Claude", rendered)
        self.assertIn("Codex", rendered)

    def test_label_width_recomputed_without_the_hidden_card(self):
        # scroll_fixture's longest label lives on codex's rows ("credits"/
        # "banked", 7 chars); hiding codex must shrink the shared label
        # column from 7 to claude's longest ("Sonnet", 6).
        base = view(scroll_fixture(), **self.BASE)
        without_codex = replace(base, hidden=frozenset({"codex"}))

        def five_hour_label_segment(v):
            frame = tui.render(v, 120, 40)
            line = next(
                line for line in frame.lines if "".join(s.text for s in line).startswith("  5h")
            )
            return line[0].text

        self.assertEqual(len(five_hour_label_segment(base)), 2 + 7 + 1)
        self.assertEqual(len(five_hour_label_segment(without_codex)), 2 + 6 + 1)

    def test_all_hidden_renders_only_the_restore_hint(self):
        everything = frozenset({"claude", "codex", "zai"})
        for width, height in ((60, 15), (80, 24)):
            with self.subTest(size=(width, height)):
                v = view(scroll_fixture(), hidden=everything, **self.BASE)
                frame = tui.render(v, width, height)
                rendered = flat(frame)
                self.assertIn("all providers hidden · press a to restore", rendered)
                for name in ("Claude", "Codex", "Z.AI"):
                    self.assertNotIn(name, rendered)
                self.assertIn("q/esc quit", rendered)  # footer still pinned
                self.assertEqual(frame.max_scroll, 0)

    def test_all_hidden_hint_is_ascii_under_ascii_glyphs(self):
        v = view(None, hidden=frozenset({"claude", "codex", "zai"}))
        frame = tui.render(v, 60, 15, tui.ASCII_GLYPHS)
        rendered = flat(frame)
        self.assertIn("all providers hidden - press a to restore", rendered)
        for line in text(frame.lines):
            self.assertTrue(line.isascii(), line)

    def test_focus_marker_prefixes_exactly_one_card_header(self):
        v = view(
            four_card_fixture()[:3],
            ids=("claude", "codex", "zai"),
            focus="codex",
            **self.BASE,
        )
        frame = tui.render(v, 120, 40)
        marked = [line for line in text(frame.lines) if line.startswith("> ")]
        self.assertEqual(len(marked), 1)
        self.assertIn("Codex", marked[0])

    def test_focus_marker_is_ascii_and_clipped_at_compact_width(self):
        # 60x15 shows only the first cards; focus claude so the marker is
        # inside the initial viewport without needing a reveal.
        v = view(scroll_fixture(), focus="claude", **self.BASE)
        frame = tui.render(v, 60, 15, tui.ASCII_GLYPHS)
        marked = [line for line in text(frame.lines) if line.startswith("> ")]
        self.assertEqual(len(marked), 1)
        self.assertIn("Claude", marked[0])
        for line in text(frame.lines):
            self.assertTrue(line.isascii(), line)
            self.assertLessEqual(len(line), 59)

    def test_focus_marker_on_fetching_placeholder(self):
        v = view(None, loading=True, focus="zai")
        rendered = flat(tui.render(v, 80, 24))
        marked = [line for line in rendered.splitlines() if line.startswith("> ")]
        self.assertEqual(len(marked), 1)
        self.assertIn("Z.AI", marked[0])
        self.assertIn("fetching", marked[0])

    def test_no_focus_marker_without_focus(self):
        frame = tui.render(view(scroll_fixture(), **self.BASE), 120, 40)
        for line in text(frame.lines):
            self.assertFalse(line.startswith("> "), line)

    def test_collapsed_card_keeps_header_drops_window_rows(self):
        v = view(scroll_fixture(), collapsed={"claude"}, **self.BASE)
        rendered = flat(tui.render(v, 120, 40))
        self.assertIn("Claude", rendered)
        for claude_only in ("Opus", "Sonnet", "Fable"):
            self.assertNotIn(claude_only, rendered)
        self.assertIn("Codex", rendered)
        self.assertIn("Z.AI", rendered)

    def test_collapse_shrinks_max_scroll(self):
        base = view(scroll_fixture(), **self.BASE)
        collapsed = replace(base, collapsed=frozenset({"claude"}))
        open_frame = tui.render(base, 60, 15)
        shut_frame = tui.render(collapsed, 60, 15)
        self.assertGreater(open_frame.max_scroll, 0)
        # claude contributes 1 header + 6 window rows; collapsing removes 6.
        self.assertEqual(open_frame.max_scroll - shut_frame.max_scroll, 6)

    def test_footer_lists_focus_toolkit_keys(self):
        footer = tui._footer_text(view(), tui.UNICODE_GLYPHS, scrollable=True)
        for hint in (
            "r/R refresh",
            "tab focus",
            "h hide",
            "c collapse",
            "a show all",
            "q/esc quit",
        ):
            self.assertIn(hint, footer)

    def test_footer_keeps_quit_visible_at_minimum_width(self):
        frame = tui.render(view(scroll_fixture(), **self.BASE), 60, 15)
        footer = "".join(s.text for s in frame.lines[-1])
        self.assertIn("q/esc quit", footer)

    def test_reveal_focus_pulls_focused_header_into_view(self):
        base = view(scroll_fixture(), focus="zai", scroll=0, **self.BASE)
        without_reveal = tui.render(replace(base, reveal_focus=False), 60, 15)
        self.assertNotIn("Z.AI", flat(without_reveal))
        revealed = tui.render(replace(base, reveal_focus=True), 60, 15)
        self.assertIn("Z.AI", flat(revealed))
        self.assertGreater(revealed.offset, 0)
        self.assertLessEqual(revealed.offset, revealed.max_scroll)

    def test_reveal_focus_pulls_back_when_focused_card_is_above_the_viewport(self):
        base = view(scroll_fixture(), focus="claude", scroll=500, **self.BASE)
        without_reveal = tui.render(replace(base, reveal_focus=False), 60, 15)
        self.assertNotIn("Claude", flat(without_reveal))
        revealed = tui.render(replace(base, reveal_focus=True), 60, 15)
        self.assertIn("Claude", flat(revealed))
        self.assertEqual(revealed.offset, 0)

    def test_hidden_collapsed_and_focused_still_respect_width_limits(self):
        v = view(
            scroll_fixture(),
            hidden={"codex"},
            collapsed={"zai"},
            focus="zai",
            **self.BASE,
        )
        for width, height in ((60, 15), (80, 24), (120, 40)):
            with self.subTest(size=(width, height)):
                frame = tui.render(v, width, height)
                for line in text(frame.lines):
                    self.assertLessEqual(len(line), width - 1)


class FocusToolkitControllerTests(unittest.TestCase):
    """tab/h/a/c/R keys and the subset-refresh merge, through AppController."""

    @staticmethod
    def settle(controller, timeout=5):
        future = controller._future
        if future is not None:
            future.result(timeout=timeout)

    def _settled_controller(self, refresh_seconds=0):
        svc = FakeService()
        controller = tui.AppController(svc, refresh_seconds, now_fn=lambda: NOW)
        controller.start()
        svc._gate.set()
        self.settle(controller)
        controller.poll(NOW)
        return controller, svc

    def test_tab_cycles_forward_and_wraps(self):
        controller, _ = self._settled_controller()
        try:
            self.assertEqual(controller.focus, "claude")
            for expected in ("codex", "zai", "claude", "codex"):
                controller.handle_key(ord("\t"))
                self.assertEqual(controller.focus, expected)
        finally:
            controller.close()

    def test_shift_tab_cycles_backward_and_wraps(self):
        controller, _ = self._settled_controller()
        try:
            for expected in ("zai", "codex", "claude", "zai"):
                controller.handle_key(curses.KEY_BTAB)
                self.assertEqual(controller.focus, expected)
        finally:
            controller.close()

    def test_tab_with_single_visible_provider_holds_focus(self):
        controller, _ = self._settled_controller()
        try:
            controller.handle_key(ord("h"))  # hide claude -> codex
            controller.handle_key(ord("h"))  # hide codex -> zai
            self.assertEqual(controller.visible_ids, ("zai",))
            for _ in range(3):
                controller.handle_key(ord("\t"))
                self.assertEqual(controller.focus, "zai")
        finally:
            controller.close()

    def test_h_hides_focused_and_moves_focus_to_next_visible(self):
        controller, _ = self._settled_controller()
        try:
            controller.handle_key(ord("h"))
            self.assertEqual(controller.hidden, {"claude"})
            self.assertEqual(controller.focus, "codex")
            rendered = flat(tui.render(controller.view(NOW), 80, 24))
            self.assertNotIn("Claude", rendered)
            self.assertIn("Codex", rendered)
        finally:
            controller.close()

    def test_a_restores_hidden_and_collapsed_and_refreshes_every_provider(self):
        controller, svc = self._settled_controller()
        try:
            controller.handle_key(ord("h"))  # hide claude; focus moves to codex
            controller.handle_key(ord("c"))  # collapse codex
            calls_before = svc.calls
            controller.handle_key(ord("a"))
            self.assertEqual(controller.hidden, set())
            self.assertEqual(controller.collapsed, set())
            self.assertEqual(svc.calls, calls_before + 1)
            self.assertEqual(svc.requested[-1], ("claude", "codex", "zai"))
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)
        finally:
            controller.close()

    def test_r_refreshes_visible_ids_only(self):
        controller, svc = self._settled_controller()
        try:
            controller.handle_key(ord("h"))  # hide claude
            calls = svc.calls
            controller.handle_key(ord("r"))
            self.assertEqual(svc.calls, calls + 1)
            self.assertEqual(svc.requested[-1], ("codex", "zai"))
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)
        finally:
            controller.close()

    def test_capital_R_refreshes_only_the_focused_provider(self):
        controller, svc = self._settled_controller()
        try:
            before = {s.provider: s for s in controller.snapshots}
            controller.handle_key(ord("\t"))  # codex
            controller.handle_key(ord("R"))
            self.assertEqual(svc.requested[-1], ("codex",))
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)
            after = {s.provider: s for s in controller.snapshots}
            self.assertEqual(set(after), {"claude", "codex", "zai"})  # merge keeps all three
            self.assertIsNot(after["codex"], before["codex"])  # focused replaced
            self.assertIs(after["claude"], before["claude"])  # others untouched
            self.assertIs(after["zai"], before["zai"])
            self.assertEqual(
                [s.provider for s in controller.snapshots], ["claude", "codex", "zai"]
            )
        finally:
            controller.close()

    def test_focused_refresh_while_pending_keeps_pending_id_set(self):
        controller, svc = self._settled_controller()
        try:
            controller.handle_key(ord("r"))  # full visible refresh, pending
            calls = svc.calls
            controller.handle_key(ord("\t"))
            controller.handle_key(ord("\t"))
            controller.handle_key(ord("R"))  # suppressed while in flight
            self.assertEqual(svc.calls, calls)
            self.assertEqual(svc.requested[-1], ("claude", "codex", "zai"))
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)
        finally:
            controller.close()

    def test_focus_survives_hiding_and_restoring_across_reordering(self):
        controller, _ = self._settled_controller()
        try:
            controller.handle_key(ord("\t"))
            self.assertEqual(controller.focus, "codex")
            controller.handle_key(ord("h"))  # hide codex; focus moves to zai
            self.assertEqual(controller.focus, "zai")
            controller.handle_key(ord("a"))  # restore all; focus must stay zai
            self.assertEqual(controller.focus, "zai")
            marked = [
                line
                for line in text(tui.render(controller.view(NOW), 120, 40).lines)
                if line.startswith("> ")
            ]
            self.assertEqual(len(marked), 1)
            self.assertIn("Z.AI", marked[0])
        finally:
            controller.close()

    def test_refresh_key_after_future_resolves_is_deferred_until_poll(self):
        controller, svc = self._settled_controller()
        try:
            controller.handle_key(ord("R"))
            svc._gate.set()
            self.settle(controller)  # done, but not yet adopted by poll

            controller.handle_key(ord("R"))
            self.assertEqual(svc.calls, 2)
            self.assertEqual(controller._deferred_refresh_ids, ("claude",))

            controller.poll(NOW)
            self.assertEqual(svc.calls, 3)
            self.assertEqual(svc.requested[-1], ("claude",))
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)
        finally:
            controller.close()

    def test_a_after_future_resolves_defers_until_poll_adopts_result(self):
        controller, svc = self._settled_controller()
        try:
            controller.handle_key(ord("h"))  # hide claude
            controller.handle_key(ord("r"))  # refresh codex + zai
            svc._gate.set()
            self.settle(controller)  # result is done, but poll has not adopted it

            controller.handle_key(ord("a"))
            self.assertEqual(svc.calls, 2)
            self.assertTrue(controller._restore_pending)

            controller.poll(NOW)
            self.assertEqual(svc.calls, 3)
            self.assertEqual(svc.requested[-1], ("claude", "codex", "zai"))
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)
            self.assertEqual(
                [snapshot.provider for snapshot in controller.snapshots],
                ["claude", "codex", "zai"],
            )
        finally:
            controller.close()

    def test_a_during_inflight_refresh_defers_the_full_refresh(self):
        # The restore contract ("a restores all AND refreshes them") must
        # survive an in-flight subset refresh: `a` while `R` is pending
        # defers the full refresh instead of silently dropping it.
        controller, svc = self._settled_controller()
        try:
            controller.handle_key(ord("\t"))  # codex
            controller.handle_key(ord("R"))  # focused refresh, pending
            controller.handle_key(ord("h"))  # hide codex
            controller.handle_key(ord("a"))  # restore all while R is in flight
            self.assertEqual(controller.hidden, set())
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)  # adopts the focused refresh, submits the deferred one
            svc._gate.set()
            self.settle(controller)
            controller.poll(NOW)
            # start(all) -> R(codex) -> deferred restore(all): exactly three refreshes.
            self.assertEqual(len(svc.requested), 3)
            self.assertEqual(svc.calls, 3)
            self.assertEqual(svc.requested[-2:], [("codex",), ("claude", "codex", "zai")])
            self.assertEqual(
                [s.provider for s in controller.snapshots], ["claude", "codex", "zai"]
            )
        finally:
            controller.close()

    def test_a_during_inflight_refresh_defers_even_when_that_refresh_fails(self):
        class FlakyFocusedService(FakeService):
            def refresh(self, provider_ids=None):
                snapshots = super().refresh(provider_ids)
                if provider_ids == ("codex",):
                    raise RuntimeError("simulated focused-refresh failure")
                return snapshots

        svc = FlakyFocusedService()
        controller = tui.AppController(svc, 0, now_fn=lambda: NOW)
        controller.start()
        svc._gate.set()
        ControllerTests.settle(controller)
        controller.poll(NOW)
        try:
            controller.handle_key(ord("\t"))  # codex
            controller.handle_key(ord("R"))  # will fail once released
            controller.handle_key(ord("a"))  # deferred behind the failing refresh
            svc._gate.set()
            ControllerRefreshFailureTests.settle_ignoring_failure(controller)
            controller.poll(NOW)
            self.assertIsNotNone(controller.last_error)  # failure surfaced
            svc._gate.set()
            ControllerTests.settle(controller)
            controller.poll(NOW)
            self.assertEqual(svc.requested[-1], ("claude", "codex", "zai"))
            self.assertIsNone(controller.last_error)
        finally:
            controller.close()

    def test_c_toggles_collapse_of_the_focused_card(self):
        controller, _ = self._settled_controller()
        try:
            controller.handle_key(ord("c"))
            self.assertEqual(controller.view(NOW).collapsed, frozenset({"claude"}))
            self.assertEqual(controller.view(NOW).focus, "claude")
            controller.handle_key(ord("c"))
            self.assertEqual(controller.view(NOW).collapsed, frozenset())
        finally:
            controller.close()

    def test_r_with_all_providers_hidden_refreshes_nothing(self):
        controller, svc = self._settled_controller()
        try:
            for _ in range(3):
                controller.handle_key(ord("h"))  # hide claude, codex, zai
            self.assertEqual(controller.visible_ids, ())
            calls = svc.calls
            controller.handle_key(ord("r"))
            self.assertEqual(svc.calls, calls)
            self.assertFalse(controller.loading)
            rendered = flat(tui.render(controller.view(NOW), 60, 15))
            self.assertIn("all providers hidden", rendered)
            controller.handle_key(ord("a"))
            self.assertEqual(controller.visible_ids, ("claude", "codex", "zai"))
            self.assertEqual(controller.focus, "claude")
        finally:
            controller.close()

    def test_view_reveal_flag_is_one_shot(self):
        controller, _ = self._settled_controller()
        try:
            controller.handle_key(ord("\t"))
            self.assertTrue(controller.view(NOW).reveal_focus)
            self.assertFalse(controller.view(NOW).reveal_focus)
        finally:
            controller.close()


class ShutdownSubprocessTests(unittest.TestCase):
    """Process-level proof that close() does not delay interpreter exit.

    concurrent.futures.ThreadPoolExecutor registers an interpreter-exit
    hook that joins every worker thread it ever created, unconditionally
    — even when the thread is marked daemon — so this can only be proven
    by actually exiting a subprocess, not by timing close() in-process.
    """

    def test_close_during_in_flight_refresh_lets_process_exit_promptly(self):
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        script = f"""
import sys, time
sys.path.insert(0, {src_dir!r})
from llmits import tui

class SlowService:
    provider_ids = ("claude",)

    def refresh(self, provider_ids=None):
        time.sleep(2)
        return ()

controller = tui.AppController(SlowService(), 0)
controller.start()
controller.close()
"""
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 1.0, f"subprocess took {elapsed:.2f}s to exit")


class RunTuiSignatureTests(unittest.TestCase):
    def test_screen_is_first_parameter_for_curses_wrapper(self):
        import inspect

        parameters = list(inspect.signature(tui.run_tui).parameters)
        self.assertEqual(parameters[0], "screen", "curses.wrapper passes stdscr first")


class ScrollTests(unittest.TestCase):
    """At the documented 60x15 minimum every provider must stay reachable."""

    @staticmethod
    def full_three():
        return scroll_fixture()

    def visible_text(self, width, height, scroll):
        v = _replace_scroll(view(self.full_three(), last_refresh=NOW - timedelta(seconds=12)), scroll)
        return flat(tui.render(v, width, height))

    def test_full_dashboard_exceeds_minimum_height_and_clips_first_screen(self):
        v = view(self.full_three(), last_refresh=NOW - timedelta(seconds=12))
        top = tui.render(_replace_scroll(v, 0), 60, 15)
        self.assertGreater(top.max_scroll, 0, "fixture must overflow one screen for this test to mean anything")
        first = flat(top)
        self.assertIn("Claude", first)
        self.assertNotIn("Z.AI", first)

    def test_every_provider_reachable_by_scrolling_at_60x15(self):
        for width, height in ((60, 15), (80, 24)):
            v = view(self.full_three(), last_refresh=NOW - timedelta(seconds=12))
            top = tui.render(_replace_scroll(v, 0), width, height)
            seen = set()
            for offset in range(0, top.max_scroll + 1):
                frame = flat(tui.render(replace(v, scroll=offset), width, height))
                for name in ("Claude", "Codex", "Z.AI"):
                    if name in frame:
                        seen.add(name)
            self.assertEqual(seen, {"Claude", "Codex", "Z.AI"}, f"{width}x{height}")

    def test_max_scroll_shrinks_by_one_as_height_grows_by_one(self):
        # render() always pads its returned lines to exactly `height`, so
        # max_scroll can't be recovered by re-rendering "tall enough to fit
        # everything" and counting lines -- it must be read off the frame.
        v = view(self.full_three(), last_refresh=NOW - timedelta(seconds=12))
        shorter = tui.render(_replace_scroll(v, 0), 60, 15)
        taller = tui.render(_replace_scroll(v, 0), 60, 16)
        self.assertGreater(shorter.max_scroll, 0)
        self.assertEqual(shorter.max_scroll, taller.max_scroll + 1)

    def test_viewport_clamps_beyond_the_end(self):
        v = view(self.full_three(), last_refresh=NOW - timedelta(seconds=12))
        clamped = tui.render(replace(v, scroll=10_000), 60, 15)
        frame = flat(clamped)
        self.assertIn("Z.AI", frame)
        self.assertLessEqual(len(clamped.lines), 15)

    def test_scroll_keys_adjust_controller_state(self):
        controller = tui.AppController(FakeService(), 0)
        self.assertEqual(controller.scroll, 0)
        controller.handle_key(ord("k"))
        self.assertEqual(controller.scroll, 0)
        controller.handle_key(curses.KEY_UP)
        self.assertEqual(controller.scroll, 0)
        controller.handle_key(curses.KEY_DOWN)
        self.assertEqual(controller.scroll, 1)
        controller.handle_key(ord("j"))
        self.assertEqual(controller.scroll, 2)
        controller.handle_key(ord("k"))
        self.assertEqual(controller.scroll, 1)
        controller.close()

    def test_scroll_clamps_to_content_after_over_scroll(self):
        # Spec section 7: the offset can run to _scroll_cap (512) while the
        # viewport silently clamps at render time, so the user would have
        # to press k dozens of times before anything visibly moves. run_tui
        # adopts Frame.offset after every draw, which pulls the
        # controller's own state back down so a single k responds
        # immediately.
        controller = tui.AppController(FakeService(), 0)
        for _ in range(50):
            controller.handle_key(ord("j"))
        self.assertEqual(controller.scroll, 50)
        frame = tui.render(
            _replace_scroll(
                view(scroll_fixture(), last_refresh=NOW - timedelta(seconds=12)), 50
            ),
            60,
            15,
        )
        self.assertGreater(frame.max_scroll, 0)
        controller.scroll = frame.offset  # what run_tui does each frame
        self.assertLessEqual(controller.scroll, frame.max_scroll)
        self.assertGreaterEqual(controller.scroll, 0)
        controller.handle_key(ord("k"))
        self.assertEqual(controller.scroll, frame.offset - 1)
        controller.close()

    def test_visible_lines_never_exceed_width_at_any_offset(self):
        v = view(self.full_three(), last_refresh=NOW - timedelta(seconds=12))
        for offset in (0, 3, 7, 11, 10_000):
            for line in text(tui.render(replace(v, scroll=offset), 60, 15).lines):
                self.assertLessEqual(len(line), 60)


def _replace_scroll(view_obj, scroll):
    return replace(view_obj, scroll=scroll)


class HostileTextTests(unittest.TestCase):
    def test_hostile_plan_name_and_error_never_reach_tui_output(self):
        hostile = snapshot(
            "codex",
            plan_name="Codex \x1b]0;pwned\x07 pro",
            windows=(full_window(20),),
        )
        failed = snapshot(
            "zai",
            status=AUTH_REQUIRED,
            error=ProviderError(
                code=AUTH_REQUIRED,
                message="bad \x9b31m\x1b[31m value",
                action="export ZAI_API_KEY",
            ),
        )
        frame = tui.render(view((hostile, failed)), 120, 40)
        flattened = flat(frame)
        for control in ("\x1b", "\x9b", "\x07"):
            self.assertNotIn(control, flattened)
        self.assertIn("Z.AI", flattened)

    def test_format_characters_never_reach_output(self):
        # Category Cf: zero-width joiners/spaces and bidi overrides, hidden
        # inside a window label and a plan name.
        zwj, bidi_override, bom = "‍", "‮", "﻿"
        hostile_label = f"5h{zwj}{bidi_override}window{bom}"
        window = QuotaWindow(
            key="5h",
            label=hostile_label,
            used_percent=10,
            remaining_percent=90,
            reset_at=NOW + timedelta(hours=1),
            period_seconds=18000,
        )
        hostile = snapshot("claude", plan_name=f"Plan{zwj}X", windows=(window,))
        frame = tui.render(view((hostile,)), 120, 40)
        flattened = flat(frame)
        for sentinel in (zwj, bidi_override, bom):
            self.assertNotIn(sentinel, flattened)


class SelectGlyphsTests(unittest.TestCase):
    """_select_glyphs(encoding): UTF-8 (any spelling) -> unicode, else ascii."""

    def test_utf8_lowercase_hyphen(self):
        self.assertIs(tui._select_glyphs("utf-8"), tui.UNICODE_GLYPHS)

    def test_utf8_uppercase_no_hyphen(self):
        self.assertIs(tui._select_glyphs("UTF8"), tui.UNICODE_GLYPHS)

    def test_utf8_underscore(self):
        self.assertIs(tui._select_glyphs("utf_8"), tui.UNICODE_GLYPHS)

    def test_ascii_falls_back_to_ascii_glyphs(self):
        self.assertIs(tui._select_glyphs("ascii"), tui.ASCII_GLYPHS)

    def test_none_falls_back_to_ascii_glyphs(self):
        self.assertIs(tui._select_glyphs(None), tui.ASCII_GLYPHS)


class BuildAttrsTests(unittest.TestCase):
    """_build_attrs(has_colors, color_count): the three tiers of spec section 6."""

    def test_no_colour_tier_is_attributes_only(self):
        attrs = tui._build_attrs(has_colors=False, color_count=0)
        self.assertEqual(attrs[tui.STYLE_ACCENT], curses.A_BOLD)
        self.assertEqual(attrs[tui.STYLE_OK], 0)
        self.assertEqual(attrs[tui.STYLE_WARN], curses.A_BOLD)
        self.assertEqual(attrs[tui.STYLE_HIGH], curses.A_BOLD)
        self.assertEqual(attrs[tui.STYLE_DIM], curses.A_DIM)
        self.assertEqual(attrs[tui.STYLE_TRACK], curses.A_DIM)
        self.assertEqual(attrs[tui.STYLE_NORMAL], 0)

    def test_256_colour_tier_allocates_one_pair_per_style(self):
        from unittest.mock import patch

        with patch.object(curses, "init_pair") as mock_init_pair, patch.object(
            curses, "color_pair", side_effect=lambda n: n
        ):
            attrs = tui._build_attrs(has_colors=True, color_count=256)
        self.assertEqual(mock_init_pair.call_count, len(tui._TIER_256))
        # Every 256-tier style, including DIM/TRACK, gets its own colour pair.
        for style in tui._TIER_256:
            self.assertNotEqual(attrs[style], 0)

    def test_eight_colour_tier_falls_back_to_dim_attribute_for_dim_and_track(self):
        from unittest.mock import patch

        with patch.object(curses, "init_pair") as mock_init_pair, patch.object(
            curses, "color_pair", side_effect=lambda n: n
        ):
            attrs = tui._build_attrs(has_colors=True, color_count=8)
        self.assertEqual(mock_init_pair.call_count, len(tui._TIER_8))
        self.assertEqual(attrs[tui.STYLE_DIM], curses.A_DIM)
        self.assertEqual(attrs[tui.STYLE_TRACK], curses.A_DIM)


class InstantService:
    """Resolves refresh() immediately with one snapshot; for run_tui smoke tests."""

    provider_ids = ("claude",)

    def refresh(self, provider_ids=None):
        return (snapshot(),)


class FakeScreen:
    """Records addnstr/getch/getmaxyx/erase/refresh/timeout/keypad calls.

    ``addnstr`` raises ``curses.error`` when a write would land on the
    bottom-right cell, just like a real terminal (which cannot advance the
    cursor past the edge of the screen) — proving ``_draw``'s per-segment
    try/except actually gets exercised rather than assumed.
    """

    def __init__(self, height, width, keys):
        self.height = height
        self.width = width
        self._keys = list(keys)
        self.calls = []
        self.timeout_ms = None
        self.keypad_on = None

    def erase(self):
        self.calls.append(("erase",))

    def getmaxyx(self):
        return (self.height, self.width)

    def addnstr(self, row, col, text_, n, attr=0):
        self.calls.append(("addnstr", row, col, text_, n, attr))
        if row == self.height - 1:
            # A real terminal refuses any write that could leave the
            # cursor needing to advance past the bottom-right cell; the
            # call is still recorded above so the test can see it was
            # attempted before _draw's try/except swallows this.
            raise curses.error("bottom right corner")

    def getch(self):
        self.calls.append(("getch",))
        if self._keys:
            return self._keys.pop(0)
        return ord("q")

    def refresh(self):
        self.calls.append(("refresh",))

    def timeout(self, ms):
        self.timeout_ms = ms
        self.calls.append(("timeout", ms))

    def keypad(self, flag):
        self.keypad_on = flag
        self.calls.append(("keypad", flag))


class RuntimeTests(unittest.TestCase):
    """run_tui/_draw driven through a fake screen (folded finding rank 14)."""

    def _run(self, screen, service=None, refresh_seconds=0, color_count=256):
        from unittest.mock import patch

        service = service or InstantService()
        with patch.object(curses, "has_colors", return_value=True), patch.object(
            curses, "start_color"
        ), patch.object(curses, "use_default_colors"), patch.object(
            curses, "init_pair"
        ) as mock_init_pair, patch.object(
            curses, "color_pair", return_value=0
        ), patch.object(
            curses, "curs_set"
        ), patch.object(
            curses, "COLORS", color_count, create=True
        ), patch.object(
            tui, "_build_attrs", wraps=tui._build_attrs
        ) as mock_build_attrs, patch.object(
            curses, "set_escdelay", create=True
        ) as mock_escdelay:
            code = tui.run_tui(screen, service, refresh_seconds)
        self.last_escdelay = mock_escdelay
        return code, mock_init_pair, mock_build_attrs

    def test_escape_delay_set_once_per_run_to_at_most_50ms(self):
        # llmits-o03.10: Escape must quit as promptly as q; ncurses' default
        # ESCDELAY of 1000 ms made it take about a second.
        screen = FakeScreen(height=24, width=80, keys=[ord("j"), ord("q")])
        self._run(screen)
        self.assertEqual(self.last_escdelay.call_count, 1)
        (delay,), _ = self.last_escdelay.call_args
        self.assertLessEqual(delay, 50)

    def test_missing_set_escdelay_does_not_abort_startup(self):
        from unittest.mock import patch

        screen = FakeScreen(height=24, width=80, keys=[ord("q")])
        with patch.object(curses, "set_escdelay", None, create=True):
            code, _, _ = self._run(screen)
        self.assertEqual(code, 0)

    def test_pressing_q_quits_and_returns_zero(self):
        screen = FakeScreen(height=24, width=80, keys=[ord("q")])
        code, _, _ = self._run(screen)
        self.assertEqual(code, 0)

    def test_esc_also_quits(self):
        screen = FakeScreen(height=24, width=80, keys=[27])
        code, _, _ = self._run(screen)
        self.assertEqual(code, 0)

    def test_attribute_table_built_exactly_once_per_run(self):
        # Several loop iterations (scroll, then quit) must not rebuild the
        # table: previously curses.init_pair ran inside _draw, on every
        # frame.
        screen = FakeScreen(height=24, width=80, keys=[ord("j"), ord("j"), ord("k"), ord("q")])
        _, mock_init_pair, mock_build_attrs = self._run(screen)
        self.assertEqual(mock_build_attrs.call_count, 1)
        # 256-colour tier allocates one pair per style in _TIER_256.
        self.assertEqual(mock_init_pair.call_count, len(tui._TIER_256))

    def test_footer_written_on_last_row(self):
        screen = FakeScreen(height=24, width=80, keys=[ord("q")])
        self._run(screen)
        footer_calls = [
            call for call in screen.calls if call[0] == "addnstr" and call[1] == screen.height - 1
        ]
        self.assertTrue(footer_calls, "expected at least one addnstr on the last row")
        self.assertTrue(any("quit" in call[3] for call in footer_calls))

    def test_survives_bottom_right_cell_curses_error(self):
        # FakeScreen raises curses.error writing the footer's last cell;
        # run_tui must still complete the loop and quit cleanly.
        screen = FakeScreen(height=24, width=80, keys=[ord("q")])
        code, _, _ = self._run(screen)
        self.assertEqual(code, 0)
        self.assertTrue(any(call[0] == "refresh" for call in screen.calls))

    def test_eight_colour_tier_used_when_fewer_than_256_colours(self):
        screen = FakeScreen(height=24, width=80, keys=[ord("q")])
        _, mock_init_pair, _ = self._run(screen, color_count=8)
        self.assertEqual(mock_init_pair.call_count, len(tui._TIER_8))


class LocaleSetupTests(unittest.TestCase):
    """cli._run_tui: locale.setlocale(LC_ALL, '') before curses.wrapper."""

    def _run_cli_tui(self, setlocale_side_effect=None):
        import locale
        from unittest.mock import patch

        from llmits import cli

        with patch.object(
            locale, "setlocale", side_effect=setlocale_side_effect
        ) as mock_setlocale, patch.object(
            curses, "wrapper", return_value=0
        ), patch.object(cli.RefreshService, "close", autospec=True):
            code = cli._run_tui(("claude",), lambda: object(), {"claude": lambda: "x"}, 0)
        return code, mock_setlocale

    def test_setlocale_called_once_with_lc_all_and_empty_string(self):
        import locale

        code, mock_setlocale = self._run_cli_tui()
        self.assertEqual(code, 0)
        mock_setlocale.assert_called_once_with(locale.LC_ALL, "")

    def test_locale_error_is_swallowed(self):
        import locale

        code, mock_setlocale = self._run_cli_tui(setlocale_side_effect=locale.Error("boom"))
        self.assertEqual(code, 0)
        mock_setlocale.assert_called_once_with(locale.LC_ALL, "")


class AsciiGlyphPurityTests(unittest.TestCase):
    """llmits-o03.7: under ASCII_GLYPHS no code point above U+007F reaches any segment.

    Walks every FOUR_CARD_SIZES entry and every scroll offset, so the
    auth-required card's action line (which once carried a hard-coded
    U+2014) is on screen for at least one frame at every size.
    """

    IDS = ("claude", "codex", "zai", "zai")

    def test_four_card_fixture_is_pure_ascii_at_every_size_and_scroll(self):
        snapshots = four_card_fixture()
        seen_action_line = False
        for width, height in FOUR_CARD_SIZES:
            first = tui.render(view(snapshots, ids=self.IDS), width, height, tui.ASCII_GLYPHS)
            for offset in range(first.max_scroll + 1):
                frame = tui.render(
                    view(snapshots, ids=self.IDS, scroll=offset), width, height, tui.ASCII_GLYPHS
                )
                for line in frame:
                    for segment in line:
                        self.assertTrue(
                            segment.text.isascii(), (width, height, offset, segment.text)
                        )
                if "provider rejected the credentials - check" in flat(frame):
                    seen_action_line = True
        self.assertTrue(seen_action_line)

    def test_unicode_detail_join_still_uses_the_spec_em_dash(self):
        frame = tui.render(view(four_card_fixture(), ids=self.IDS), 120, 40)
        self.assertIn("provider rejected the credentials \u2014 check your ZAI_API_KEY", flat(frame))


class FoldedFindingGapTests(unittest.TestCase):
    """llmits-o03.8: ranks 17, 27 and 121 proven at the level the findings asked for."""

    IDS = ("claude", "codex", "zai")

    def test_view_without_now_uses_the_injected_clock(self):
        # rank 17: AppController.view() with no argument must consult now_fn.
        clock = NOW + timedelta(minutes=3)
        controller = tui.AppController(FakeService(), 300, now_fn=lambda: clock)
        self.assertEqual(controller.view().now, clock)
        self.assertEqual(controller.view(NOW).now, NOW)

    def test_updated_text_present_and_absent_at_compact_and_wide_widths(self):
        # rank 27: the header's "updated ..." branch, through a full render,
        # in both the compact (<= 80) and the wide layout.
        snaps = four_card_fixture()[:3]
        for width, height in ((80, 24), (120, 40)):
            with_refresh = flat(
                tui.render(
                    view(snaps, ids=self.IDS, last_refresh=NOW - timedelta(seconds=12)), width, height
                )
            )
            self.assertIn("updated 12s ago", with_refresh, (width, height))
            without = flat(tui.render(view(snaps, ids=self.IDS, last_refresh=None), width, height))
            self.assertNotIn("updated", without, (width, height))

    def test_label_segment_is_exactly_label_width_code_points(self):
        # rank 121: the per-label slice to L is proven on the label segment
        # itself, independent of the outer width-1 clip.
        for label in ("日本語ラベルテキスト" * 3, "🎉" * 20):
            window = pct_window("w", label, 50, reset_at=NOW + timedelta(hours=1))
            line = tui._window_line(window, tui.UNICODE_GLYPHS, NOW, tui.LABEL_MAX, 20, False)
            segment = line[0].text
            self.assertEqual(segment, f"  {label[:tui.LABEL_MAX]} ")
            self.assertEqual(len(segment), 2 + tui.LABEL_MAX + 1)
            self.assertTrue(label.startswith(segment.strip()))


class TerminalCodesetTests(unittest.TestCase):
    """llmits-o03.9: the glyph set follows the C-library codeset, not sys.stdout.encoding."""

    def test_utf8_codeset_selects_unicode(self):
        from unittest.mock import patch

        with patch.object(tui.locale, "nl_langinfo", return_value="UTF-8", create=True):
            self.assertIs(tui._select_glyphs(tui._terminal_codeset()), tui.UNICODE_GLYPHS)

    def test_c_locale_codeset_selects_ascii_even_when_stdout_claims_utf8(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        utf8_stdout = SimpleNamespace(encoding="utf-8")
        with patch.object(tui.locale, "nl_langinfo", return_value="ANSI_X3.4-1968", create=True):
            codeset = tui._terminal_codeset(stream=utf8_stdout)
        self.assertEqual(codeset, "ANSI_X3.4-1968")
        self.assertIs(tui._select_glyphs(codeset), tui.ASCII_GLYPHS)

    def test_falls_back_to_stream_encoding_without_nl_langinfo(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        with patch.object(tui, "locale", SimpleNamespace()):
            self.assertEqual(tui._terminal_codeset(stream=SimpleNamespace(encoding="ascii")), "ascii")
            self.assertEqual(tui._terminal_codeset(stream=SimpleNamespace(encoding="UTF8")), "UTF8")
            self.assertIsNone(tui._terminal_codeset(stream=SimpleNamespace()))

    def test_empty_or_failing_nl_langinfo_falls_back_to_stream_encoding(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        with patch.object(tui.locale, "nl_langinfo", return_value="", create=True):
            self.assertEqual(tui._terminal_codeset(stream=SimpleNamespace(encoding="ascii")), "ascii")
        with patch.object(tui.locale, "nl_langinfo", side_effect=ValueError("no CODESET"), create=True):
            self.assertEqual(tui._terminal_codeset(stream=SimpleNamespace(encoding="utf-8")), "utf-8")


if __name__ == "__main__":
    unittest.main()
