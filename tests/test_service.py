import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from llmits import auth, providers, service
from llmits.models import AVAILABLE, AUTH_REQUIRED, NETWORK_ERROR, PARSE_ERROR

FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "orchestration-sentinel-token"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeTransport:
    def __init__(self, behavior=None):
        # behavior: callable (host, path, headers) -> Response
        self.behavior = behavior or (lambda host, path, headers: _ok(_fixture_bytes("claude_usage_full.json")))
        self.calls = []

    def get(self, host, path, headers):
        self.calls.append((host, path, headers))
        return self.behavior(host, path, headers)


class Response:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}


def _ok(body):
    return Response(200, body)


CLAUDE_BODY = _fixture_bytes("claude_usage_full.json")
CODEX_BODY = _fixture_bytes("codex_usage_full.json")
ZAI_BODY = _fixture_bytes("zai_quota_full.json")


def by_host(host, path, headers):
    return {
        "api.anthropic.com": _ok(CLAUDE_BODY),
        "chatgpt.com": _ok(CODEX_BODY),
        "api.z.ai": _ok(ZAI_BODY),
    }[host]


def readers(token=SENTINEL):
    return {
        "claude": lambda: token,
        "codex": lambda: token,
        "zai": lambda: token,
    }


class ServiceTests(unittest.TestCase):
    def make_service(self, transport, ids=("claude", "codex", "zai"), readers_map=None):
        return service.RefreshService(
            ids,
            transport_factory=lambda: transport,
            credential_readers=readers_map or readers(),
        )

    def test_all_providers_available_in_requested_order(self):
        svc = self.make_service(FakeTransport(by_host))
        snapshots = svc.refresh()
        self.assertEqual([s.provider for s in snapshots], ["claude", "codex", "zai"])
        self.assertTrue(all(s.status == AVAILABLE for s in snapshots))
        self.assertEqual(snapshots[0].plan_name, "Claude Pro/Max")
        self.assertEqual(snapshots[1].plan_name, "Codex plus")
        self.assertEqual(snapshots[2].plan_name, "Z.AI pro")

    def test_subset_refresh_only_touches_requested_providers(self):
        transport = FakeTransport(by_host)
        svc = self.make_service(transport)
        snapshots = svc.refresh(("codex",))
        self.assertEqual([s.provider for s in snapshots], ["codex"])
        hosts = {host for host, _, _ in transport.calls}
        self.assertEqual(hosts, {"chatgpt.com"})

    def test_credential_failure_is_isolated_per_provider(self):
        def broken():
            raise auth.CredentialError("no Claude credentials file found", "run claude login")

        mixed = readers()
        mixed["claude"] = broken
        svc = self.make_service(FakeTransport(by_host), readers_map=mixed)
        snapshots = svc.refresh()
        by_id = {s.provider: s for s in snapshots}
        self.assertEqual(by_id["claude"].status, AUTH_REQUIRED)
        self.assertIn("claude login", by_id["claude"].error.action)
        self.assertEqual(by_id["codex"].status, AVAILABLE)
        self.assertEqual(by_id["zai"].status, AVAILABLE)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            self.make_service(FakeTransport(), ids=("claude", "grok"))

    def test_empty_provider_ids_rejected_at_construction(self):
        with self.assertRaisesRegex(ValueError, "no providers configured"):
            service.RefreshService(
                (), transport_factory=lambda: FakeTransport(), credential_readers=readers()
            )

    def test_provider_ids_property_returns_constructed_ids(self):
        svc = self.make_service(FakeTransport(by_host), ids=("codex", "claude"))
        self.assertEqual(svc.provider_ids, ("codex", "claude"))

    def test_missing_credential_reader_is_treated_as_auth_required(self):
        partial = readers()
        del partial["zai"]
        svc = self.make_service(FakeTransport(by_host), ids=("claude", "zai"), readers_map=partial)
        snapshots = svc.refresh()
        by_id = {s.provider: s for s in snapshots}
        self.assertEqual(by_id["claude"].status, AVAILABLE)
        self.assertEqual(by_id["zai"].status, AUTH_REQUIRED)
        self.assertEqual(by_id["zai"].error.message, "no credential reader configured")
        self.assertIn("internal llmits error", by_id["zai"].error.action)

    def test_failure_retains_last_good_windows_as_stale(self):
        state = {"fail": False}

        def behavior(host, path, headers):
            if state["fail"]:
                return Response(500, b"boom")
            return by_host(host, path, headers)

        transport = FakeTransport(behavior)
        svc = self.make_service(transport)
        first = svc.refresh(("claude",))
        self.assertEqual(first[0].status, AVAILABLE)
        self.assertFalse(first[0].stale)

        state["fail"] = True
        second = svc.refresh(("claude",))
        self.assertEqual(second[0].status, AVAILABLE)
        self.assertTrue(second[0].stale)
        self.assertEqual(second[0].windows, first[0].windows)
        self.assertEqual(second[0].fetched_at, first[0].fetched_at)
        self.assertEqual(second[0].plan_name, first[0].plan_name)
        self.assertEqual(second[0].error.code, NETWORK_ERROR)
        self.assertIn("HTTP 500", second[0].error.message)

        state["fail"] = False
        third = svc.refresh(("claude",))
        self.assertFalse(third[0].stale)
        self.assertIsNone(third[0].error)

    def test_first_failure_without_history_is_plain_error(self):
        transport = FakeTransport(lambda host, path, headers: Response(401, b"nope"))
        svc = self.make_service(transport, ids=("zai",))
        snapshots = svc.refresh()
        self.assertEqual(snapshots[0].status, AUTH_REQUIRED)
        self.assertFalse(snapshots[0].stale)
        self.assertEqual(snapshots[0].windows, ())

    def test_refresh_none_means_all_configured_providers(self):
        svc = self.make_service(FakeTransport(by_host))
        explicit_all = svc.refresh(("claude", "codex", "zai"))
        default = svc.refresh(None)
        self.assertEqual(
            [s.provider for s in default], [s.provider for s in explicit_all]
        )
        self.assertEqual([s.provider for s in default], ["claude", "codex", "zai"])

    def test_refresh_explicit_empty_sequence_refreshes_nothing(self):
        transport = FakeTransport(by_host)
        factory_calls = []

        def transport_factory():
            factory_calls.append(True)
            return transport

        svc = service.RefreshService(
            ("claude", "codex", "zai"),
            transport_factory=transport_factory,
            credential_readers=readers(),
        )
        snapshots = svc.refresh(())
        self.assertEqual(snapshots, ())
        self.assertEqual(factory_calls, [])
        self.assertEqual(transport.calls, [])

    def test_refresh_duplicate_ids_collapse_to_one_entry(self):
        transport = FakeTransport(by_host)
        svc = self.make_service(transport)
        snapshots = svc.refresh(("claude", "claude"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].provider, "claude")
        hosts = [host for host, _, _ in transport.calls]
        self.assertEqual(hosts, ["api.anthropic.com"])

    def test_refresh_unknown_provider_rejected_via_shared_helper(self):
        svc = self.make_service(FakeTransport())
        with self.assertRaises(ValueError):
            svc.refresh(("claude", "grok"))


class UnexpectedFetcherFailureTests(unittest.TestCase):
    """_fetch_one's broad `except Exception` is the documented guarantee that a
    provider bug can never kill the app. Patch a raising fetcher into the
    provider registry (active only for this test) rather than special-casing
    production code, and prove the resulting snapshot carries only the
    exception's class name — never its message, which could hold secrets.
    """

    def test_unexpected_exception_becomes_parse_error_and_spares_siblings(self):
        def raising_fetch(token, transport, now=None):
            raise ValueError("leaky secret detail that must never surface")

        transport = FakeTransport(by_host)
        svc = service.RefreshService(
            ("claude", "codex", "zai"),
            transport_factory=lambda: transport,
            credential_readers=readers(),
        )
        with mock.patch.dict(providers.FETCHERS, {"claude": raising_fetch}):
            snapshots = svc.refresh()

        by_id = {s.provider: s for s in snapshots}
        self.assertEqual(by_id["claude"].status, PARSE_ERROR)
        self.assertEqual(
            by_id["claude"].error.message, "internal provider error (ValueError)"
        )
        self.assertEqual(by_id["codex"].status, AVAILABLE)
        self.assertEqual(by_id["zai"].status, AVAILABLE)


class DefaultTransportWiringTests(unittest.TestCase):
    """Every real invocation gets its network client from
    `transport_factory or HttpTransport` in __init__. No other test ever
    constructs RefreshService without an explicit transport_factory, so this
    wiring — and that the resulting factory is actually usable — was
    otherwise never proven.
    """

    def test_omitting_transport_factory_wires_and_uses_http_transport(self):
        calls = []

        class FakeHttpTransport:
            def __init__(self):
                calls.append("constructed")

            def get(self, host, path, headers):
                return by_host(host, path, headers)

        with mock.patch.object(service, "HttpTransport", FakeHttpTransport):
            svc = service.RefreshService(("claude",), credential_readers=readers())
            self.assertIs(svc._transport_factory, FakeHttpTransport)
            snapshots = svc.refresh()

        self.assertEqual(calls, ["constructed"])
        self.assertEqual(snapshots[0].status, AVAILABLE)


class ConcurrentRefreshTests(unittest.TestCase):
    """RefreshService holds shared mutable _last_good behind self._lock so a
    manual refresh can safely race the auto-refresh timer. Two threads drive
    real refresh() calls for the same provider through a barrier so their
    post-fetch critical sections actually overlap, one seeing success and one
    seeing failure each round, proving the lock keeps _last_good coherent
    under real contention instead of merely being present but unexercised.
    """

    def test_concurrent_refresh_calls_never_corrupt_or_lose_last_good_state(self):
        barrier = threading.Barrier(2)
        rounds_per_thread = 20

        class AlternatingTransport:
            def __init__(self):
                self._count_lock = threading.Lock()
                self._count = 0

            def get(self, host, path, headers):
                with self._count_lock:
                    self._count += 1
                    n = self._count
                barrier.wait(timeout=5)
                if n % 2 == 0:
                    return Response(500, b"boom")
                return by_host(host, path, headers)

        seed_transport = FakeTransport(by_host)
        svc = service.RefreshService(
            ("claude",),
            transport_factory=lambda: seed_transport,
            credential_readers=readers(),
        )
        # Seed _last_good with a real success outside the barrier (a lone
        # refresh() here would deadlock: the barrier needs two arrivals).
        seed = svc.refresh()
        self.assertEqual(seed[0].status, AVAILABLE)

        transport = AlternatingTransport()
        svc._transport_factory = lambda: transport

        errors = []
        results = []
        results_lock = threading.Lock()

        def worker():
            try:
                for _ in range(rounds_per_thread):
                    snapshots = svc.refresh()
                    with results_lock:
                        results.append(snapshots[0])
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertFalse(any(t.is_alive() for t in threads), "worker thread hung")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2 * rounds_per_thread)
        # Every result the lock hands back — fresh success or stale
        # fallback — must carry the established plan_name and windows; a
        # torn or lost update under the lock would show up as one missing.
        for snapshot in results:
            self.assertEqual(snapshot.plan_name, seed[0].plan_name)
            self.assertEqual(snapshot.windows, seed[0].windows)
        last_good = svc._last_good.get("claude")
        self.assertIsNotNone(last_good)
        self.assertEqual(last_good.status, AVAILABLE)


class ShutdownSubprocessTests(unittest.TestCase):
    """Process-level proof that close() does not delay interpreter exit.

    concurrent.futures.ThreadPoolExecutor registers an interpreter-exit
    hook that joins every worker thread it ever created, unconditionally
    — even when the thread is marked daemon — so this can only be proven
    by actually exiting a subprocess, not by timing close() in-process.
    """

    def test_close_during_in_flight_fetch_lets_process_exit_promptly(self):
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        script = f"""
import sys, threading, time
sys.path.insert(0, {src_dir!r})
from llmits import service as service_mod

class SlowTransport:
    def get(self, host, path, headers):
        time.sleep(2)
        raise RuntimeError("should never actually return")

svc = service_mod.RefreshService(
    ("claude",),
    transport_factory=lambda: SlowTransport(),
    credential_readers={{"claude": lambda: "token"}},
)
# Mirrors production usage: refresh() itself runs on a daemon thread
# (AppController._run_refresh), so it must not pin interpreter exit.
threading.Thread(target=svc.refresh, daemon=True).start()
time.sleep(0.05)  # let the in-flight fetch actually start
svc.close()
"""
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 1.0, f"subprocess took {elapsed:.2f}s to exit")


if __name__ == "__main__":
    unittest.main()
