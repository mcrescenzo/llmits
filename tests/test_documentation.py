"""Public documentation contracts for security reporting and release readiness."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class PublicDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = (REPO_ROOT / "README.md").read_text()
        cls.security = (REPO_ROOT / "SECURITY.md").read_text()
        cls.contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        cls.agents = (REPO_ROOT / "AGENTS.md").read_text()
        cls.claude = (REPO_ROOT / "CLAUDE.md").read_text()

    def test_security_policy_has_private_reporting_and_safe_public_fallback(self) -> None:
        self.assertNotIn("This is a private tool", self.security)
        self.assertIn(
            "https://github.com/mcrescenzo/llmits/security/advisories/new",
            self.security,
        )
        self.assertIn("https://github.com/mcrescenzo/llmits/issues", self.security)
        self.assertIn("Do not post exploit details", self.security)

    def test_security_policy_names_supported_python_versions(self) -> None:
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(version, self.security)

    def test_prepublication_checklist_does_not_claim_host_settings_are_enabled(self) -> None:
        self.assertIn("does not claim they are currently enabled", self.security)
        for safeguard in (
            "private vulnerability reporting",
            "secret scanning and push protection",
            "Protect the release branch",
            "artifact SHA-256",
        ):
            self.assertIn(safeguard, self.security)

    def test_readme_describes_runtime_writes_synthetic_fixtures_and_license(self) -> None:
        for contract in (
            "`__pycache__`",
            "`dist/llmits`",
            "provider-response fixtures are synthetic",
            "`llmits/LICENSE`",
            "development-only dependencies",
        ):
            self.assertIn(contract, self.readme)

    def test_security_policy_describes_remediated_boundaries(self) -> None:
        for contract in (
            "SSLKEYLOGFILE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "provider-derived limit names",
            "transport exception text",
            "Provider-response fixtures under `tests/fixtures/` are synthetic",
            "complete MIT notice",
        ):
            self.assertIn(contract, self.security)

    def test_agent_guidance_is_aligned_and_portable(self) -> None:
        self.assertEqual(self.agents, self.claude)
        combined = self.agents + self.contributing
        for machine_specific in (
            "bd prime",
            ".beads",
            "home-directory skill",
            "docs/superpowers",
            "/home/",
            "/ssd/",
        ):
            if machine_specific == "home-directory skill":
                self.assertIn("does not require", combined)
            else:
                self.assertNotIn(machine_specific, combined)
        for command in ("make check", "make lint", "make typecheck", "make release-check"):
            self.assertIn(command, self.agents)
            self.assertIn(command, self.contributing)

    def test_project_owned_markdown_links_resolve(self) -> None:
        documents = ("README.md", "SECURITY.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md")
        for document in documents:
            text = (REPO_ROOT / document).read_text()
            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
                if "://" in target or target.startswith(("#", "mailto:")):
                    continue
                path = target.split("#", 1)[0]
                self.assertTrue((REPO_ROOT / path).exists(), f"{document}: missing link {target}")


if __name__ == "__main__":
    unittest.main()
