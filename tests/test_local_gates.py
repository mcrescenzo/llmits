"""Contracts for local release validation without hosted automation."""
from __future__ import annotations

import re
import stat
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
PRE_PUSH = REPO_ROOT / ".githooks" / "pre-push"


class LocalGateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.makefile = MAKEFILE.read_text()

    def test_no_github_actions_workflows_or_dependabot_config(self):
        workflows = REPO_ROOT / ".github" / "workflows"
        self.assertEqual(list(workflows.rglob("*")) if workflows.exists() else [], [])
        for config in ("dependabot.yml", "dependabot.yaml"):
            path = REPO_ROOT / ".github" / config
            if path.exists():
                self.assertNotRegex(path.read_text(), r"package-ecosystem:\s*['\"]?github-actions")

    def test_release_gate_depends_on_history_check(self):
        self.assertIn("history-check:", self.makefile)
        self.assertIsNotNone(
            re.search(r"^release-check:.*\bhistory-check\b", self.makefile, re.MULTILINE)
        )
        self.assertIn("tools/public_history.py --scan-history --repository .", self.makefile)

    def test_install_hooks_points_git_at_the_tracked_hooks_directory(self):
        self.assertIn("install-hooks:", self.makefile)
        self.assertIn("git config core.hooksPath .githooks", self.makefile)

    def test_pre_push_hook_is_executable_and_runs_the_gate_with_one_history_scan(self):
        self.assertTrue(PRE_PUSH.is_file())
        self.assertTrue(PRE_PUSH.stat().st_mode & stat.S_IXUSR)
        hook = PRE_PUSH.read_text()
        self.assertIn("set -euo pipefail", hook)
        self.assertIn("make release-check", hook)
        self.assertNotIn("make history-check", hook)
        self.assertEqual(hook.count("make release-check"), 1)
        self.assertNotIn("public_history.py", hook)
        dry_run = subprocess.run(
            ["make", "-n", "release-check", "PY=python"],
            capture_output=True,
            check=True,
            cwd=REPO_ROOT,
            text=True,
        )
        scan_lines = [
            line
            for line in dry_run.stdout.splitlines()
            if "public_history.py --scan-history" in line
        ]
        self.assertEqual(len(scan_lines), 1, scan_lines)


if __name__ == "__main__":
    unittest.main()
