"""Provider-neutral, read-only credential discovery.

Providers declare ordered, audience-bound environment or structured-file
sources. Adding a provider does not require a new file-access path.

Security contract (enforced by tests):
- Files are opened with O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC.
- The file must be a regular file owned by the current user, with no group
  or other permission bits, and at most 1 MiB.
- Errors never contain tokens or credential paths.
"""
from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

from .models import AUTH_REQUIRED, ProviderError

MAX_CREDENTIAL_BYTES = 1024 * 1024

# Recovery guidance shared by a provider's per-file sources and its overall
# missing-credential error, so the two can never drift apart.
_CLAUDE_RELOGIN_ACTION = (
    "log in with the Claude Code CLI (claude login), or pass --claude-credentials"
)
_CODEX_RELOGIN_ACTION = "log in with the Codex CLI (codex login), or pass --codex-credentials"

# O_NONBLOCK prevents a malicious FIFO at the credential path from blocking
# the open; it is a no-op for regular files, and non-regular files are
# rejected by the fstat checks before any read happens.
_OPEN_FLAGS = (
    os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


class CredentialError(Exception):
    """A credential could not be securely resolved."""

    def __init__(self, reason: str, action: str) -> None:
        super().__init__(reason)
        self.error = ProviderError(code=AUTH_REQUIRED, message=reason, action=action)


class CredentialSource(Protocol):
    """Provider-bound source adapter used by ordered discovery.

    ``audience`` is declared as a read-only property so frozen dataclass
    sources (whose fields cannot be reassigned) satisfy the protocol; the
    audience is fixed when a source is constructed and never re-bound.
    """

    @property
    def audience(self) -> str: ...

    def resolve(self) -> str | None: ...


def _is_header_encodable(value: str) -> bool:
    """Whether a credential value can be sent as an HTTP header field value.

    ``http.client`` raises ``ValueError`` from ``putheader`` for a value that
    cannot be encoded as latin-1 or that contains a bare CR or LF, so such a
    credential can never be sent: rejecting it at discovery time surfaces a
    credential problem where it belongs instead of an unexpected transport
    exception mid-request. Latin-1 obs-text (e.g. an accented letter) is
    encodable and stays accepted.
    """
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return "\r" not in value and "\n" not in value


@dataclass(frozen=True)
class EnvironmentSource:
    """One provider-bound environment variable in a precedence chain."""

    audience: str
    variable: str

    def resolve(self) -> str | None:
        value = os.environ.get(self.variable)
        if not value or not value.strip():
            return None
        token = value.strip()
        if not _is_header_encodable(token):
            raise CredentialError(
                f"{self.variable} contains a malformed credential value",
                f"re-export {self.variable} as a single-line latin-1 value",
            )
        return token


@dataclass(frozen=True)
class CredentialSpec:
    """Ordered credential sources and provider-local missing guidance."""

    provider: str
    sources: tuple[CredentialSource, ...]
    missing_message: str
    missing_action: str


def discover_credential(spec: CredentialSpec) -> str:
    """Return the first usable credential whose audience matches the provider."""
    for source in spec.sources:
        if getattr(source, "audience", None) != spec.provider:
            raise CredentialError(
                "credential source has the wrong provider audience",
                "report this internal llmits credential configuration error",
            )
        value = source.resolve()
        if value:
            return value
    raise CredentialError(spec.missing_message, spec.missing_action)


def _validate_stat(st: os.stat_result, what: str) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise CredentialError(
            f"{what} is not a regular file",
            f"point the {what} setting at a regular credentials file",
        )
    if st.st_uid != os.geteuid():
        raise CredentialError(
            f"{what} is not owned by the current user",
            f"fix ownership of the {what} file (chown)",
        )
    if st.st_mode & 0o077:
        raise CredentialError(
            f"{what} is readable by group or other users",
            f"run chmod 600 on the {what} file",
        )
    if st.st_size > MAX_CREDENTIAL_BYTES:
        raise CredentialError(
            f"{what} is unexpectedly large",
            f"check that the {what} path points at the real credentials file",
        )


def _secure_read_bytes(path: Path, what: str) -> bytes:
    """Read a credential-bearing file with all safety checks applied."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        raise CredentialError(
            f"{what} could not be opened securely",
            f"check that the {what} path is a regular file you own, not a symlink",
        ) from None
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            raise CredentialError(
                f"{what} could not be inspected",
                f"check that the {what} file still exists and is readable",
            ) from None
        _validate_stat(st, what)
        chunks = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                raise CredentialError(
                    f"{what} could not be read",
                    f"check permissions on the {what} file",
                ) from None
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CREDENTIAL_BYTES:
                raise CredentialError(
                    f"{what} is unexpectedly large",
                    f"check that the {what} path points at the real credentials file",
                )
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def secure_read_json(path: Path, what: str) -> dict:
    """Securely read a credential-bearing JSON object."""
    try:
        payload = json.loads(_secure_read_bytes(path, what).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CredentialError(
            f"{what} is not valid JSON",
            "log in again with the official CLI to recreate the file",
        ) from None
    if not isinstance(payload, dict):
        raise CredentialError(
            f"{what} has an unexpected format",
            "log in again with the official CLI to recreate the file",
        )
    return payload


def secure_read_toml(path: Path, what: str) -> dict:
    """Securely read a credential-bearing TOML document."""
    try:
        payload = tomllib.loads(_secure_read_bytes(path, what).decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError):
        raise CredentialError(
            f"{what} is not valid TOML",
            "reconfigure the official tool to recreate the file",
        ) from None
    if not isinstance(payload, dict):
        raise CredentialError(
            f"{what} has an unexpected format",
            "reconfigure the official tool to recreate the file",
        )
    return payload


@dataclass(frozen=True)
class StructuredFileSource:
    """An exact structured file whose extractor also validates provider identity.

    Optional sources are best-effort integrations with other tools: absent,
    insecure, malformed, or non-matching files are skipped. Strict sources
    preserve the existing fail-closed behavior for a provider's own auth file.
    """

    audience: str
    path: Callable[[], Path]
    what: str
    loader: Callable[[Path, str], dict]
    extract: Callable[[dict], str | None]
    optional: bool = False
    missing_action: str = "reconfigure the credential source"

    def resolve(self) -> str | None:
        path = self.path()
        if not path.exists():
            return None
        try:
            payload = self.loader(path, self.what)
        except CredentialError:
            if self.optional:
                return None
            raise
        value = self.extract(payload)
        if isinstance(value, str) and value.strip():
            token = value.strip()
            if not _is_header_encodable(token):
                if self.optional:
                    return None
                raise CredentialError(
                    f"{self.what} contains a malformed access token",
                    self.missing_action,
                )
            return token
        if self.optional:
            return None
        raise CredentialError(
            f"{self.what} does not contain an access token",
            self.missing_action,
        )


def _strict_json_source(
    audience: str,
    path: Path,
    what: str,
    extract: Callable[[dict], str | None],
    missing_action: str,
) -> StructuredFileSource:
    return StructuredFileSource(
        audience=audience,
        path=lambda: path,
        what=what,
        loader=secure_read_json,
        extract=extract,
        missing_action=missing_action,
    )


def _claude_access_token(data: dict) -> str | None:
    oauth = data.get("claudeAiOauth")
    return oauth.get("accessToken") if isinstance(oauth, dict) else None


def _claude_sources(cli_path: str | None) -> tuple[CredentialSource, ...]:
    paths: list[Path] = []
    if cli_path:
        paths.append(Path(cli_path).expanduser())
    override = os.environ.get("LLMITS_CLAUDE_CREDENTIALS")
    if override:
        paths.append(Path(override).expanduser())
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        paths.append(Path(config_dir).expanduser() / ".credentials.json")
    paths.append(Path.home() / ".claude" / ".credentials.json")
    return tuple(
        _strict_json_source(
            "claude",
            path,
            "Claude credentials file",
            _claude_access_token,
            _CLAUDE_RELOGIN_ACTION,
        )
        for path in paths
    )


def read_claude_token(cli_path: str | None = None) -> str:
    return discover_credential(
        CredentialSpec(
            provider="claude",
            sources=_claude_sources(cli_path),
            missing_message="no Claude credentials file found",
            missing_action=_CLAUDE_RELOGIN_ACTION,
        )
    )


def _codex_access_token(data: dict) -> str | None:
    tokens = data.get("tokens")
    return tokens.get("access_token") if isinstance(tokens, dict) else None


def _codex_sources(cli_path: str | None) -> tuple[CredentialSource, ...]:
    paths: list[Path] = []
    if cli_path:
        paths.append(Path(cli_path).expanduser())
    override = os.environ.get("LLMITS_CODEX_CREDENTIALS")
    if override:
        paths.append(Path(override).expanduser())
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        paths.append(Path(codex_home).expanduser() / "auth.json")
    paths.append(Path.home() / ".codex" / "auth.json")
    return tuple(
        _strict_json_source(
            "codex", path, "Codex auth file", _codex_access_token, _CODEX_RELOGIN_ACTION
        )
        for path in paths
    )


def read_codex_token(cli_path: str | None = None) -> str:
    return discover_credential(
        CredentialSpec(
            provider="codex",
            sources=_codex_sources(cli_path),
            missing_message="no Codex auth file found",
            missing_action=_CODEX_RELOGIN_ACTION,
        )
    )


def _pi_literal_api_key(entry_name: str):
    """Extractor for one Pi ``auth.json`` entry carrying a literal api key.

    Pi supports shell commands (``!cmd``) and environment interpolation
    (``$VAR``) in the ``key`` field; llmits never executes commands and
    must not treat an unresolved reference as a credential.
    """

    def extract(data: dict) -> str | None:
        entry = data.get(entry_name)
        if not isinstance(entry, dict) or entry.get("type") != "api_key":
            return None
        key = entry.get("key")
        if not isinstance(key, str):
            return None
        if key.startswith(("!", "$")):
            return None
        return key

    return extract


_pi_zai_key = _pi_literal_api_key("zai")


def _opencode_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        candidate = Path(data_home)
        if candidate.is_absolute():
            return candidate / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _opencode_go_key(data: dict) -> str | None:
    entry = data.get("opencode-go")
    if not isinstance(entry, dict) or entry.get("type") != "api":
        return None
    key = entry.get("key")
    return key if isinstance(key, str) else None


def _opencode_sources() -> tuple[CredentialSource, ...]:
    return (
        StructuredFileSource(
            audience="opencode",
            path=_opencode_auth_path,
            what="OpenCode auth file",
            loader=secure_read_json,
            extract=_opencode_go_key,
            missing_action="log in to OpenCode Go again",
        ),
    )


def read_opencode_go_key() -> str:
    return discover_credential(
        CredentialSpec(
            provider="opencode",
            sources=_opencode_sources(),
            missing_message="OpenCode Go key not set or discoverable",
            missing_action="log in to OpenCode Go again",
        )
    )


_KIMI_CODE_BASE_URLS = frozenset(
    {"https://api.kimi.com/coding/v1", "https://api.kimi.com/coding/v1/"}
)


def _kimi_code_config_path() -> Path:
    configured = os.environ.get("KIMI_CODE_HOME")
    if configured:
        return Path(configured).expanduser() / "config.toml"
    return Path.home() / ".kimi-code" / "config.toml"


def _kimi_key_from_cli_config(data: dict) -> str | None:
    """A ``[providers.<name>]`` ``api_key`` bound to the Kimi Code base URL.

    Mirrors the Z.AI binding rule: only an entry whose ``base_url`` is the
    exact documented Kimi Code endpoint (byte-for-byte, one optional
    trailing slash) can contribute a key. An entry pointed at
    ``api.moonshot.ai`` (pay-as-you-go Moonshot) or any other base is
    skipped, and so is an entry without a ``base_url``: the default
    platform for such an entry is not evidenced, and llmits does not guess
    audiences.
    """
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return None
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        base_url = entry.get("base_url")
        if not isinstance(base_url, str) or base_url not in _KIMI_CODE_BASE_URLS:
            continue
        key = entry.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def _kimi_sources() -> tuple[CredentialSource, ...]:
    return (
        StructuredFileSource(
            audience="kimi",
            path=lambda: Path.home() / ".pi" / "agent" / "auth.json",
            what="Pi auth file",
            loader=secure_read_json,
            extract=_pi_literal_api_key("kimi-coding"),
            optional=True,
        ),
        StructuredFileSource(
            audience="kimi",
            path=_kimi_code_config_path,
            what="Kimi CLI config file",
            loader=secure_read_toml,
            extract=_kimi_key_from_cli_config,
            optional=True,
        ),
    )


def read_kimi_key() -> str:
    return discover_credential(
        CredentialSpec(
            provider="kimi",
            sources=_kimi_sources(),
            missing_message="Kimi Coding Plan key not set or discoverable",
            missing_action=(
                "configure kimi-cli or Pi with a provider-bound Kimi Coding Plan key"
            ),
        )
    )


_ZAI_CLAUDE_BASE_URLS = frozenset(
    {"https://api.z.ai/api/anthropic", "https://api.z.ai/api/anthropic/"}
)
_ZAI_CODEX_BASE_URLS = frozenset(
    {"https://api.z.ai/api/v1", "https://api.z.ai/api/v1/"}
)


def _zai_key_from_claude_settings(data: dict) -> str | None:
    env = data.get("env")
    if not isinstance(env, dict):
        return None
    base_url = env.get("ANTHROPIC_BASE_URL")
    if not isinstance(base_url, str) or base_url not in _ZAI_CLAUDE_BASE_URLS:
        return None
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    return token if isinstance(token, str) else None


def _claude_settings_path() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser() / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _zai_key_from_codex_config(data: dict) -> str | None:
    providers = data.get("model_providers")
    if not isinstance(providers, dict):
        return None
    entry = providers.get("ZAI")
    if not isinstance(entry, dict):
        return None
    base_url = entry.get("base_url")
    if not isinstance(base_url, str) or base_url not in _ZAI_CODEX_BASE_URLS:
        return None
    token = entry.get("experimental_bearer_token")
    return token if isinstance(token, str) else None


def _codex_config_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _zai_sources() -> tuple[CredentialSource, ...]:
    return (
        EnvironmentSource("zai", "ZAI_API_KEY"),
        EnvironmentSource("zai", "ZHIPU_API_KEY"),
        StructuredFileSource(
            audience="zai",
            path=lambda: Path.home() / ".pi" / "agent" / "auth.json",
            what="Pi auth file",
            loader=secure_read_json,
            extract=_pi_zai_key,
            optional=True,
        ),
        StructuredFileSource(
            audience="zai",
            path=_claude_settings_path,
            what="Claude settings file",
            loader=secure_read_json,
            extract=_zai_key_from_claude_settings,
            optional=True,
        ),
        StructuredFileSource(
            audience="zai",
            path=_codex_config_path,
            what="Codex config file",
            loader=secure_read_toml,
            extract=_zai_key_from_codex_config,
            optional=True,
        ),
    )


def read_zai_key() -> str:
    return discover_credential(
        CredentialSpec(
            provider="zai",
            sources=_zai_sources(),
            missing_message="Z.AI API key not set or discoverable",
            missing_action=(
                "log in to Z.AI through a supported coding tool or export ZAI_API_KEY"
            ),
        )
    )


_PROVIDER_CREDENTIAL_READERS: dict[str, Callable[[str | None], str]] = {
    "claude": read_claude_token,
    "codex": read_codex_token,
    "zai": lambda _explicit_path: read_zai_key(),
    "kimi": lambda _explicit_path: read_kimi_key(),
    "opencode": lambda _explicit_path: read_opencode_go_key(),
}


def credential_provider_ids() -> tuple[str, ...]:
    """Return provider ids with registered credential-discovery specifications."""
    return tuple(_PROVIDER_CREDENTIAL_READERS)


def read_provider_credential(provider: str, explicit_path: str | None = None) -> str:
    """Resolve one provider's credential through its ordered source specification."""
    reader = _PROVIDER_CREDENTIAL_READERS.get(provider)
    if reader is None:
        raise CredentialError(
            "no credential discovery specification for provider",
            "report this internal llmits provider configuration error",
        )
    return reader(explicit_path)


def default_credential_readers(
    explicit_paths: Mapping[str, str | None] | None = None,
) -> dict[str, Callable[[], str]]:
    """Build a ``{provider: reader}`` map over every registered provider id.

    ``explicit_paths`` supplies a CLI-provided path override per provider id
    (providers whose reader takes no explicit path, such as Z.AI, simply
    ignore it). Shared by ``cli._credential_reader_map`` (explicit CLI paths)
    and ``service.RefreshService``'s default-reader fallback (no explicit
    paths) so the two never drift on how the map is built.
    """
    paths = explicit_paths or {}
    readers: dict[str, Callable[[], str]] = {}
    for provider in credential_provider_ids():
        paths_for_provider = paths.get(provider)
        readers[provider] = partial(
            read_provider_credential, provider, explicit_path=paths_for_provider
        )
    return readers
