import http.client
import os
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

        class FakeContext:
            verify_mode = ssl.CERT_REQUIRED
            check_hostname = True

            def wrap_socket(self, sock, server_hostname):
                self.server_hostname = server_hostname
                return sock

        context = FakeContext()
        connection = ll_http._DeadlineHTTPSConnection(
            "chatgpt.com",
            443,
            timeout=10.0,
            context=context,
            deadline=10.0,
        )
        connect_timeouts = []

        def create_connection(address, timeout, source_address):
            connect_timeouts.append(timeout)
            return raw_socket

        connection._create_connection = create_connection
        with mock.patch.object(ll_http.time, "monotonic", side_effect=[1.0, 4.0, 7.0]):
            connection.connect()

        self.assertEqual(connect_timeouts, [9.0])
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
