"""Packaging contract: the shipped archive is licensed and reproducible."""
from __future__ import annotations

import hashlib
import stat
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import package  # noqa: E402


def shutil_copytree(source: Path, target: Path) -> None:
    import shutil

    shutil.copytree(source, target)


class ArchiveManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output = Path(self._tmp.name) / "llmits"

    def build(self) -> zipfile.ZipFile:
        artifact = package.build(self.output)
        return zipfile.ZipFile(artifact)

    def test_archive_contains_only_first_party_python_source(self):
        with self.build() as archive:
            names = archive.namelist()
        allowed = {"__main__.py", "llmits/LICENSE", "llmits/py.typed"} | {
            f"llmits/{path.relative_to(package.SOURCE_PACKAGE)}"
            for path in sorted(package.SOURCE_PACKAGE.rglob("*.py"))
        }
        allowed = {name.replace("\\", "/") for name in allowed}
        for name in list(allowed):
            parts = name.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                allowed.add("/".join(parts[:i]) + "/")
        unexpected = sorted(set(names) - allowed)
        self.assertEqual(unexpected, [], f"unexpected archive entries: {unexpected}")

    def test_archive_has_no_bytecode_or_caches(self):
        with self.build() as archive:
            names = archive.namelist()
        bad = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        self.assertEqual(bad, [])

    def test_archive_has_no_typo_or_test_trees(self):
        with self.build() as archive:
            names = archive.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        self.assertEqual(roots, {"__main__.py", "llmits"})

    def test_archive_contains_complete_mit_license(self):
        license_bytes = (REPO_ROOT / "LICENSE").read_bytes()
        with self.build() as archive:
            packed = archive.read("llmits/LICENSE")
        # The complete MIT notice, byte-for-byte, plus canary substrings so
        # a future swap to a stub file fails loudly.
        self.assertEqual(packed, license_bytes)
        self.assertIn(b"MIT License", packed)
        self.assertIn(b"Copyright", packed)

    def test_missing_license_file_fails_the_build(self):
        # A build whose staged tree cannot carry the repository LICENSE is
        # rejected up front, so the licensed-archive contract cannot drift
        # into shipping an unlicensed artifact.
        with tempfile.TemporaryDirectory() as tmp:
            source_copy = Path(tmp) / "llmits"
            shutil_copytree(package.SOURCE_PACKAGE, source_copy)
            absent = Path(tmp) / "absent-LICENSE"
            with mock.patch.object(package, "LICENSE_FILE", absent):
                with self.assertRaises(SystemExit):
                    package.build(self.output, source=source_copy)

    def test_two_clean_builds_are_byte_identical(self):
        # Two independently staged source trees (different mtimes, one with
        # stray bytecode the build must strip) must produce identical bytes
        # and SHA-256 values: no clock, timezone, locale, ordering, or
        # umask dependence.
        outputs = []
        for index, extra_pyc in enumerate((False, True)):
            with tempfile.TemporaryDirectory() as tmp:
                source_copy = Path(tmp) / "llimits"
                shutil_copytree(package.SOURCE_PACKAGE, source_copy)
                if extra_pyc:
                    cache = source_copy / "__pycache__"
                    cache.mkdir(exist_ok=True)
                    (cache / "noise.cpython-311.pyc").write_bytes(b"\x00junk")
                output = Path(self._tmp.name) / f"llmits-{index}"
                package.build(output, source=source_copy)
                outputs.append(output)
        first, second = outputs
        self.assertEqual(
            hashlib.sha256(first.read_bytes()).hexdigest(),
            hashlib.sha256(second.read_bytes()).hexdigest(),
        )
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_every_entry_has_normalized_metadata_and_sorted_order(self):
        with self.build() as archive:
            names = archive.namelist()
            # __main__.py is written first; every package entry follows in
            # path-sorted order with fully normalized metadata.
            self.assertEqual(names, ["__main__.py"] + sorted(names[1:]))
            self.assertEqual(package.nondeterministic_entries(archive), [])
            infos = {info.filename: info for info in archive.infolist()}
            main_bytes = archive.read("__main__.py")
        for name, info in infos.items():
            self.assertEqual(info.date_time, package.FIXED_DATE_TIME, name)
            self.assertEqual(info.create_system, package.UNIX_CREATE_SYSTEM, name)
            expected_mode = (
                package.ENTRYPOINT_MODE if name == "__main__.py" else package.FIXED_FILE_MODE
            )
            self.assertEqual((info.external_attr >> 16) & 0xFFFF, expected_mode, name)
        self.assertEqual(main_bytes, b"import llmits.cli\nllmits.cli.run()\n")

    def test_built_artifact_is_executable_and_not_group_or_world_writable(self):
        artifact = package.build(self.output)
        mode = stat.S_IMODE(artifact.stat().st_mode)
        self.assertTrue(mode & stat.S_IXUSR)
        self.assertEqual(mode & (stat.S_IWGRP | stat.S_IWOTH), 0)

    def test_built_artifact_runs_version(self):
        import subprocess

        artifact = package.build(self.output)
        result = subprocess.run(
            [sys.executable, str(artifact), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("llmits", result.stdout)

    def test_built_artifact_runs_json_with_no_credentials(self):
        import json
        import os
        import subprocess
        import tempfile

        artifact = package.build(self.output)

        # An isolated, credential-free HOME (mirroring tests/test_cli.py's
        # isolated_home()) makes the run deterministic and network-free: every
        # provider's credential lookup fails before any transport is used, so
        # this exercises the real zipimport --json path (RefreshService,
        # the production credential reader map, JSON serialization, and the
        # entry point's own exit code) without touching the network.
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = dict(os.environ)
        env["HOME"] = home.name
        for var in (
            "LLMITS_CLAUDE_CREDENTIALS",
            "LLMITS_CODEX_CREDENTIALS",
            "CLAUDE_CONFIG_DIR",
            "ZAI_API_KEY",
            "ZHIPU_API_KEY",
            "KIMI_API_KEY",
            "KIMI_CODE_HOME",
        ):
            env.pop(var, None)

        result = subprocess.run(
            [sys.executable, str(artifact), "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["schema_version"], 1)
        statuses = {p["provider"]: p["status"] for p in document["providers"]}
        self.assertEqual(
            statuses,
            {
                "claude": "auth_required",
                "codex": "auth_required",
                "zai": "auth_required",
                "kimi": "auth_required",
            },
        )

    def test_missing_init_py_raises_system_exit(self):
        missing_source = Path(self._tmp.name) / "not-a-package"
        with self.assertRaises(SystemExit):
            package.build(self.output, source=missing_source)


class ProjectMetadataTests(unittest.TestCase):
    def test_metadata_has_single_dynamic_version_and_zero_runtime_dependencies(self):
        metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        project = metadata["project"]
        self.assertEqual(project["name"], "llmits")
        self.assertEqual(project["dynamic"], ["version"])
        self.assertNotIn("version", project)
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["scripts"], {"llmits": "llmits.cli:main"})
        self.assertEqual(
            metadata["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "llmits.__version__"},
        )


class MainEntrypointTests(unittest.TestCase):
    """Covers package.main(argv=None), the argparse wrapper `make build` runs."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output = Path(self._tmp.name) / "llmits"

    def test_main_builds_artifact_via_output_flag(self):
        from contextlib import redirect_stdout
        from io import StringIO

        out = StringIO()
        with redirect_stdout(out):
            code = package.main(["--output", str(self.output)])

        self.assertEqual(code, 0)
        self.assertTrue(self.output.is_file())
        self.assertIn(f"built {self.output}", out.getvalue())
        with zipfile.ZipFile(self.output) as archive:
            self.assertIn("__main__.py", archive.namelist())


if __name__ == "__main__":
    unittest.main()
