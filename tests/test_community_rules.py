import unittest

from community_rules import caps_percent, has_invite, recent_count


class CommunityRuleTests(unittest.TestCase):
    def test_caps_ignores_numbers_and_punctuation(self):
        self.assertEqual(caps_percent("THIS is 123!"), 67)
        self.assertEqual(caps_percent("123!"), 0)

    def test_discord_invites_are_detected(self):
        self.assertTrue(has_invite("join https://discord.gg/example"))
        self.assertTrue(has_invite("DISCORD.COM/invite/example"))
        self.assertFalse(has_invite("https://example.com"))

    def test_only_events_inside_window_count(self):
        self.assertEqual(recent_count([80, 91, 95, 100], 100, 10), 3)


if __name__ == "__main__":
    unittest.main()
