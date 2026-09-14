"""OpenCode Go adapter tests using invented data only; no live requests."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from llmits.http import TransportError
from llmits.models import (
    AUTH_REQUIRED,
    AVAILABLE,
    NETWORK_ERROR,
    PARSE_ERROR,
    RATE_LIMITED,
    UNAVAILABLE,
)
from llmits.providers import opencode

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
SENTINEL = "opencode-sentinel-key"
BODY_SENTINEL = "opencode-sentinel-body"


def fixture() -> dict:
    return json.loads((FIXTURES / "opencode_go_usage_full.json").read_text())


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
        self.calls.append((host, path, dict(headers)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class OpenCodeParseTests(unittest.TestCase):
    def test_all_evidenced_windows_use_reported_percent(self):
        windows = opencode.parse_usage(fixture())

        self.assertEqual([window.key for window in windows], ["5h", "weekly", "monthly"])
        self.assertEqual([window.label for window in windows], ["5h", "7d", "month"])
        self.assertEqual([window.used_percent for window in windows], [42, 18, 7])
        self.assertEqual([window.remaining_percent for window in windows], [58, 82, 93])
        self.assertEqual(
            [window.period_seconds for window in windows],
            [5 * 3600, 7 * 86400, None],
        )
        self.assertEqual(
            windows[0].reset_at,
            datetime(2026, 9, 15, 15, 0, 0, tzinfo=timezone.utc),
        )

    def test_rate_limited_window_stays_available_at_one_hundred_percent(self):
        payload = fixture()
        payload["usage"]["monthly"].update(status="rate-limited", percent=100)
        windows = opencode.parse_usage(payload)
        self.assertEqual(windows[-1].used_percent, 100)
        self.assertEqual(windows[-1].remaining_percent, 0)

    def test_status_and_percent_must_agree(self):
        for status, percent in (("ok", 100), ("rate-limited", 99)):
            with self.subTest(status=status, percent=percent):
                payload = fixture()
                payload["usage"]["rolling"].update(status=status, percent=percent)
                with self.assertRaises(ValueError):
                    opencode.parse_usage(payload)

    def test_complete_wire_shape_is_required_and_unknown_fields_fail_closed(self):
        cases = []
        cases.append([])
        cases.append({})
        extra_root = fixture()
        extra_root["unexpected"] = "shape drift"
        cases.append(extra_root)
        missing_window = fixture()
        del missing_window["usage"]["monthly"]
        cases.append(missing_window)
        extra_window = fixture()
        extra_window["usage"]["daily"] = copy.deepcopy(extra_window["usage"]["rolling"])
        cases.append(extra_window)
        extra_field = fixture()
        extra_field["usage"]["rolling"]["remaining"] = 58
        cases.append(extra_field)
        missing_field = fixture()
        del missing_field["usage"]["weekly"]["resetsAt"]
        cases.append(missing_field)
        wrong_item = fixture()
        wrong_item["usage"]["rolling"] = []
        cases.append(wrong_item)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    opencode.parse_usage(payload)

    def test_status_percent_and_timestamp_domains_are_strict(self):
        mutations = (
            ("status", "future-status"),
            ("status", None),
            ("percent", True),
            ("percent", 42.5),
            ("percent", -1),
            ("percent", 101),
            ("percent", "42"),
            ("resetsAt", None),
            ("resetsAt", "2026-09-15 15:00:00Z"),
            ("resetsAt", "2026-09-15T15:00:00+00:00"),
            ("resetsAt", "not-a-timestamp"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                payload = fixture()
                payload["usage"]["rolling"][field] = value
                with self.assertRaises(ValueError):
                    opencode.parse_usage(payload)


class OpenCodeFetchTests(unittest.TestCase):
    def fetch(self, response):
        transport = FakeTransport(response)
        return opencode.fetch(SENTINEL, transport, now=NOW), transport

    def test_success_uses_fixed_endpoint_and_bearer_header(self):
        snapshot, transport = self.fetch(
            Response(200, (FIXTURES / "opencode_go_usage_full.json").read_bytes())
        )

        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(snapshot.provider, "opencode")
        self.assertEqual(snapshot.plan_name, "OpenCode Go")
        self.assertEqual(len(snapshot.windows), 3)
        self.assertEqual(len(transport.calls), 1)
        host, path, headers = transport.calls[0]
        self.assertEqual((host, path), ("opencode.ai", "/zen/go/v1/usage"))
        self.assertEqual(headers["Authorization"], f"Bearer {SENTINEL}")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertIn("User-Agent", headers)

    def test_http_statuses_are_normalized_without_body_leakage(self):
        for status, expected in (
            (401, AUTH_REQUIRED),
            (403, UNAVAILABLE),
            (404, UNAVAILABLE),
            (429, RATE_LIMITED),
            (500, NETWORK_ERROR),
        ):
            with self.subTest(status=status):
                snapshot, _ = self.fetch(Response(status, BODY_SENTINEL))
                self.assertEqual(snapshot.status, expected)
                text = snapshot.error.message + snapshot.error.action
                self.assertNotIn(BODY_SENTINEL, text)
                self.assertNotIn(SENTINEL, text)
        snapshot, _ = self.fetch(Response(403, BODY_SENTINEL))
        self.assertIn("OpenCode Go subscription", snapshot.error.message)

    def test_invalid_json_and_shape_drift_are_parse_errors(self):
        for body in (
            b"not json",
            b"[]",
            b'{"usage":{}}',
            b'{"usage":{},"unexpected":"' + BODY_SENTINEL.encode() + b'"}',
        ):
            with self.subTest(body=body):
                snapshot, _ = self.fetch(Response(200, body))
                self.assertEqual(snapshot.status, PARSE_ERROR)
                text = snapshot.error.message + snapshot.error.action
                self.assertNotIn(BODY_SENTINEL, text)
                self.assertNotIn(SENTINEL, text)

    def test_transport_error_is_sanitized(self):
        snapshot, transport = self.fetch(
            TransportError(f"failed with {SENTINEL} and {BODY_SENTINEL}")
        )
        self.assertEqual(snapshot.status, NETWORK_ERROR)
        self.assertEqual(len(transport.calls), 1)
        text = snapshot.error.message + snapshot.error.action
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(BODY_SENTINEL, text)


if __name__ == "__main__":
    unittest.main()
