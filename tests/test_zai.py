import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from llmits.models import AVAILABLE, AUTH_REQUIRED, UNAVAILABLE
from llmits.providers import zai

FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "zai-plan-sentinel-key"


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


class ZaiParseTests(unittest.TestCase):
    def test_full_fixture_windows(self):
        plan, windows = zai.parse_usage(fixture("zai_quota_full.json"))
        self.assertEqual(plan, "Z.AI pro")
        self.assertEqual([w.key for w in windows], ["5h", "weekly", "monthly_mcp"])
        # Spec section 3 short labels for every zai key.
        self.assertEqual([w.label for w in windows], ["5h", "7d", "MCP"])
        five = windows[0]
        self.assertEqual((five.used_value, five.limit_value, five.remaining_value), (72000, 1000000, 928000))
        self.assertEqual(five.used_percent, 7)
        self.assertEqual(five.period_seconds, 18000)
        self.assertEqual(five.reset_at, datetime(2026, 7, 2, 1, 46, 40, tzinfo=timezone.utc))
        weekly = windows[1]
        self.assertEqual(weekly.used_percent, 53)
        mcp = windows[2]
        self.assertEqual(mcp.used_percent, 4)
        self.assertEqual(mcp.period_seconds, 30 * 86400)

    def test_credit_limit_variant(self):
        plan, windows = zai.parse_usage(fixture("zai_quota_credit.json"))
        self.assertEqual(plan, "Z.AI max")
        self.assertEqual([w.key for w in windows], ["5h_credits", "weekly_credits"])
        self.assertEqual([w.label for w in windows], ["5h", "7d"])
        self.assertEqual([w.used_percent for w in windows], [18, 61])
        self.assertEqual(windows[0].used_value, None)  # percentage-only form

    def test_sparse_percentage_only(self):
        plan, windows = zai.parse_usage(fixture("zai_quota_sparse.json"))
        self.assertEqual(plan, "Z.AI lite")
        self.assertEqual([w.key for w in windows], ["5h", "weekly"])
        self.assertEqual([w.label for w in windows], ["5h", "7d"])
        self.assertEqual([w.used_percent for w in windows], [2, 33])
        self.assertEqual(len(windows), 2)
        self.assertIsNone(windows[0].reset_at)
        self.assertIsNone(windows[1].reset_at)

    def test_empty_limits(self):
        self.assertEqual(zai.parse_usage({"data": {"level": "pro", "limits": []}}), ("Z.AI pro", ()))

    def test_unknown_raw_type_gets_distinct_keys_by_label(self):
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "Future Quota A", "rawType": "FUTURE_LIMIT", "percentage": 10},
                    {"type": "Future Quota B", "rawType": "FUTURE_LIMIT", "percentage": 20},
                ],
            },
        }
        plan, windows = zai.parse_usage(payload)
        keys = [w.key for w in windows]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(key.startswith("other:") for key in keys))

    def test_unknown_raw_type_exact_duplicate_still_dedupes(self):
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "Future Quota", "rawType": "FUTURE_LIMIT", "percentage": 10},
                    {"type": "Future Quota", "rawType": "FUTURE_LIMIT", "percentage": 20},
                ],
            },
        }
        plan, windows = zai.parse_usage(payload)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].used_percent, 10)

    def test_synthetic_fixture_shape_without_rawtype(self):
        # Synthetic edge-case fixture (tests/fixtures/zai_quota_synthetic.json):
        # the shape mirrors a payload whose kind token sits in "type" with
        # no "rawType" at all (llmits-o03.11), but every value is invented
        # -- no plan, quota, or reset number here comes from any account.
        plan, windows = zai.parse_usage(fixture("zai_quota_synthetic.json"))
        self.assertEqual(plan, "Z.AI pro")
        self.assertEqual([w.key for w in windows], ["monthly_mcp", "5h"])
        self.assertEqual([w.label for w in windows], ["MCP", "5h"])
        self.assertEqual([w.period_seconds for w in windows], [30 * 86400, 5 * 3600])
        mcp, five = windows
        self.assertEqual(
            (mcp.used_value, mcp.limit_value, mcp.remaining_value), (1234, 456789, 455555)
        )
        self.assertEqual(mcp.used_percent, 0)
        self.assertEqual(mcp.reset_at, datetime(2100, 1, 1, 0, 0, tzinfo=timezone.utc))
        self.assertIsNone(five.reset_at)

    def test_type_only_free_text_still_uses_fallback(self):
        # A "type" that is free text (not one of the three exact kind
        # tokens) must keep taking the vetted-label fallback path, exactly
        # as it did before llmits-o03.11 — not every bare "type" becomes a
        # recognised kind.
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "Weekly token budget", "unit": 6, "percentage": 40},
                ],
            },
        }
        plan, windows = zai.parse_usage(payload)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window.key, "other:weekly_token_budget")
        self.assertEqual(window.label, "Weekly token budget")

    def test_raw_type_wins_over_type_when_both_present(self):
        # When both fields are present and disagree, rawType is the kind
        # token, not type — even when type happens to spell out a different
        # exact kind token.
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "TIME_LIMIT", "rawType": "TOKENS_LIMIT", "unit": 3, "percentage": 5},
                ],
            },
        }
        plan, windows = zai.parse_usage(payload)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].key, "5h")
        self.assertEqual(windows[0].label, "5h")

    def test_credit_limit_fallback_key_is_never_credits(self):
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "Monthly", "rawType": "CREDIT_LIMIT", "unit": 5, "percentage": 15},
                ],
            },
        }
        plan, windows = zai.parse_usage(payload)
        self.assertEqual(len(windows), 1)
        self.assertNotEqual(windows[0].key, "credits")
        self.assertEqual(windows[0].key, "credits_other")

    def test_boolean_quota_numbers_are_treated_as_missing(self):
        base = {"type": "5h Token", "rawType": "TOKENS_LIMIT", "unit": 3}

        _, (counts,) = zai.parse_usage(
            {"data": {"limits": [{**base, "usage": True, "currentValue": True}]}}
        )
        self.assertEqual(counts.used_percent, 0)
        self.assertEqual(
            (counts.used_value, counts.limit_value, counts.remaining_value),
            (None, None, None),
        )

        _, (percentage,) = zai.parse_usage(
            {"data": {"limits": [{**base, "percentage": True}]}}
        )
        self.assertEqual(percentage.used_percent, 0)

        _, (remaining,) = zai.parse_usage(
            {
                "data": {
                    "limits": [
                        {**base, "usage": 10, "currentValue": 2, "remaining": True}
                    ]
                }
            }
        )
        self.assertEqual(remaining.remaining_value, 8)


class ZaiFetchTests(unittest.TestCase):
    def test_out_of_range_reset_time_keeps_snapshot_available(self):
        # nextResetTime=1e21 ms is far outside datetime.fromtimestamp's
        # representable range; the affected window must degrade to
        # reset_at=None instead of the whole snapshot becoming parse_error,
        # and an unaffected sibling window's reset_at must stay intact.
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro",
                "limits": [
                    {
                        "type": "5h Token",
                        "rawType": "TOKENS_LIMIT",
                        "unit": 3,
                        "usage": 1000000,
                        "currentValue": 72000,
                        "nextResetTime": 1e21,
                    },
                    {
                        "type": "Weekly Token",
                        "rawType": "TOKENS_LIMIT",
                        "unit": 6,
                        "usage": 5000000,
                        "currentValue": 2650000,
                        "nextResetTime": 1782956800000,
                    },
                ],
            },
        }
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(len(snapshot.windows), 2)
        five, weekly = snapshot.windows
        self.assertEqual(five.key, "5h")
        self.assertIsNone(five.reset_at)
        self.assertEqual(weekly.key, "weekly")
        self.assertEqual(weekly.reset_at, datetime(2026, 7, 2, 1, 46, 40, tzinfo=timezone.utc))

    def test_success_with_bare_authorization_header(self):
        transport = FakeTransport(Response(200, json.dumps(fixture("zai_quota_full.json"))))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        host, path, headers = transport.calls[0]
        self.assertEqual((host, path), ("api.z.ai", "/api/monitor/usage/quota/limit"))
        self.assertEqual(headers["Authorization"], SENTINEL)
        self.assertEqual(len(transport.calls), 1)

    def test_bearer_retry_after_401_against_same_host(self):
        transport = FakeTransport(
            Response(401, b"unauthorized"),
            Response(200, json.dumps(fixture("zai_quota_full.json"))),
        )
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(len(transport.calls), 2)
        first, second = transport.calls
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])
        self.assertEqual(second[2]["Authorization"], f"Bearer {SENTINEL}")

    def test_bearer_retry_after_403(self):
        transport = FakeTransport(
            Response(403, b"forbidden"),
            Response(200, json.dumps(fixture("zai_quota_sparse.json"))),
        )
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)

    def test_double_401_maps_to_auth_required(self):
        transport = FakeTransport(Response(401, b"e1"), Response(401, b"e2"))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AUTH_REQUIRED)
        self.assertIn("ZAI_API_KEY", snapshot.error.action)

    def test_non_200_code_maps_to_unavailable_with_fixed_local_message(self):
        payload = {"code": 401, "msg": "api key invalid " + SENTINEL}
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, UNAVAILABLE)
        self.assertEqual(snapshot.error.message, "Z.AI reported an error for the coding plan")
        blob = snapshot.error.message + snapshot.error.action
        self.assertNotIn("api key invalid", blob)
        self.assertNotIn(SENTINEL, blob)
        self.assertIn("GLM Coding Plan", snapshot.error.action)

    def test_hostile_level_is_never_echoed_into_plan_name(self):
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro\x1b]0;pwned\x07" + "x" * 300 + " " + SENTINEL,
                "limits": [
                    {"type": "5h", "rawType": "TOKENS_LIMIT", "unit": 3, "percentage": 5}
                ],
            },
        }
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        self.assertEqual(snapshot.plan_name, "Z.AI Coding Plan")
        self.assertNotIn("\x1b", snapshot.plan_name or "")
        self.assertNotIn(SENTINEL, snapshot.plan_name or "")

    def test_non_finite_numbers_do_not_crash_parsing(self):
        payload = {
            "code": 200,
            "msg": "ok",
            "data": {
                "level": "pro",
                "limits": [
                    {
                        "type": "5h Token",
                        "rawType": "TOKENS_LIMIT",
                        "unit": 3,
                        "usage": 1e999,
                        "currentValue": 1e999,
                        "remaining": 1e999,
                        "percentage": 1e999,
                    },
                    {
                        "type": "Weekly Token",
                        "rawType": "TOKENS_LIMIT",
                        "unit": 6,
                        "percentage": 33,
                    },
                ],
            },
        }
        transport = FakeTransport(Response(200, json.dumps(payload)))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, AVAILABLE)
        five = snapshot.windows[0]
        self.assertEqual((five.used_value, five.limit_value), (0, 0))
        self.assertEqual(five.used_percent, 0)

    def test_missing_limits_maps_to_parse_error(self):
        transport = FakeTransport(Response(200, json.dumps({"code": 200, "msg": "ok", "data": {}})))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, "parse_error")
        self.assertIn("no quota limits", snapshot.error.message)

    def test_invalid_json_body_maps_to_parse_error(self):
        transport = FakeTransport(Response(200, b"not json"))
        snapshot = zai.fetch(SENTINEL, transport, now=NOW)
        self.assertEqual(snapshot.status, "parse_error")
        self.assertIn("invalid JSON", snapshot.error.message)

    def test_no_token_leak_in_any_error_path(self):
        for script in (
            [Response(401, b"x"), Response(401, b"x")],
            [Response(500, b"x")],
            [Response(200, b"not json")],
        ):
            transport = FakeTransport(*script)
            snapshot = zai.fetch(SENTINEL, transport, now=NOW)
            blob = (
                snapshot.error.message if snapshot.error else ""
            ) + (snapshot.error.action if snapshot.error else "") + str(snapshot.plan_name)
            self.assertNotIn(SENTINEL, blob)


class ZaiDefaultClockTests(unittest.TestCase):
    def test_fetch_with_now_none_falls_back_to_common_utcnow(self):
        sentinel_now = datetime(2027, 3, 2, 8, 0, 0, tzinfo=timezone.utc)
        transport = FakeTransport(Response(200, json.dumps(fixture("zai_quota_full.json"))))
        with patch("llmits.providers.common.utcnow", return_value=sentinel_now) as mock_utcnow:
            snapshot = zai.fetch(SENTINEL, transport, now=None)
        mock_utcnow.assert_called_once()
        self.assertEqual(snapshot.fetched_at, sentinel_now)


if __name__ == "__main__":
    unittest.main()
