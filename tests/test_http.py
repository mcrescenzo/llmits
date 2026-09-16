import http.client
import os
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llmits import http as ll_http


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self._headers = headers or []
        self._chunks = list(chunks)

    def getheaders(self):
        return self._headers

    def read(self, n=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeSocket:
    def __init__(self):
        self.timeouts = []
        self.socket_options = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def setsockopt(self, level, option, value):
        self.socket_options.append((level, option, value))


class FakeConnection:
    script: list = []
    last_instance = None

    def __init__(self, host, port=None, timeout=None, context=None, deadline=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.deadline = deadline
        self.requests = []
        self.closed = False
        self.sock = FakeSocket()
        type(self).last_instance = self

    def request(self, method, path, headers=None, **kwargs):
        self.requests.append((method, path, headers))

    def getresponse(self):
        item = type(self).script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class FakeTlsContext:
    """TLS context double: records server_hostname and returns the socket."""

    verify_mode = ssl.CERT_REQUIRED
    check_hostname = True

    def __init__(self):
        self.server_hostname = None

    def wrap_socket(self, sock, server_hostname):
        self.server_hostname = server_hostname
        return sock


def fake_address_entry(address):
    """One getaddrinfo-shaped entry for an invented TEST-NET address."""
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


class TransportHarness(unittest.TestCase):
    def setUp(self):
        self.contexts = []
        original_factory = ssl.create_default_context

        def factory():
            context = original_factory()
            self.contexts.append(context)
            return context

        self.transport = ll_http.HttpTransport(
            ssl_context_factory=factory,
            connection_factory=FakeConnection,
        )
        FakeConnection.script = []
        FakeConnection.last_instance = None


class AllowlistTests(TransportHarness):
    def test_allowlist_covers_exactly_the_provider_hosts(self):
        self.assertEqual(
            ll_http.ALLOWED_HOSTS,
            {"api.anthropic.com", "chatgpt.com", "api.z.ai", "api.kimi.com", "opencode.ai"},
        )

    def test_disallowed_host_is_refused_without_network(self):
        with self.assertRaises(ll_http.HostNotAllowed):
            self.transport.get("evil.example", "/x", {})
        self.assertIsNone(FakeConnection.last_instance)


class ConnectionTests(TransportHarness):
    def test_default_transport_uses_deadline_aware_connection(self):
        transport = ll_http.HttpTransport()
        self.assertIs(transport._connection_factory, ll_http._DeadlineHTTPSConnection)

    def test_connection_gets_fixed_port_timeout_and_verified_context(self):
        FakeConnection.script = [FakeResponse(200, [], [b"{}"])]
        self.transport.get("chatgpt.com", "/backend-api/wham/usage", {"A": "b"})
        conn = FakeConnection.last_instance
        self.assertEqual((conn.host, conn.port), ("chatgpt.com", 443))
        self.assertEqual(conn.timeout, 10.0)
        self.assertIsInstance(conn.context, ssl.SSLContext)

    def test_tcp_tls_and_request_io_share_the_remaining_deadline(self):
        raw_socket = FakeSocket()
        context = FakeTlsContext()
        connection = ll_http._DeadlineHTTPSConnection(
            "chatgpt.com",
            443,
            timeout=10.0,
            context=context,
            deadline=10.0,
        )
        entry = fake_address_entry("192.0.2.10")
        resolutions = []
        attempts = []

        def resolve(host, port):
            resolutions.append((host, port))
            ll_http.time.monotonic()  # resolution consumes 3s: 1.0 -> 4.0
            return [entry]

        def connect_one(entry, timeout, source_address):
            attempts.append((entry, timeout, source_address))
            return raw_socket

        with mock.patch.object(ll_http, "_resolve_addresses", resolve), mock.patch.object(
            ll_http, "_connect_address", connect_one
        ), mock.patch.object(
            ll_http.time, "monotonic", side_effect=[1.0, 4.0, 4.0, 7.0, 7.0]
        ):
            connection.connect()

        self.assertEqual(resolutions, [("chatgpt.com", 443)])
        # The 3s spent in resolution is deducted: the dial gets only the
        # remaining budget, never a fresh TIMEOUT_SECONDS.
        self.assertEqual(attempts, [(entry, 6.0, None)])
        # TLS and later I/O share what is left of the same deadline.
        self.assertEqual(raw_socket.timeouts, [6.0, 3.0])
        self.assertEqual(context.server_hostname, "chatgpt.com")

    def test_get_forwards_method_path_and_headers(self):
        FakeConnection.script = [FakeResponse(200, [], [b"ok"])]
        self.transport.get(
            "api.anthropic.com",
            "/api/oauth/usage",
            {"Authorization": "Bearer tok", "anthropic-beta": "oauth-2025-04-20"},
        )
        method, path, headers = FakeConnection.last_instance.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/api/oauth/usage")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")


class ConnectPhaseDeadlineTests(unittest.TestCase):
    """One deadline spans name resolution and every resolved-address attempt.

    The resolver and single-address connector are fakes and every address
    is an invented TEST-NET value, so no test here performs a real DNS
    lookup or network connection. Fakes advance the mocked monotonic
    clock to model how much of the single 10s budget each step consumes.
    """

    def _connection(self, context=None):
        return ll_http._DeadlineHTTPSConnection(
            "chatgpt.com",
            443,
            timeout=10.0,
            context=context if context is not None else FakeTlsContext(),
            deadline=10.0,
        )

    def _transport(self):
        return ll_http.HttpTransport(
            ssl_context_factory=FakeTlsContext,
            connection_factory=ll_http._DeadlineHTTPSConnection,
        )

    def test_resolution_cost_leaves_only_the_remainder_for_attempts(self):
        entries = [fake_address_entry("192.0.2.10"), fake_address_entry("192.0.2.11")]
        attempts = []

        def resolve(host, port):
            # Resolution starts at 0.0; the first budget recompute reads
            # 4.0, so DNS consumed 4s of the 10s budget.
            ll_http.time.monotonic()
            return entries

        def connect_one(entry, timeout, source_address):
            attempts.append((entry[4], timeout))
            ll_http.time.monotonic()  # a failed attempt consumes 3s more
            raise ConnectionRefusedError()

        with mock.patch.object(ll_http, "_resolve_addresses", resolve), mock.patch.object(
            ll_http, "_connect_address", connect_one
        ), mock.patch.object(
            ll_http.time, "monotonic", side_effect=[0.0, 4.0, 7.0, 7.0, 7.0, 7.0]
        ):
            with self.assertRaises(ConnectionRefusedError):
                self._connection().connect()

        # Every attempt receives a decreasing remainder, never the fresh
        # 10.0 timeout the old single socket.create_connection call used.
        self.assertEqual([timeout for _, timeout in attempts], [6.0, 3.0])
        self.assertEqual(
            [address for address, _ in attempts],
            [("192.0.2.10", 443), ("192.0.2.11", 443)],
        )

    def test_spent_deadline_stops_attempts_and_surfaces_transport_error(self):
        entries = [
            fake_address_entry(address)
            for address in ("192.0.2.10", "192.0.2.11", "192.0.2.12")
        ]
        attempts = []

        def resolve(host, port):
            return entries

        def connect_one(entry, timeout, source_address):
            attempts.append((entry[4], timeout))
            ll_http.time.monotonic()  # each failing attempt consumes its budget
            raise ConnectionRefusedError()

        # get() reads 0.0 for the deadline; the budget recomputes then see
        # 2.0, 5.0, and 11.0, so the third address is never attempted.
        with mock.patch.object(ll_http, "_resolve_addresses", resolve), mock.patch.object(
            ll_http, "_connect_address", connect_one
        ), mock.patch.object(
            ll_http.time, "monotonic", side_effect=[0.0, 2.0, 5.0, 5.0, 11.0, 11.0, 11.0]
        ):
            with self.assertRaises(ll_http.RequestDeadlineExceeded) as ctx:
                self._transport().get("chatgpt.com", "/p", {})

        self.assertEqual(
            str(ctx.exception), "provider request exceeded the total time limit"
        )
        self.assertEqual([timeout for _, timeout in attempts], [8.0, 5.0])
        # The third address proves the deadline ended the phase instead of
        # granting each address a fresh full timeout.
        self.assertEqual(len(attempts), 2)

    def test_successful_later_address_connects_and_tls_wraps_with_server_hostname(self):
        raw_socket = FakeSocket()
        context = FakeTlsContext()
        entries = [fake_address_entry("192.0.2.10"), fake_address_entry("192.0.2.11")]
        attempts = []

        def resolve(host, port):
            ll_http.time.monotonic()  # resolution consumes 3s: 1.0 -> 4.0
            return entries

        def connect_one(entry, timeout, source_address):
            attempts.append((entry[4], timeout))
            if entry is entries[0]:
                ll_http.time.monotonic()  # the failed attempt consumes 2s more
                raise ConnectionRefusedError()
            return raw_socket

        with mock.patch.object(ll_http, "_resolve_addresses", resolve), mock.patch.object(
            ll_http, "_connect_address", connect_one
        ), mock.patch.object(
            ll_http.time, "monotonic", side_effect=[1.0, 4.0, 6.0, 6.0, 6.0, 9.0, 9.0]
        ):
            connection = self._connection(context)
            connection.connect()

        # The later address still connects once budget remains, and the
        # whole chain keeps sharing one deadline.
        self.assertEqual([timeout for _, timeout in attempts], [6.0, 4.0])
        self.assertIs(connection.sock, raw_socket)
        self.assertEqual(context.server_hostname, "chatgpt.com")
        self.assertEqual(raw_socket.timeouts, [4.0, 1.0])
        self.assertIn(
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1), raw_socket.socket_options
        )

    def test_resolution_exceeding_deadline_fails_fast_without_connecting(self):
        attempts = []

        def resolve(host, port):
            ll_http.time.monotonic()  # resolution starts at 2.0 ...
            ll_http.time.monotonic()  # ... and ends at 15.0, past the deadline
            return [fake_address_entry("192.0.2.10")]

        def connect_one(entry, timeout, source_address):
            attempts.append(timeout)
            raise AssertionError("connect attempted although the deadline was already spent")

        with mock.patch.object(ll_http, "_resolve_addresses", resolve), mock.patch.object(
            ll_http, "_connect_address", connect_one
        ), mock.patch.object(
            ll_http.time, "monotonic", side_effect=[2.0, 15.0, 15.0, 15.0]
        ):
            with self.assertRaises(ll_http.RequestDeadlineExceeded):
                self._connection().connect()

        self.assertEqual(attempts, [])

    def test_empty_resolution_list_is_a_connection_error(self):
        attempts = []

        def resolve(host, port):
            return []

        def connect_one(entry, timeout, source_address):
            attempts.append(timeout)
            raise AssertionError("connect attempted for an empty address list")

        with mock.patch.object(ll_http, "_resolve_addresses", resolve), mock.patch.object(
            ll_http, "_connect_address", connect_one
        ), mock.patch.object(
            ll_http.time, "monotonic", side_effect=[1.0, 1.0]
        ):
            with self.assertRaises(OSError) as ctx:
                self._connection().connect()

        # Matches socket.create_connection: no addresses is a connection
        # error, not a deadline event.
        self.assertEqual(str(ctx.exception), "getaddrinfo returns an empty list")
        self.assertEqual(attempts, [])

    def test_all_addresses_failing_surfaces_last_connection_error(self):
        entries = [fake_address_entry("192.0.2.10"), fake_address_entry("192.0.2.11")]
        attempts = []

        def resolve(host, port):
            return entries

        def connect_one(entry, timeout, source_address):
            attempts.append(timeout)
            raise ConnectionRefusedError()

        with mock.patch.object(ll_http, "_resolve_addresses", resolve), mock.patch.object(
            ll_http, "_connect_address", connect_one
        ), mock.patch.object(
            ll_http.time, "monotonic", side_effect=[0.0, 1.0, 1.0, 1.0]
        ):
            with self.assertRaises(ll_http.TransportError) as ctx:
                self._transport().get("chatgpt.com", "/p", {})

        # The last connection error surfaces, sanitized to its class name,
        # exactly as socket.create_connection's last-error behavior did.
        self.assertEqual(str(ctx.exception), "network error (ConnectionRefusedError)")
        self.assertEqual(attempts, [9.0, 9.0])


class DefaultTlsContextTests(TransportHarness):
    """x47.1: the default transport context must ignore ambient TLS env vars.

    ``ssl.create_default_context()`` honors ``SSLKEYLOGFILE`` (creating a
    writable key-log file at an attacker-chosen path) and its
    ``load_default_certs()`` honors ``SSL_CERT_FILE``/``SSL_CERT_DIR``
    (redirecting the trust store). Every test here runs with sentinel
    values for those variables and never touches the network: the fake
    connection harness stands in for the handshake.
    """

    def test_default_context_ignores_sslkeylogfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "keylog.txt"
            with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": str(sentinel)}):
                context = ll_http.create_tls_context()
                transport = ll_http.HttpTransport(connection_factory=FakeConnection)
                FakeConnection.script = [FakeResponse(200, [], [b"{}"])]
                transport.get("chatgpt.com", "/p", {})
            # No key-log file is created (or written) at the sentinel path.
            self.assertFalse(sentinel.exists())
        self.assertIsNone(context.keylog_filename)
        used = FakeConnection.last_instance.context
        self.assertIsInstance(used, ssl.SSLContext)
        self.assertIsNone(used.keylog_filename)
        self.assertEqual(used.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(used.check_hostname)

    def test_default_transport_never_uses_create_default_context(self):
        # Guard the seam itself: the transport default must remain the
        # hardened factory, never ssl.create_default_context.
        with mock.patch.object(ssl, "create_default_context") as ambient_factory:
            transport = ll_http.HttpTransport(connection_factory=FakeConnection)
            FakeConnection.script = [FakeResponse(200, [], [b"{}"])]
            transport.get("chatgpt.com", "/p", {})
        ambient_factory.assert_not_called()
        self.assertIsInstance(FakeConnection.last_instance.context, ssl.SSLContext)

    def test_default_context_uses_compiled_in_ca_paths_not_ambient_env(self):
        compiled = ssl.DefaultVerifyPaths(
            cafile="/ambient-sentinel/resolved-cafile.pem",
            capath="/ambient-sentinel/resolved-capath",
            openssl_cafile_env="SSL_CERT_FILE",
            openssl_cafile="/compiled-in/cafile.pem",
            openssl_capath_env="SSL_CERT_DIR",
            openssl_capath="/compiled-in/capath",
        )
        hostile_env = {
            "SSL_CERT_FILE": "/ambient-sentinel/cafile.pem",
            "SSL_CERT_DIR": "/ambient-sentinel/capath",
        }
        with mock.patch.dict(os.environ, hostile_env):
            with mock.patch.object(ssl, "get_default_verify_paths", return_value=compiled):
                with mock.patch.object(ssl.SSLContext, "load_verify_locations") as load:
                    context = ll_http.create_tls_context()
        loaded = [
            (call.kwargs.get("cafile"), call.kwargs.get("capath"))
            for call in load.call_args_list
        ]
        # Only the compiled-in locations are loaded, each guarded on its own.
        self.assertEqual(loaded, [("/compiled-in/cafile.pem", None), (None, "/compiled-in/capath")])
        self.assertNotIn("ambient", str(loaded))
        self.assertNotIn("sentinel", str(loaded))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_default_context_skips_missing_compiled_in_locations(self):
        empty = ssl.DefaultVerifyPaths(
            cafile=None,
            capath=None,
            openssl_cafile_env="SSL_CERT_FILE",
            openssl_cafile=None,
            openssl_capath_env="SSL_CERT_DIR",
            openssl_capath=None,
        )
        with mock.patch.object(ssl, "get_default_verify_paths", return_value=empty):
            with mock.patch.object(ssl.SSLContext, "load_verify_locations") as load:
                context = ll_http.create_tls_context()
        load.assert_not_called()
        # No compiled-in store: construction still succeeds and verification
        # stays required (failing closed at handshake time instead).
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_default_context_loads_compiled_store_under_hostile_ca_env(self):
        paths = ssl.get_default_verify_paths()
        if not (paths.openssl_cafile and os.path.isfile(paths.openssl_cafile)):
            self.skipTest("no readable compiled-in CA bundle on this platform")
        hostile_env = {
            "SSL_CERT_FILE": "/ambient-sentinel/cafile.pem",
            "SSL_CERT_DIR": "/ambient-sentinel/capath",
        }
        with mock.patch.dict(os.environ, hostile_env):
            context = ll_http.create_tls_context()
        # The ambient redirect is ignored and the real compiled-in store
        # still loads, so verification remains possible.
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class ResponseTests(TransportHarness):
    def test_success_returns_lowercased_headers_and_full_body(self):
        FakeConnection.script = [
            FakeResponse(200, [("Content-Type", "application/json"), ("X-A", "1")], [b'{"a":', b"1}"])
        ]
        response = self.transport.get("api.z.ai", "/p", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers, {"content-type": "application/json", "x-a": "1"})
        self.assertEqual(response.body, b'{"a":1}')

    def test_connection_is_closed_on_success(self):
        FakeConnection.script = [FakeResponse(200, [], [b"ok"])]
        self.transport.get("api.z.ai", "/p", {})
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_redirect_is_not_followed(self):
        FakeConnection.script = [
            FakeResponse(302, [("Location", "https://evil.example/x")], [b""])
        ]
        with self.assertRaises(ll_http.RedirectedError):
            self.transport.get("chatgpt.com", "/p", {})
        self.assertEqual(len(FakeConnection.last_instance.requests), 1)
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_redirect_boundary_299_is_treated_as_success(self):
        FakeConnection.script = [FakeResponse(299, [], [])]
        response = self.transport.get("chatgpt.com", "/p", {})
        self.assertEqual(response.status, 299)
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_redirect_boundary_300_is_redirect(self):
        FakeConnection.script = [FakeResponse(300, [], [b""])]
        with self.assertRaises(ll_http.RedirectedError):
            self.transport.get("chatgpt.com", "/p", {})
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_redirect_boundary_399_is_redirect(self):
        FakeConnection.script = [FakeResponse(399, [], [b""])]
        with self.assertRaises(ll_http.RedirectedError):
            self.transport.get("chatgpt.com", "/p", {})
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_redirect_boundary_400_is_treated_as_success(self):
        FakeConnection.script = [FakeResponse(400, [], [])]
        response = self.transport.get("chatgpt.com", "/p", {})
        self.assertEqual(response.status, 400)
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_oversized_body_is_rejected(self):
        big = b"x" * 65536
        FakeConnection.script = [FakeResponse(200, [], [big] * 17)]
        with self.assertRaises(ll_http.ResponseTooLarge):
            self.transport.get("api.z.ai", "/p", {})
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_body_of_exactly_max_size_is_accepted(self):
        body = b"x" * ll_http.MAX_RESPONSE_BYTES
        FakeConnection.script = [FakeResponse(200, [], [body])]
        response = self.transport.get("api.z.ai", "/p", {})
        self.assertEqual(len(response.body), ll_http.MAX_RESPONSE_BYTES)
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_body_one_byte_over_max_size_is_rejected(self):
        body = b"x" * (ll_http.MAX_RESPONSE_BYTES + 1)
        FakeConnection.script = [FakeResponse(200, [], [body])]
        with self.assertRaises(ll_http.ResponseTooLarge):
            self.transport.get("api.z.ai", "/p", {})
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_trickling_body_cannot_exceed_total_request_deadline(self):
        FakeConnection.script = [FakeResponse(200, [], [b"a", b"b"])]
        with mock.patch.object(
            ll_http.time,
            "monotonic",
            side_effect=[0.0, 2.0, 9.0, 10.1],
        ):
            with self.assertRaises(ll_http.RequestDeadlineExceeded):
                self.transport.get("api.z.ai", "/p", {})

        connection = FakeConnection.last_instance
        self.assertEqual(connection.sock.timeouts, [8.0, 1.0])
        self.assertTrue(connection.closed)

    def test_connection_refused_becomes_safe_transport_error(self):
        FakeConnection.script = [ConnectionRefusedError()]
        with self.assertRaises(ll_http.TransportError) as ctx:
            self.transport.get("api.z.ai", "/p", {})
        self.assertIn("ConnectionRefusedError", str(ctx.exception))
        self.assertNotIn("api.z.ai", str(ctx.exception))
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_tls_error_message_excludes_exception_detail(self):
        FakeConnection.script = [ssl.SSLError("certificate verify failed: SECRET")]
        with self.assertRaises(ll_http.TransportError) as ctx:
            self.transport.get("api.anthropic.com", "/p", {})
        self.assertIn("SSLError", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_http_exception_becomes_transport_error_without_detail(self):
        FakeConnection.script = [http.client.BadStatusLine("garbage SECRET")]
        with self.assertRaises(ll_http.TransportError) as ctx:
            self.transport.get("chatgpt.com", "/p", {})
        self.assertEqual(str(ctx.exception), "network error (BadStatusLine)")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertTrue(FakeConnection.last_instance.closed)

    def test_timeout_becomes_transport_error(self):
        FakeConnection.script = [TimeoutError()]
        with self.assertRaises(ll_http.TransportError):
            self.transport.get("api.z.ai", "/p", {})
        self.assertTrue(FakeConnection.last_instance.closed)


if __name__ == "__main__":
    unittest.main()
