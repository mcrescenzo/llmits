"""Fixed-host HTTPS transport with hard safety properties.

- Only three allowlisted hosts may be contacted; hosts and paths are
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

import http.client
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass

TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
ALLOWED_HOSTS = frozenset({"api.anthropic.com", "chatgpt.com", "api.z.ai"})


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

    def __init__(self, ssl_context_factory: Callable[[], ssl.SSLContext] | None = None) -> None:
        self._ssl_context_factory = ssl_context_factory or create_tls_context

    def get(
        self, host: str, path: str, headers: Mapping[str, str]
    ) -> TransportResponse:
        if host not in ALLOWED_HOSTS:
            raise HostNotAllowed("host is not in the llmits allowlist")
        context = self._ssl_context_factory()
        connection = http.client.HTTPSConnection(
            host, 443, timeout=TIMEOUT_SECONDS, context=context
        )
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            status = response.status
            if 300 <= status < 400:
                raise RedirectedError()
            body = bytearray()
            while True:
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
