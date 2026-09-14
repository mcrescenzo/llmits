"""Static contract for the release-quality GitHub Actions workflow."""
from __future__ import annotations

import re
import stat
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "check.yml"
MAKEFILE = REPO_ROOT / "Makefile"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
PRE_PUSH = REPO_ROOT / ".githooks" / "pre-push"
ACTION_PIN = re.compile(r"^\s*uses:\s*([^\s@]+)@([0-9a-f]{40})\s+#\s+v\d", re.MULTILINE)


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()

    def test_runner_matrix_permissions_and_timeout_are_fixed(self):
        self.assertIn("runs-on: ubuntu-24.04", self.text)
        self.assertNotIn("ubuntu-latest", self.text)
        self.assertIn("timeout-minutes: 15", self.text)
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertIn('python-version: ["3.11", "3.12", "3.13", "3.14"]', self.text)

    def test_every_action_is_pinned_to_an_annotated_commit(self):
        uses_lines = [line.strip() for line in self.text.splitlines() if "uses:" in line]
        pins = ACTION_PIN.findall(self.text)
        self.assertEqual(len(pins), len(uses_lines), uses_lines)
        self.assertEqual(
            pins,
            [
                ("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1"),
                ("actions/setup-python", "5fda3b95a4ea91299a34e894583c3862153e4b97"),
            ],
        )

    def test_ci_runs_the_canonical_release_gate_without_credentials(self):
        self.assertIn("make release-check PY=python", self.text)
        self.assertIn("HOME: ${{ runner.temp }}/llmits-empty-home", self.text)
        self.assertNotIn("secrets.", self.text)
        for unsafe in ("git push", "gh release", "workflow_dispatch"):
            self.assertNotIn(unsafe, self.text)

    def test_checkout_fetches_full_history_for_the_history_gate(self):
        self.assertIn("fetch-depth: 0", self.text)
        # The canonical gate must run the full-history scan on that checkout.
        self.assertIn("make release-check PY=python", self.text)


class RepositoryHygieneContractTests(unittest.TestCase):
    """Wiring for the history gate: Makefile targets, hook, and Dependabot."""

    def setUp(self) -> None:
        self.makefile = MAKEFILE.read_text()

    def test_release_gate_depends_on_history_check(self):
        self.assertIn("history-check:", self.makefile)
        self.assertIsNotNone(
            re.search(r"^release-check:.*\bhistory-check\b", self.makefile, re.MULTILINE)
        )
        self.assertIn("tools/public_history.py --scan-history --repository .", self.makefile)

    def test_install_hooks_points_git_at_the_tracked_hooks_directory(self):
        self.assertIn("install-hooks:", self.makefile)
        self.assertIn("git config core.hooksPath .githooks", self.makefile)

    def test_pre_push_hook_is_executable_and_runs_both_gates(self):
        self.assertTrue(PRE_PUSH.is_file())
        self.assertTrue(PRE_PUSH.stat().st_mode & stat.S_IXUSR)
        hook = PRE_PUSH.read_text()
        self.assertIn("make history-check", hook)
        self.assertIn("make release-check", hook)
        self.assertIn("set -euo pipefail", hook)

    def test_dependabot_updates_github_actions_weekly(self):
        dependabot = DEPENDABOT.read_text()
        self.assertIn("version: 2", dependabot)
        self.assertIn('package-ecosystem: "github-actions"', dependabot)
        self.assertIn('interval: "weekly"', dependabot)
        self.assertIn('directory: "/"', dependabot)


if __name__ == "__main__":
    unittest.main()
