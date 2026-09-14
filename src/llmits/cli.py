"""Command-line entry point.

Exit codes: 0 success; 1 a requested provider failed; 2 invalid invocation,
non-TTY interactive run, or fatal internal error; 3 every provider
succeeded but a window met the ``--fail-used-percent`` threshold (a
provider failure keeps priority and exits 1); 130 if the TUI is
interrupted (Ctrl-C).
"""
from __future__ import annotations

import argparse
import contextlib
import locale
import sys
from collections.abc import Callable, Mapping, Sequence

from . import __version__, auth
from .json_output import (
    all_available,
    overview_line,
    to_document,
    used_percent_at_least,
)
from .models import (
    AUTH_REQUIRED,
    AVAILABLE,
    NETWORK_ERROR,
    PARSE_ERROR,
    RATE_LIMITED,
    UNAVAILABLE,
)
from .providers import PROVIDER_IDS
from .service import RefreshService

MIN_REFRESH_SECONDS = 60
DEFAULT_REFRESH_SECONDS = 300


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmits",
        description=(
            "Show Claude, Codex, Z.AI, Kimi, and OpenCode Go limits in one terminal pane."
        ),
    )
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--json",
        action="store_true",
        help="print one JSON snapshot and exit (never starts the TUI)",
    )
    output_mode.add_argument(
        "--diagnose",
        action="store_true",
        help=(
            "check credentials, fixed provider endpoints, and payload compatibility "
            "without printing secrets, then exit"
        ),
    )
    output_mode.add_argument(
        "--overview",
        action="store_true",
        help=(
            "print one newline-terminated ASCII line of provider=state tokens and exit "
            "(never starts the TUI; works without a terminal)"
        ),
    )
    parser.add_argument(
        "--order",
        choices=("configured", "urgency"),
        default="configured",
        help=(
            "with --overview: configured (default) keeps the de-duplicated provider "
            "order; urgency sorts failures first, then stale providers, then available "
            "providers with windows by highest used percent descending, then available "
            "providers without windows (ties keep the configured order)"
        ),
    )
    parser.add_argument(
        "--fail-used-percent",
        type=int,
        default=None,
        metavar="N",
        help=(
            "with --json: exit 3 when every provider succeeded and any window "
            "is used at least N percent (0-100); a provider failure still exits 1"
        ),
    )
    parser.add_argument(
        "--providers",
        default=",".join(PROVIDER_IDS),
        help=f"comma-separated subset of {','.join(PROVIDER_IDS)} (default: all)",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=DEFAULT_REFRESH_SECONDS,
        help="TUI auto-refresh interval in seconds; 0 disables; values 1-59 are rejected",
    )
    parser.add_argument(
        "--claude-credentials", default=None, help="path to the Claude credentials file"
    )
    parser.add_argument("--codex-credentials", default=None, help="path to the Codex auth.json")
    parser.add_argument("--version", action="version", version=f"llmits {__version__}")
    return parser


def parse_provider_list(raw: str) -> tuple[str, ...]:
    requested = [item.strip() for item in raw.split(",") if item.strip()]
    if not requested:
        raise ValueError("no providers requested")
    for item in requested:
        if item not in PROVIDER_IDS:
            raise ValueError(f"unknown provider {item!r}; choose from {', '.join(PROVIDER_IDS)}")
    return tuple(dict.fromkeys(requested))


def _credential_reader_map(claude_path, codex_path):
    return auth.default_credential_readers({"claude": claude_path, "codex": codex_path})


def main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[[], object] | None = None,
    credential_readers: Mapping[str, Callable[[], str]] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        provider_ids = parse_provider_list(args.providers)
    except ValueError as exc:
        parser.error(str(exc))

    if args.refresh_seconds != 0 and args.refresh_seconds < MIN_REFRESH_SECONDS:
        parser.error(
            f"--refresh-seconds must be 0 (disabled) or at least {MIN_REFRESH_SECONDS}"
        )

    if args.fail_used_percent is not None:
        if not args.json:
            parser.error("--fail-used-percent requires --json")
        if not 0 <= args.fail_used_percent <= 100:
            parser.error("--fail-used-percent must be an integer between 0 and 100")

    if args.order == "urgency" and not args.overview:
        parser.error("--order urgency requires --overview")

    readers = credential_readers or _credential_reader_map(
        args.claude_credentials, args.codex_credentials
    )

    if args.json:
        return _run_json(provider_ids, transport_factory, readers, args.fail_used_percent)

    if args.overview:
        return _run_overview(provider_ids, transport_factory, readers, args.order)

    if args.diagnose:
        return _run_diagnose(provider_ids, readers, transport_factory)

    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        print(
            "llmits: interactive TUI needs a terminal; use --json for non-interactive output",
            file=sys.stderr,
        )
        return 2

    return _run_tui(provider_ids, transport_factory, readers, args.refresh_seconds)


def _run_json(provider_ids, transport_factory, readers, fail_used_percent=None) -> int:
    service = RefreshService(
        provider_ids, transport_factory=transport_factory, credential_readers=readers
    )
    try:
        snapshots = service.refresh()
    except Exception:
        print("llmits: fatal internal error during refresh", file=sys.stderr)
        return 2
    finally:
        service.close()
    print(to_document(snapshots))
    if not all_available(snapshots):
        return 1  # a provider failure outranks the threshold result
    if fail_used_percent is not None and used_percent_at_least(snapshots, fail_used_percent):
        return 3
    return 0


def _run_overview(provider_ids, transport_factory, readers, order: str) -> int:
    """One-shot --overview mode: print the single overview line and exit.

    Mirrors ``_run_json``: same refresh path, same fatal-error handling, and
    the same exit contract minus the JSON-only threshold -- a stale retained
    snapshot still counts as available, so only a provider failure exits 1.
    The formatter itself only accepts fixed provider ids, and a formatter
    failure is a fatal internal error (exit 2, fixed stderr text).
    """
    service = RefreshService(
        provider_ids, transport_factory=transport_factory, credential_readers=readers
    )
    try:
        snapshots = service.refresh()
    except Exception:
        print("llmits: fatal internal error during refresh", file=sys.stderr)
        return 2
    finally:
        service.close()
    try:
        line = overview_line(snapshots, order)
    except Exception:
        # Fail closed with fixed local text: a snapshot the formatter must
        # reject (e.g. a non-fixed provider id) never reaches stdout, and
        # its hostile value never reaches stderr through a traceback.
        print("llmits: fatal internal error formatting the overview line", file=sys.stderr)
        return 2
    print(line)
    return 0 if all_available(snapshots) else 1


_DIAGNOSTIC_OUTCOMES: Mapping[str, tuple[str, str]] = {
    AVAILABLE: ("endpoint reachable", "payload compatible"),
    AUTH_REQUIRED: ("endpoint rejected credential", "payload not checked"),
    UNAVAILABLE: ("endpoint unavailable", "payload not checked"),
    RATE_LIMITED: ("endpoint rate limited", "payload not checked"),
    NETWORK_ERROR: ("endpoint unreachable", "payload not checked"),
    PARSE_ERROR: ("endpoint reachable", "payload incompatible"),
}


def _constant_reader(value: str) -> Callable[[], str]:
    """Return an in-memory reader so diagnostics resolve each credential once."""

    def read() -> str:
        return value

    return read


def _run_diagnose(provider_ids, readers, transport_factory=None) -> int:
    """Secret-safe credential, endpoint, and payload compatibility checks.

    Credentials are resolved once through the production readers, retained
    only in memory for this invocation, and passed through the ordinary
    fixed-host refresh path. Every output phrase is local and fixed: tokens,
    paths, source names, provider bodies, and exception text are never
    rendered. A missing credential skips that provider's endpoint entirely.
    """
    resolved: dict[str, Callable[[], str]] = {}
    credential_available: dict[str, bool] = {}
    for provider_id in provider_ids:
        reader = readers.get(provider_id)
        try:
            token = reader() if reader is not None else ""
        except Exception:
            token = ""
        available = isinstance(token, str) and bool(token)
        credential_available[provider_id] = available
        if available:
            resolved[provider_id] = _constant_reader(token)

    snapshots = {}
    if resolved:
        service = RefreshService(
            tuple(resolved),
            transport_factory=transport_factory,
            credential_readers=resolved,
        )
        try:
            snapshots = {snapshot.provider: snapshot for snapshot in service.refresh()}
        except Exception:
            # A transport-construction or orchestration failure is reported
            # with fixed local text for every affected provider.
            snapshots = {}
        finally:
            service.close()

    version = sys.version_info
    print(
        f"llmits {__version__} · Python {version.major}.{version.minor}.{version.micro} "
        f"· {sys.platform}"
    )
    every_usable = True
    for provider_id in provider_ids:
        if not credential_available[provider_id]:
            credential = "credential unavailable"
            endpoint, payload = "endpoint not checked", "payload not checked"
            every_usable = False
        else:
            credential = "credential available"
            snapshot = snapshots.get(provider_id)
            if snapshot is None:
                endpoint, payload = "endpoint check failed", "payload not checked"
                every_usable = False
            else:
                endpoint, payload = _DIAGNOSTIC_OUTCOMES.get(
                    snapshot.status, ("endpoint check failed", "payload not checked")
                )
                if snapshot.status != AVAILABLE:
                    every_usable = False
        print(f"{provider_id}: {credential} · {endpoint} · {payload}")
    return 0 if every_usable else 1


def _run_tui(provider_ids, transport_factory, readers, refresh_seconds: int) -> int:
    import curses

    from .tui import run_tui

    # Best-effort: on a hostile locale, wide-character glyphs just fall
    # back to ASCII instead of aborting startup.
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_ALL, "")

    service = RefreshService(
        provider_ids, transport_factory=transport_factory, credential_readers=readers
    )
    try:
        return curses.wrapper(run_tui, service, refresh_seconds)
    except curses.error:
        print("llmits: could not initialize the terminal UI", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("llmits: fatal internal error in the terminal UI", file=sys.stderr)
        return 2
    finally:
        service.close()


def run(argv: Sequence[str] | None = None) -> None:
    sys.exit(main(argv))
