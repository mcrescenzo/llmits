"""Fixed-host HTTPS transport with hard safety properties.

- Only the allowlisted hosts may be contacted; hosts and paths are
  constants inside the provider adapters and can never be influenced by
  CLI options or environment variables.
- TLS verification is always enabled; the default context is built by
  ``create_tls_context`` from the interpreter's compiled-in CA locations
  only, so ambient ``SSLKEYLOGFILE``, ``SSL_CERT_FILE``, and ``SSL_CERT_DIR``
  environment variables never influence key logging or the trust store.
- Proxies are never used.
- Redirects are never followed (reported as an error).
- Requests time out and response bodies are size-capped.
- Error messages contain exception class names only — never URLs,
  headers, or bodies.
"""
from __future__ import annotations

import errno
import http.client
import socket
import ssl
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
ALLOWED_HOSTS = frozenset(
    {"api.anthropic.com", "chatgpt.com", "api.z.ai", "api.kimi.com", "opencode.ai"}
)


class TransportError(Exception):
    """A safe, host-agnostic network failure."""


class HostNotAllowed(TransportError):
    pass


class RedirectedError(TransportError):
    def __init__(self) -> None:
        super().__init__("provider endpoint returned an unexpected redirect")


class ResponseTooLarge(TransportError):
    def __init__(self) -> None:
        super().__init__("provider response exceeded the size limit")


class RequestDeadlineExceeded(TransportError):
    def __init__(self) -> None:
        super().__init__("provider request exceeded the total time limit")


def _remaining_request_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RequestDeadlineExceeded()
    return remaining


def _apply_request_deadline(connection: http.client.HTTPSConnection, deadline: float) -> None:
    """Apply the remaining total budget to the next blocking socket operation."""
    remaining = _remaining_request_time(deadline)
    sock = getattr(connection, "sock", None)
    if sock is not None:
        sock.settimeout(remaining)


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP, TLS, and later I/O share one deadline."""

    def __init__(self, *args, deadline: float, **kwargs) -> None:
        self._request_deadline = deadline
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        # The transport never configures a tunnel or proxy. Keeping this
        # branch closed prevents a future caller from bypassing the fixed
        # direct-connection contract through HTTPSConnection internals.
        if getattr(self, "_tunnel_host", None) is not None:
            raise TransportError("proxy tunnels are not supported")
        sys.audit("http.client.connect", self, self.host, self.port)
        self.timeout = _remaining_request_time(self._request_deadline)
        self.sock = self._create_connection(  # type: ignore[attr-defined]
            (self.host, self.port),
            self.timeout,
            self.source_address,  # type: ignore[attr-defined]
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        _apply_request_deadline(self, self._request_deadline)
        self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]
            self.sock, server_hostname=self.host
        )
        _apply_request_deadline(self, self._request_deadline)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def create_tls_context() -> ssl.SSLContext:
    """Verified TLS context that ignores the ambient TLS environment.

    ``ssl.create_default_context()`` is not used because it honors
    ``SSLKEYLOGFILE`` (writing TLS session secrets to an attacker-chosen
    path) and ``load_default_certs()`` honors ``SSL_CERT_FILE`` and
    ``SSL_CERT_DIR`` (redirecting the trust store to attacker-chosen
    paths). Instead the context is constructed directly — which never
    reads those variables — and loaded with only the compiled-in CA
    locations from ``ssl.get_default_verify_paths()``. Certificate and
    hostname verification stay required; a missing compiled-in location
    is left unloaded so verification fails closed at handshake time
    rather than falling back to ambient environment paths.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    paths = ssl.get_default_verify_paths()
    for cafile, capath in (
        (paths.openssl_cafile, None),
        (None, paths.openssl_capath),
    ):
        if not cafile and not capath:
            continue
        try:
            context.load_verify_locations(cafile=cafile, capath=capath)
        except (OSError, ssl.SSLError):
            # A missing or unreadable compiled-in location must not abort
            # context construction (or fall back to an ambient env path):
            # verification stays CERT_REQUIRED and fails closed.
            continue
    return context


class HttpTransport:
    """Minimal GET-only HTTPS transport behind an injectable seam."""

    def __init__(
        self,
        ssl_context_factory: Callable[[], ssl.SSLContext] | None = None,
        connection_factory: Callable[..., http.client.HTTPSConnection] | None = None,
    ) -> None:
        self._ssl_context_factory = ssl_context_factory or create_tls_context
        self._connection_factory = connection_factory or _DeadlineHTTPSConnection

    def get(
        self, host: str, path: str, headers: Mapping[str, str]
    ) -> TransportResponse:
        if host not in ALLOWED_HOSTS:
            raise HostNotAllowed("host is not in the llmits allowlist")
        context = self._ssl_context_factory()
        deadline = time.monotonic() + TIMEOUT_SECONDS
        connection = self._connection_factory(
            host,
            443,
            timeout=TIMEOUT_SECONDS,
            context=context,
            deadline=deadline,
        )
        try:
            connection.request("GET", path, headers=dict(headers))
            _apply_request_deadline(connection, deadline)
            response = connection.getresponse()
            status = response.status
            if 300 <= status < 400:
                raise RedirectedError()
            body = bytearray()
            while True:
                _apply_request_deadline(connection, deadline)
                chunk = response.read(65536)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ResponseTooLarge()
            response_headers = {
                name.lower(): value for name, value in response.getheaders()
            }
            return TransportResponse(status=status, headers=response_headers, body=bytes(body))
        except TransportError:
            raise
        except (TimeoutError, ssl.SSLError) as exc:
            raise TransportError(f"network error ({type(exc).__name__})") from None
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"network error ({type(exc).__name__})") from None
        finally:
            connection.close()
