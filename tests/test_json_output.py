import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from llmits import json_output
from llmits.models import (
    AVAILABLE,
    AUTH_REQUIRED,
    NETWORK_ERROR,
    ProviderError,
    ProviderSnapshot,
    QuotaWindow,
)
from llmits.providers import claude, codex, zai

SENTINEL = "json-sentinel-token"
GENERATED_AT = datetime(2026, 7, 14, 2, 31, 7, tzinfo=timezone.utc)
FETCHED_AT = datetime(2026, 7, 14, 2, 31, 6, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def sample_snapshots():
    window = QuotaWindow(
        key="5h",
        label="5-hour window",
        used_percent=42,
        remaining_percent=58,
        reset_at=datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
        period_seconds=18000,
    )
    ok = ProviderSnapshot(
        provider="claude",
        status=AVAILABLE,
        plan_name="Claude Pro/Max",
        fetched_at=FETCHED_AT,
        windows=(window,),
    )
    failed = ProviderSnapshot(
        provider="zai",
        status=AUTH_REQUIRED,
        plan_name=None,
        fetched_at=FETCHED_AT,
        error=ProviderError(
            code=AUTH_REQUIRED,
            message="Z.AI API key not set",
            action="export ZAI_API_KEY with your GLM Coding Plan key",
        ),
    )
    return [ok, failed]


class JsonOutputTests(unittest.TestCase):
    def test_document_shape_and_ordering(self):
        document = json.loads(json_output.to_document(sample_snapshots(), GENERATED_AT))
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["generated_at"], "2026-07-14T02:31:07Z")
        self.assertEqual([p["provider"] for p in document["providers"]], ["claude", "zai"])

        ok = document["providers"][0]
        self.assertEqual(ok["status"], "available")
        self.assertEqual(ok["plan_name"], "Claude Pro/Max")
        self.assertEqual(ok["fetched_at"], "2026-07-14T02:31:06Z")
        self.assertFalse(ok["stale"])
        self.assertIsNone(ok["error"])
        window = ok["windows"][0]
        self.assertEqual(
            sorted(window),
            [
                "key",
                "label",
                "limit_value",
                "period_seconds",
                "remaining_percent",
                "remaining_value",
                "reset_at",
                "used_percent",
                "used_value",
            ],
        )
        self.assertEqual(window["reset_at"], "2026-07-13T10:00:00Z")

        failed = document["providers"][1]
        self.assertEqual(failed["status"], "auth_required")
        self.assertEqual(failed["windows"], [])
        self.assertEqual(failed["plan_name"], None)
        self.assertEqual(
            failed["error"],
            {
                "code": "auth_required",
                "message": "Z.AI API key not set",
                "action": "export ZAI_API_KEY with your GLM Coding Plan key",
            },
        )

    def test_schema_structurally_cannot_carry_secrets(self):
        text = json_output.to_document(sample_snapshots(), GENERATED_AT)
        document = json.loads(text)
        allowed = {"schema_version", "generated_at", "providers"}
        self.assertEqual(set(document), allowed)
        for provider in document["providers"]:
            self.assertEqual(
                set(provider),
                {"provider", "status", "plan_name", "fetched_at", "stale", "windows", "error"},
            )
        self.assertNotIn(SENTINEL, text)

    def test_stale_flag_serializes(self):
        from dataclasses import replace

        snapshots = sample_snapshots()
        stale = replace(snapshots[0], stale=True, error=ProviderError(code=NETWORK_ERROR, message="x"))
        document = json.loads(json_output.to_document([stale], GENERATED_AT))
        self.assertTrue(document["providers"][0]["stale"])
        self.assertEqual(document["providers"][0]["error"]["code"], "network_error")

    def test_all_available_helper(self):
        self.assertTrue(json_output.all_available(sample_snapshots()[:1]))
        self.assertFalse(json_output.all_available(sample_snapshots()))

    def test_naive_datetime_normalizes_to_utc_with_z_suffix(self):
        naive = datetime(2026, 7, 14, 2, 31, 7)
        self.assertEqual(json_output._rfc3339(naive), "2026-07-14T02:31:07Z")

    def test_generated_at_defaults_to_now_when_omitted(self):
        document = json.loads(json_output.to_document(sample_snapshots()))
        generated = datetime.strptime(document["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self.assertLess(abs((datetime.now(timezone.utc) - generated).total_seconds()), 5)

    def test_boolean_available_count_never_serializes_as_a_json_bool(self):
        """remaining_value is documented as int|null -- never a JSON bool."""
        payload = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 10, "limit_window_seconds": 18000, "reset_at": 1}
            },
            "rate_limit_reset_credits": {"available_count": True},
        }
        plan_name, windows = codex.parse_usage(payload)
        snapshot = ProviderSnapshot(
            provider="codex",
            status=AVAILABLE,
            plan_name=plan_name,
            fetched_at=FETCHED_AT,
            windows=windows,
        )
        document = json_output.to_document([snapshot], GENERATED_AT)
        parsed = json.loads(document)
        provider_windows = parsed["providers"][0]["windows"]
        self.assertNotIn("reset_credits", [w["key"] for w in provider_windows])
        for window in provider_windows:
            value = window["remaining_value"]
            self.assertTrue(value is None or isinstance(value, int))
            self.assertNotIsInstance(value, bool)



class ShortLabelDocumentTests(unittest.TestCase):
    """Every provider emits the stable short labels used by JSON and the TUI."""

    def _document_windows(self, provider: str, plan_name, windows) -> list[dict]:
        snapshot = ProviderSnapshot(
            provider=provider,
            status=AVAILABLE,
            plan_name=plan_name,
            fetched_at=FETCHED_AT,
            windows=windows,
        )
        document = json.loads(json_output.to_document([snapshot], GENERATED_AT))
        return document["providers"][0]["windows"]

    def test_claude_fixture_labels_match_spec(self):
        windows = claude.parse_usage(_fixture("claude_usage_full.json"))
        doc_windows = self._document_windows("claude", "Claude Pro/Max", windows)
        labels = {w["key"]: w["label"] for w in doc_windows}
        self.assertEqual(
            labels,
            {
                "5h": "5h",
                "weekly": "7d",
                "weekly_opus": "Opus",
                "weekly_sonnet": "Sonnet",
                "weekly_fable": "Fable",
                "extra_credits": "extra",
            },
        )

    def test_codex_fixture_labels_match_spec(self):
        for name, expected in (
            (
                "codex_usage_full.json",
                {"5h": "5h", "7d": "7d", "spk/7d": "Spark", "credits": "credits", "reset_credits": "banked"},
            ),
            ("codex_usage_week_primary.json", {"7d": "7d"}),
        ):
            plan_name, windows = codex.parse_usage(_fixture(name))
            doc_windows = self._document_windows("codex", plan_name, windows)
            labels = {w["key"]: w["label"] for w in doc_windows}
            self.assertEqual(labels, expected, name)

    def test_zai_fixture_labels_match_spec(self):
        for name, expected in (
            ("zai_quota_full.json", {"5h": "5h", "weekly": "7d", "monthly_mcp": "MCP"}),
            ("zai_quota_credit.json", {"5h_credits": "5h", "weekly_credits": "7d"}),
            ("zai_quota_sparse.json", {"5h": "5h", "weekly": "7d"}),
        ):
            plan_name, windows = zai.parse_usage(_fixture(name))
            doc_windows = self._document_windows("zai", plan_name, windows)
            labels = {w["key"]: w["label"] for w in doc_windows}
            self.assertEqual(labels, expected, name)


class HostilePayloadTests(unittest.TestCase):
    def test_service_output_with_hostile_provider_text_is_clean(self):
        """End to end: hostile response strings never survive to JSON."""
        import json as _json

        from llmits import json_output as _json_output
        from llmits import service as _service

        sentinel = "token-sentinel-END2END"
        hostile = _json.dumps({
            "code": 200,
            "msg": "echo " + sentinel,
            "data": {
                "level": "pro\x1b]0;pwn\x07" + sentinel,
                "limits": [
                    {"type": "5h", "rawType": "TOKENS_LIMIT", "unit": 3, "percentage": 9}
                ],
            },
        })

        class _Resp:
            status = 200
            body = hostile.encode()
            headers = {}

        class _Transport:
            def get(self, host, path, headers):
                return _Resp()

        svc = _service.RefreshService(
            ("zai",), transport_factory=_Transport, credential_readers={"zai": lambda: sentinel}
        )
        document = _json_output.to_document(svc.refresh(), GENERATED_AT)
        self.assertNotIn(sentinel, document)
        self.assertNotIn("\x1b", document)
        parsed = _json.loads(document)
        provider = parsed["providers"][0]
        self.assertEqual(provider["status"], "available")
        self.assertEqual(provider["plan_name"], "Z.AI Coding Plan")


if __name__ == "__main__":
    unittest.main()
