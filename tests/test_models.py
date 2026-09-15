import unicodedata
import unittest

from llmits import models

# The Unicode format (Cf) code points named in the remediation bead: bidi
# marks and zero-widths, bidi embeddings/overrides, bidi isolates, BOM.
LISTED_FORMAT_CHARS = (
    [chr(c) for c in range(0x200B, 0x2010)]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + ["\ufeff"]
)


class TestQuotaWindow(unittest.TestCase):
    def test_is_immutable(self):
        window = models.QuotaWindow(key="5h", label="5-hour", used_percent=10, remaining_percent=90)
        with self.assertRaises(Exception):
            window.used_percent = 20  # type: ignore[misc]

    def test_blank_key_falls_back_to_window(self):
        window = models.QuotaWindow(key="   ", label="5-hour", used_percent=1, remaining_percent=99)
        self.assertEqual(window.key, "window")

    def test_blank_label_falls_back_to_sanitized_key(self):
        window = models.QuotaWindow(key="5h", label="  ", used_percent=1, remaining_percent=99)
        self.assertEqual(window.label, "5h")

    def test_blank_key_and_label_both_fall_back_to_window(self):
        # Two distinct windows that both sanitize to blank key/label would
        # otherwise silently collapse into the same "window" identity.
        window = models.QuotaWindow(key="\x00", label="", used_percent=1, remaining_percent=99)
        self.assertEqual(window.key, "window")
        self.assertEqual(window.label, "window")


class TestQuotaWindowFromPercent(unittest.TestCase):
    def test_derives_complementary_remaining_percent(self):
        window = models.QuotaWindow.from_percent(key="5h", label="5-hour", percent=42.4)
        self.assertEqual(window.used_percent, 42)
        self.assertEqual(window.remaining_percent, 58)

    def test_bounds_out_of_range_percent(self):
        window = models.QuotaWindow.from_percent(key="5h", label="5-hour", percent=180)
        self.assertEqual(window.used_percent, 100)
        self.assertEqual(window.remaining_percent, 0)

    def test_non_numeric_percent_becomes_zero_used_full_remaining(self):
        window = models.QuotaWindow.from_percent(key="5h", label="5-hour", percent="not a number")
        self.assertEqual(window.used_percent, 0)
        self.assertEqual(window.remaining_percent, 100)

    def test_passes_through_other_fields(self):
        from datetime import datetime, timezone

        reset = datetime(2026, 1, 1, tzinfo=timezone.utc)
        window = models.QuotaWindow.from_percent(
            key="5h",
            label="5-hour",
            percent=10,
            reset_at=reset,
            period_seconds=18000,
            used_value=1,
            limit_value=10,
            remaining_value=9,
        )
        self.assertEqual(window.reset_at, reset)
        self.assertEqual(window.period_seconds, 18000)
        self.assertEqual((window.used_value, window.limit_value, window.remaining_value), (1, 10, 9))


class TestBoundedPercent(unittest.TestCase):
    def test_rounds_and_clamps(self):
        self.assertEqual(models.bounded_percent(42.4), 42)
        self.assertEqual(models.bounded_percent(42.5), 42)  # banker's rounding
        self.assertEqual(models.bounded_percent(43.5), 44)
        self.assertEqual(models.bounded_percent(-5), 0)
        self.assertEqual(models.bounded_percent(180), 100)
        self.assertEqual(models.bounded_percent("not a number"), 0)
        self.assertEqual(models.bounded_percent(None), 0)


class TestProviderError(unittest.TestCase):
    def test_rejects_available_code(self):
        with self.assertRaises(ValueError):
            models.ProviderError(code="available", message="x", action="y")

    def test_accepts_error_codes(self):
        for code in models.ERROR_CODES:
            err = models.ProviderError(code=code, message="m", action="a")
            self.assertEqual(err.code, code)

    def test_sanitizes_control_characters_and_caps_length(self):
        err = models.ProviderError(
            code="parse_error",
            message="bad \x1b[31m\x00 value " + "x" * 500,
            action="a\tb",
        )
        self.assertNotIn("\x1b", err.message)
        self.assertNotIn("\x00", err.message)
        self.assertNotIn("\t", err.action)
        self.assertLessEqual(len(err.message), models.MAX_TEXT)
        self.assertLessEqual(len(err.action), models.MAX_TEXT)

    def test_plain_messages_survive_unchanged(self):
        err = models.ProviderError(
            code="auth_required", message="no credentials found", action="run claude login"
        )
        self.assertEqual(err.message, "no credentials found")
        self.assertEqual(err.action, "run claude login")


class TestProviderSnapshot(unittest.TestCase):
    def test_valid_snapshot(self):
        from datetime import datetime, timezone

        window = models.QuotaWindow(key="5h", label="5h", used_percent=1, remaining_percent=99)
        snap = models.ProviderSnapshot(
            provider="claude",
            status=models.AVAILABLE,
            plan_name="Claude Pro/Max",
            fetched_at=datetime.now(timezone.utc),
            windows=(window,),
        )
        self.assertEqual(snap.windows, (window,))

    def test_rejects_unknown_status(self):
        from datetime import datetime, timezone

        with self.assertRaises(ValueError):
            models.ProviderSnapshot(
                provider="claude",
                status="exploded",
                plan_name=None,
                fetched_at=datetime.now(timezone.utc),
            )

    def test_coerces_window_list_to_tuple(self):
        from datetime import datetime, timezone

        window = models.QuotaWindow(key="5h", label="5h", used_percent=1, remaining_percent=99)
        snap = models.ProviderSnapshot(
            provider="claude",
            status=models.AVAILABLE,
            plan_name=None,
            fetched_at=datetime.now(timezone.utc),
            windows=[window],
        )
        self.assertIsInstance(snap.windows, tuple)


class TestSanitizationHardening(unittest.TestCase):
    def test_sanitize_strips_c1_and_osc_sequences(self):
        hostile = "pro\x9b31m\x1b]0;evil\x07plan"
        cleaned = models.sanitize_text(hostile)
        for bad in ("\x9b", "\x1b", "\x07"):
            self.assertNotIn(bad, cleaned)
        self.assertIn("pro", cleaned)
        self.assertIn("plan", cleaned)

    def test_bounded_percent_rejects_infinite_and_nan(self):
        self.assertEqual(models.bounded_percent(float("inf")), 0)
        self.assertEqual(models.bounded_percent(float("-inf")), 0)
        self.assertEqual(models.bounded_percent(float("nan")), 0)
        self.assertEqual(models.bounded_percent(1e999), 0)
        self.assertEqual(models.bounded_percent(-1e999), 0)

    def test_bounded_int_rejects_non_finite(self):
        self.assertEqual(models.bounded_int(1e999), 0)
        self.assertEqual(models.bounded_int(float("nan")), 0)
        self.assertEqual(models.bounded_int("junk"), 0)
        self.assertEqual(models.bounded_int(42.6), 43)
        self.assertEqual(models.bounded_int(-5), 0)

    def test_oversized_integer_provider_values_become_zero(self):
        oversized = 10**310
        self.assertEqual(models.bounded_percent(oversized), 0)
        self.assertEqual(models.bounded_int(oversized), 0)

    def test_snapshot_sanitizes_and_bounds_plan_name(self):
        from datetime import datetime, timezone

        hostile = "Plan\x1b[31m " + "x" * 500
        snap = models.ProviderSnapshot(
            provider="codex",
            status=models.AVAILABLE,
            plan_name=hostile,
            fetched_at=datetime.now(timezone.utc),
        )
        self.assertNotIn("\x1b", snap.plan_name)
        self.assertLessEqual(len(snap.plan_name), models.MAX_PLAN_NAME)

    def test_snapshot_blank_plan_name_becomes_none(self):
        from datetime import datetime, timezone

        snap = models.ProviderSnapshot(
            provider="codex",
            status=models.AVAILABLE,
            plan_name="  ",
            fetched_at=datetime.now(timezone.utc),
        )
        self.assertIsNone(snap.plan_name)


class TestUnicodeFormatStripping(unittest.TestCase):
    def test_listed_code_points_are_all_format_characters(self):
        # Guard the fixture: every listed code point really is category Cf,
        # so the assertions below exercise the Cf rule and not whitespace
        # collapsing or the control-character regex.
        for char in LISTED_FORMAT_CHARS:
            self.assertEqual(unicodedata.category(char), "Cf", f"U+{ord(char):04X}")

    def test_sanitize_drops_each_listed_format_character(self):
        for char in LISTED_FORMAT_CHARS:
            cleaned = models.sanitize_text(f"Op{char}us")
            self.assertNotIn(char, cleaned, f"U+{ord(char):04X} survived")
            # Dropped, not replaced by a space: zero-widths must not split words.
            self.assertEqual(cleaned, "Opus", f"U+{ord(char):04X}")

    def test_sanitize_drops_every_bmp_format_character(self):
        bmp_cf = [chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Cf"]
        self.assertGreaterEqual(len(bmp_cf), len(LISTED_FORMAT_CHARS))
        cleaned = models.sanitize_text("a" + "".join(bmp_cf) + "b")
        self.assertEqual(cleaned, "ab")

    def test_sanitize_drops_supplementary_plane_format_characters(self):
        # Tag characters (U+E0000 block) enable invisible ASCII smuggling;
        # U+1D173 is a musical-symbol format control; U+110BD is Kaithi.
        for char in ("\U000E0001", "\U000E0041", "\U000E007F", "\U0001D173", "\U000110BD"):
            self.assertEqual(unicodedata.category(char), "Cf")
            self.assertEqual(models.sanitize_text(f"a{char}b"), "ab")

    def test_sanitize_drops_lone_surrogates(self):
        # json.loads('"\\ud800"') yields a lone surrogate that cannot be
        # encoded for the terminal; it is dropped at the boundary.
        self.assertEqual(models.sanitize_text("a\ud800b\udfff"), "ab")

    def test_sanitize_keeps_ordinary_non_ascii_letters(self):
        self.assertEqual(models.sanitize_text("café naïve 東京"), "café naïve 東京")

    def test_sanitize_still_replaces_controls_with_a_space(self):
        self.assertEqual(models.sanitize_text("a\x1bb\x9cc"), "a b c")

    def test_docstring_claims_match_behaviour(self):
        doc = models.sanitize_text.__doc__
        self.assertIn("control-free", doc)
        self.assertIn("format-character-free", doc)
        self.assertIn("Cf", models.__doc__ + doc)

    def test_window_key_and_label_are_format_free(self):
        window = models.QuotaWindow(
            key="5h\u202e\u200b", label="5-hour\ufeff window\u2066", used_percent=1, remaining_percent=99
        )
        self.assertEqual(window.key, "5h")
        self.assertEqual(window.label, "5-hour window")

    def test_error_and_plan_name_are_format_free(self):
        from datetime import datetime, timezone

        err = models.ProviderError(code="parse_error", message="bad\u202e value", action="do\u200d it")
        self.assertEqual((err.message, err.action), ("bad value", "do it"))
        snap = models.ProviderSnapshot(
            provider="codex",
            status=models.AVAILABLE,
            plan_name="Pro\u2069 Plan\ufeff",
            fetched_at=datetime.now(timezone.utc),
        )
        self.assertEqual(snap.plan_name, "Pro Plan")


class TestVettedLabel(unittest.TestCase):
    def test_accepts_plain_display_names(self):
        for name in ("Opus", "Sonnet 4.5", "Claude Opus 4.1", "5h Token", "MCP usage (monthly)", "Pro/Max+"):
            self.assertEqual(models.vetted_label(name), name)

    def test_sanitizes_before_vetting(self):
        self.assertEqual(models.vetted_label("\ufeffWeekly\u200b Token \u202e"), "Weekly Token")
        self.assertEqual(models.vetted_label("  Opus\t"), "Opus")

    def test_rejects_non_strings_and_blank(self):
        for value in (None, 5, b"Opus", ["Opus"], "", "   ", "\u200b"):
            self.assertIsNone(models.vetted_label(value), repr(value))

    def test_rejects_non_ascii_and_shell_or_markup_characters(self):
        for value in ("\u041epus", "café", "東京", "<b>Opus</b>", "a;b", "a|b", "a`b", "a$b", "a\\b", "a\"b", "a'b"):
            self.assertIsNone(models.vetted_label(value), repr(value))

    def test_rejects_leading_punctuation(self):
        for value in (".hidden", "-flag", "(x)", "/x", "+x"):
            self.assertIsNone(models.vetted_label(value), repr(value))

    def test_rejects_over_length_instead_of_truncating(self):
        self.assertEqual(models.vetted_label("x" * models.MAX_VETTED_LABEL), "x" * models.MAX_VETTED_LABEL)
        self.assertIsNone(models.vetted_label("x" * (models.MAX_VETTED_LABEL + 1)))
        self.assertIsNone(models.vetted_label("x" * 500))
        self.assertEqual(models.vetted_label("abcdef", limit=6), "abcdef")
        self.assertIsNone(models.vetted_label("abcdefg", limit=6))


if __name__ == "__main__":
    unittest.main()
