import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llmits import auth
from llmits.models import AUTH_REQUIRED
from llmits.providers import FETCHERS, PROVIDER_IDS

SENTINEL = "sk-sentinel-token-0123456789abcdef"
# Planted in files that discovery must skip; returning it means a guard failed.
DECOY = "decoy-key-that-must-never-be-returned"

# Every variable any credential source consults, cleared for each test so the
# developer's real environment can neither satisfy nor break discovery.
ISOLATED_ENV_VARS = (
    "LLMITS_CLAUDE_CREDENTIALS",
    "LLMITS_CODEX_CREDENTIALS",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "ZAI_API_KEY",
    "ZHIPU_API_KEY",
    "KIMI_API_KEY",
    "KIMI_CODE_HOME",
    "FIRST_TEST_KEY",
    "SECOND_TEST_KEY",
)


def write_file(tmpdir: Path, name: str, text: str, mode: int = 0o600) -> Path:
    """Write raw text at a relative path, replacing any earlier file there."""
    path = tmpdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_text(text)
    os.chmod(path, mode)
    return path


def make_creds(tmpdir: Path, name: str, payload: dict, mode: int = 0o600) -> Path:
    return write_file(tmpdir, name, json.dumps(payload), mode)


class IsolatedHomeMixin:
    """Give every test a private temporary HOME and a restored os.environ."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._env = dict(os.environ)
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(self._env))
        for var in ISOLATED_ENV_VARS:
            os.environ.pop(var, None)
        os.environ["HOME"] = str(self.tmp)


class GenericDiscoveryTests(IsolatedHomeMixin, unittest.TestCase):
    def test_ordered_environment_sources_return_first_nonblank(self):
        os.environ["FIRST_TEST_KEY"] = "   "
        os.environ["SECOND_TEST_KEY"] = SENTINEL
        spec = auth.CredentialSpec(
            provider="example",
            sources=(
                auth.EnvironmentSource("example", "FIRST_TEST_KEY"),
                auth.EnvironmentSource("example", "SECOND_TEST_KEY"),
            ),
            missing_message="example credential missing",
            missing_action="set an example credential",
        )
        self.assertEqual(auth.discover_credential(spec), SENTINEL)

    def test_structured_file_source_extracts_provider_bound_key(self):
        path = make_creds(self.tmp, "tool.json", {"provider": {"key": SENTINEL}})
        source = auth.StructuredFileSource(
            audience="example",
            path=lambda: path,
            what="example tool credentials",
            loader=auth.secure_read_json,
            extract=lambda data: (data.get("provider") or {}).get("key"),
        )
        spec = auth.CredentialSpec(
            provider="example",
            sources=(source,),
            missing_message="example credential missing",
            missing_action="set an example credential",
        )
        self.assertEqual(auth.discover_credential(spec), SENTINEL)

    def test_provider_entrypoint_dispatches_through_registered_reader(self):
        os.environ["ZAI_API_KEY"] = SENTINEL
        self.assertEqual(auth.read_provider_credential("zai"), SENTINEL)

    def test_unregistered_provider_fails_closed_without_leaking(self):
        os.environ["ZAI_API_KEY"] = SENTINEL
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_provider_credential("not-a-provider")
        error = ctx.exception.error
        self.assertEqual(error.code, AUTH_REQUIRED)
        self.assertIn("no credential discovery specification", error.message)
        self.assertIn("internal llmits provider configuration error", error.action)
        self.assertNotIn(SENTINEL, error.message + error.action)

    def test_registered_credential_providers_match_supported_providers(self):
        self.assertEqual(auth.credential_provider_ids(), PROVIDER_IDS)
        self.assertEqual(auth.credential_provider_ids(), tuple(FETCHERS))

    def test_rejects_source_registered_for_another_provider(self):
        os.environ["FIRST_TEST_KEY"] = SENTINEL
        spec = auth.CredentialSpec(
            provider="example",
            sources=(auth.EnvironmentSource("other", "FIRST_TEST_KEY"),),
            missing_message="example credential missing",
            missing_action="set an example credential",
        )
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.discover_credential(spec)
        self.assertIn("wrong provider audience", ctx.exception.error.message)
        self.assertNotIn(SENTINEL, ctx.exception.error.message + ctx.exception.error.action)

    def test_optional_insecure_file_is_skipped_for_safe_fallback(self):
        path = make_creds(self.tmp, "tool.json", {"provider": {"key": DECOY}}, mode=0o644)
        os.environ["SECOND_TEST_KEY"] = SENTINEL
        spec = auth.CredentialSpec(
            provider="example",
            sources=(
                auth.StructuredFileSource(
                    audience="example",
                    path=lambda: path,
                    what="optional tool credentials",
                    loader=auth.secure_read_json,
                    extract=lambda data: (data.get("provider") or {}).get("key"),
                    optional=True,
                ),
                auth.EnvironmentSource("example", "SECOND_TEST_KEY"),
            ),
            missing_message="example credential missing",
            missing_action="set an example credential",
        )
        self.assertEqual(auth.discover_credential(spec), SENTINEL)


class SecureReadTests(IsolatedHomeMixin, unittest.TestCase):
    def creds(self, payload, mode=0o600, name="creds.json"):
        return make_creds(self.tmp, name, payload, mode)

    def test_accepts_owned_private_regular_file(self):
        path = self.creds({"claudeAiOauth": {"accessToken": SENTINEL}})
        data = auth.secure_read_json(path, "Claude credentials file")
        self.assertEqual(data["claudeAiOauth"]["accessToken"], SENTINEL)

    def test_accepts_file_of_exactly_max_credential_bytes(self):
        prefix, suffix = '{"claudeAiOauth": {"accessToken": "', '"}}'
        padding = auth.MAX_CREDENTIAL_BYTES - len(prefix) - len(suffix) - len(SENTINEL)
        token = SENTINEL + "x" * padding
        path = write_file(self.tmp, "max.json", prefix + token + suffix)
        self.assertEqual(path.stat().st_size, auth.MAX_CREDENTIAL_BYTES)
        data = auth.secure_read_json(path, "Claude credentials file")
        self.assertEqual(data["claudeAiOauth"]["accessToken"], token)

    def test_rejects_group_readable(self):
        path = self.creds({"x": 1}, mode=0o640)
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_json(path, "Claude credentials file")
        self.assertIn("chmod 600", ctx.exception.error.action)

    def test_rejects_other_readable(self):
        path = self.creds({"x": 1}, mode=0o604)
        with self.assertRaises(auth.CredentialError):
            auth.secure_read_json(path, "Claude credentials file")

    def test_rejects_symlink_with_credential_error_not_oserror(self):
        real = self.creds({"x": 1})
        link = self.tmp / "link.json"
        os.symlink(real, link)
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_json(link, "Claude credentials file")
        self.assertIn("could not be opened securely", ctx.exception.error.message)
        self.assertIn("symlink", ctx.exception.error.action)
        self.assertNotIn(str(link), ctx.exception.error.message + ctx.exception.error.action)
        self.assertNotIn(str(real), ctx.exception.error.message + ctx.exception.error.action)

    def test_rejects_directory(self):
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_json(self.tmp, "Claude credentials file")
        self.assertIn("regular file", ctx.exception.error.message)

    def test_rejects_fifo(self):
        fifo = self.tmp / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(auth.CredentialError):
            auth.secure_read_json(fifo, "Claude credentials file")

    def test_rejects_oversized_file(self):
        path = write_file(self.tmp, "big.json", " " * (auth.MAX_CREDENTIAL_BYTES + 1))
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_json(path, "Claude credentials file")
        self.assertIn("large", ctx.exception.error.message)

    def test_rejects_malformed_json(self):
        path = write_file(self.tmp, "bad.json", "not json{")
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_json(path, "Claude credentials file")
        self.assertIn("not valid JSON", ctx.exception.error.message)

    def test_rejects_non_object_json(self):
        path = write_file(self.tmp, "list.json", "[1,2]")
        with self.assertRaises(auth.CredentialError):
            auth.secure_read_json(path, "Claude credentials file")

    def test_wrong_owner_rejected_by_stat_check(self):
        st = os.stat_result((0o100600, 1, 1, 1, 999_999, 100, 10, 0, 0, 0))
        with self.assertRaises(auth.CredentialError) as ctx:
            auth._validate_stat(st, "Claude credentials file")
        self.assertIn("ownership", ctx.exception.error.action)

    def test_fstat_failure_is_credential_error_and_closes_descriptor(self):
        path = self.creds({"claudeAiOauth": {"accessToken": SENTINEL}})
        with (
            mock.patch.object(os, "close", wraps=os.close) as close,
            mock.patch.object(os, "fstat", side_effect=OSError(116, "Stale file handle")),
        ):
            with self.assertRaises(auth.CredentialError) as ctx:
                auth.secure_read_json(path, "Claude credentials file")
        text = ctx.exception.error.message + ctx.exception.error.action
        self.assertIn("could not be inspected", ctx.exception.error.message)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(str(path), text)
        self.assertEqual(close.call_count, 1)

    def test_read_failure_mid_stream_is_credential_error_and_closes_descriptor(self):
        path = self.creds({"claudeAiOauth": {"accessToken": SENTINEL}})
        with (
            mock.patch.object(os, "close", wraps=os.close) as close,
            mock.patch.object(os, "read", side_effect=[b"{", OSError(5, "Input/output error")]),
        ):
            with self.assertRaises(auth.CredentialError) as ctx:
                auth.secure_read_json(path, "Claude credentials file")
        text = ctx.exception.error.message + ctx.exception.error.action
        self.assertIn("could not be read", ctx.exception.error.message)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(str(path), text)
        self.assertEqual(close.call_count, 1)


class SecureReadTomlTests(IsolatedHomeMixin, unittest.TestCase):
    def toml(self, text, mode=0o600, name="config.toml"):
        return write_file(self.tmp, name, text, mode)

    def test_accepts_owned_private_regular_file(self):
        path = self.toml(f'[model_providers.ZAI]\nexperimental_bearer_token = "{SENTINEL}"\n')
        data = auth.secure_read_toml(path, "Codex config file")
        self.assertEqual(data["model_providers"]["ZAI"]["experimental_bearer_token"], SENTINEL)

    def test_rejects_group_readable(self):
        path = self.toml('key = "value"\n', mode=0o640)
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_toml(path, "Codex config file")
        self.assertIn("chmod 600", ctx.exception.error.action)

    def test_rejects_malformed_toml(self):
        path = self.toml(f'[model_providers.ZAI\nexperimental_bearer_token = "{SENTINEL}"\n')
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_toml(path, "Codex config file")
        text = ctx.exception.error.message + ctx.exception.error.action
        self.assertIn("not valid TOML", ctx.exception.error.message)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(str(path), text)

    def test_rejects_undecodable_bytes_as_invalid_toml(self):
        path = self.tmp / "binary.toml"
        path.write_bytes(b"\xff\xfe key = 1\n")
        os.chmod(path, 0o600)
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.secure_read_toml(path, "Codex config file")
        self.assertIn("not valid TOML", ctx.exception.error.message)

    def test_rejects_non_table_document(self):
        # tomllib only ever yields a table for valid input, so the guard against
        # a non-dict payload is reachable only through a substituted parser.
        path = self.toml('key = "value"\n')
        with mock.patch.object(auth.tomllib, "loads", return_value=["not", "a", "table"]):
            with self.assertRaises(auth.CredentialError) as ctx:
                auth.secure_read_toml(path, "Codex config file")
        self.assertIn("unexpected format", ctx.exception.error.message)


class ClaudeCredentialTests(IsolatedHomeMixin, unittest.TestCase):
    def payload(self):
        return {"claudeAiOauth": {"accessToken": SENTINEL, "refreshToken": "r"}}

    def test_read_token_from_default_home_location(self):
        make_creds(self.tmp, ".claude/.credentials.json", self.payload())
        self.assertEqual(auth.read_claude_token(), SENTINEL)

    def test_cli_argument_takes_precedence(self):
        cli_file = make_creds(self.tmp, "cli.json", self.payload())
        make_creds(
            self.tmp, ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "other"}}
        )
        self.assertEqual(auth.read_claude_token(str(cli_file)), SENTINEL)

    def test_env_override_beats_config_dir_and_home(self):
        env_file = make_creds(self.tmp, "env.json", self.payload())
        os.environ["LLMITS_CLAUDE_CREDENTIALS"] = str(env_file)
        config_dir = self.tmp / "cfg"
        make_creds(config_dir, ".credentials.json", {"claudeAiOauth": {"accessToken": "other"}})
        os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        self.assertEqual(auth.read_claude_token(), SENTINEL)

    def test_config_dir_beats_home(self):
        config_dir = self.tmp / "cfg"
        make_creds(config_dir, ".credentials.json", self.payload())
        make_creds(
            self.tmp, ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "other"}}
        )
        os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        self.assertEqual(auth.read_claude_token(), SENTINEL)

    def test_missing_everywhere_is_actionable_and_secret_free(self):
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_claude_token()
        message = ctx.exception.error.message + ctx.exception.error.action
        self.assertNotIn(SENTINEL, message)
        self.assertNotIn(str(self.tmp), message)
        self.assertIn("claude login", ctx.exception.error.action)

    def test_existing_file_without_token_is_actionable(self):
        file_with_junk = make_creds(self.tmp, "junk.json", {"something": "else"})
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_claude_token(str(file_with_junk))
        error = ctx.exception.error
        self.assertIn("Claude credentials file does not contain an access token", error.message)
        self.assertIn("claude login", error.action)
        self.assertIn("--claude-credentials", error.action)
        self.assertNotIn(SENTINEL, error.message + error.action)
        self.assertNotIn(str(file_with_junk), error.message + error.action)

    def test_missing_and_tokenless_errors_share_one_relogin_action(self):
        with self.assertRaises(auth.CredentialError) as missing:
            auth.read_claude_token()
        junk = make_creds(self.tmp, "junk.json", {"something": "else"})
        with self.assertRaises(auth.CredentialError) as tokenless:
            auth.read_claude_token(str(junk))
        self.assertEqual(missing.exception.error.action, tokenless.exception.error.action)


class CodexCredentialTests(IsolatedHomeMixin, unittest.TestCase):
    def payload(self):
        return {"tokens": {"access_token": SENTINEL, "refresh_token": "r"}}

    def test_read_from_default_location(self):
        make_creds(self.tmp, ".codex/auth.json", self.payload())
        self.assertEqual(auth.read_codex_token(), SENTINEL)

    def test_env_override(self):
        env_file = make_creds(self.tmp, "env.json", self.payload())
        os.environ["LLMITS_CODEX_CREDENTIALS"] = str(env_file)
        self.assertEqual(auth.read_codex_token(), SENTINEL)

    def test_cli_argument_wins(self):
        cli_file = make_creds(self.tmp, "cli.json", self.payload())
        env_file = make_creds(self.tmp, "env.json", {"tokens": {"access_token": "other"}})
        os.environ["LLMITS_CODEX_CREDENTIALS"] = str(env_file)
        self.assertEqual(auth.read_codex_token(str(cli_file)), SENTINEL)

    def test_missing_everywhere_is_actionable(self):
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_codex_token()
        self.assertIn("Codex auth file", ctx.exception.error.message)
        self.assertIn("codex login", ctx.exception.error.action)

    def test_existing_file_without_token_is_actionable(self):
        file_with_junk = make_creds(self.tmp, "junk.json", {"tokens": {"id_token": "x"}})
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_codex_token(str(file_with_junk))
        error = ctx.exception.error
        self.assertIn("Codex auth file does not contain an access token", error.message)
        self.assertIn("codex login", error.action)
        self.assertIn("--codex-credentials", error.action)
        self.assertNotIn(str(file_with_junk), error.message + error.action)


class ZaiCredentialTests(IsolatedHomeMixin, unittest.TestCase):
    @staticmethod
    def bound_claude_settings(token: str) -> dict:
        return {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                "ANTHROPIC_AUTH_TOKEN": token,
            }
        }

    @staticmethod
    def bound_codex_config(token: str) -> str:
        return (
            '[model_providers.ZAI]\n'
            'name = "ZAI"\n'
            'base_url = "https://api.z.ai/api/v1"\n'
            f'experimental_bearer_token = "{token}"\n'
        )

    @staticmethod
    def skipped_variants(decoy_text: str, malformed_text: str, foreign_text: str):
        """(label, text, mode) files an optional source must skip, never raise on."""
        variants = [
            ("insecure permissions", decoy_text, 0o644),
            ("malformed", malformed_text, 0o600),
            ("wrong audience", foreign_text, 0o600),
        ]
        if os.geteuid() != 0:
            # Mode 000 only denies access to non-root users; root would read it.
            variants.append(("unreadable", decoy_text, 0o000))
        return variants

    def test_prefers_zai_api_key(self):
        os.environ["ZAI_API_KEY"] = SENTINEL
        os.environ["ZHIPU_API_KEY"] = "zhipu-key"
        self.assertEqual(auth.read_zai_key(), SENTINEL)

    def test_falls_back_to_zhipu(self):
        os.environ["ZHIPU_API_KEY"] = SENTINEL
        self.assertEqual(auth.read_zai_key(), SENTINEL)

    def test_blank_values_are_skipped(self):
        os.environ["ZAI_API_KEY"] = "   "
        os.environ["ZHIPU_API_KEY"] = SENTINEL
        self.assertEqual(auth.read_zai_key(), SENTINEL)

    def test_discovers_provider_scoped_pi_auth_key(self):
        make_creds(self.tmp, ".pi/agent/auth.json", {"zai": {"type": "api_key", "key": SENTINEL}})
        self.assertEqual(auth.read_zai_key(), SENTINEL)

    def test_discovers_zai_key_from_bound_claude_settings(self):
        make_creds(self.tmp, ".claude/settings.json", self.bound_claude_settings(SENTINEL))
        self.assertEqual(auth.read_zai_key(), SENTINEL)

    def test_discovers_zai_key_from_bound_codex_config(self):
        write_file(self.tmp, ".codex/config.toml", self.bound_codex_config(SENTINEL))
        self.assertEqual(auth.read_zai_key(), SENTINEL)

    def test_refuses_tool_keys_not_bound_to_api_zai(self):
        make_creds(
            self.tmp,
            ".claude/settings.json",
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://proxy.example/api/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": SENTINEL,
                }
            },
        )
        write_file(
            self.tmp,
            ".codex/config.toml",
            '[model_providers.ZAI]\n'
            'base_url = "https://proxy.example/api/v1"\n'
            f'experimental_bearer_token = "{SENTINEL}"\n',
        )
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_zai_key()
        text = ctx.exception.error.message + ctx.exception.error.action
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("proxy.example", text)

    def test_refuses_pi_command_and_environment_expressions(self):
        for expression in ("!printf secret", "$OTHER_PROVIDER_KEY"):
            make_creds(
                self.tmp,
                ".pi/agent/auth.json",
                {"zai": {"type": "api_key", "key": expression}},
            )
            with self.assertRaises(auth.CredentialError):
                auth.read_zai_key()

    def test_refuses_pi_entry_whose_type_is_not_api_key(self):
        for entry in (
            {"type": "oauth", "key": SENTINEL, "access": SENTINEL},
            {"key": SENTINEL},
            SENTINEL,
        ):
            with self.subTest(entry=entry):
                make_creds(self.tmp, ".pi/agent/auth.json", {"zai": entry})
                with self.assertRaises(auth.CredentialError) as ctx:
                    auth.read_zai_key()
                error = ctx.exception.error
                self.assertIn("Z.AI API key not set", error.message)
                self.assertNotIn(SENTINEL, error.message + error.action)

    def test_refuses_pi_entry_whose_key_is_not_a_string(self):
        for key in (12345, [SENTINEL], {"value": SENTINEL}, None):
            with self.subTest(key=key):
                make_creds(
                    self.tmp, ".pi/agent/auth.json", {"zai": {"type": "api_key", "key": key}}
                )
                with self.assertRaises(auth.CredentialError) as ctx:
                    auth.read_zai_key()
                error = ctx.exception.error
                self.assertIn("Z.AI API key not set", error.message)
                self.assertNotIn(SENTINEL, error.message + error.action)

    def test_pi_auth_failures_fall_through_to_claude_settings(self):
        make_creds(self.tmp, ".claude/settings.json", self.bound_claude_settings(SENTINEL))
        for label, text, mode in self.skipped_variants(
            json.dumps({"zai": {"type": "api_key", "key": DECOY}}),
            "not json{",
            json.dumps({"anthropic": {"type": "api_key", "key": DECOY}}),
        ):
            with self.subTest(variant=label):
                write_file(self.tmp, ".pi/agent/auth.json", text, mode)
                self.assertEqual(auth.read_provider_credential("zai"), SENTINEL)

    def test_claude_settings_failures_fall_through_to_codex_config(self):
        write_file(self.tmp, ".codex/config.toml", self.bound_codex_config(SENTINEL))
        foreign = {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                "ANTHROPIC_AUTH_TOKEN": DECOY,
            }
        }
        for label, text, mode in self.skipped_variants(
            json.dumps(self.bound_claude_settings(DECOY)),
            "not json{",
            json.dumps(foreign),
        ):
            with self.subTest(variant=label):
                write_file(self.tmp, ".claude/settings.json", text, mode)
                self.assertEqual(auth.read_provider_credential("zai"), SENTINEL)

    def test_codex_config_failures_fall_through_to_actionable_missing_error(self):
        # The Codex config is the last source, so skipping it must surface the
        # provider's own missing-credential guidance rather than a loader error.
        foreign = (
            '[model_providers.ZAI]\n'
            'base_url = "https://api.openai.com/v1"\n'
            f'experimental_bearer_token = "{DECOY}"\n'
        )
        for label, text, mode in self.skipped_variants(
            self.bound_codex_config(DECOY),
            "[model_providers.ZAI\nbase_url = ",
            foreign,
        ):
            with self.subTest(variant=label):
                write_file(self.tmp, ".codex/config.toml", text, mode)
                with self.assertRaises(auth.CredentialError) as ctx:
                    auth.read_provider_credential("zai")
                error = ctx.exception.error
                self.assertIn("Z.AI API key not set", error.message)
                self.assertIn("ZAI_API_KEY", error.action)
                self.assertNotIn(DECOY, error.message + error.action)
                self.assertNotIn(str(self.tmp), error.message + error.action)

    def test_explicit_environment_key_precedes_discovered_files(self):
        os.environ["ZAI_API_KEY"] = SENTINEL
        make_creds(self.tmp, ".pi/agent/auth.json", {"zai": {"type": "api_key", "key": DECOY}})
        self.assertEqual(auth.read_zai_key(), SENTINEL)

    def test_missing_is_actionable(self):
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_zai_key()
        self.assertIn("Z.AI API key not set", ctx.exception.error.message)
        self.assertIn("ZAI_API_KEY", ctx.exception.error.action)


class KimiCredentialTests(IsolatedHomeMixin, unittest.TestCase):
    @staticmethod
    def bound_cli_config(token: str) -> str:
        return (
            '[providers.kimi-code]\n'
            'name = "Kimi Code"\n'
            'base_url = "https://api.kimi.com/coding/v1"\n'
            f'api_key = "{token}"\n'
        )

    def test_ignores_ambiguous_kimi_api_key(self):
        os.environ["KIMI_API_KEY"] = DECOY
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_kimi_key()
        self.assertNotIn(DECOY, ctx.exception.error.message + ctx.exception.error.action)

    def test_discovers_pi_auth_json_kimi_coding_entry(self):
        make_creds(
            self.tmp,
            ".pi/agent/auth.json",
            {"kimi-coding": {"type": "api_key", "key": SENTINEL}},
        )
        self.assertEqual(auth.read_kimi_key(), SENTINEL)

    def test_refuses_pi_command_and_environment_expressions_and_other_types(self):
        for payload in (
            {"kimi-coding": {"type": "api_key", "key": "!printf secret"}},
            {"kimi-coding": {"type": "api_key", "key": "$OTHER_PROVIDER_KEY"}},
            {"kimi-coding": {"type": "oauth", "key": SENTINEL}},
            {"kimi-coding": {"key": SENTINEL}},
        ):
            with self.subTest(payload=payload):
                make_creds(self.tmp, ".pi/agent/auth.json", payload)
                with self.assertRaises(auth.CredentialError):
                    auth.read_kimi_key()

    def test_discovers_key_from_cli_config_bound_to_the_coding_base_url(self):
        write_file(self.tmp, ".kimi-code/config.toml", self.bound_cli_config(SENTINEL))
        self.assertEqual(auth.read_kimi_key(), SENTINEL)

    def test_cli_config_path_honors_kimi_code_home(self):
        custom = self.tmp / "custom-home"
        custom.mkdir()
        os.environ["KIMI_CODE_HOME"] = str(custom)
        write_file(custom, "config.toml", self.bound_cli_config(SENTINEL))
        self.assertEqual(auth.read_kimi_key(), SENTINEL)

    def test_refuses_cli_config_keys_not_bound_to_the_coding_base_url(self):
        # A pay-as-you-go Moonshot entry (or any other base) must never be
        # treated as a Kimi Coding Plan key; an entry with no base_url is
        # skipped too because its default platform is not evidenced.
        for body in (
            '[providers.moonshot]\n'
            'base_url = "https://api.moonshot.ai/v1"\n'
            f'api_key = "{DECOY}"\n',
            '[providers.proxied]\n'
            'base_url = "https://proxy.example/v1"\n'
            f'api_key = "{DECOY}"\n',
            '[providers.default]\n' f'api_key = "{DECOY}"\n',
            '[providers.bound-but-trailing-slashes]\n'
            'base_url = "https://api.kimi.com/coding/v1//"\n'
            f'api_key = "{DECOY}"\n',
        ):
            with self.subTest(body=body):
                write_file(self.tmp, ".kimi-code/config.toml", body)
                with self.assertRaises(auth.CredentialError) as ctx:
                    auth.read_kimi_key()
                error = ctx.exception.error
                self.assertIn("Kimi Coding Plan key not set", error.message)
                self.assertNotIn(DECOY, error.message + error.action)
                self.assertNotIn(str(self.tmp), error.message + error.action)

    def test_optional_sources_are_skipped_not_fatal(self):
        # Insecure permissions, malformed TOML, and a non-table document all
        # skip the optional kimi sources instead of failing discovery.
        write_file(self.tmp, ".pi/agent/auth.json", "{}", 0o644)
        write_file(self.tmp, ".kimi-code/config.toml", "[providers.unclosed\n")
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_kimi_key()
        self.assertIn("Kimi Coding Plan key not set", ctx.exception.error.message)

    def test_missing_is_actionable(self):
        with self.assertRaises(auth.CredentialError) as ctx:
            auth.read_kimi_key()
        self.assertIn("Kimi Coding Plan key not set", ctx.exception.error.message)
        self.assertIn("kimi-cli", ctx.exception.error.action)


if __name__ == "__main__":
    unittest.main()
