"""Command-line entry point.

Exit codes: 0 success; 1 a requested provider failed; 2 invalid invocation,
non-TTY interactive run, or fatal internal error; 130 if the TUI is
interrupted (Ctrl-C).
"""
from __future__ import annotations

import argparse
import contextlib
import locale
import sys
from collections.abc import Callable, Mapping, Sequence

from . import __version__, auth
from .json_output import all_available, to_document
from .providers import PROVIDER_IDS
from .service import RefreshService

MIN_REFRESH_SECONDS = 60
DEFAULT_REFRESH_SECONDS = 300


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmits",
        description="Show Claude, Codex, and Z.AI subscription limits in one terminal pane.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON snapshot and exit (never starts the TUI)",
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

    readers = credential_readers or _credential_reader_map(
        args.claude_credentials, args.codex_credentials
    )

    if args.json:
        return _run_json(provider_ids, transport_factory, readers)

    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        print(
            "llmits: interactive TUI needs a terminal; use --json for non-interactive output",
            file=sys.stderr,
        )
        return 2

    return _run_tui(provider_ids, transport_factory, readers, args.refresh_seconds)


def _run_json(provider_ids, transport_factory, readers) -> int:
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
    return 0 if all_available(snapshots) else 1


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
