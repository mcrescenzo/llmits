"""Static contract for the release-quality GitHub Actions workflow."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "check.yml"
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
                ("actions/checkout", "11bd71901bbe5b1630ceea73d27597364c9af683"),
                ("actions/setup-python", "a26af69be951a213d495a4c3e4e4022e16d87065"),
            ],
        )

    def test_ci_runs_the_canonical_release_gate_without_credentials(self):
        self.assertIn("make release-check PY=python", self.text)
        self.assertIn("HOME: ${{ runner.temp }}/llmits-empty-home", self.text)
        self.assertNotIn("secrets.", self.text)
        for unsafe in ("git push", "gh release", "workflow_dispatch"):
            self.assertNotIn(unsafe, self.text)


if __name__ == "__main__":
    unittest.main()
