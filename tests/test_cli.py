import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from llmits import cli

FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "cli-sentinel-token"


class Response:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}


class OkTransport:
    def __init__(self):
        self.bodies = {
            "api.anthropic.com": (FIXTURES / "claude_usage_full.json").read_bytes(),
            "chatgpt.com": (FIXTURES / "codex_usage_full.json").read_bytes(),
            "api.z.ai": (FIXTURES / "zai_quota_full.json").read_bytes(),
        }

    def get(self, host, path, headers):
        return Response(200, self.bodies[host])


class FailTransport:
    def get(self, host, path, headers):
        return Response(401, b"denied")


def readers(token=SENTINEL):
    return {
        "claude": lambda: token,
        "codex": lambda: token,
        "zai": lambda: token,
    }


def isolated_home():
    tmp = tempfile.TemporaryDirectory()
    os.environ["HOME"] = tmp.name
    for var in (
        "LLMITS_CLAUDE_CREDENTIALS",
        "LLMITS_CODEX_CREDENTIALS",
        "CLAUDE_CONFIG_DIR",
        "ZAI_API_KEY",
        "ZHIPU_API_KEY",
    ):
        os.environ.pop(var, None)
    return tmp


class ArgValidationTests(unittest.TestCase):
    def run_cli(self, argv, **kwargs):
        out, err = StringIO(), StringIO()
        code = None
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(argv, **kwargs)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_help_exits_zero(self):
        code, out, err = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--json", out)

    def test_version_prints_version(self):
        code, out, err = self.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("llmits 0.1.0", out)

    def test_unknown_provider_exits_two(self):
        code, out, err = self.run_cli(["--json", "--providers", "claude,grok"])
        self.assertEqual(code, 2)
        self.assertIn("unknown provider", err)

    def test_empty_provider_list_exits_two(self):
        code, out, err = self.run_cli(["--json", "--providers", " , "])
        self.assertEqual(code, 2)
        self.assertIn("no providers requested", err)

    def test_refresh_seconds_below_sixty_rejected(self):
        for bad in ("30", "1", "59"):
            code, _, err = self.run_cli(["--json", "--refresh-seconds", bad])
            self.assertEqual(code, 2, bad)
            self.assertIn("--refresh-seconds must be 0 (disabled) or at least 60", err)

    def test_refresh_seconds_zero_and_valid_accepted_for_json(self):
        for good in ("0", "60", "3600"):
            code, _, _ = self.run_cli(
                ["--json", "--refresh-seconds", good],
                transport_factory=OkTransport,
                credential_readers=readers(),
            )
            self.assertEqual(code, 0, good)


class JsonModeTests(unittest.TestCase):
    def run_json(self, argv, transport, reader_map):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv, transport_factory=transport, credential_readers=reader_map)
        return code, out.getvalue(), err.getvalue()

    def test_all_available_exits_zero_with_single_json_document(self):
        code, out, err = self.run_json(["--json"], lambda: OkTransport(), readers())
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(len(out.strip().splitlines()) > 1, True)
        self.assertEqual(out.count("\n}\n"), 1)  # exactly one document
        self.assertEqual(len(document["providers"]), 3)
        self.assertNotIn(SENTINEL, out)

    def test_provider_subset_orders_output(self):
        code, out, _ = self.run_json(
            ["--json", "--providers", "zai,claude"], lambda: OkTransport(), readers()
        )
        document = json.loads(out)
        self.assertEqual([p["provider"] for p in document["providers"]], ["zai", "claude"])

    def test_partial_failure_exits_one(self):
        code, out, _ = self.run_json(
            ["--json", "--providers", "claude"], lambda: FailTransport(), readers()
        )
        self.assertEqual(code, 1)
        document = json.loads(out)
        self.assertEqual(document["providers"][0]["status"], "auth_required")

    def test_missing_credentials_exit_one_with_guidance(self):
        original_env = dict(os.environ)
        tmp = isolated_home()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))
        code, out, _ = self.run_json(["--json"], lambda: OkTransport(), None)
        self.assertEqual(code, 1)
        document = json.loads(out)
        statuses = {p["provider"]: p["status"] for p in document["providers"]}
        self.assertEqual(statuses, {"claude": "auth_required", "codex": "auth_required", "zai": "auth_required"})
        actions = " ".join(p["error"]["action"] for p in document["providers"])
        self.assertIn("claude login", actions)
        self.assertIn("codex login", actions)
        self.assertIn("ZAI_API_KEY", actions)


class TuiModeTests(unittest.TestCase):
    def test_non_tty_interactive_run_advises_json(self):
        out, err = StringIO(), StringIO()
        stdout_is_tty = sys.stdout.isatty
        stdin_is_tty = sys.stdin.isatty
        sys.stdout.isatty = lambda: False
        sys.stdin.isatty = lambda: False
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--providers", "claude"])
        finally:
            sys.stdout.isatty = stdout_is_tty
            sys.stdin.isatty = stdin_is_tty
        self.assertEqual(code, 2)
        self.assertIn("--json", err.getvalue())

    def test_fatal_internal_error_in_tui_exits_two_without_traceback(self):
        import curses
        from unittest.mock import patch

        # redirect_stdout swaps sys.stdout for `out`, so the tty stand-in has
        # to live on `out` itself; sys.stdin is untouched by the redirect
        # context managers, so it can be patched at module scope as usual.
        out, err = StringIO(), StringIO()
        out.isatty = lambda: True
        stdin_is_tty = sys.stdin.isatty
        sys.stdin.isatty = lambda: True
        try:
            with patch.object(curses, "wrapper", side_effect=RuntimeError("boom")):
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(
                        ["--providers", "claude"],
                        transport_factory=lambda: OkTransport(),
                        credential_readers=readers(),
                    )
        finally:
            sys.stdin.isatty = stdin_is_tty
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("boom", err.getvalue())
        self.assertIn("fatal internal error", err.getvalue())


class SymlinkCredentialTests(unittest.TestCase):
    def test_symlink_credentials_yield_auth_required_json_with_exit_one(self):
        import os as _os
        import tempfile as _tempfile
        from pathlib import Path as _Path

        original_env = dict(os.environ)
        tmp = _tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))
        os.environ["HOME"] = tmp.name
        for var in (
            "LLMITS_CLAUDE_CREDENTIALS",
            "LLMITS_CODEX_CREDENTIALS",
            "CLAUDE_CONFIG_DIR",
            "ZAI_API_KEY",
            "ZHIPU_API_KEY",
        ):
            os.environ.pop(var, None)

        real = _Path(tmp.name) / "real.json"
        real.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
        _os.chmod(real, 0o600)
        link = _Path(tmp.name) / "link.json"
        _os.symlink(real, link)

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--json", "--claude-credentials", str(link)],
                transport_factory=OkTransport,
                credential_readers=None,
            )
        self.assertEqual(code, 1, err.getvalue())
        document = json.loads(out.getvalue())
        by_provider = {p["provider"]: p for p in document["providers"]}
        self.assertEqual(by_provider["claude"]["status"], "auth_required")
        self.assertIn("symlink", by_provider["claude"]["error"]["action"])
        self.assertEqual(by_provider["codex"]["status"], "auth_required")
        self.assertEqual(by_provider["zai"]["status"], "auth_required")
        self.assertNotIn(str(link), out.getvalue())


class FatalInternalErrorTests(unittest.TestCase):
    """cli._run_json's `except Exception` fatal fallback (documented exit 2).

    RefreshService.refresh() calls the transport factory outside any
    try/except of its own, so a raising factory is a realistic production
    trigger for this path -- not just a contrived mock.
    """

    def test_raising_transport_factory_exits_two_without_traceback(self):
        def raising_transport_factory():
            raise RuntimeError("boom")

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--json"],
                transport_factory=raising_transport_factory,
                credential_readers=readers(),
            )
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("boom", err.getvalue())
        self.assertIn("fatal internal error during refresh", err.getvalue())


class TuiWrapperTests(unittest.TestCase):
    """cli._run_tui: curses.wrapper's return/raise mapped to the documented

    exit codes, with RefreshService.close() proven to run in every case via
    the module-scope finally: block.
    """

    def _run_tui_main(self, **wrapper_kwargs):
        import curses
        from unittest.mock import patch

        out, err = StringIO(), StringIO()
        out.isatty = lambda: True
        stdin_is_tty = sys.stdin.isatty
        sys.stdin.isatty = lambda: True
        try:
            with patch.object(cli.RefreshService, "close", autospec=True) as mock_close:
                with patch.object(curses, "wrapper", **wrapper_kwargs):
                    with redirect_stdout(out), redirect_stderr(err):
                        code = cli.main(
                            ["--providers", "claude"],
                            transport_factory=lambda: OkTransport(),
                            credential_readers=readers(),
                        )
        finally:
            sys.stdin.isatty = stdin_is_tty
        return code, out.getvalue(), err.getvalue(), mock_close

    def test_normal_return_propagates_and_closes_service(self):
        code, out, err, mock_close = self._run_tui_main(return_value=0)
        self.assertEqual(code, 0)
        mock_close.assert_called_once()

    def test_curses_error_exits_two_and_closes_service(self):
        import curses

        code, out, err, mock_close = self._run_tui_main(side_effect=curses.error("boom"))
        self.assertEqual(code, 2)
        self.assertIn("could not initialize the terminal UI", err)
        mock_close.assert_called_once()

    def test_keyboard_interrupt_exits_130_and_closes_service(self):
        code, out, err, mock_close = self._run_tui_main(side_effect=KeyboardInterrupt())
        self.assertEqual(code, 130)
        mock_close.assert_called_once()


class ExplicitCredentialsFlagTests(unittest.TestCase):
    """--claude-credentials / --codex-credentials through the real reader map.

    Every other CLI test injects a fake credential_readers map; these point
    the flags at a real temp file and go through cli._credential_reader_map
    (credential_readers=None) so a swapped explicit_paths.get or broken
    late-binding capture would show up as a failure here.
    """

    def _isolated_env(self):
        original_env = dict(os.environ)
        tmp = isolated_home()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))
        return tmp

    def test_claude_credentials_flag_resolves_real_file_to_exit_zero(self):
        tmp = self._isolated_env()
        path = Path(tmp.name) / "claude-creds.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        os.chmod(path, 0o600)

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--json", "--providers", "claude", "--claude-credentials", str(path)],
                transport_factory=OkTransport,
                credential_readers=None,
            )
        self.assertEqual(code, 0, err.getvalue())
        document = json.loads(out.getvalue())
        self.assertEqual(document["providers"][0]["status"], "available")

    def test_codex_credentials_flag_resolves_real_file_to_exit_zero(self):
        tmp = self._isolated_env()
        path = Path(tmp.name) / "codex-creds.json"
        path.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
        os.chmod(path, 0o600)

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--json", "--providers", "codex", "--codex-credentials", str(path)],
                transport_factory=OkTransport,
                credential_readers=None,
            )
        self.assertEqual(code, 0, err.getvalue())
        document = json.loads(out.getvalue())
        self.assertEqual(document["providers"][0]["status"], "available")


class RunTests(unittest.TestCase):
    """cli.run(argv), the entry point both __main__.py and the zipapp use."""

    def test_run_exits_with_mains_return_code(self):
        from unittest.mock import patch

        with patch.object(cli, "main", return_value=7) as mock_main:
            with self.assertRaises(SystemExit) as ctx:
                cli.run(["--json"])
        self.assertEqual(ctx.exception.code, 7)
        mock_main.assert_called_once_with(["--json"])

    def test_run_forwards_argv_none_default(self):
        from unittest.mock import patch

        with patch.object(cli, "main", return_value=0) as mock_main:
            with self.assertRaises(SystemExit) as ctx:
                cli.run()
        self.assertEqual(ctx.exception.code, 0)
        mock_main.assert_called_once_with(None)


class MainModuleTests(unittest.TestCase):
    """src/llmits/__main__.py, exercised the same way `python -m llmits` is."""

    def test_module_invocation_runs_version_and_exits_zero(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "llmits", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent / "src"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("llmits", result.stdout)


class ParseProviderListTests(unittest.TestCase):
    """cli.parse_provider_list called directly, including its de-dup branch."""

    def test_duplicate_ids_collapse_to_one_in_first_seen_order(self):
        self.assertEqual(cli.parse_provider_list("claude,claude"), ("claude",))

    def test_duplicates_interleaved_with_other_ids_collapse_in_first_seen_order(self):
        self.assertEqual(cli.parse_provider_list("claude,zai,claude"), ("claude", "zai"))

    def test_distinct_ids_preserve_requested_order(self):
        self.assertEqual(cli.parse_provider_list("zai,claude"), ("zai", "claude"))

    def test_empty_list_raises_value_error_with_message(self):
        with self.assertRaises(ValueError) as ctx:
            cli.parse_provider_list(" , ")
        self.assertIn("no providers requested", str(ctx.exception))

    def test_unknown_provider_raises_value_error_with_message(self):
        with self.assertRaises(ValueError) as ctx:
            cli.parse_provider_list("claude,grok")
        self.assertIn("unknown provider", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
