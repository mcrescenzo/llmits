"""Tests for the provider registry: llmits.providers.display_name / is_known."""
import unittest

from llmits import providers


class DisplayNameTests(unittest.TestCase):
    def test_known_providers_return_their_configured_display_name(self):
        self.assertEqual(providers.display_name("claude"), "Claude")
        self.assertEqual(providers.display_name("codex"), "Codex")
        self.assertEqual(providers.display_name("zai"), "Z.AI")
        self.assertEqual(providers.display_name("kimi"), "Kimi")
        self.assertEqual(providers.display_name("opencode"), "OpenCode")

    def test_every_registered_provider_has_an_explicit_display_name(self):
        # Completeness guard against registry drift: display names must cover
        # exactly the registered ids, and none may resolve to its own raw id
        # (which is what a missing mapping looks like to a caller). The
        # FETCHERS/PROVIDER_IDS correspondence is already asserted, more
        # strongly, by test_auth's credential-provider test.
        self.assertEqual(set(providers.DISPLAY_NAMES), set(providers.PROVIDER_IDS))
        for provider_id in providers.PROVIDER_IDS:
            with self.subTest(provider=provider_id):
                self.assertNotEqual(providers.display_name(provider_id), provider_id)

    def test_unknown_provider_falls_back_to_the_raw_id(self):
        self.assertEqual(providers.display_name("grok"), "grok")


class IsKnownTests(unittest.TestCase):
    def test_every_registered_provider_id_is_known(self):
        for provider_id in providers.PROVIDER_IDS:
            self.assertTrue(providers.is_known(provider_id))

    def test_unrecognized_provider_id_is_not_known(self):
        self.assertFalse(providers.is_known("grok"))


if __name__ == "__main__":
    unittest.main()
