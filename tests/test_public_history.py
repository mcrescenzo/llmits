"""Deterministic and non-destructive public-history candidate generation."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import public_history  # noqa: E402


class PublicHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    @staticmethod
    def git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_two_candidates_from_same_tree_have_same_clean_root(self) -> None:
        source_before = public_history._source_state(REPO_ROOT)
        first_path = self.root / "first"
        second_path = self.root / "second"
        first = public_history.build_candidate(first_path)
        second = public_history.build_candidate(second_path)

        self.assertEqual(first["commit"], second["commit"])
        self.assertEqual(public_history._source_state(REPO_ROOT), source_before)
        self.assertEqual(first["scan_findings"], [])
        self.assertEqual(second["scan_findings"], [])
        self.assertEqual(self.git(first_path, "remote"), "")
        self.assertEqual(
            self.git(first_path, "rev-list", "--parents", "-n", "1", "HEAD").split(),
            [first["commit"]],
        )
        candidate_names = self.git(first_path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        manifest_names = [item.path for item in public_history.source_manifest()]
        self.assertEqual(candidate_names, manifest_names)

        identity = self.git(first_path, "show", "-s", "--format=%an%n%ae%n%cn%n%ce", "HEAD")
        self.assertEqual(
            identity.splitlines(),
            [
                public_history.PUBLIC_NAME,
                public_history.PUBLIC_EMAIL,
                public_history.PUBLIC_NAME,
                public_history.PUBLIC_EMAIL,
            ],
        )
        self.assertEqual((first_path / "LICENSE").read_bytes(), (REPO_ROOT / "LICENSE").read_bytes())
        self.assertTrue((first_path / "tests" / "fixtures" / "zai_quota_synthetic.json").is_file())
        removed_fixture = "zai_quota_" + "live_2026-09.json"
        self.assertFalse((first_path / "tests" / "fixtures" / removed_fixture).exists())
        self.assertFalse((first_path / ".beads").exists())
        self.assertFalse((first_path / ".repo-review").exists())

    def test_scanner_detects_each_prohibited_content_class(self) -> None:
        samples = (
            b"https://" + b"claude.ai/share/example",
            b"zai_quota_" + b"live_2026-09.json",
            b"live-" + b"derived response fixture",
            b"-----BEGIN " + b"PRIVATE KEY-----",
            b"sk-" + b"abcdefghijklmnopqrstuvwxyz",
            b"ZAI_" + b"API_KEY=" + b"abcdefghijklmnop",
            b"/" + b"home/example/project",
            b"192." + b"168.10.2",
        )
        for index, sample in enumerate(samples):
            with self.subTest(index=index):
                self.assertTrue(public_history.scan_bytes(sample, "sample"))

    def test_output_must_be_new_and_outside_source(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            public_history.build_candidate(existing)
        with self.assertRaisesRegex(RuntimeError, "outside the source"):
            public_history.build_candidate(REPO_ROOT / "dist" / "candidate")


if __name__ == "__main__":
    unittest.main()
