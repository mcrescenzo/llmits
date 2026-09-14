import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from llmits import json_output
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



class OverviewLineTests(unittest.TestCase):
    """json_output.overview_line / overview_state: the --overview one-line format."""

    @staticmethod
    def _snapshot(
        provider="claude",
        status=AVAILABLE,
        percents=(),
        stale=False,
        error=None,
        plan_name="Claude Pro/Max",
        windows=None,
    ):
        if windows is None:
            windows = tuple(
                QuotaWindow(
                    key=f"w{i}",
                    label=f"window {i}",
                    used_percent=percent,
                    remaining_percent=100 - percent,
                )
                for i, percent in enumerate(percents)
            )
        return ProviderSnapshot(
            provider=provider,
            status=status,
            plan_name=plan_name,
            fetched_at=FETCHED_AT,
            windows=windows,
            stale=stale,
            error=error,
        )

    @staticmethod
    def _failed(provider, code, message="upstream reported an error"):
        return ProviderSnapshot(
            provider=provider,
            status=code,
            plan_name=None,
            fetched_at=FETCHED_AT,
            error=ProviderError(code=code, message=message, action="retry later"),
        )

    def test_all_five_output_states(self):
        cases = (
            (self._snapshot(percents=(42,)), "claude=42%"),
            (self._snapshot(percents=(42,), stale=True), "claude=42%~"),
            (self._snapshot(), "claude=available"),
            (self._snapshot(stale=True), "claude=stale"),
            (self._failed("claude", NETWORK_ERROR), "claude=network_error"),
        )
        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(json_output.overview_line([snapshot]), expected)
                self.assertEqual(json_output.overview_state(snapshot), expected.split("=", 1)[1])

    def test_failed_status_outranks_windows(self):
        snapshot = replace(
            self._failed("zai", PARSE_ERROR),
            windows=(QuotaWindow(key="5h", label="5h", used_percent=9, remaining_percent=91),),
        )
        self.assertEqual(json_output.overview_line([snapshot]), "zai=parse_error")

    def test_stale_windowed_state_still_counts_as_available(self):
        stale = self._snapshot(percents=(31,), stale=True)
        self.assertEqual(json_output.overview_line([stale]), "claude=31%~")
        self.assertTrue(json_output.all_available([stale]))

    def test_maximum_percent_across_multiple_windows(self):
        self.assertEqual(
            json_output.overview_line([self._snapshot(percents=(7, 91, 13))]),
            "claude=91%",
        )

    def test_every_normalized_failure_status_renders_verbatim(self):
        for code in (AUTH_REQUIRED, UNAVAILABLE, RATE_LIMITED, NETWORK_ERROR, PARSE_ERROR):
            with self.subTest(code=str(code)):
                self.assertEqual(
                    json_output.overview_line([self._failed("codex", code)]),
                    f"codex={code}",
                )

    def test_tokens_join_with_single_ascii_spaces_in_given_order(self):
        snapshots = [
            self._snapshot("kimi", percents=(10,)),
            self._failed("claude", AUTH_REQUIRED),
            self._snapshot("zai", percents=(99,)),
        ]
        line = json_output.overview_line(snapshots)
        self.assertEqual(line, "kimi=10% claude=auth_required zai=99%")
        self.assertEqual(line.count(" "), len(snapshots) - 1)
        self.assertTrue(line.isascii())

    def test_configured_order_is_the_default(self):
        snapshots = [
            self._snapshot("claude", percents=(10,)),
            self._snapshot("zai", percents=(90,)),
            self._failed("kimi", RATE_LIMITED),
        ]
        self.assertEqual(
            json_output.overview_line(snapshots, "configured"),
            json_output.overview_line(snapshots),
        )

    def test_urgency_order_ranks_failed_then_stale_then_windowed_then_windowless(self):
        snapshots = [
            self._snapshot("zai", percents=(10,)),  # fresh windowed, lowest
            self._snapshot("kimi"),  # fresh windowless
            self._snapshot("claude", percents=(30,), stale=True),  # stale windowed
            self._failed("codex", RATE_LIMITED),  # failed
            self._snapshot("zai", percents=(80,)),  # fresh windowed, highest
            self._snapshot("claude", stale=True),  # stale windowless
        ]
        self.assertEqual(
            json_output.overview_line(snapshots, "urgency"),
            "codex=rate_limited claude=30%~ claude=stale zai=80% zai=10% kimi=available",
        )
        # configured order is untouched by the same call sequence
        self.assertEqual(
            json_output.overview_line(snapshots),
            "zai=10% kimi=available claude=30%~ codex=rate_limited zai=80% claude=stale",
        )

    def test_urgency_orders_fresh_windowed_by_max_percent_descending(self):
        snapshots = [
            self._snapshot("claude", percents=(5, 20)),
            self._snapshot("kimi", percents=(60,)),
            self._snapshot("zai", percents=(40, 50)),
        ]
        self.assertEqual(
            json_output.overview_line(snapshots, "urgency"),
            "kimi=60% zai=50% claude=20%",
        )

    def test_urgency_sort_is_stable_on_ties(self):
        snapshots = [
            self._snapshot("zai", percents=(40,)),
            self._snapshot("claude", percents=(40,)),
            self._failed("kimi", UNAVAILABLE),
            self._failed("codex", PARSE_ERROR),
            self._snapshot("zai", stale=True),
            self._snapshot("claude", stale=True),
        ]
        self.assertEqual(
            json_output.overview_line(snapshots, "urgency"),
            "kimi=unavailable codex=parse_error zai=stale claude=stale zai=40% claude=40%",
        )

    def test_unknown_order_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            json_output.overview_line([self._snapshot()], "alphabetical")
        self.assertIn("unknown overview order", str(ctx.exception))


class OverviewHostileDataTests(unittest.TestCase):
    """Overview renders fixed ids, statuses, and bounded integers only.

    ``QuotaWindow.used_percent`` is not validated at construction, so these
    snapshots are built by hand (like a model bypassing adapter
    normalization) to prove the formatter itself keeps the line ASCII and
    bounded. Plan names, window labels, and error text must never render.
    """

    HOSTILE_PLAN = "claude=99% PLAN-SENTINEL-9x"
    HOSTILE_LABEL = "label\u202e SENTINEL \x1b]0;pwn\x07"
    HOSTILE_PERCENT = "percent-SENTINEL-9x"

    def _window(self, used_percent):
        return QuotaWindow(
            key="5h",
            label=self.HOSTILE_LABEL,
            used_percent=used_percent,
            remaining_percent=0,
        )

    def _snapshot(self, status=AVAILABLE, used_percent=0, error=None):
        return ProviderSnapshot(
            provider="claude",
            status=status,
            plan_name=self.HOSTILE_PLAN,
            fetched_at=FETCHED_AT,
            windows=(self._window(used_percent),),
            error=error,
        )

    def test_non_numeric_used_percent_renders_zero(self):
        self.assertEqual(
            json_output.overview_line([self._snapshot(used_percent=self.HOSTILE_PERCENT)]),
            "claude=0%",
        )

    def test_non_finite_used_percent_renders_zero(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self.assertEqual(
                    json_output.overview_line([self._snapshot(used_percent=bad)]),
                    "claude=0%",
                )

    def test_out_of_range_used_percent_clamps_to_100(self):
        for high in (250, 1000000):
            with self.subTest(high=high):
                self.assertEqual(
                    json_output.overview_line([self._snapshot(used_percent=high)]),
                    "claude=100%",
                )

    def test_fractional_used_percent_renders_a_bounded_integer(self):
        self.assertEqual(
            json_output.overview_line([self._snapshot(used_percent=47.2)]),
            "claude=47%",
        )

    def test_hostile_plan_label_and_error_text_never_reach_the_line(self):
        snapshots = [
            self._snapshot(used_percent=42),
            self._snapshot(
                status=PARSE_ERROR,
                used_percent=99,
                error=ProviderError(
                    code=PARSE_ERROR,
                    message="message " + self.HOSTILE_PERCENT,
                    action="action " + self.HOSTILE_PERCENT,
                ),
            ),
        ]
        line = json_output.overview_line(snapshots, "urgency")
        self.assertEqual(line, "claude=parse_error claude=42%")
        self.assertTrue(line.isascii())
        for hostile in (self.HOSTILE_PLAN, self.HOSTILE_PERCENT, "SENTINEL", "9x"):
            self.assertNotIn(hostile, line)

    def test_non_fixed_provider_ids_are_rejected_not_rendered(self):
        """A provider id outside PROVIDER_IDS can never inject line breaks.

        ``ProviderSnapshot`` does not validate ``provider``, so the formatter
        itself must keep the fixed-id promise: rendering such a snapshot
        would emit a second line (or non-ASCII text), so it raises instead.
        """
        hostile_ids = (
            "evil\nx=99%",
            "claude\tx=99%",
            "claudé",
            "claude ",
            "",
            "SENTINEL-9x",
        )
        for hostile in hostile_ids:
            with self.subTest(hostile=repr(hostile)):
                snapshot = ProviderSnapshot(
                    provider=hostile,
                    status=AVAILABLE,
                    plan_name=self.HOSTILE_PLAN,
                    fetched_at=FETCHED_AT,
                    windows=(self._window(42),),
                )
                for order in ("configured", "urgency"):
                    with self.subTest(order=order):
                        with self.assertRaises(ValueError) as ctx:
                            json_output.overview_line([snapshot], order)
                        self.assertIn("unknown provider id", str(ctx.exception))


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
