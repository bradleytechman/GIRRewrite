import unittest

from community_rules import caps_percent, has_invite, is_image_attachment, recent_count


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

    def test_image_attachment_recognizes_mime_and_filename_fallback(self):
        self.assertTrue(is_image_attachment("image/png", "upload.bin"))
        self.assertTrue(is_image_attachment(None, "Screenshot.HEIC"))
        self.assertFalse(is_image_attachment("application/pdf", "statement.pdf"))


if __name__ == "__main__":
    unittest.main()
