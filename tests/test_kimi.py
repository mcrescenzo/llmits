"""Kimi Coding Plan adapter tests (invented fixtures; no live requests)."""
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from llmits.http import TransportError
from llmits.models import AVAILABLE, AUTH_REQUIRED, PARSE_ERROR, UNAVAILABLE
from llmits.providers import kimi

FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "kimi-sentinel-key"
NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class Response:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}


class FakeTransport:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def get(self, host, path, headers):
        self.calls.append((host, path, headers))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class KimiParseTests(unittest.TestCase):
    def test_full_fixture_windows(self):
        windows = kimi.parse_usage(fixture("kimi_usages_full.json"))
        self.assertEqual([w.key for w in windows], ["5h", "weekly"])
        self.assertEqual([w.label for w in windows], ["5h", "7d"])
        five, weekly = windows
        # (6000 - 3480) / 6000 == 42%, from string-typed numbers.
        self.assertEqual(five.used_percent, 42)
        self.assertEqual((five.used_value, five.limit_value, five.remaining_value), (2520, 6000, 3480))
        self.assertEqual(five.period_seconds, 5 * 3600)
        self.assertEqual(five.reset_at, datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc))
        # (300 - 174) / 300 == 42%.
        self.assertEqual(weekly.used_percent, 42)
        self.assertEqual(weekly.period_seconds, 7 * 86400)
        self.assertEqual(weekly.reset_at, datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc))

    def test_plain_numbers_and_epoch_reset_are_accepted(self):
        payload = {
            "usage": {"limit": 10, "remaining": 2, "reset_at": 1782956800},
            "limits": [
                {"duration": 5, "timeUnit": "TIME_UNIT_HOUR",
                 "detail": {"limit": 10, "remaining": 5, "resets_at": 1782956800}}
            ],
        }
        windows = kimi.parse_usage(payload)
        self.assertEqual([w.key for w in windows], ["5h", "weekly"])
        self.assertEqual(windows[0].used_percent, 50)
        self.assertEqual(windows[1].used_percent, 80)
        self.assertEqual(
            windows[0].reset_at, datetime(2026, 7, 2, 1, 46, 40, tzinfo=timezone.utc)
        )

    def test_numeric_string_epoch_reset_is_parsed(self):
        # "1782956800" is not RFC 3339; it must fall through to the epoch
        # path instead of being dropped (the docstring promises this form).
        payload = {"usage": {"limit": "10", "remaining": "2", "resetTime": "1782956800"}}
        (window,) = kimi.parse_usage(payload)
        self.assertEqual(
            window.reset_at, datetime(2026, 7, 2, 1, 46, 40, tzinfo=timezone.utc)
        )

    def test_non_timestamp_reset_strings_stay_none(self):
        payload = {"usage": {"limit": "10", "remaining": "2", "resetTime": "not-a-time"}}
        (window,) = kimi.parse_usage(payload)
        self.assertIsNone(window.reset_at)

    def test_used_field_overrides_derived_difference(self):
        payload = {"usage": {"limit": 100, "remaining": 10, "used": 25}}
        (window,) = kimi.parse_usage(payload)
        self.assertEqual(window.used_percent, 25)
        self.assertEqual(window.remaining_percent, 75)

    def test_counts_on_the_entry_itself_when_detail_is_absent(self):
        payload = {
            "limits": [
                {"duration": "300", "timeUnit": "TIME_UNIT_MINUTE",
                 "limit": "10", "remaining": "5"}
            ]
        }
        (window,) = kimi.parse_usage(payload)
        self.assertEqual(window.key, "5h")
        self.assertEqual(window.used_percent, 50)

    def test_non_five_hour_limit_entries_are_skipped(self):
        payload = {
            "limits": [
                {"duration": "1", "timeUnit": "TIME_UNIT_DAY",
                 "detail": {"limit": "10", "remaining": "5"}},
                {"duration": "300", "timeUnit": "TIME_UNIT_MINUTE",
                 "detail": {"limit": "10", "remaining": "2"}},
            ],
            "usage": None,
        }
        windows = kimi.parse_usage(payload)
        self.assertEqual([w.key for w in windows], ["5h"])
        self.assertEqual(windows[0].used_percent, 80)

    def test_limits_without_a_usable_duration_or_unit_are_skipped(self):
        payload = {
            "limits": [
                {"detail": {"limit": "10", "remaining": "5"}},  # no duration
                {"duration": "300", "detail": {"limit": "10", "remaining": "5"}},  # no unit
                {"duration": "many", "timeUnit": "TIME_UNIT_MINUTE",
                 "detail": {"limit": "10", "remaining": "5"}},  # non-numeric
            ]
        }
        self.assertEqual(kimi.parse_usage(payload), ())

    def test_colliding_or_unknown_unit_tokens_are_rejected(self):
        # Substring matching would alias NANOSECOND to SECOND and accept
        # duration 18000 as a 5-hour window; exact aliases must not.
        for unit in ("TIME_UNIT_NANOSECOND", "TIME_UNIT_MICROSECOND", "FORTNIGHT", ""):
            with self.subTest(unit=unit):
                payload = {
                    "limits": [
                        {
                            "duration": 18000,
                            "timeUnit": unit,
                            "detail": {"limit": "10", "remaining": "5"},
                        }
                    ]
                }
                self.assertEqual(kimi.parse_usage(payload), ())

    def test_unit_aliases_match_exactly_and_case_insensitively(self):
        for unit, duration in (("time_unit_minute", "300"), ("MINUTE", 300), ("Hour", 5)):
            with self.subTest(unit=unit):
                payload = {
                    "limits": [
                        {
                            "duration": duration,
                            "timeUnit": unit,
                            "detail": {"limit": "10", "remaining": "5"},
                        }
                    ]
                }
                (window,) = kimi.parse_usage(payload)
                self.assertEqual(window.key, "5h")

    def test_non_positive_or_missing_limits_yield_no_window(self):
        for usage in (
            {"limit": "0", "remaining": "0"},
            {"limit": "-5", "remaining": "-1"},
            {"remaining": "5"},
            {"limit": "5"},
            {"limit": "NaN", "remaining": "1"},
        ):
            with self.subTest(usage=usage):
                self.assertEqual(kimi.parse_usage({"usage": usage}), ())

    def test_used_greater_than_limit_clamps_to_full(self):
        (window,) = kimi.parse_usage({"usage": {"limit": 10, "remaining": -5}})
        self.assertEqual(window.used_percent, 100)
        self.assertEqual(window.remaining_percent, 0)
        self.assertEqual(window.remaining_value, 0)

    def test_unrecognized_concepts_are_dropped_not_parsed(self):
        # totalQuota / parallel / user are documented-but-unverified concepts;
        # they must never produce windows or leak into any output text.
        payload = fixture("kimi_usages_full.json")
        windows = kimi.parse_usage(payload)
        self.assertEqual([w.key for w in windows], ["5h", "weekly"])


class KimiFetchTests(unittest.TestCase):
    def _fetch(self, *script):
        transport = FakeTransport(*script)
        snapshot = kimi.fetch(SENTINEL, transport, now=NOW)
        return snapshot, transport

    def test_success_snapshot_uses_fixed_local_plan_name(self):
        snapshot, transport = self._fetch(
            Response(200, (FIXTURES / "kimi_usages_full.json").read_bytes())
        )
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(snapshot.provider, "kimi")
        self.assertEqual(snapshot.plan_name, "Kimi Coding Plan")
        self.assertEqual(snapshot.fetched_at, NOW)
        # The payload's account identifiers never survive the adapter.
        flat = snapshot.plan_name or ""
        self.assertNotIn("invented-user-1", flat)
        (host, path, headers), = transport.calls
        self.assertEqual((host, path), (kimi.HOST, kimi.PATH))
        self.assertEqual(headers["Authorization"], f"Bearer {SENTINEL}")

    def test_empty_object_fails_closed_to_parse_error(self):
        snapshot, _ = self._fetch(Response(200, b"{}"))
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertIsNone(snapshot.plan_name)

    def test_recognizable_shape_absent_is_parse_error_not_guess(self):
        snapshot, _ = self._fetch(Response(200, b'{"usage": {"limit": "0"}}'))
        self.assertEqual(snapshot.status, PARSE_ERROR)

    def test_auth_rejection_is_auth_required_without_body(self):
        body = b'{"error": "bad kimi-sentinel-key"}'
        snapshot, _ = self._fetch(Response(401, body))
        self.assertEqual(snapshot.status, AUTH_REQUIRED)
        self.assertNotIn("bad", snapshot.error.message + snapshot.error.action)
        self.assertNotIn(SENTINEL, snapshot.error.message + snapshot.error.action)

    def test_unmapped_status_is_unavailable(self):
        snapshot, _ = self._fetch(Response(404, b"gone"))
        self.assertEqual(snapshot.status, UNAVAILABLE)

    def test_transport_failure_is_network_error_without_exception_text(self):
        snapshot, _ = self._fetch(TransportError("kimi-sentinel-key leak"))
        self.assertEqual(snapshot.status, "network_error")
        self.assertNotIn(SENTINEL, snapshot.error.message + snapshot.error.action)


if __name__ == "__main__":
    unittest.main()
