import http.client
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from llmits import cli
from tests import support

FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "cli-sentinel-token"
# Invented token planted in a temporary Codex config; never a real credential.
CODEX_ZAI_TOKEN = "zai-invented-token-0123456789abcdef"


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
            "api.kimi.com": (FIXTURES / "kimi_usages_full.json").read_bytes(),
            "opencode.ai": (FIXTURES / "opencode_go_usage_full.json").read_bytes(),
        }

    def get(self, host, path, headers):
        return Response(200, self.bodies[host])


class FailTransport:
    def get(self, host, path, headers):
        return Response(401, b"denied")


class HeaderCheckingTransport:
    """Drives request headers through the real http.client header writer.

    ``putheader`` only buffers the header block (the connection is never
    opened or sent), so this reproduces exactly the production encoding
    failure for a token that cannot be a header value, with no network.
    """

    def __init__(self):
        self.calls = []

    def get(self, host, path, headers):
        self.calls.append((host, path))
        conn = http.client.HTTPConnection("unused.invalid")
        conn.putrequest("GET", path)
        for name, value in headers.items():
            conn.putheader(name, value)
        return Response(200, (FIXTURES / "claude_usage_full.json").read_bytes())


def readers(token=SENTINEL):
    return {
        "claude": lambda: token,
        "codex": lambda: token,
        "zai": lambda: token,
        "kimi": lambda: token,
        "opencode": lambda: token,
    }


def isolated_home():
    tmp = tempfile.TemporaryDirectory()
    os.environ["HOME"] = tmp.name
    for var in support.CREDENTIAL_ENV_VARS:
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
        self.assertIn("llmits 0.3.1", out)

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
        self.assertEqual(len(document["providers"]), 5)
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
        self.assertEqual(
            statuses,
            {
                "claude": "auth_required",
                "codex": "auth_required",
                "zai": "auth_required",
                "kimi": "auth_required",
                "opencode": "auth_required",
            },
        )
        actions = " ".join(p["error"]["action"] for p in document["providers"])
        self.assertIn("claude login", actions)
        self.assertIn("codex login", actions)
        self.assertIn("ZAI_API_KEY", actions)
        self.assertIn("kimi-cli", actions)
        self.assertIn("OpenCode Go", actions)

    def test_exported_codex_home_cannot_supply_a_zai_credential(self):
        """A developer's exported CODEX_HOME must not satisfy ZAI discovery."""
        original_env = dict(os.environ)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        codex_home = Path(sandbox.name) / "codex-home"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text(
            "[model_providers.ZAI]\n"
            'base_url = "https://api.z.ai/api/v1"\n'
            f'experimental_bearer_token = "{CODEX_ZAI_TOKEN}"\n'
        )
        os.chmod(config, 0o600)
        empty_home = Path(sandbox.name) / "empty-home"
        empty_home.mkdir()

        # Liveness: an environment that honors CODEX_HOME really does discover
        # the planted file, so the isolation assertions below cannot pass on an
        # inert fixture.
        with mock.patch.dict(os.environ):
            os.environ.clear()
            os.environ["HOME"] = str(empty_home)
            os.environ["CODEX_HOME"] = str(codex_home)
            self.assertEqual(cli._credential_reader_map(None, None)["zai"](), CODEX_ZAI_TOKEN)

        # A developer shell that exports CODEX_HOME is this same poison.
        os.environ["CODEX_HOME"] = str(codex_home)
        tmp = isolated_home()
        self.addCleanup(tmp.cleanup)

        class RecordingTransport:
            def __init__(self):
                self.calls = []

            def get(self, host, path, headers):
                self.calls.append((host, path, headers))
                return Response(200, (FIXTURES / "zai_quota_full.json").read_bytes())

        transport = RecordingTransport()
        code, out, _ = self.run_json(["--json"], lambda: transport, None)
        self.assertEqual(code, 1)
        self.assertEqual(
            {p["provider"]: p["status"] for p in json.loads(out)["providers"]},
            {
                "claude": "auth_required",
                "codex": "auth_required",
                "zai": "auth_required",
                "kimi": "auth_required",
                "opencode": "auth_required",
            },
        )
        self.assertEqual(transport.calls, [])
        self.assertNotIn(CODEX_ZAI_TOKEN, out)

    def test_ambiguous_kimi_environment_key_is_never_forwarded(self):
        original_env = dict(os.environ)
        tmp = isolated_home()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))
        os.environ["KIMI_API_KEY"] = SENTINEL

        class RecordingTransport:
            def __init__(self):
                self.calls = []

            def get(self, host, path, headers):
                self.calls.append((host, path, headers))
                return Response(200, (FIXTURES / "kimi_usages_full.json").read_bytes())

        transport = RecordingTransport()
        code, out, _ = self.run_json(
            ["--json", "--providers", "kimi"], lambda: transport, None
        )
        self.assertEqual(code, 1)
        self.assertEqual(transport.calls, [])
        self.assertEqual(json.loads(out)["providers"][0]["status"], "auth_required")
        self.assertNotIn(SENTINEL, out)


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
        from pathlib import Path as _Path

        original_env = dict(os.environ)
        tmp = isolated_home()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))

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


class MalformedCredentialTokenTests(unittest.TestCase):
    """A Claude access token http.client cannot send as a header value
    (embedded CR/LF, non-latin-1 text) must surface as a credential failure
    (auth_required) with sanitized output — never as a parse_error "internal
    provider error (ValueError)" and never leaking the token or file path.
    """

    def test_header_unencodable_token_fails_auth_required_not_parse_error(self):
        original_env = dict(os.environ)
        tmp = isolated_home()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))
        for label, suffix in (("embedded crlf", "\r\ninjected-header"), ("non-latin-1", "\u4e2d")):
            with self.subTest(label=label):
                creds = Path(tmp.name) / "creds.json"
                creds.write_text(
                    json.dumps({"claudeAiOauth": {"accessToken": SENTINEL + suffix}})
                )
                os.chmod(creds, 0o600)

                transport = HeaderCheckingTransport()
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(
                        ["--json", "--providers", "claude", "--claude-credentials", str(creds)],
                        transport_factory=lambda: transport,
                        credential_readers=None,
                    )
                self.assertEqual(code, 1, err.getvalue())
                self.assertEqual(transport.calls, [])  # no request is ever attempted
                document = json.loads(out.getvalue())
                provider = document["providers"][0]
                self.assertEqual(provider["status"], "auth_required")
                self.assertNotEqual(provider["status"], "parse_error")
                self.assertIn("claude login", provider["error"]["action"])
                combined = out.getvalue() + err.getvalue()
                self.assertNotIn(SENTINEL, combined)
                self.assertNotIn("injected-header", combined)
                self.assertNotIn("\u4e2d", combined)
                self.assertNotIn(str(creds), combined)


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


class DiagnoseModeTests(unittest.TestCase):
    """--diagnose: secret-free credential, endpoint, and payload checks."""

    SENTINEL_TOKEN = "diagnose-sentinel-token"
    SENTINEL_PATH = "/tmp/diagnose-sentinel-credentials.json"
    SENTINEL_ERROR = "RuntimeError(diagnose-sentinel)"

    @staticmethod
    def header():
        version = sys.version_info
        return f"llmits {cli.__version__} · Python {version.major}.{version.minor}.{version.micro} · {sys.platform}"

    def run_diagnose(self, argv, reader_map, transport_factory=OkTransport):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(
                    argv,
                    transport_factory=transport_factory,
                    credential_readers=reader_map,
                )
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_all_available_exits_zero_with_version_and_provider_results(self):
        code, out, err = self.run_diagnose(
            ["--diagnose"], readers(self.SENTINEL_TOKEN)
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            out.splitlines(),
            [
                self.header(),
                "claude: credential available · endpoint reachable · payload compatible",
                "codex: credential available · endpoint reachable · payload compatible",
                "zai: credential available · endpoint reachable · payload compatible",
                "kimi: credential available · endpoint reachable · payload compatible",
                "opencode: credential available · endpoint reachable · payload compatible",
            ],
        )

    def test_selected_provider_order_is_preserved(self):
        code, out, _ = self.run_diagnose(
            ["--diagnose", "--providers", "kimi,claude"], readers(self.SENTINEL_TOKEN)
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines()[1:],
            [
                "kimi: credential available · endpoint reachable · payload compatible",
                "claude: credential available · endpoint reachable · payload compatible",
            ],
        )

    def test_unavailable_reader_skips_the_endpoint_and_exits_one(self):
        calls = []

        def forbidden_transport():
            calls.append(True)
            raise AssertionError("transport must not be built without a credential")

        def missing():
            raise cli.auth.CredentialError(
                f"no credentials at {self.SENTINEL_PATH}", "log in with the official CLI"
            )

        code, out, err = self.run_diagnose(
            ["--diagnose", "--providers", "zai"],
            {"zai": missing},
            forbidden_transport,
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(
            out.splitlines()[1],
            "zai: credential unavailable · endpoint not checked · payload not checked",
        )
        self.assertEqual(err, "")

    def test_missing_raising_and_empty_readers_do_not_block_available_provider(self):
        def exploding():
            raise RuntimeError(self.SENTINEL_ERROR)

        reader_map = {
            "claude": lambda: self.SENTINEL_TOKEN,
            "codex": exploding,
            "zai": lambda: "",
        }
        code, out, _ = self.run_diagnose(
            ["--diagnose", "--providers", "claude,codex,zai"], reader_map
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            out.splitlines()[1:],
            [
                "claude: credential available · endpoint reachable · payload compatible",
                "codex: credential unavailable · endpoint not checked · payload not checked",
                "zai: credential unavailable · endpoint not checked · payload not checked",
            ],
        )

    def test_parse_error_distinguishes_reachable_endpoint_from_incompatible_payload(self):
        class InvalidPayloadTransport:
            def get(self, host, path, headers):
                return Response(200, b"{}")

        code, out, _ = self.run_diagnose(
            ["--diagnose", "--providers", "claude"],
            {"claude": lambda: self.SENTINEL_TOKEN},
            InvalidPayloadTransport,
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "claude: credential available · endpoint reachable · payload incompatible",
            out,
        )

    def test_transport_failure_uses_fixed_text_without_leaking_exception(self):
        from llmits.http import TransportError

        class HostileTransport:
            def get(inner_self, host, path, headers):
                raise TransportError(
                    f"{self.SENTINEL_TOKEN} {self.SENTINEL_PATH} {self.SENTINEL_ERROR}"
                )

        code, out, err = self.run_diagnose(
            ["--diagnose", "--providers", "claude"],
            {"claude": lambda: self.SENTINEL_TOKEN},
            HostileTransport,
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "claude: credential available · endpoint unreachable · payload not checked",
            out,
        )
        for stream in (out, err):
            for sentinel in (self.SENTINEL_TOKEN, self.SENTINEL_PATH, "RuntimeError"):
                self.assertNotIn(sentinel, stream)

    def test_rejected_credential_and_rate_limit_are_distinct_fixed_outcomes(self):
        for status, expected in (
            (401, "endpoint rejected credential"),
            (429, "endpoint rate limited"),
        ):
            with self.subTest(status=status):
                class StatusTransport:
                    def get(self, host, path, headers):
                        return Response(status, b"ignored")

                code, out, _ = self.run_diagnose(
                    ["--diagnose", "--providers", "claude"],
                    {"claude": lambda: self.SENTINEL_TOKEN},
                    StatusTransport,
                )
                self.assertEqual(code, 1)
                self.assertIn(
                    f"claude: credential available · {expected} · payload not checked",
                    out,
                )

    def test_no_secret_or_path_or_reader_exception_reaches_either_stream(self):
        def hostile():
            raise RuntimeError(
                f"{self.SENTINEL_TOKEN} at {self.SENTINEL_PATH}: {self.SENTINEL_ERROR}"
            )

        code, out, err = self.run_diagnose(
            ["--diagnose", "--providers", "claude"], {"claude": hostile}
        )
        self.assertEqual(code, 1)
        for stream in (out, err):
            for sentinel in (self.SENTINEL_TOKEN, self.SENTINEL_PATH, "RuntimeError"):
                self.assertNotIn(sentinel, stream)

    def test_diagnose_and_json_are_mutually_exclusive(self):
        code, out, err = self.run_diagnose(
            ["--json", "--diagnose"], readers(self.SENTINEL_TOKEN)
        )
        self.assertEqual(code, 2)
        self.assertNotIn("{", out)
        self.assertIn("not allowed with argument", err)

    def test_diagnose_works_without_a_tty(self):
        stdout_is_tty = sys.stdout.isatty
        sys.stdout.isatty = lambda: False
        try:
            code, out, _ = self.run_diagnose(
                ["--diagnose", "--providers", "claude"],
                readers(self.SENTINEL_TOKEN),
            )
        finally:
            sys.stdout.isatty = stdout_is_tty
        self.assertEqual(code, 0)
        self.assertIn("payload compatible", out)

    def test_diagnose_uses_the_production_reader_map(self):
        original_env = dict(os.environ)
        tmp = isolated_home()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(original_env))
        code, out, _ = self.run_diagnose(
            ["--diagnose", "--providers", "claude"], None
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "claude: credential unavailable · endpoint not checked · payload not checked",
            out,
        )


class OverviewModeTests(unittest.TestCase):
    """--overview: one newline-terminated ASCII line of provider=state tokens."""

    SENTINEL_TOKEN = "overview-sentinel-token"
    SENTINEL_ERROR = "RuntimeError(overview-sentinel)"

    def run_overview(self, argv, transport_factory=OkTransport, reader_map=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(
                    argv,
                    transport_factory=transport_factory,
                    credential_readers=reader_map or readers(self.SENTINEL_TOKEN),
                )
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_configured_order_one_ascii_line_exit_zero(self):
        code, out, err = self.run_overview(["--overview"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "claude=47% codex=45% zai=53% kimi=42% opencode=42%\n")
        self.assertTrue(out.isascii())
        self.assertEqual(out.count("\n"), 1)

    def test_subset_and_duplicate_providers_collapse_in_first_seen_order(self):
        code, out, _ = self.run_overview(["--overview", "--providers", "kimi,zai,zai"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "kimi=42% zai=53%\n")

    def test_urgency_order_reorders_tokens_only(self):
        code, out, _ = self.run_overview(["--overview", "--order", "urgency"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "zai=53% claude=47% codex=45% kimi=42% opencode=42%\n")

    def test_provider_failure_exits_one(self):
        code, out, _ = self.run_overview(["--overview"], transport_factory=FailTransport)
        self.assertEqual(code, 1)
        self.assertEqual(
            out,
            "claude=auth_required codex=auth_required zai=auth_required kimi=auth_required opencode=auth_required\n",
        )

    def test_single_provider_failure_exits_one(self):
        class OneFailingTransport:
            def __init__(self):
                self.ok = OkTransport()

            def get(self, host, path, headers):
                if host == "api.z.ai":
                    return Response(401, b"denied")
                return self.ok.get(host, path, headers)

        code, out, _ = self.run_overview(["--overview"], transport_factory=OneFailingTransport)
        self.assertEqual(code, 1)
        self.assertEqual(out, "claude=47% codex=45% zai=auth_required kimi=42% opencode=42%\n")

    def test_mutually_exclusive_with_json_and_diagnose(self):
        for argv in (["--overview", "--json"], ["--overview", "--diagnose"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_overview(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("not allowed with argument", err)

    def test_order_urgency_is_rejected_outside_overview(self):
        for argv in (
            ["--json", "--order", "urgency"],
            ["--diagnose", "--order", "urgency"],
            ["--providers", "claude", "--order", "urgency"],
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_overview(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--order urgency requires --overview", err)

    def test_order_configured_stays_a_json_noop_with_schema_unchanged(self):
        code, out, _ = self.run_overview(
            ["--json", "--providers", "claude", "--order", "configured"]
        )
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            set(document["providers"][0]),
            {"provider", "status", "plan_name", "fetched_at", "stale", "windows", "error"},
        )

    def test_fail_used_percent_stays_json_only(self):
        code, out, err = self.run_overview(["--overview", "--fail-used-percent", "50"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--fail-used-percent requires --json", err)

    def test_overview_works_without_a_tty(self):
        stdout_is_tty = sys.stdout.isatty
        sys.stdout.isatty = lambda: False
        try:
            code, out, err = self.run_overview(["--overview", "--providers", "claude"])
        finally:
            sys.stdout.isatty = stdout_is_tty
        self.assertEqual(code, 0)
        self.assertEqual(out, "claude=47%\n")
        self.assertEqual(err, "")

    def test_raising_transport_factory_exits_two_without_stdout(self):
        def raising_transport_factory():
            raise RuntimeError(self.SENTINEL_ERROR)

        code, out, err = self.run_overview(
            ["--overview"], transport_factory=raising_transport_factory
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertNotIn(self.SENTINEL_ERROR, err)
        self.assertIn("fatal internal error during refresh", err)

    def test_non_fixed_provider_id_fails_closed_without_stdout(self):
        """A snapshot the formatter must reject produces exit 2, no stdout.

        The real pipeline only produces fixed ids, so this injects the
        hostile snapshot at the RefreshService seam and proves the real
        formatter plus the real fatal path contain it: fixed stderr text,
        exit 2, and no hostile bytes on either stream.
        """
        from datetime import datetime, timezone
        from unittest.mock import patch

        from llmits.models import AVAILABLE, ProviderSnapshot

        hostile = ProviderSnapshot(
            provider=f"evil\nx=99% {self.SENTINEL_TOKEN}",
            status=AVAILABLE,
            plan_name=None,
            fetched_at=datetime.now(timezone.utc),
        )

        class HostileRefreshService:
            def __init__(self, provider_ids, **kwargs):
                pass

            def refresh(self):
                return (hostile,)

            def close(self):
                pass

        with patch.object(cli, "RefreshService", HostileRefreshService):
            code, out, err = self.run_overview(["--overview"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "llmits: fatal internal error formatting the overview line\n")
        for stream in (out, err):
            self.assertNotIn(self.SENTINEL_TOKEN, stream)
            self.assertNotIn("evil", stream)
        self.assertNotIn("Traceback", err)

    def test_hostile_transport_and_credential_text_never_reaches_output(self):
        from llmits.http import TransportError

        class HostileTransport:
            def __init__(inner_self):
                inner_self.bodies = OkTransport().bodies

            def get(inner_self, host, path, headers):
                if host == "api.anthropic.com":
                    raise TransportError(f"{self.SENTINEL_TOKEN} {self.SENTINEL_ERROR}")
                return Response(200, inner_self.bodies[host])

        code, out, err = self.run_overview(
            ["--overview"],
            transport_factory=HostileTransport,
            reader_map=readers(self.SENTINEL_TOKEN),
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "claude=network_error codex=45% zai=53% kimi=42% opencode=42%\n")
        for stream in (out, err):
            self.assertNotIn(self.SENTINEL_TOKEN, stream)
            self.assertNotIn(self.SENTINEL_ERROR, stream)

    def test_help_lists_overview_and_order(self):
        code, out, err = self.run_overview(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--overview", out)
        self.assertIn("--order", out)


class FailUsedPercentTests(unittest.TestCase):
    """--json --fail-used-percent N: exit 3 only on an all-success breach."""

    def run_json(self, argv, transport=lambda: OkTransport(), reader_map=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(
                    argv, transport_factory=transport, credential_readers=reader_map or readers()
                )
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_threshold_below_max_window_exits_three(self):
        # Claude's fixture tops out at 47% (the Fable window): a threshold
        # strictly below it is a clear breach; equality has its own test.
        code, out, _ = self.run_json(
            ["--json", "--providers", "claude", "--fail-used-percent", "46"]
        )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["schema_version"], 1)

    def test_threshold_not_met_exits_zero(self):
        code, out, _ = self.run_json(
            ["--json", "--providers", "claude", "--fail-used-percent", "48"]
        )
        self.assertEqual(code, 0)

    def test_comparison_is_inclusive(self):
        code, _, _ = self.run_json(
            ["--json", "--providers", "claude", "--fail-used-percent", "47"]
        )
        self.assertEqual(code, 3)  # exactly equal fires

    def test_threshold_zero_fires_when_any_window_exists(self):
        code, _, _ = self.run_json(
            ["--json", "--providers", "claude", "--fail-used-percent", "0"]
        )
        self.assertEqual(code, 3)

    def test_provider_failure_outranks_threshold_breach(self):
        code, out, _ = self.run_json(
            ["--json", "--fail-used-percent", "0"], transport=lambda: FailTransport()
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["schema_version"], 1)

    def test_without_flag_still_exits_zero(self):
        code, _, _ = self.run_json(["--json", "--providers", "claude"])
        self.assertEqual(code, 0)

    def test_requires_json(self):
        for argv in (
            ["--fail-used-percent", "47"],
            ["--providers", "claude", "--fail-used-percent", "47"],
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_json(argv)
                self.assertEqual(code, 2)
                self.assertNotIn("{", out)
                self.assertIn("--fail-used-percent requires --json", err)

    def test_out_of_range_values_exit_two_without_json(self):
        for value in ("-1", "101", "1000"):
            with self.subTest(value=value):
                code, out, err = self.run_json(
                    ["--json", "--fail-used-percent", value]
                )
                self.assertEqual(code, 2)
                self.assertNotIn("{", out)
                self.assertIn("between 0 and 100", err)

    def test_non_integer_value_exits_two(self):
        code, out, err = self.run_json(["--json", "--fail-used-percent", "half"])
        self.assertEqual(code, 2)
        self.assertNotIn("{", out)
        self.assertIn("invalid int value", err)

    def test_document_shape_is_unchanged_schema_v1(self):
        code, out, _ = self.run_json(
            ["--json", "--providers", "claude", "--fail-used-percent", "47"]
        )
        document = json.loads(out)
        self.assertEqual(set(document), {"schema_version", "generated_at", "providers"})
        self.assertEqual(set(document["providers"][0]),
                         {"provider", "status", "plan_name", "fetched_at", "stale", "windows", "error"})
        self.assertNotIn("fail_used_percent", out)

    def test_helper_compares_available_windows_only(self):
        from llmits.json_output import used_percent_at_least

        def snap(status, percents):
            return type(
                "S",
                (),
                {
                    "status": status,
                    "windows": tuple(
                        type("W", (), {"used_percent": percent})() for percent in percents
                    ),
                },
            )()

        self.assertFalse(used_percent_at_least([snap("available", ())], 0))
        self.assertTrue(used_percent_at_least([snap("available", (42,))], 42))
        self.assertFalse(used_percent_at_least([snap("available", (42,))], 43))
        self.assertFalse(used_percent_at_least([snap("auth_required", (99,))], 0))


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
