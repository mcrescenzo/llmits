"""Reusable conformance suite for every registered provider adapter.

Each provider keeps its own detailed tests (Claude's beta header and model
limits, Codex's parsing details, Z.AI's bearer retry). This suite pins the
contract every adapter must satisfy regardless of provider: the registry
wiring, a synthetic 200 success normalizing to the documented windows, the
fail-closed parse-error path, transport-failure and authentication
classification, and the compile-time host/path pin. Adding a provider
without extending CASES fails `test_every_registered_provider_has_a_case`.

All fixtures are synthetic; no provider service is contacted.
"""
from __future__ import annotations

import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from llmits.http import TransportError
from llmits.models import (
    AUTH_REQUIRED,
    AVAILABLE,
    NETWORK_ERROR,
    PARSE_ERROR,
    UNAVAILABLE,
    ProviderStatus,
)
from llmits.providers import FETCHERS, PROVIDER_IDS, claude, codex, kimi, opencode, zai

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
SENTINEL = "conformance-sentinel-token"
BODY_SENTINEL = "conformance-sentinel-body"


@dataclass(frozen=True)
class ProviderCase:
    """One provider's conformance inputs and expected normalized output."""

    provider_id: str
    module: types.ModuleType
    fixture: str
    plan_name: str
    window_keys: tuple[str, ...]
    auth_attempts: int  # transport GETs a single 401/403 response triggers
    forbidden_status: ProviderStatus


CASES: dict[str, ProviderCase] = {
    "claude": ProviderCase(
        provider_id="claude",
        module=claude,
        fixture="claude_usage_full.json",
        plan_name="Claude Pro/Max",
        window_keys=(
            "5h",
            "weekly",
            "weekly_opus",
            "weekly_sonnet",
            "weekly_fable",
            "extra_credits",
        ),
        auth_attempts=1,
        forbidden_status=AUTH_REQUIRED,
    ),
    "codex": ProviderCase(
        provider_id="codex",
        module=codex,
        fixture="codex_usage_full.json",
        plan_name="Codex plus",
        window_keys=("5h", "7d", "spk/7d", "credits", "reset_credits"),
        auth_attempts=1,
        forbidden_status=AUTH_REQUIRED,
    ),
    "zai": ProviderCase(
        provider_id="zai",
        module=zai,
        fixture="zai_quota_full.json",
        plan_name="Z.AI pro",
        window_keys=("5h", "weekly", "monthly_mcp"),
        # 401/403 retries once with a Bearer header before giving up.
        auth_attempts=2,
        forbidden_status=AUTH_REQUIRED,
    ),
    "kimi": ProviderCase(
        provider_id="kimi",
        module=kimi,
        fixture="kimi_usages_full.json",
        plan_name="Kimi Coding Plan",
        window_keys=("5h", "weekly"),
        auth_attempts=1,
        forbidden_status=AUTH_REQUIRED,
    ),
    "opencode": ProviderCase(
        provider_id="opencode",
        module=opencode,
        fixture="opencode_go_usage_full.json",
        plan_name="OpenCode Go",
        window_keys=("5h", "weekly", "monthly"),
        auth_attempts=1,
        forbidden_status=UNAVAILABLE,
    ),
}


class Response:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}


class ScriptedTransport:
    """Records every request; answers from a fixed (host, path, headers) rule."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def get(self, host, path, headers):
        self.calls.append((host, path, dict(headers)))
        return self.behavior(host, path, headers)


def ok_fixture(module) -> Response:
    return Response(200, (FIXTURES / _case(module).fixture).read_bytes())


def _case(module) -> ProviderCase:
    for case in CASES.values():
        if case.module is module:
            return case
    raise AssertionError(f"no conformance case for module {module!r}")


def _texts(snapshot) -> list[str]:
    texts = []
    if snapshot.plan_name:
        texts.append(snapshot.plan_name)
    if snapshot.error is not None:
        texts.extend((snapshot.error.message, snapshot.error.action))
    return texts


class ProviderConformanceTests(unittest.TestCase):
    def test_registry_fetcher_is_the_module_fetcher(self):
        for provider_id in PROVIDER_IDS:
            with self.subTest(provider=provider_id):
                case = CASES[provider_id]
                self.assertIs(FETCHERS[provider_id], case.module.fetch)

    def test_every_registered_provider_has_a_case(self):
        self.assertEqual(set(CASES), set(PROVIDER_IDS))

    def test_synthetic_success_normalizes_to_expected_snapshot(self):
        for provider_id in PROVIDER_IDS:
            with self.subTest(provider=provider_id):
                case = CASES[provider_id]
                transport = ScriptedTransport(lambda *_: ok_fixture(case.module))
                snapshot = case.module.fetch(SENTINEL, transport, now=NOW)
                self.assertEqual(snapshot.provider, provider_id)
                self.assertEqual(snapshot.status, AVAILABLE)
                self.assertEqual(snapshot.plan_name, case.plan_name)
                self.assertEqual(snapshot.fetched_at, NOW)
                self.assertFalse(snapshot.stale)
                self.assertIsNone(snapshot.error)
                self.assertEqual(
                    tuple(window.key for window in snapshot.windows), case.window_keys
                )
                # The token traveled only in the Authorization header.
                for host, path, headers in transport.calls:
                    self.assertEqual((host, path), (case.module.HOST, case.module.PATH))
                    self.assertNotIn(SENTINEL, host)
                    self.assertNotIn(SENTINEL, path)

    def test_malformed_success_body_fails_closed_to_parse_error(self):
        for provider_id in PROVIDER_IDS:
            with self.subTest(provider=provider_id):
                case = CASES[provider_id]
                transport = ScriptedTransport(
                    lambda *_: Response(200, b"\xff\xfe not json at all")
                )
                snapshot = case.module.fetch(SENTINEL, transport, now=NOW)
                self.assertEqual(snapshot.status, PARSE_ERROR)
                self.assertIsNone(snapshot.plan_name)
                self.assertEqual(snapshot.windows, ())
                for text in _texts(snapshot):
                    self.assertNotIn(BODY_SENTINEL, text)

    def test_transport_failure_is_network_error_without_exception_text(self):
        for provider_id in PROVIDER_IDS:
            with self.subTest(provider=provider_id):
                case = CASES[provider_id]

                def raising(*_args):
                    raise TransportError(f"leak {BODY_SENTINEL} connection reset")

                transport = ScriptedTransport(raising)
                snapshot = case.module.fetch(SENTINEL, transport, now=NOW)
                self.assertEqual(snapshot.status, NETWORK_ERROR)
                for text in _texts(snapshot):
                    self.assertNotIn(BODY_SENTINEL, text)
                    self.assertNotIn(SENTINEL, text)

    def test_authentication_or_entitlement_failure_is_normalized_without_leakage(self):
        for provider_id in PROVIDER_IDS:
            case = CASES[provider_id]
            for status in (401, 403):
                with self.subTest(provider=provider_id, status=status):
                    body = f'{{"error": "denied {BODY_SENTINEL}"}}'.encode()
                    transport = ScriptedTransport(lambda *_: Response(status, body))
                    snapshot = case.module.fetch(SENTINEL, transport, now=NOW)
                    expected = AUTH_REQUIRED if status == 401 else case.forbidden_status
                    self.assertEqual(snapshot.status, expected)
                    self.assertEqual(len(transport.calls), case.auth_attempts)
                    for host, path, headers in transport.calls:
                        self.assertEqual((host, path), (case.module.HOST, case.module.PATH))
                    for text in _texts(snapshot):
                        self.assertNotIn(BODY_SENTINEL, text)
                        self.assertNotIn(SENTINEL, text)

    def test_every_request_pins_the_module_host_and_path(self):
        # Across success, malformed, transport-failure, and auth paths, no
        # adapter ever contacts anything but its compile-time host+path.
        for provider_id in PROVIDER_IDS:
            with self.subTest(provider=provider_id):
                case = CASES[provider_id]
                behaviors = [
                    lambda *_: ok_fixture(case.module),
                    lambda *_: Response(200, b"\xff\xfe"),
                    lambda *_: Response(status=500, body=b"boom"),
                    lambda *_: Response(status=429, body=b"slow down"),
                ]

                def raising(*_args):
                    raise TransportError("network down")

                behaviors.append(raising)
                for behavior in behaviors:
                    transport = ScriptedTransport(behavior)
                    case.module.fetch(SENTINEL, transport, now=NOW)
                    for host, path, _headers in transport.calls:
                        self.assertEqual((host, path), (case.module.HOST, case.module.PATH))


if __name__ == "__main__":
    unittest.main()
