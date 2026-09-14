"""Security contract tests over src/llmits.

The static tests fail if someone introduces a dependency, a subprocess, a
file-writing API, or an os.open mode outside the credential read flags. The
sentinel tests at the end inject hostile provider text and prove it never
reaches a TUI segment or the --json document.
"""
from __future__ import annotations

import ast
import json
import re
import unicodedata
import unittest
from datetime import datetime, timezone
from pathlib import Path

from llmits import json_output, tui
from llmits.http import TransportError
from llmits.models import (
    AVAILABLE,
    PARSE_ERROR,
    MAX_VETTED_LABEL,
    ProviderError,
    ProviderSnapshot,
    QuotaWindow,
)
from llmits.providers import claude, codex, zai
from llmits.providers.common import transport_error_snapshot

SRC = Path(__file__).resolve().parent.parent / "src" / "llmits"

FORBIDDEN_MODULES = {
    "subprocess",
    "urllib",
    "webbrowser",
    "ctypes",
    "pickle",
    "shelve",
    "shutil",
    "ftplib",
    "telnetlib",
    "smtplib",
    "socketserver",
    "http.server",
    "requests",
    "urllib3",
    "asyncio",
    "pickletools",
}

FORBIDDEN_BUILTIN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}

FORBIDDEN_OS_ATTRIBUTES = {
    "system",
    "popen",
    "fork",
    "forkpty",
    "kill",
    "killpg",
    "chmod",
    "chown",
    "remove",
    "removedirs",
    "unlink",
    "rmdir",
    "mkdir",
    "makedirs",
    "rename",
    "renames",
    "replace",
    "truncate",
    "putenv",
    "setuid",
    "setgid",
    "listdir",
    "scandir",
    "walk",
}

FORBIDDEN_ATTRIBUTE_CALLS = {
    "write_text",
    "write_bytes",
    "unlink",
    "mkdir",
    "rmdir",
    "rename",
    "chmod",
    "symlink",
    "hardlink",
    "touch",
    "glob",
    "rglob",
    "iterdir",
}

WRITE_MODE_CHARS = set("wax+")

ALLOWED_OS_OPEN_FLAGS = {
    "O_RDONLY",
    "O_NOFOLLOW",
    "O_NONBLOCK",
    "O_CLOEXEC",
}

FORBIDDEN_OS_OPEN_FLAGS = {
    "O_WRONLY",
    "O_RDWR",
    "O_CREAT",
    "O_APPEND",
    "O_TRUNC",
    "O_EXCL",
    "O_TMPFILE",
    "O_SYNC",
}


def source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py"))


def parse_all() -> dict[Path, ast.Module]:
    return {path: ast.parse(path.read_text(), filename=str(path)) for path in source_files()}


class ImportTests(unittest.TestCase):
    def test_src_tree_is_nonempty(self):
        self.assertGreater(len(source_files()), 8)

    def test_no_forbidden_imports(self):
        offenders = []
        for path, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        full = alias.name
                        if full in FORBIDDEN_MODULES or root in FORBIDDEN_MODULES:
                            offenders.append(f"{path.name}:{node.lineno} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    root = module.split(".")[0]
                    joined = module
                    if joined in FORBIDDEN_MODULES or root in FORBIDDEN_MODULES:
                        offenders.append(f"{path.name}:{node.lineno} imports from {module}")
        self.assertEqual(offenders, [])


class CallTests(unittest.TestCase):
    def test_no_forbidden_builtin_calls(self):
        offenders = []
        for path, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTIN_CALLS:
                        offenders.append(f"{path.name}:{node.lineno} calls {func.id}()")
        self.assertEqual(offenders, [])

    def test_no_os_mutation_or_process_calls(self):
        offenders = []
        for path, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    func = node.func
                    value = func.value
                    if isinstance(value, ast.Name) and value.id == "os":
                        if func.attr in FORBIDDEN_OS_ATTRIBUTES:
                            offenders.append(f"{path.name}:{node.lineno} calls os.{func.attr}()")
                    if func.attr in FORBIDDEN_ATTRIBUTE_CALLS:
                        offenders.append(f"{path.name}:{node.lineno} calls .{func.attr}()")
        self.assertEqual(offenders, [])

    def test_open_is_never_called_for_writing(self):
        offenders = []
        for path, tree in parse_all().items():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != "open" or len(node.args) < 2:
                    continue
                mode = node.args[1]
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                    if WRITE_MODE_CHARS & set(mode.value):
                        offenders.append(f"{path.name}:{node.lineno} opens for writing")
        self.assertEqual(offenders, [])

    def test_os_open_flags_restricted_to_credential_read(self):
        offenders = []
        for path, tree in parse_all().items():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "os"):
                    continue
                if node.func.attr != "open":
                    continue
                flags_names = set()
                for arg in node.args[1:]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Attribute):
                            flags_names.add(sub.attr)
                bad = flags_names & FORBIDDEN_OS_OPEN_FLAGS
                unknown = flags_names - ALLOWED_OS_OPEN_FLAGS - FORBIDDEN_OS_OPEN_FLAGS
                if bad or unknown:
                    offenders.append(
                        f"{path.name}:{node.lineno} os.open flags {sorted(flags_names)}"
                    )
        self.assertEqual(offenders, [])


class HardcodedEndpointTests(unittest.TestCase):
    def test_transport_allowlist_covers_every_provider_host(self):
        http_source = (SRC / "http.py").read_text()
        for host in (
            "api.anthropic.com",
            "chatgpt.com",
            "api.z.ai",
            "api.kimi.com",
            "opencode.ai",
        ):
            self.assertIn(host, http_source)
        self.assertNotIn("http://", http_source.replace("https://", ""))
        self.assertNotIn("evil", http_source)

    def test_provider_modules_pin_host_and_path_constants(self):
        for module, host, path in (
            ("claude.py", "api.anthropic.com", "/api/oauth/usage"),
            ("codex.py", "chatgpt.com", "/backend-api/wham/usage"),
            ("zai.py", "api.z.ai", "/api/monitor/usage/quota/limit"),
            ("kimi.py", "api.kimi.com", "/coding/v1/usages"),
            ("opencode.py", "opencode.ai", "/zen/go/v1/usage"),
        ):
            source = (SRC / "providers" / module).read_text()
            self.assertIn(f'HOST = "{host}"', source)
            self.assertIn(f'PATH = "{path}"', source)


# ---------------------------------------------------------------------------
# Runtime sentinel tests: hostile provider text vs. every output surface.
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

# Unicode format (Cf) code points that enable Trojan-Source-style spoofing:
# zero-width space/joiners and bidi marks, bidi embeddings/overrides, bidi
# isolates, and the byte-order mark.
FORMAT_SENTINELS = tuple(
    [chr(c) for c in range(0x200B, 0x2010)]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + ["\ufeff"]
)
# Terminal-control sentinels the older hardening already covered; kept in
# the same sweep so the two classes are proven together.
CONTROL_SENTINELS = ("\x1b", "\x9b", "\x07", "\x00")
ALL_SENTINELS = FORMAT_SENTINELS + CONTROL_SENTINELS
LABEL_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+()/-]*")


def _hostile(text: str) -> str:
    """Inject every sentinel into ``text``; a correct boundary yields ``text`` back.

    All format sentinels go inside the first word (proving they are dropped
    without splitting it) and the control sentinels go at the end (proving
    the space they become is trimmed).
    """
    return text[0] + "".join(FORMAT_SENTINELS) + text[1:] + "".join(CONTROL_SENTINELS)


def _segments(snapshots, width: int, height: int) -> list[str]:
    view = tui.TuiView(
        provider_ids=tuple(s.provider for s in snapshots),
        snapshots=tuple(snapshots),
        loading=False,
        last_refresh=NOW,
        refresh_seconds=300,
        version="0.2.0",
        now=NOW,
    )
    return [segment.text for line in tui.render(view, width, height) for segment in line]


def _json_strings(document: str) -> list[str]:
    """Every string value in the parsed document, plus the raw text itself."""
    found = [document]

    def walk(node):
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(document))
    return found


class FormatCharacterSentinelTests(unittest.TestCase):
    """Injected into plan_name, key, label, and error text; absent from every sink."""

    def setUp(self):
        window = QuotaWindow(
            key=_hostile("5h"),
            label=_hostile("5-hour"),
            used_percent=42,
            remaining_percent=58,
            reset_at=NOW,
            period_seconds=18000,
            used_value=1,
            limit_value=2,
            remaining_value=1,
        )
        self.available = ProviderSnapshot(
            provider="claude",
            status=AVAILABLE,
            plan_name=_hostile("Claude Pro/Max"),
            fetched_at=NOW,
            windows=(window,),
        )
        self.stale = ProviderSnapshot(
            provider="codex",
            status=AVAILABLE,
            plan_name=_hostile("Plus"),
            fetched_at=NOW,
            windows=(window,),
            stale=True,
            error=ProviderError(
                code=PARSE_ERROR, message=_hostile("last refresh broke"), action=_hostile("retry")
            ),
        )
        self.failed = ProviderSnapshot(
            provider="zai",
            status=PARSE_ERROR,
            plan_name=None,
            fetched_at=NOW,
            error=ProviderError(
                code=PARSE_ERROR,
                message=_hostile("provider response changed"),
                action=_hostile("check for a newer llmits release"),
            ),
        )
        self.snapshots = [self.available, self.stale, self.failed]

    def test_sentinel_fixture_is_hostile(self):
        # Guard the harness: the injected text really contains every sentinel
        # and every format sentinel really is category Cf.
        blob = _hostile("x")
        for sentinel in ALL_SENTINELS:
            self.assertIn(sentinel, blob)
        for sentinel in FORMAT_SENTINELS:
            self.assertEqual(unicodedata.category(sentinel), "Cf", f"U+{ord(sentinel):04X}")

    def test_absent_from_every_tui_segment_at_every_layout(self):
        for width, height in ((120, 40), (80, 24), (60, 15)):
            segments = _segments(self.snapshots, width, height)
            self.assertTrue(segments)
            for segment in segments:
                for sentinel in ALL_SENTINELS:
                    self.assertNotIn(
                        sentinel, segment, f"U+{ord(sentinel):04X} in {segment!r} at {width}x{height}"
                    )
            flat = "\n".join(segments)
            self.assertIn("5-hour", flat)
            self.assertIn("provider response changed", flat)
        # The wide layout also shows plan names and error detail; all clean.
        flat = "\n".join(_segments(self.snapshots, 120, 40))
        self.assertIn("[Claude Pro/Max]", flat)
        self.assertIn("last update failed: last refresh broke", flat)
        self.assertIn("provider response changed — check for a newer llmits release", flat)

    def test_absent_from_json_document_raw_and_parsed(self):
        document = json_output.to_document(self.snapshots, NOW)
        for text in _json_strings(document):
            for sentinel in ALL_SENTINELS:
                self.assertNotIn(sentinel, text, f"U+{ord(sentinel):04X} in {text!r}")
                # json.dumps escapes non-ASCII, so also prove the escaped form is gone.
                self.assertNotIn("\\u%04x" % ord(sentinel), text.lower())
        parsed = json.loads(document)
        self.assertEqual(parsed["providers"][0]["plan_name"], "Claude Pro/Max")
        self.assertEqual(parsed["providers"][0]["windows"][0]["key"], "5h")
        self.assertEqual(parsed["providers"][0]["windows"][0]["label"], "5-hour")
        self.assertEqual(parsed["providers"][2]["error"]["message"], "provider response changed")


class HostileLabelSinkTests(unittest.TestCase):
    """The two label sinks built from raw provider strings now emit vetted text only."""

    HOSTILE_NAME = "Opus\u202e\x1b]0;pwned\x07 <evil>;rm -rf /"

    def test_claude_display_name_with_format_characters_is_vetted_not_raw(self):
        payload = {
            "limits": [
                {
                    "group": "weekly",
                    "percent": 47,
                    "scope": {"model": {"display_name": "Opus\u200b\u202e\ufeff"}},
                }
            ]
        }
        (window,) = claude.parse_usage(payload)
        self.assertEqual(window.key, "weekly_opus")
        self.assertEqual(window.label, "Opus")

    def test_hostile_claude_display_name_falls_back_to_slug_text(self):
        payload = {
            "limits": [
                {
                    "group": "weekly",
                    "percent": 47,
                    "scope": {"model": {"display_name": self.HOSTILE_NAME}},
                }
            ]
        }
        (window,) = claude.parse_usage(payload)
        self.assertNotIn(self.HOSTILE_NAME, window.label)
        self.assertNotIn("<evil>", window.label)
        self.assertNotIn(";", window.label)
        for sentinel in ALL_SENTINELS:
            self.assertNotIn(sentinel, window.label)
            self.assertNotIn(sentinel, window.key)
        self.assertTrue(LABEL_SHAPE.fullmatch(window.label), window.label)
        self.assertTrue(re.fullmatch(r"weekly_[a-z0-9_]+", window.key), window.key)

    def test_hostile_claude_seven_day_key_suffix_is_vetted_not_raw(self):
        payload = {"seven_day_" + self.HOSTILE_NAME: {"utilization": 12}}
        (window,) = claude.parse_usage(payload)
        self.assertNotIn(self.HOSTILE_NAME, window.label)
        self.assertNotIn("<evil>", window.label)
        for sentinel in ALL_SENTINELS:
            self.assertNotIn(sentinel, window.label)
            self.assertNotIn(sentinel, window.key)
        self.assertTrue(LABEL_SHAPE.fullmatch(window.label), window.label)
        self.assertTrue(re.fullmatch(r"weekly_[a-z0-9_]+", window.key), window.key)

    def test_claude_non_ascii_display_name_yields_ascii_only_key_and_label(self):
        # Cyrillic homoglyph of "Opus": str.isalnum() would have let it into
        # the key and label; the ASCII-only slug drops it, and vetted_label
        # rejects the display text, so neither surface carries non-ASCII.
        payload = {
            "limits": [
                {"group": "weekly", "percent": 1, "scope": {"model": {"display_name": "\u041epus"}}}
            ],
            "seven_day_\u041epus": {"utilization": 2},
        }
        (window,) = claude.parse_usage(payload)
        self.assertTrue(window.key.isascii() and window.label.isascii(), (window.key, window.label))
        self.assertNotIn("\u041e", window.key + window.label)
        self.assertTrue(re.fullmatch(r"weekly_[a-z0-9_]+", window.key), window.key)
        self.assertTrue(LABEL_SHAPE.fullmatch(window.label), window.label)
        # An all-non-ASCII name has no slug at all and produces no window.
        self.assertEqual(
            claude.parse_usage({"seven_day_\u041e\u043f\u0443\u0441": {"utilization": 2}}), ()
        )

    def test_claude_fixture_labels_unchanged(self):
        payload = {
            "seven_day_opus": {"utilization": 5},
            "limits": [
                {"group": "weekly", "percent": 47, "scope": {"model": {"display_name": "Fable"}}}
            ],
        }
        self.assertEqual(
            [(w.key, w.label) for w in claude.parse_usage(payload)],
            [("weekly_opus", "Opus"), ("weekly_fable", "Fable")],
        )

    def test_zai_label_with_format_characters_is_vetted_not_raw(self):
        payload = {
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "\ufeffFuture\u200b Quota\u202e", "rawType": "FUTURE_LIMIT", "percentage": 10}
                ],
            }
        }
        _plan, (window,) = zai.parse_usage(payload)
        self.assertEqual(window.label, "Future Quota")
        self.assertEqual(window.key, "other:future_quota")

    def test_hostile_zai_label_falls_back_to_key_slug_text(self):
        hostile = "Future\u202e Quota\x1b[31m <b>x</b>"
        payload = {
            "data": {
                "level": "pro",
                "limits": [{"type": hostile, "rawType": "FUTURE_LIMIT", "percentage": 10}],
            }
        }
        _plan, (window,) = zai.parse_usage(payload)
        self.assertNotEqual(window.label, hostile)
        self.assertNotIn("<b>", window.label)
        for sentinel in ALL_SENTINELS:
            self.assertNotIn(sentinel, window.label)
            self.assertNotIn(sentinel, window.key)
        self.assertTrue(LABEL_SHAPE.fullmatch(window.label), window.label)
        self.assertTrue(re.fullmatch(r"other(:[a-z0-9_]{1,20})?", window.key), window.key)

    def test_zai_non_ascii_or_over_long_label_falls_back_to_generic_or_slug(self):
        payload = {
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "\u672a\u6765\u914d\u989d", "rawType": "FUTURE_LIMIT", "percentage": 1},
                    {"type": "a" * (MAX_VETTED_LABEL + 1), "rawType": "OTHER_LIMIT", "percentage": 2},
                ],
            }
        }
        _plan, windows = zai.parse_usage(payload)
        labels = {w.key: w.label for w in windows}
        self.assertEqual(labels["other"], "quota")
        self.assertEqual(labels["other:" + "a" * 20], "a" * 20)

    def test_hostile_labels_survive_to_tui_and_json_as_vetted_text(self):
        claude_windows = claude.parse_usage(
            {
                "limits": [
                    {
                        "group": "weekly",
                        "percent": 47,
                        "scope": {"model": {"display_name": self.HOSTILE_NAME}},
                    }
                ]
            }
        )
        _plan, zai_windows = zai.parse_usage(
            {
                "data": {
                    "level": "pro",
                    "limits": [
                        {"type": self.HOSTILE_NAME, "rawType": "FUTURE_LIMIT", "percentage": 10}
                    ],
                }
            }
        )
        snapshots = [
            ProviderSnapshot("claude", AVAILABLE, claude.PLAN_NAME, NOW, claude_windows),
            ProviderSnapshot("zai", AVAILABLE, "Z.AI pro", NOW, zai_windows),
        ]
        outputs = _segments(snapshots, 120, 40) + _json_strings(json_output.to_document(snapshots, NOW))
        for text in outputs:
            self.assertNotIn(self.HOSTILE_NAME, text)
            self.assertNotIn("<evil>", text)
            for sentinel in ALL_SENTINELS:
                self.assertNotIn(sentinel, text, f"U+{ord(sentinel):04X} in {text!r}")


class AsciiSentinelSinkTests(unittest.TestCase):
    """x47.2: ordinary ASCII sentinel text must not reach JSON or the TUI.

    The format/control sentinels above are caught by sanitization; ordinary
    provider-derived ASCII (an unknown Codex ``limit_name``, a
    ``TransportError`` message) previously passed sanitization verbatim into
    window keys/labels and error messages. These tests prove the adapter
    boundary replaces it with fixed local text before any renderer runs.
    """

    LIMIT_SENTINEL = "acme-X47SENTINEL-plan"
    ERROR_SENTINEL = "SECRET-SENTINEL-xyz"

    def _codex_payload(self) -> dict:
        return {
            "plan_type": "plus",
            "additional_rate_limits": [
                {
                    "limit_name": self.LIMIT_SENTINEL,
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 21,
                            "limit_window_seconds": 604800,
                            "reset_at": 1784934400,
                        }
                    },
                },
                {
                    "limit_name": "acme-X47SENTINEL-plan-b",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 22,
                            "limit_window_seconds": 604800,
                            "reset_at": 1784934400,
                        }
                    },
                },
            ],
        }

    def _snapshots(self) -> list[ProviderSnapshot]:
        plan_name, windows = codex.parse_usage(self._codex_payload())
        available = ProviderSnapshot(
            provider="codex",
            status=AVAILABLE,
            plan_name=plan_name,
            fetched_at=NOW,
            windows=windows,
        )
        network_failed = transport_error_snapshot(
            "zai", TransportError(f"{self.ERROR_SENTINEL} connection reset"), NOW
        )
        return [available, network_failed]

    def test_sentinel_absent_from_keys_labels_and_errors(self):
        _, windows = codex.parse_usage(self._codex_payload())
        self.assertEqual([w.key for w in windows], ["x1/7d", "x2/7d"])
        for window in windows:
            self.assertNotIn("X47SENTINEL", window.key)
            self.assertNotIn("X47SENTINEL", window.label)
            self.assertNotIn("acme", window.key)
            self.assertNotIn("acme", window.label)
        snapshot = transport_error_snapshot(
            "zai", TransportError(f"{self.ERROR_SENTINEL} connection reset"), NOW
        )
        self.assertEqual(snapshot.error.message, "network error (TransportError)")

    def test_sentinel_absent_from_every_tui_segment_at_every_layout(self):
        snapshots = self._snapshots()
        for width, height in ((120, 40), (80, 24), (60, 15)):
            segments = _segments(snapshots, width, height)
            self.assertTrue(segments)
            flat = "\n".join(segments)
            self.assertNotIn(self.LIMIT_SENTINEL, flat)
            self.assertNotIn(self.ERROR_SENTINEL, flat)
            self.assertNotIn("X47SENTINEL", flat)
            self.assertNotIn("acme", flat)
            # The replacement text really is rendered instead.
            self.assertIn("Limit 1", flat)
            self.assertIn("Limit 2", flat)
            self.assertIn("network error (TransportError)", flat)

    def test_sentinel_absent_from_json_document(self):
        document = json_output.to_document(self._snapshots(), NOW)
        for text in _json_strings(document):
            self.assertNotIn("X47SENTINEL", text)
            self.assertNotIn("acme", text)
            self.assertNotIn(self.ERROR_SENTINEL, text)
        parsed = json.loads(document)
        keys = [w["key"] for w in parsed["providers"][0]["windows"]]
        labels = [w["label"] for w in parsed["providers"][0]["windows"]]
        self.assertEqual(keys, ["x1/7d", "x2/7d"])
        self.assertEqual(labels, ["Limit 1", "Limit 2"])
        self.assertEqual(
            parsed["providers"][1]["error"]["message"], "network error (TransportError)"
        )


if __name__ == "__main__":
    unittest.main()
