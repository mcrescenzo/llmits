import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from llmits.http import RedirectedError
from llmits.models import AVAILABLE, AUTH_REQUIRED, PARSE_ERROR, RATE_LIMITED
from llmits.providers import claude

FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "sk-sentinel-claude-token"


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


class ClaudeParseTests(unittest.TestCase):
    def test_full_fixture_produces_expected_windows(self):
        windows = claude.parse_usage(fixture("claude_usage_full.json"))
        keys = [w.key for w in windows]
        self.assertEqual(keys, ["5h", "weekly", "weekly_opus", "weekly_sonnet", "weekly_fable", "extra_credits"])
        # Spec section 3 short labels: 5h/7d for the fixed windows, the
        # (title-cased) model display name for each weekly_<model> window,
        # and "extra" for the monthly extra-usage window.
        labels = [w.label for w in windows]
        self.assertEqual(labels, ["5h", "7d", "Opus", "Sonnet", "Fable", "extra"])
        five = windows[0]
        self.assertEqual((five.used_percent, five.remaining_percent), (42, 58))
        self.assertEqual(five.period_seconds, 18000)
        self.assertEqual(five.reset_at, datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc))
        fable = windows[4]
        self.assertEqual((fable.used_percent, fable.remaining_percent), (47, 53))
        extra = windows[5]
        self.assertEqual((extra.used_value, extra.limit_value, extra.remaining_value), (42, 100, 58))
        self.assertEqual(extra.used_percent, 42)

    def test_unknown_fields_are_ignored(self):
        windows = claude.parse_usage(fixture("claude_usage_full.json"))
        self.assertTrue(all("unknown" not in w.key for w in windows))

    def test_empty_payload_yields_no_windows(self):
        self.assertEqual(claude.parse_usage({}), ())


class ClaudeFetchTests(unittest.TestCase):
    def test_success_snapshot(self):
        transport = FakeTransport(Response(200, json.dumps(fixture("claude_usage_full.json"))))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(snapshot.plan_name, "Claude Pro/Max")
        self.assertEqual(snapshot.fetched_at, NOW)
        self.assertEqual(len(snapshot.windows), 6)

    def test_request_headers_use_bearer_beta_and_fixed_host(self):
        transport = FakeTransport(Response(200, json.dumps(fixture("claude_usage_full.json"))))
        claude.fetch(SENTINEL, transport, now=NOW)
        host, path, headers = transport.calls[0]
        self.assertEqual((host, path), ("api.anthropic.com", "/api/oauth/usage"))
        self.assertTrue(headers["Authorization"].endswith(SENTINEL))
        self.assertIn("Bearer", headers["Authorization"])
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertTrue(headers["User-Agent"].startswith("llmits/"))

    def test_401_maps_to_auth_required_with_relogin_action(self):
        transport = FakeTransport(Response(401, b'{"error": {"message": "bad token ' + SENTINEL.encode() + b'"}}'))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AUTH_REQUIRED)
        self.assertIn("claude login", snapshot.error.action)
        self.assertNotIn(SENTINEL, snapshot.error.message)

    def test_429_includes_sanitized_retry_after(self):
        transport = FakeTransport(Response(429, b"{}", headers={"retry-after": "120"}))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, RATE_LIMITED)
        self.assertIn("retry in 120s", snapshot.error.message)

    def test_500_maps_to_network_error_without_body(self):
        transport = FakeTransport(Response(500, b"upstream exploded SECRET"))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, "network_error")
        self.assertNotIn("SECRET", snapshot.error.message)

    def test_redirect_maps_to_parse_error(self):
        transport = FakeTransport(RedirectedError())
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertIn("redirected", snapshot.error.message)

    def test_invalid_json_maps_to_parse_error(self):
        transport = FakeTransport(Response(200, b"not-json"))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertIn("invalid JSON", snapshot.error.message)

    def test_empty_usage_maps_to_parse_error(self):
        transport = FakeTransport(Response(200, b"{}"))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, PARSE_ERROR)
        self.assertIn("no usage data", snapshot.error.message)

    def test_non_finite_extra_usage_does_not_crash(self):
        payload = {
            "five_hour": {"utilization": 10.0},
            "extra_usage": {"is_enabled": True, "monthly_limit": 1e999, "used_credits": 1e999},
        }
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        keys = [w.key for w in snapshot.windows]
        self.assertIn("5h", keys)
        self.assertNotIn("extra_credits", keys)
    def test_non_finite_utilization_becomes_zero_percent(self):
        payload = {"five_hour": {"utilization": 1e999}}
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = claude.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.windows[0].used_percent, 0)


class ClaudeDefaultClockTests(unittest.TestCase):
    def test_fetch_with_now_none_falls_back_to_common_utcnow(self):
        sentinel_now = datetime(2027, 3, 2, 8, 0, 0, tzinfo=timezone.utc)
        transport = FakeTransport(Response(200, json.dumps(fixture("claude_usage_full.json"))))
        with patch("llmits.providers.common.utcnow", return_value=sentinel_now) as mock_utcnow:
            snapshot = claude.fetch(SENTINEL, transport, now=None)
        mock_utcnow.assert_called_once()
        self.assertEqual(snapshot.fetched_at, sentinel_now)


if __name__ == "__main__":
    unittest.main()
