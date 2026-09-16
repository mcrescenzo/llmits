"""Kimi Coding Plan adapter tests (invented fixtures; no live requests)."""
import dataclasses
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

# One invented sentinel per identifier-like payload field in
# kimi_usages_full.json, so a failing leak assertion names the leaking field.
# The two "used" values sit in fields the parser reads today (their
# non-numeric strings are tolerated, so the counts stay derived from
# limit - remaining); the rest are label-like extras or documented-but-dropped
# concepts the parser never reads. limits[0].timeUnit deliberately keeps a
# real value: the parser consumes it to select the 5h window, so poisoning it
# would change the parse outcome instead of only the leak surface.
PAYLOAD_SENTINELS = (
    "invented-user-1",  # user.userId (ignored)
    "invented-pro",  # user.membership (ignored)
    "invented-region",  # user.region (ignored)
    "invented-limit-name",  # limits[0].name (ignored)
    "invented-usage-used",  # usage.used (consumed)
    "invented-detail-used",  # limits[0].detail.used (consumed)
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _strings(value) -> list[str]:
    """Every string reachable in a snapshot (or window tuple), recursively.

    ProviderSnapshot, QuotaWindow, and ProviderError hold all user-visible
    text as string fields (plan name, window keys and labels, error
    message/action); fixed enum members (provider, status, error code)
    stringify and are collected too, and every other field is a number,
    datetime, boolean, or None. Walking the dataclasses generically
    instead of naming fields keeps the leak assertions complete: a string
    field added to these types later is collected automatically instead
    of silently escaping the check.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [
            text
            for field in dataclasses.fields(value)
            for text in _strings(getattr(value, field.name))
        ]
    return []


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

    def test_rfc3339_timezone_overflow_clears_the_reset(self):
        payload = {
            "usage": {
                "limit": 10,
                "remaining": 5,
                "resetTime": "9999-12-31T23:59:59-23:59",
            }
        }
        (window,) = kimi.parse_usage(payload)
        self.assertEqual(window.used_percent, 50)
        self.assertIsNone(window.reset_at)

    def test_oversized_integer_numeric_fields_are_skipped(self):
        oversized = 10**310
        self.assertEqual(
            kimi.parse_usage({"usage": {"limit": oversized, "remaining": 1}}),
            (),
        )
        windows = kimi.parse_usage(
            {
                "usage": {"limit": 10, "remaining": 5},
                "limits": [
                    {
                        "duration": oversized,
                        "timeUnit": "TIME_UNIT_MINUTE",
                        "detail": {"limit": 10, "remaining": 5},
                    }
                ],
            }
        )
        self.assertEqual([window.key for window in windows], ["weekly"])

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
        self.assertEqual([w.label for w in windows], ["5h", "7d"])
        for sentinel in PAYLOAD_SENTINELS:
            for text in _strings(windows):
                with self.subTest(sentinel=sentinel, text=text):
                    self.assertNotIn(sentinel, text)
        # Guard against a vacuously passing loop: the collector must have
        # observed every user-visible string on these windows.
        self.assertEqual(set(_strings(windows)), {"5h", "7d", "weekly"})


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
        (host, path, headers), = transport.calls
        self.assertEqual((host, path), (kimi.HOST, kimi.PATH))
        self.assertEqual(headers["Authorization"], f"Bearer {SENTINEL}")

    def test_payload_identifiers_never_reach_user_visible_text(self):
        # Every user-visible string this adapter emits is a local constant,
        # so this cannot fail today; it exists to catch a future regression
        # that maps a payload field into a plan name, window key/label, or
        # error message/action. The fixture carries one invented sentinel
        # per field (see PAYLOAD_SENTINELS), including fields the parser
        # consumes, so the failing subTest names the leaking field.
        snapshot, _ = self._fetch(
            Response(200, (FIXTURES / "kimi_usages_full.json").read_bytes())
        )
        self.assertEqual(snapshot.status, AVAILABLE)
        for sentinel in PAYLOAD_SENTINELS:
            for text in _strings(snapshot):
                with self.subTest(sentinel=sentinel, text=text):
                    self.assertNotIn(sentinel, text)
        # Guard against a vacuously passing loop: the helper must have
        # observed every user-visible surface of this snapshot.
        self.assertEqual(
            set(_strings(snapshot)),
            {"kimi", "available", "Kimi Coding Plan", "5h", "weekly", "7d"},
        )

    def test_empty_object_fails_closed_to_parse_error(self):
        snapshot, _ = self._fetch(Response(200, b"{}"))
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertIsNone(snapshot.plan_name)

    def test_recognizable_shape_absent_is_parse_error_not_guess(self):
        # No usable windows, on a body carrying the same hostile sentinels:
        # the parse error must not quote them either.
        body = json.dumps(
            {
                "usage": {"limit": "0", "used": "invented-usage-used"},
                "limits": [
                    {
                        "duration": "300",
                        "timeUnit": "TIME_UNIT_MINUTE",
                        "name": "invented-limit-name",
                        "detail": {"limit": "0", "used": "invented-detail-used"},
                    }
                ],
                "user": {
                    "userId": "invented-user-1",
                    "region": "invented-region",
                    "membership": "invented-pro",
                },
            }
        ).encode()
        snapshot, _ = self._fetch(Response(200, body))
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertIsNone(snapshot.plan_name)
        self.assertEqual(snapshot.windows, ())
        for sentinel in PAYLOAD_SENTINELS:
            for text in _strings(snapshot):
                with self.subTest(sentinel=sentinel, text=text):
                    self.assertNotIn(sentinel, text)
        # Guard against a vacuously passing loop on the error surface too:
        # the collector must have observed the error message and action.
        self.assertIsNotNone(snapshot.error)
        self.assertEqual(
            set(_strings(snapshot)),
            {
                "kimi",
                "parse_error",
                "no usage data in Kimi response",
                "the provider API may have changed; check for a newer llmits release",
            },
        )

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
