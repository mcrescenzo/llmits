"""Tests for the provider registry: llmits.providers.display_name / is_known."""
import unittest

from llmits import providers


class DisplayNameTests(unittest.TestCase):
    def test_known_providers_return_their_configured_display_name(self):
        self.assertEqual(providers.display_name("claude"), "Claude")
        self.assertEqual(providers.display_name("codex"), "Codex")
        self.assertEqual(providers.display_name("zai"), "Z.AI")
        self.assertEqual(providers.display_name("opencode"), "OpenCode")

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
