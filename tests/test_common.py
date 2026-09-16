import math
import sys
import unittest
from datetime import datetime, timezone

from llmits.http import RedirectedError, TransportError
from llmits.models import (
    AUTH_REQUIRED,
    NETWORK_ERROR,
    PARSE_ERROR,
    RATE_LIMITED,
    UNAVAILABLE,
    ProviderSnapshot,
)
from llmits.providers import common


class TestEpochToDatetime(unittest.TestCase):
    def test_zero_is_none(self):
        self.assertIsNone(common.epoch_to_datetime(0))
        self.assertIsNone(common.epoch_to_datetime(0, millis=True))

    def test_negative_is_none(self):
        self.assertIsNone(common.epoch_to_datetime(-1))
        self.assertIsNone(common.epoch_to_datetime(-1784502400, millis=True))

    def test_nan_and_infinite_are_none(self):
        self.assertIsNone(common.epoch_to_datetime(float("nan")))
        self.assertIsNone(common.epoch_to_datetime(float("inf")))
        self.assertIsNone(common.epoch_to_datetime(float("-inf")))
        self.assertIsNone(common.epoch_to_datetime(1e999))  # json.loads(Infinity)-style overflow
        self.assertIsNone(common.epoch_to_datetime(1e999, millis=True))

    def test_non_numeric_is_none(self):
        self.assertIsNone(common.epoch_to_datetime(None))
        self.assertIsNone(common.epoch_to_datetime("1784502400"))
        self.assertIsNone(common.epoch_to_datetime([1784502400]))

    def test_boolean_is_not_an_epoch(self):
        self.assertIsNone(common.epoch_to_datetime(True))
        self.assertIsNone(common.epoch_to_datetime(True, millis=True))

    def test_oversized_integer_is_none_not_raise(self):
        oversized = 10**310
        self.assertIsNone(common.epoch_to_datetime(oversized))
        self.assertIsNone(common.epoch_to_datetime(oversized, millis=True))

    def test_valid_epoch_seconds(self):
        result = common.epoch_to_datetime(1784502400)
        self.assertEqual(result, datetime(2026, 7, 19, 23, 6, 40, tzinfo=timezone.utc))

    def test_valid_epoch_milliseconds(self):
        result = common.epoch_to_datetime(1784502400000, millis=True)
        self.assertEqual(result, datetime(2026, 7, 19, 23, 6, 40, tzinfo=timezone.utc))

    def test_out_of_range_seconds_is_none_not_raise(self):
        self.assertIsNone(common.epoch_to_datetime(1e18))

    def test_out_of_range_milliseconds_is_none_not_raise(self):
        self.assertIsNone(common.epoch_to_datetime(1e21, millis=True))

    def test_out_of_range_is_finite_sanity_check(self):
        # Guard the test fixture itself: 1e18/1e21 must be finite numbers
        # (not accidentally inf) so the out-of-range assertions above are
        # exercising the fromtimestamp guard, not the isfinite guard.
        self.assertTrue(math.isfinite(1e18))
        self.assertTrue(math.isfinite(1e21))


class TestVettedPlanValue(unittest.TestCase):
    def test_valid_token_is_lowercased(self):
        self.assertEqual(common.vetted_plan_value("Pro Plus"), "pro plus")

    def test_non_string_is_none(self):
        self.assertIsNone(common.vetted_plan_value(None))
        self.assertIsNone(common.vetted_plan_value(42))
        self.assertIsNone(common.vetted_plan_value(["pro"]))

    def test_empty_or_leading_symbol_is_none(self):
        self.assertIsNone(common.vetted_plan_value(""))
        self.assertIsNone(common.vetted_plan_value("   "))
        self.assertIsNone(common.vetted_plan_value("-pro"))

    def test_disallowed_characters_are_none(self):
        self.assertIsNone(common.vetted_plan_value("pro/plus"))
        self.assertIsNone(common.vetted_plan_value("pro\nplus"))

    def test_over_length_is_none(self):
        self.assertIsNone(common.vetted_plan_value("a" * 25))
        self.assertEqual(common.vetted_plan_value("a" * 24), "a" * 24)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(common.vetted_plan_value("  pro  "), "pro")


class TestDecodeJsonObject(unittest.TestCase):
    def _now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_valid_object_is_returned(self):
        result = common.decode_json_object(
            "claude", "Claude", b'{"plan": "pro"}', self._now()
        )
        self.assertEqual(result, {"plan": "pro"})

    def test_invalid_json_is_parse_error(self):
        result = common.decode_json_object("claude", "Claude", b"not json", self._now())
        self.assertIsInstance(result, ProviderSnapshot)
        self.assertEqual(result.status, PARSE_ERROR)
        self.assertIn("invalid JSON", result.error.message)
        self.assertIn("Claude", result.error.message)

    def test_invalid_utf8_is_parse_error(self):
        result = common.decode_json_object("codex", "Codex", b"\xff\xfe", self._now())
        self.assertIsInstance(result, ProviderSnapshot)
        self.assertEqual(result.status, PARSE_ERROR)
        self.assertIn("invalid JSON", result.error.message)

    def test_oversized_json_integer_literal_is_parse_error(self):
        # A body that is syntactically JSON but carries an integer literal
        # beyond the interpreter's int-string digit limit (Python 3.11+;
        # 4300 digits by default) makes json.loads raise a plain ValueError
        # that is not a JSONDecodeError — the boundary must normalize it
        # rather than let it escape the adapter. The limit is pinned to the
        # documented default and restored afterwards so an ambient
        # PYTHONINTMAXSTRDIGITS can never change the outcome.
        previous_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(4300)
        self.addCleanup(sys.set_int_max_str_digits, previous_limit)
        body = b'{"used_percent": ' + b"9" * (sys.get_int_max_str_digits() + 1) + b"}"
        result = common.decode_json_object("codex", "Codex", body, self._now())
        self.assertIsInstance(result, ProviderSnapshot)
        self.assertEqual(result.status, PARSE_ERROR)
        self.assertEqual(result.error.message, "Codex returned invalid JSON")

    def test_deeply_nested_json_is_parse_error_not_recursion_error(self):
        # llmits-9kv: a body of nested arrays deeper than the interpreter's
        # recursion guard makes json.loads raise RecursionError, which is not
        # a ValueError, so without the widened catch it escapes this shared
        # boundary and every adapter. The limit is pinned well above the test
        # runner's call depth (about a dozen frames) and restored afterwards,
        # so CPython <= 3.11 — where json's guard follows
        # sys.setrecursionlimit — can never depend on ambient settings. On
        # 3.12+ the guard is not controllable that way: it is a fixed constant
        # on 3.12/3.13 (about 10k nesting levels) and scales with the C stack
        # on 3.14 (about 100k at the default 8 MiB, and no trip at all with an
        # unlimited stack). The depth stays under the 1 MiB response cap and
        # far above the usual guards, and the assertions below accept either
        # sanitized parse-error message, so the test proves the contract —
        # RecursionError never escapes — without depending on the runner's
        # stack size. The body is built by byte multiplication so
        # constructing it does not itself recurse.
        previous_limit = sys.getrecursionlimit()
        pinned = 300
        sys.setrecursionlimit(pinned)
        self.addCleanup(sys.setrecursionlimit, previous_limit)
        depth = pinned * 1000
        body = b"[" * depth + b"]" * depth
        result = common.decode_json_object("codex", "Codex", body, self._now())
        self.assertIsInstance(result, ProviderSnapshot)
        self.assertEqual(result.status, PARSE_ERROR)
        # Where the guard trips the body is "not valid JSON"; on a C stack
        # large enough to finish the parse the nested array is an "unexpected
        # payload" instead. Both are sanitized parse errors.
        self.assertIn(
            result.error.message,
            {"Codex returned invalid JSON", "Codex returned an unexpected payload"},
        )

    def test_non_object_json_is_parse_error(self):
        result = common.decode_json_object("zai", "Z.AI", b"[1, 2, 3]", self._now())
        self.assertIsInstance(result, ProviderSnapshot)
        self.assertEqual(result.status, PARSE_ERROR)
        self.assertIn("unexpected payload", result.error.message)
        self.assertIn("Z.AI", result.error.message)

    def test_provider_id_flows_into_snapshot(self):
        result = common.decode_json_object("zai", "Z.AI", b"null", self._now())
        self.assertEqual(result.provider, "zai")


class TestRetryAfterText(unittest.TestCase):
    def test_none_is_empty(self):
        self.assertEqual(common._retry_after_text(None), "")

    def test_empty_string_is_empty(self):
        self.assertEqual(common._retry_after_text(""), "")

    def test_zero_is_empty(self):
        self.assertEqual(common._retry_after_text("0"), "")

    def test_non_digit_is_empty(self):
        self.assertEqual(common._retry_after_text("soon"), "")

    def test_non_ascii_digits_are_empty(self):
        self.assertEqual(common._retry_after_text("²"), "")
        self.assertEqual(common._retry_after_text("٣"), "")

    def test_pathologically_long_digits_are_empty(self):
        self.assertEqual(common._retry_after_text("9" * 4301), "")

    def test_http_date_value_is_empty(self):
        # RFC 7231 also allows an HTTP-date for Retry-After; this adapter
        # only understands the delta-seconds form and silently drops the rest.
        self.assertEqual(common._retry_after_text("Wed, 21 Oct 2026 07:28:00 GMT"), "")

    def test_negative_number_is_empty(self):
        # "-5".isdigit() is False, so this takes the non-digit branch, not
        # the cap.
        self.assertEqual(common._retry_after_text("-5"), "")

    def test_typical_value_is_rendered(self):
        self.assertEqual(common._retry_after_text("120"), "; retry in 120s")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(common._retry_after_text("  45  "), "; retry in 45s")

    def test_value_at_cap_is_unchanged(self):
        self.assertEqual(common._retry_after_text("86400"), "; retry in 86400s")

    def test_value_over_cap_is_capped_to_24h(self):
        self.assertEqual(common._retry_after_text("100000"), "; retry in 86400s")


class TestStatusErrorSnapshot(unittest.TestCase):
    def _now(self) -> datetime:
        return datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    def test_401_maps_to_auth_required(self):
        snapshot = common.status_error_snapshot("claude", 401, {}, self._now(), "log back in")
        self.assertEqual(snapshot.status, AUTH_REQUIRED)
        self.assertEqual(snapshot.provider, "claude")
        self.assertIsNone(snapshot.plan_name)
        self.assertEqual(snapshot.fetched_at, self._now())
        self.assertEqual(snapshot.error.message, "provider rejected the credentials")
        self.assertEqual(snapshot.error.action, "log back in")

    def test_403_maps_to_auth_required(self):
        snapshot = common.status_error_snapshot("codex", 403, {}, self._now(), "log back in")
        self.assertEqual(snapshot.status, AUTH_REQUIRED)
        self.assertEqual(snapshot.error.message, "provider rejected the credentials")
        self.assertEqual(snapshot.error.action, "log back in")

    def test_429_without_retry_after_has_no_suffix(self):
        snapshot = common.status_error_snapshot("zai", 429, {}, self._now(), "relog")
        self.assertEqual(snapshot.status, RATE_LIMITED)
        self.assertEqual(snapshot.error.message, "rate limited")
        self.assertEqual(
            snapshot.error.action, "wait for the rate limit window to pass before refreshing"
        )

    def test_429_with_retry_after_includes_suffix(self):
        snapshot = common.status_error_snapshot(
            "zai", 429, {"retry-after": "30"}, self._now(), "relog"
        )
        self.assertEqual(snapshot.status, RATE_LIMITED)
        self.assertEqual(snapshot.error.message, "rate limited; retry in 30s")

    def test_429_with_invalid_digit_text_stays_rate_limited(self):
        for value in ("²", "9" * 4301):
            with self.subTest(value_length=len(value)):
                snapshot = common.status_error_snapshot(
                    "zai", 429, {"retry-after": value}, self._now(), "relog"
                )
                self.assertEqual(snapshot.status, RATE_LIMITED)
                self.assertEqual(snapshot.error.message, "rate limited")

    def test_500_maps_to_network_error(self):
        snapshot = common.status_error_snapshot("claude", 500, {}, self._now(), "relog")
        self.assertEqual(snapshot.status, NETWORK_ERROR)
        self.assertEqual(snapshot.error.message, "provider server error (HTTP 500)")
        self.assertEqual(snapshot.error.action, "try again shortly")

    def test_503_maps_to_network_error(self):
        snapshot = common.status_error_snapshot("claude", 503, {}, self._now(), "relog")
        self.assertEqual(snapshot.status, NETWORK_ERROR)
        self.assertEqual(snapshot.error.message, "provider server error (HTTP 503)")

    def test_unmapped_client_status_maps_to_unavailable(self):
        snapshot = common.status_error_snapshot("codex", 400, {}, self._now(), "relog")
        self.assertEqual(snapshot.status, UNAVAILABLE)
        self.assertEqual(snapshot.error.message, "provider returned HTTP 400")
        self.assertEqual(
            snapshot.error.action,
            "the plan or endpoint may be unavailable; try the provider console",
        )

    def test_another_unmapped_status_maps_to_unavailable(self):
        # A second, distinct value guards against a fix that only special-cases 400.
        snapshot = common.status_error_snapshot("codex", 418, {}, self._now(), "relog")
        self.assertEqual(snapshot.status, UNAVAILABLE)
        self.assertEqual(snapshot.error.message, "provider returned HTTP 418")


class TestTransportErrorSnapshot(unittest.TestCase):
    def _now(self) -> datetime:
        return datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)

    def test_exception_message_text_is_never_echoed(self):
        # x47.2: the exception's own text (URLs, hostnames, injected
        # strings) is untrusted; only the class name survives.
        exc = TransportError("SECRET-SENTINEL-xyz https://evil.example/tok")
        snapshot = common.transport_error_snapshot("codex", exc, self._now())
        self.assertEqual(snapshot.status, NETWORK_ERROR)
        self.assertEqual(snapshot.error.message, "network error (TransportError)")
        self.assertNotIn("SECRET-SENTINEL-xyz", snapshot.error.message)
        self.assertNotIn("evil.example", snapshot.error.message)
        self.assertEqual(snapshot.error.action, "check your connection and try again shortly")

    def test_exception_without_message_still_classifies(self):
        snapshot = common.transport_error_snapshot("claude", OSError(), self._now())
        self.assertEqual(snapshot.status, NETWORK_ERROR)
        self.assertEqual(snapshot.error.message, "network error (OSError)")

    def test_redirect_maps_to_fixed_parse_error_text(self):
        exc = RedirectedError()
        snapshot = common.transport_error_snapshot("zai", exc, self._now())
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertEqual(snapshot.error.message, "provider endpoint unexpectedly redirected")
        self.assertEqual(snapshot.provider, "zai")

    def test_snapshot_metadata_is_local_only(self):
        snapshot = common.transport_error_snapshot(
            "zai", TransportError("SECRET-SENTINEL-xyz"), self._now()
        )
        self.assertIsNone(snapshot.plan_name)
        self.assertEqual(snapshot.fetched_at, self._now())
        self.assertEqual(snapshot.windows, ())


class TestUtcnow(unittest.TestCase):
    def test_returns_timezone_aware_current_utc_time(self):
        before = datetime.now(timezone.utc)
        result = common.utcnow()
        after = datetime.now(timezone.utc)
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertLessEqual(before, result)
        self.assertLessEqual(result, after)


if __name__ == "__main__":
    unittest.main()
