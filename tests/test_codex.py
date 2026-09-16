import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from llmits.http import TransportError
from llmits.models import AVAILABLE, AUTH_REQUIRED, PARSE_ERROR
from llmits.providers import codex

FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "codex-oauth-sentinel-token"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


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


class Response:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}


NOW = datetime(2026, 7, 14, 2, 31, 6, tzinfo=timezone.utc)


class CodexParseTests(unittest.TestCase):
    def test_full_fixture_windows(self):
        plan, windows = codex.parse_usage(fixture("codex_usage_full.json"))
        self.assertEqual(plan, "Codex plus")
        keys = [w.key for w in windows]
        self.assertEqual(keys, ["5h", "7d", "spk/7d", "credits", "reset_credits"])
        # Spec section 3 short labels for every codex key.
        labels = [w.label for w in windows]
        self.assertEqual(labels, ["5h", "7d", "Spark", "credits", "banked"])
        five = windows[0]
        self.assertEqual((five.used_percent, five.remaining_percent), (23, 77))
        self.assertEqual(five.period_seconds, 18000)
        self.assertEqual(five.reset_at, datetime(2026, 7, 19, 23, 6, 40, tzinfo=timezone.utc))
        week = windows[1]
        self.assertEqual(week.used_percent, 45)
        spark = windows[2]
        self.assertEqual((spark.key, spark.used_percent), ("spk/7d", 12))
        credits = windows[3]
        self.assertEqual(credits.remaining_value, 4250)
        resets = windows[4]
        self.assertEqual(resets.remaining_value, 2)

    def test_week_primary_only_fixture(self):
        plan, windows = codex.parse_usage(fixture("codex_usage_week_primary.json"))
        self.assertEqual(plan, "Codex pro")
        self.assertEqual([w.key for w in windows], ["7d"])
        self.assertEqual(windows[0].label, "7d")
        self.assertEqual(windows[0].used_percent, 76)
        self.assertEqual(windows[0].period_seconds, 604800)

    def test_unlimited_true_suppresses_credit_window_even_with_nonzero_balance(self):
        payload = {"plan_type": "pro", "credits": {"unlimited": True, "balance": "12.50"}}
        _, windows = codex.parse_usage(payload)
        self.assertEqual(windows, ())

    def test_unlimited_string_false_does_not_suppress_credit_window(self):
        payload = {"plan_type": "pro", "credits": {"unlimited": "false", "balance": "12.50"}}
        _, windows = codex.parse_usage(payload)
        self.assertEqual([w.key for w in windows], ["credits"])
        self.assertEqual(windows[0].remaining_value, 1250)

    def test_additional_rate_limits_with_colliding_suffix_get_distinct_keys(self):
        payload = {
            "plan_type": "plus",
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 10,
                            "limit_window_seconds": 604800,
                            "reset_at": 1,
                        }
                    },
                },
                {
                    "limit_name": "GPT-5.4-Codex-Spark",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 20,
                            "limit_window_seconds": 604800,
                            "reset_at": 1,
                        }
                    },
                },
            ],
        }
        _, windows = codex.parse_usage(payload)
        keys = [w.key for w in windows]
        self.assertEqual(keys, ["spk/7d", "x1/7d"])
        self.assertEqual([w.label for w in windows], ["Spark", "Limit 1"])
        self.assertEqual({w.used_percent for w in windows}, {10, 20})

    def test_unknown_limit_names_get_ordinal_ids_without_raw_text(self):
        # x47.2: unrecognized upstream limit_name material must never be
        # promoted into a key or label; distinct unknown names get fixed
        # ordinal ids, and an exact repeat still collapses to one window.
        payload = {
            "plan_type": "plus",
            "additional_rate_limits": [
                {
                    "limit_name": "acme-X47SENTINEL-alpha",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "limit_window_seconds": 604800,
                            "reset_at": 1,
                        }
                    },
                },
                {
                    "limit_name": "acme-X47SENTINEL-alpha",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 9,
                            "limit_window_seconds": 604800,
                            "reset_at": 1,
                        }
                    },
                },
                {
                    "limit_name": "acme-X47SENTINEL-beta",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 7,
                            "limit_window_seconds": 604800,
                            "reset_at": 1,
                        }
                    },
                },
            ],
        }
        _, windows = codex.parse_usage(payload)
        # Exact repeat dedupes; distinct unknowns get distinct ordinals.
        self.assertEqual([w.key for w in windows], ["x1/7d", "x2/7d"])
        self.assertEqual([w.label for w in windows], ["Limit 1", "Limit 2"])
        self.assertEqual([w.used_percent for w in windows], [5, 7])
        for window in windows:
            self.assertNotIn("X47SENTINEL", window.key + window.label)
            self.assertNotIn("acme", window.key + window.label)

    def test_hostile_limit_name_is_never_echoed_into_keys_or_labels(self):
        hostile = "X47SENTINEL-\u202e\x1b]0;pwned\x07 <evil>;rm -rf /"
        payload = {
            "plan_type": "plus",
            "additional_rate_limits": [
                {
                    "limit_name": hostile,
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 11,
                            "limit_window_seconds": 604800,
                            "reset_at": 1,
                        }
                    },
                },
            ],
        }
        _, windows = codex.parse_usage(payload)
        (window,) = windows
        self.assertEqual(window.key, "x1/7d")
        self.assertEqual(window.label, "Limit 1")
        self.assertNotIn("X47SENTINEL", window.key + window.label)
        self.assertNotIn("<evil>", window.key + window.label)
        for sentinel in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(sentinel, window.key + window.label)

    def test_boolean_available_count_yields_no_reset_credits_window(self):
        payload = {
            "plan_type": "pro",
            "rate_limit_reset_credits": {"available_count": True},
        }
        _, windows = codex.parse_usage(payload)
        self.assertEqual([w for w in windows if w.key == "reset_credits"], [])

    def test_additional_rate_limit_without_name_or_feature_uses_limit_prefix(self):
        payload = {
            "plan_type": "plus",
            "additional_rate_limits": [
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 30,
                            "limit_window_seconds": 604800,
                            "reset_at": 1,
                        }
                    },
                },
            ],
        }
        _, windows = codex.parse_usage(payload)
        keys = [w.key for w in windows]
        self.assertEqual(keys, ["limit/7d"])
        self.assertEqual(windows[0].used_percent, 30)


class CodexFetchTests(unittest.TestCase):
    def test_out_of_range_reset_at_keeps_snapshot_available(self):
        # reset_at=1e18 seconds is far outside datetime.fromtimestamp's
        # representable range; the affected window must degrade to
        # reset_at=None instead of the whole snapshot becoming parse_error,
        # and an unaffected sibling window's reset_at must stay intact.
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10,
                    "limit_window_seconds": 18000,
                    "reset_at": 1e18,
                },
                "secondary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784934400,
                },
            },
        }
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(len(snapshot.windows), 2)
        five, week = snapshot.windows
        self.assertEqual(five.key, "5h")
        self.assertIsNone(five.reset_at)
        self.assertEqual(five.used_percent, 10)
        self.assertEqual(week.key, "7d")
        self.assertEqual(week.reset_at, datetime(2026, 7, 24, 23, 6, 40, tzinfo=timezone.utc))

    def test_success_snapshot_and_single_request(self):
        transport = FakeTransport(Response(200, json.dumps(fixture("codex_usage_full.json"))))
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(snapshot.plan_name, "Codex plus")
        self.assertEqual(len(snapshot.windows), 5)
        # No second call to the reset-credit detail endpoint.
        self.assertEqual(len(transport.calls), 1)
        host, path, headers = transport.calls[0]
        self.assertEqual((host, path), ("chatgpt.com", "/backend-api/wham/usage"))
        self.assertTrue(headers["Authorization"].endswith(SENTINEL))
        self.assertTrue(headers["User-Agent"].startswith("llmits/"))

    def test_403_maps_to_auth_required(self):
        transport = FakeTransport(Response(403, b"forbidden"))
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AUTH_REQUIRED)
        self.assertIn("codex login", snapshot.error.action)

    def test_no_usage_signal_maps_to_parse_error(self):
        transport = FakeTransport(Response(200, json.dumps({"unrelated": True})))
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, "parse_error")
        self.assertIn("no usage data", snapshot.error.message)

    def test_additional_only_rate_limit_payload_is_available(self):
        payload = {
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 12,
                            "limit_window_seconds": 604800,
                            "reset_at": 1784934400,
                        }
                    },
                }
            ]
        }
        snapshot = codex.fetch(
            SENTINEL, FakeTransport(Response(200, json.dumps(payload))), now=NOW
        )
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual([window.key for window in snapshot.windows], ["spk/7d"])
        self.assertEqual(snapshot.windows[0].used_percent, 12)

    def test_signal_key_with_no_parseable_windows_maps_to_parse_error(self):
        # "plan_type" is a recognized signal key, but its value is not a
        # vettable string and there is no rate_limit/credits data to parse
        # into a window -- this must not surface as an empty AVAILABLE card.
        transport = FakeTransport(Response(200, json.dumps({"plan_type": 123})))
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, "parse_error")
        self.assertEqual(snapshot.windows, ())
        self.assertIn("no usage data", snapshot.error.message)

    def test_transport_error_maps_to_network_error_without_secret(self):
        transport = FakeTransport(
            TransportError("SECRET-SENTINEL-xyz network error (ConnectionResetError)")
        )
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, "network_error")
        # Only the exception class name survives: the exception's own text
        # (which a hostile or buggy raiser controls) never does.
        self.assertEqual(snapshot.error.message, "network error (TransportError)")
        self.assertNotIn("SECRET-SENTINEL-xyz", snapshot.error.message)
        self.assertNotIn(SENTINEL, snapshot.error.message)

    def test_hostile_plan_type_is_never_echoed_into_plan_name(self):
        payload = {
            "plan_type": "\x1b[31mPro\x1b[0m " + SENTINEL + " " + "y" * 300,
            "rate_limit": {
                "primary_window": {"used_percent": 10, "limit_window_seconds": 18000, "reset_at": 1784502400}
            },
        }
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(snapshot.plan_name, "Codex")
        self.assertNotIn("\x1b", snapshot.plan_name or "")
        self.assertNotIn(SENTINEL, snapshot.plan_name or "")
    def test_normal_plan_type_values_pass_vetting(self):
        for raw, expected in (
            ("Plus", "Codex plus"),
            ("PRO", "Codex pro"),
            ("edu-2026", "Codex edu-2026"),
            ("team plan v2", "Codex team plan v2"),
        ):
            payload = {
                "plan_type": raw,
                "rate_limit": {
                    "primary_window": {"used_percent": 10, "limit_window_seconds": 18000, "reset_at": 1}
                },
            }
            transport = FakeTransport(Response(200, json.dumps(payload)))
            snapshot = codex.fetch(SENTINEL, transport, now=NOW)
            self.assertEqual(snapshot.plan_name, expected, raw)
    def test_non_finite_numbers_do_not_crash_parsing(self):
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 1e999,
                    "limit_window_seconds": 1e999,
                    "reset_at": 1e999,
                }
            },
            "credits": {"balance": "1e999"},
            "rate_limit_reset_credits": {"available_count": 3},
        }
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = codex.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        window = snapshot.windows[0]
        self.assertEqual(window.used_percent, 0)
        # Non-finite limit_window_seconds/reset_at fall back to the primary
        # window's default period and a cleared reset time, not a raw
        # non-finite value.
        self.assertEqual(window.period_seconds, 18000)
        self.assertIsNone(window.reset_at)
        credits = [w for w in snapshot.windows if w.key == "credits"]
        self.assertEqual(credits, [])

    def test_boolean_rate_limit_numbers_are_rejected(self):
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": True,
                    "limit_window_seconds": 18000,
                },
                "secondary_window": {
                    "used_percent": 25,
                    "limit_window_seconds": True,
                },
            }
        }
        _, windows = codex.parse_usage(payload)
        self.assertEqual([window.key for window in windows], ["7d"])
        self.assertEqual(windows[0].used_percent, 25)
        self.assertEqual(windows[0].period_seconds, 604800)

    def test_oversized_integer_numbers_do_not_crash_parsing(self):
        oversized = 10**310
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": oversized,
                    "limit_window_seconds": oversized,
                    "reset_at": oversized,
                }
            }
        }
        snapshot = codex.fetch(
            SENTINEL, FakeTransport(Response(200, json.dumps(payload))), now=NOW
        )
        self.assertEqual(snapshot.status, AVAILABLE)
        (window,) = snapshot.windows
        self.assertEqual(window.used_percent, 0)
        self.assertEqual(window.period_seconds, 18000)
        self.assertIsNone(window.reset_at)

    def test_oversized_integer_literal_body_maps_to_parse_error(self):
        # llmits-6wg: a hostile HTTP 200 body can be syntactically JSON yet
        # carry an integer literal longer than the interpreter's int-string
        # digit limit; json.loads then raises a plain ValueError that is
        # neither a JSONDecodeError nor a TransportError, so fetch must
        # return the sanitized parse_error snapshot instead of raising out
        # of the adapter. The limit is pinned to the documented default and
        # restored afterwards so an ambient PYTHONINTMAXSTRDIGITS can never
        # change the outcome.
        previous_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(4300)
        self.addCleanup(sys.set_int_max_str_digits, previous_limit)
        body = (
            b'{"rate_limit": {"primary_window": {"used_percent": '
            + b"9" * (sys.get_int_max_str_digits() + 1)
            + b"}}}"
        )
        snapshot = codex.fetch(
            SENTINEL, FakeTransport(Response(200, body)), now=NOW
        )
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertEqual(snapshot.error.message, "Codex returned invalid JSON")
        self.assertIsNone(snapshot.plan_name)
        self.assertEqual(snapshot.windows, ())

    def test_deeply_nested_body_maps_to_parse_error(self):
        # llmits-9kv: a hostile HTTP 200 body nested beyond the interpreter
        # recursion guard makes json.loads raise RecursionError inside the
        # shared decode boundary; it is neither a ValueError nor a
        # TransportError, so without the widened catch it escapes the
        # adapter entirely (service.py then reports it as an "internal
        # provider error"). Same pinning rationale as the common-level test:
        # the pinned limit sits well above the runner's call depth and is
        # restored afterwards, and the depth stays under the 1 MiB response
        # cap. CPython 3.12+ scales its guard with the C stack rather than
        # this limit, so the assertion below accepts either sanitized
        # parse-error message and the test does not depend on stack size.
        # Built by byte multiplication so constructing the body does not
        # itself recurse.
        previous_limit = sys.getrecursionlimit()
        pinned = 300
        sys.setrecursionlimit(pinned)
        self.addCleanup(sys.setrecursionlimit, previous_limit)
        depth = pinned * 1000
        body = b"[" * depth + b"]" * depth
        snapshot = codex.fetch(
            SENTINEL, FakeTransport(Response(200, body)), now=NOW
        )
        self.assertEqual(snapshot.status, PARSE_ERROR)
        # Where the guard trips the body is "not valid JSON"; on a C stack
        # large enough to finish the parse the nested array is an "unexpected
        # payload" instead. Both are sanitized parse errors.
        self.assertIn(
            snapshot.error.message,
            {"Codex returned invalid JSON", "Codex returned an unexpected payload"},
        )
        # The snapshot is fully sanitized: no payload bytes and no exception
        # text survive, only the fixed message.
        self.assertNotIn("RecursionError", snapshot.error.message)
        self.assertNotIn("[", snapshot.error.message)
        self.assertIsNone(snapshot.plan_name)
        self.assertEqual(snapshot.windows, ())


class CodexDefaultClockTests(unittest.TestCase):
    def test_fetch_with_now_none_falls_back_to_common_utcnow(self):
        sentinel_now = datetime(2027, 3, 2, 8, 0, 0, tzinfo=timezone.utc)
        transport = FakeTransport(Response(200, json.dumps(fixture("codex_usage_full.json"))))
        with patch("llmits.providers.common.utcnow", return_value=sentinel_now) as mock_utcnow:
            snapshot = codex.fetch(SENTINEL, transport, now=None)
        mock_utcnow.assert_called_once()
        self.assertEqual(snapshot.fetched_at, sentinel_now)


if __name__ == "__main__":
    unittest.main()
