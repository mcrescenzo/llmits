"""Deterministic and non-destructive public-history candidate generation."""
from __future__ import annotations

import json
import os
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


class HistoryScanTests(unittest.TestCase):
    """Full-ancestry scanner coverage, including content deleted before HEAD."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "--quiet")

    @staticmethod
    def _env() -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Test Committer",
                "GIT_AUTHOR_EMAIL": "committer@example.test",
                "GIT_COMMITTER_NAME": "Test Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.test",
                "GIT_AUTHOR_DATE": "2001-02-03T04:05:06+00:00",
                "GIT_COMMITTER_DATE": "2001-02-03T04:05:06+00:00",
            }
        )
        return env

    @classmethod
    def git(cls, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=cls._env(),
        ).stdout

    def write(self, relative: str, data: bytes) -> None:
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def head(self) -> str:
        return self.git(self.repo, "rev-parse", "HEAD").strip()

    def commit(self, message: str) -> str:
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "--quiet", "-m", message)
        return self.head()

    def run_scanner(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "public_history.py"),
                "--scan-history",
                "--repository",
                str(self.repo),
            ],
            capture_output=True,
            text=True,
        )

    def test_secret_deleted_before_head_is_still_found(self) -> None:
        token = b"sk-" + b"ancestorprobe0123"
        self.write("settings.txt", b"api_key=" + token + b"\n")
        self.commit("add local settings")
        self.git(self.repo, "rm", "--quiet", "settings.txt")
        self.commit("remove local settings")
        # The prohibited value exists only in an ancestor, not in the HEAD tree,
        # so a HEAD-only scan would pass and this test would fail.
        self.assertNotIn(
            "settings.txt", self.git(self.repo, "ls-tree", "-r", "--name-only", "HEAD")
        )

        report = public_history.history_scan_report(self.repo)
        self.assertEqual(
            [finding["rule"] for finding in report["findings"]],
            ["high-confidence access token"],
        )
        self.assertRegex(
            report["findings"][0]["location"], r"^blob [0-9a-f]{40}( at settings\.txt)?$"
        )
        self.assertNotIn(token.decode(), json.dumps(report))

        completed = self.run_scanner()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("high-confidence access token", completed.stdout)
        self.assertNotIn(token.decode(), completed.stdout)

    def test_clean_full_history_with_deletions_passes(self) -> None:
        self.write("keep.txt", b"ordinary content\n")
        self.commit("first")
        self.write("nested/deep.txt", b"more ordinary content\n")
        self.commit("second")
        self.git(self.repo, "rm", "--quiet", "keep.txt")
        self.commit("third")

        report = public_history.history_scan_report(self.repo)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["commits"], 3)
        completed = self.run_scanner()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["findings"], [])

    def test_commit_message_content_is_scanned(self) -> None:
        url = (b"https://" + b"claude.ai/share/" + b"note").decode()
        self.write("doc.txt", b"clean\n")
        oid = self.commit(f"see {url} for context")
        report = public_history.history_scan_report(self.repo)
        self.assertEqual(
            report["findings"],
            [{"rule": "Claude session URL", "location": f"commit {oid}"}],
        )

    def test_prohibited_path_name_is_found_without_being_echoed(self) -> None:
        fixture_name = "zai_quota_" + "live_2026-09.json"
        self.write(fixture_name, b"{}\n")
        self.commit("add fixture")
        report = public_history.history_scan_report(self.repo)
        rules = {finding["rule"] for finding in report["findings"]}
        self.assertIn("removed account fixture filename", rules)
        serialized = json.dumps(report)
        self.assertNotIn(fixture_name, serialized)
        self.assertNotIn("live_2026-09", serialized)

    def test_unique_blob_is_scanned_once_across_paths(self) -> None:
        payload = b"sk-" + b"sharedblobprobe12"
        self.write("one.txt", payload)
        self.commit("first copy")
        self.write("two.txt", payload)
        self.commit("second copy")
        report = public_history.history_scan_report(self.repo)
        token_findings = [
            finding
            for finding in report["findings"]
            if finding["rule"] == "high-confidence access token"
        ]
        self.assertEqual(len(token_findings), 1)
        self.assertEqual(report["blobs"], 1)
        self.assertEqual(report["historical_paths"], 2)
        self.assertEqual(report["commits"], 2)

    def test_moved_unchanged_subtree_preserves_every_historical_path(self) -> None:
        old_directory = "zai_quota_" + "live_2026-09"
        self.write(f"{old_directory}/payload.txt", b"ordinary content\n")
        self.commit("add synthetic tree")
        self.git(self.repo, "mv", old_directory, "clean")
        self.commit("move synthetic tree")
        self.assertNotIn(
            old_directory,
            self.git(self.repo, "ls-tree", "-r", "--name-only", "HEAD"),
        )

        report = public_history.history_scan_report(self.repo)
        self.assertIn(
            "removed account fixture filename",
            {finding["rule"] for finding in report["findings"]},
        )
        self.assertEqual(report["historical_paths"], 2)
        self.assertNotIn(old_directory, json.dumps(report))

    def test_scanner_cli_modes_are_mutually_exclusive(self) -> None:
        script = str(REPO_ROOT / "tools" / "public_history.py")
        both = subprocess.run(
            [sys.executable, script, "--output", str(self.root / "out"), "--scan-history"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(both.returncode, 2)
        self.assertFalse((self.root / "out").exists())
        misdirected = subprocess.run(
            [
                sys.executable,
                script,
                "--output",
                str(self.root / "out"),
                "--repository",
                str(self.repo),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(misdirected.returncode, 2)

    def test_public_repository_history_is_clean(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "public_history.py"), "--scan-history"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["findings"], [])


if __name__ == "__main__":
    unittest.main()
