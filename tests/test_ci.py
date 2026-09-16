"""Static contract for the release-quality GitHub Actions workflow."""
from __future__ import annotations

import re
import stat
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "check.yml"
MAKEFILE = REPO_ROOT / "Makefile"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
PRE_PUSH = REPO_ROOT / ".githooks" / "pre-push"
# A step's `uses` key, which YAML permits with surrounding quotes and with
# whitespace before the colon. Finding those spellings keeps an unpinned step
# visible to the validator instead of silently passing it; the validator then
# accepts the key spelled either way as long as the reference itself is pinned.
USES_KEY_PATTERN = r"['\"]?uses['\"]?\s*:"
USES_KEY = re.compile(USES_KEY_PATTERN)
ACTION_PIN = re.compile(
    r"^\s*(?:-\s+)?" + USES_KEY_PATTERN + r"\s*([^\s@]+)@([0-9a-f]{40})\s+#\s+v[0-9]",
    re.MULTILINE,
)
# The structural contract: exactly these actions may appear in `uses:` lines.
# Their commit SHAs are deliberately not frozen here — Dependabot updates the
# commits, and an approved bump must pass without editing this file.
EXPECTED_ACTIONS = ("actions/checkout", "actions/setup-python")


def workflow_pin_findings(workflow_text: str) -> list[str]:
    """Return one human-readable finding per action-pin violation.

    The contract is structural, not value-based: every ``uses:`` line must pin
    one of ``EXPECTED_ACTIONS`` to a full 40-hex-character commit SHA (never a
    tag, branch, or abbreviated commit) annotated with a ``# vN...`` version
    comment, so the valid-pin count equals the ``uses:`` line count. Findings
    name the offending line so a failure pinpoints what to fix.
    """
    uses_lines = [
        (number, line.strip())
        for number, line in enumerate(workflow_text.splitlines(), start=1)
        if USES_KEY.search(line)
    ]
    findings = [
        f"line {number} is not an action pinned to a full 40-hex commit SHA"
        f" with a '# vN' version annotation: {line!r}"
        for number, line in uses_lines
        if ACTION_PIN.match(line) is None
    ]
    pins = ACTION_PIN.findall(workflow_text)
    if len(pins) != len(uses_lines):
        findings.append(
            f"every uses line must be a valid pin: found {len(pins)} valid pins"
            f" for {len(uses_lines)} uses lines: {[line for _, line in uses_lines]}"
        )
    names = sorted({name for name, _sha in pins})
    if names != sorted(EXPECTED_ACTIONS):
        findings.append(f"workflow must use exactly {sorted(EXPECTED_ACTIONS)}, found {names}")
    return findings


def bumped_action_commits(workflow_text: str) -> str:
    """Replace each pinned action commit with a different valid 40-hex SHA.

    This is the only change an approved Dependabot actions update makes: the
    action names and ``# vN`` version annotations stay untouched.
    """
    index = 0

    def bump(match: re.Match[str]) -> str:
        nonlocal index
        index += 1
        return f"{match.group(1)}@{index:040x}"

    return re.sub(r"(uses:\s*[^\s@]+)@[0-9a-f]{40}", bump, workflow_text)


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
        self.assertEqual(workflow_pin_findings(self.text), [])
        names = {name for name, _sha in ACTION_PIN.findall(self.text)}
        self.assertEqual(names, set(EXPECTED_ACTIONS))

    def test_approved_dependabot_commit_bump_keeps_the_pin_contract(self):
        # Dependabot edits only the workflow's `uses:` SHAs, never this test:
        # the bumped workflow must satisfy the same pin contract unedited.
        bumped = bumped_action_commits(self.text)
        self.assertNotEqual(bumped, self.text)
        original = dict(ACTION_PIN.findall(self.text))
        for name, sha in ACTION_PIN.findall(bumped):
            self.assertRegex(sha, r"^[0-9a-f]{40}$")
            self.assertNotEqual(sha, original[name])
        self.assertEqual(workflow_pin_findings(bumped), [])

    def test_tag_reference_is_rejected(self):
        # The annotation stays so the only weakened property is the mutable
        # tag ref standing in for the full commit SHA.
        variant = re.sub(
            r"(uses:\s*actions/checkout)@[0-9a-f]{40}",
            r"\1@v4",
            self.text,
            count=1,
        )
        self.assertNotEqual(variant, self.text)
        findings = workflow_pin_findings(variant)
        self.assertTrue(any("actions/checkout@v4" in finding for finding in findings), findings)

    def test_abbreviated_commit_is_rejected(self):
        abbreviated = dict(ACTION_PIN.findall(self.text))["actions/checkout"][:8]
        variant = re.sub(
            r"(uses:\s*actions/checkout)@[0-9a-f]{40}",
            rf"\1@{abbreviated}",
            self.text,
            count=1,
        )
        self.assertNotEqual(variant, self.text)
        findings = workflow_pin_findings(variant)
        self.assertTrue(any(f"@{abbreviated}" in finding for finding in findings), findings)

    def test_full_commit_without_version_annotation_is_rejected(self):
        sha = dict(ACTION_PIN.findall(self.text))["actions/checkout"]
        variant = re.sub(
            rf"uses:\s*actions/checkout@{sha}\s+#\s+v\S+",
            f"uses: actions/checkout@{sha}",
            self.text,
            count=1,
        )
        self.assertNotEqual(variant, self.text)
        findings = workflow_pin_findings(variant)
        self.assertTrue(any(f"actions/checkout@{sha}" in finding for finding in findings), findings)

    def test_extra_uses_line_without_a_valid_pin_is_rejected(self):
        variant = self.text.replace(
            "    steps:\n",
            "    steps:\n      - name: Unpinned helper action\n        uses: actions/cache@v4\n",
            1,
        )
        self.assertNotEqual(variant, self.text)
        findings = workflow_pin_findings(variant)
        self.assertTrue(any("actions/cache@v4" in finding for finding in findings), findings)

    def test_unexpected_action_name_is_rejected(self):
        variant = self.text.replace("actions/checkout@", "actions/cache@", 1)
        self.assertNotEqual(variant, self.text)
        findings = workflow_pin_findings(variant)
        self.assertTrue(any("actions/cache" in finding for finding in findings), findings)

    def test_yaml_key_spellings_cannot_hide_an_unpinned_action(self):
        # `uses :` and "uses": are legal YAML spellings of the same key. An
        # unpinned reference hidden behind either must still be rejected, and
        # a correctly pinned step written that way must still be accepted.
        for spelling in ("uses :", '"uses":', "'uses':"):
            with self.subTest(spelling=spelling):
                unpinned = re.sub(
                    r"uses:\s*actions/checkout@[0-9a-f]{40}",
                    f"{spelling} actions/cache@v4",
                    self.text,
                    count=1,
                )
                self.assertNotEqual(unpinned, self.text)
                findings = workflow_pin_findings(unpinned)
                self.assertTrue(
                    any("actions/cache@v4" in finding for finding in findings), findings
                )
                pinned = re.sub(
                    r"uses:\s*(actions/setup-python@[0-9a-f]{40}\s*#\s*v\S+)",
                    rf"{spelling} \1",
                    self.text,
                    count=1,
                )
                self.assertNotEqual(pinned, self.text)
                self.assertEqual(workflow_pin_findings(pinned), [])

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

    def test_pre_push_hook_is_executable_and_runs_the_gate_with_one_history_scan(self):
        self.assertTrue(PRE_PUSH.is_file())
        self.assertTrue(PRE_PUSH.stat().st_mode & stat.S_IXUSR)
        hook = PRE_PUSH.read_text()
        self.assertIn("set -euo pipefail", hook)
        # The hook delegates the full-history scan to Make's dependency graph:
        # a separate `make history-check` here would duplicate the most
        # expensive gate, which `release-check` already runs as a prerequisite.
        self.assertIn("make release-check", hook)
        self.assertNotIn("make history-check", hook)
        # A second gate invocation, or the scanner called directly from the
        # hook, would run the full-history scan again outside the dependency
        # graph; the count assertion keeps the single-invocation contract.
        self.assertEqual(hook.count("make release-check"), 1)
        self.assertNotIn("public_history.py", hook)
        # The combined gate must still expand to exactly one full-history scan.
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

    def test_dependabot_updates_github_actions_weekly(self):
        dependabot = DEPENDABOT.read_text()
        self.assertIn("version: 2", dependabot)
        self.assertIn('package-ecosystem: "github-actions"', dependabot)
        self.assertIn('interval: "weekly"', dependabot)
        self.assertIn('directory: "/"', dependabot)


if __name__ == "__main__":
    unittest.main()
