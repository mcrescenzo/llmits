"""Concurrent provider refresh orchestration with in-memory last-good state."""
from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timezone

from . import auth
from .http import HttpTransport
from .models import AUTH_REQUIRED, AVAILABLE, ProviderError, ProviderSnapshot
from .providers import FETCHERS, common, is_known


def _validate_ids(ids: Sequence[str]) -> None:
    """Raise ``ValueError`` naming every id in ``ids`` that isn't a known provider.

    Shared by ``RefreshService.__init__`` and ``RefreshService.refresh`` so the
    two entry points can never drift on what counts as a valid provider id.
    """
    unknown = [pid for pid in ids if not is_known(pid)]
    if unknown:
        raise ValueError(f"unknown providers: {', '.join(unknown)}")


class RefreshService:
    """Fetches the configured providers concurrently, in requested order.

    A successful outcome replaces the provider's stored last-good state. When
    a provider previously succeeded and now fails, the stored last-good
    windows are left in place (not replaced) — refresh() returns a derived,
    stale-marked copy carrying the latest error instead.

    Each ``refresh()`` call fetches every requested provider on its own
    short-lived daemon thread rather than a pooled
    ``concurrent.futures.ThreadPoolExecutor``: that pool registers its
    worker threads with an interpreter-exit hook that joins them
    unconditionally, even when marked daemon, which would keep the process
    alive until an in-flight HTTP call returns (up to the transport's
    timeout). ``close()`` therefore has nothing to shut down and abandons
    any fetches still in flight rather than waiting for them.
    """

    def __init__(
        self,
        provider_ids: Sequence[str],
        transport_factory: Callable[[], object] | None = None,
        credential_readers: Mapping[str, Callable[[], str]] | None = None,
    ) -> None:
        _validate_ids(provider_ids)
        if not provider_ids:
            raise ValueError("no providers configured")
        self._ids = tuple(provider_ids)
        self._transport_factory = transport_factory or HttpTransport
        self._readers = (
            dict(credential_readers) if credential_readers else auth.default_credential_readers()
        )
        self._lock = threading.Lock()
        self._last_good: dict[str, ProviderSnapshot] = {}

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return self._ids

    def refresh(self, provider_ids: Sequence[str] | None = None) -> tuple[ProviderSnapshot, ...]:
        """Refresh the requested providers and return their snapshots.

        ``provider_ids=None`` (the default) refreshes every provider this
        service was constructed with, in that order. An explicit empty
        sequence refreshes nothing and returns ``()``. Duplicate ids collapse
        to a single fetch and a single result entry, matching the CLI's own
        dedup contract.
        """
        requested = self._ids if provider_ids is None else tuple(provider_ids)
        _validate_ids(requested)
        ids = tuple(dict.fromkeys(requested))
        if not ids:
            return ()
        transport = self._transport_factory()
        futures: dict[str, Future] = {provider_id: Future() for provider_id in ids}
        for provider_id, future in futures.items():
            threading.Thread(
                target=self._run_fetch,
                args=(provider_id, transport, future),
                name=f"llmits-fetch-{provider_id}",
                daemon=True,
            ).start()
        fresh = {provider_id: future.result() for provider_id, future in futures.items()}
        with self._lock:
            results = []
            for provider_id in ids:
                snapshot = fresh[provider_id]
                if snapshot.status == AVAILABLE:
                    self._last_good[provider_id] = snapshot
                    results.append(snapshot)
                    continue
                previous = self._last_good.get(provider_id)
                if previous is not None:
                    results.append(
                        replace(previous, stale=True, error=snapshot.error)
                    )
                else:
                    results.append(snapshot)
            return tuple(results)

    def _fetch_one(self, provider_id: str, transport) -> ProviderSnapshot:
        now = datetime.now(timezone.utc)
        error: ProviderError | None
        try:
            token = self._readers[provider_id]()
        except auth.CredentialError as exc:
            error = exc.error
        except KeyError:
            error = ProviderError(
                code=AUTH_REQUIRED,
                message="no credential reader configured",
                action="this is an internal llmits error; please report it",
            )
        else:
            error = None
        if error is not None:
            return ProviderSnapshot(
                provider=provider_id,
                status=AUTH_REQUIRED,
                plan_name=None,
                fetched_at=now,
                error=error,
            )
        try:
            return FETCHERS[provider_id](token, transport, now=now)
        except Exception as exc:  # defensive: a provider bug must not kill the app
            snapshot = common.parse_error_snapshot(
                provider_id, f"internal provider error ({type(exc).__name__})", now
            )
            return snapshot

    def _run_fetch(self, provider_id: str, transport, future: Future) -> None:
        """Run one provider fetch on a daemon thread and resolve ``future``.

        ``_fetch_one`` already turns provider/credential failures into an
        error ``ProviderSnapshot`` instead of raising; this catch-all is a
        last-resort backstop so an unexpected exception still resolves the
        future (as a failure) instead of leaving ``refresh()`` hung on
        ``future.result()`` forever.
        """
        try:
            future.set_result(self._fetch_one(provider_id, transport))
        except Exception as exc:  # pragma: no cover - defensive backstop
            future.set_exception(exc)

    def close(self) -> None:
        """Abandon any in-flight provider fetches; never waits for them.

        There is no worker pool to shut down — each fetch runs on its own
        short-lived daemon thread (see ``_run_fetch``) — so this is a no-op
        by design: any fetch still running when close() is called keeps
        running in the background, but being daemon it cannot delay
        interpreter exit. See the class docstring.
        """
