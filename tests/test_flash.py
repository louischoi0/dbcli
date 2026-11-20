import unittest

from dbcli.ui import FLASH_DIP, FLASH_FADE, FLASH_MIN, FLASH_PERIOD, blend, flash_level


class FlashTest(unittest.TestCase):
    def test_pulses_from_the_start(self):
        self.assertAlmostEqual(flash_level(0, None, 0), 1.0)  # lit at once
        self.assertAlmostEqual(flash_level(0, None, FLASH_PERIOD / 2), 1 - FLASH_DIP)  # the dip
        self.assertAlmostEqual(flash_level(0, None, FLASH_PERIOD), 1.0)  # and back

    def test_a_quick_run_lasts_the_minimum(self):
        self.assertEqual(FLASH_MIN, 1.0)
        fade_at = FLASH_MIN - FLASH_FADE
        # done at once: until the fade it is the pulse alone (not dimmed yet)
        self.assertAlmostEqual(flash_level(0, 0.01, fade_at), flash_level(0, None, fade_at))
        self.assertLess(flash_level(0, 0.01, fade_at + FLASH_FADE * 0.9), 0.05)
        self.assertIsNone(flash_level(0, 0.01, FLASH_MIN))

    def test_a_long_run_pulses_until_it_ends_then_fades(self):
        self.assertIsNotNone(flash_level(0, None, 60.0))  # a minute in, still running: still on
        done = 30.0
        self.assertAlmostEqual(flash_level(0, done, done), 1.0)  # whole seconds in: at the top of a pulse
        self.assertIsNotNone(flash_level(0, done, done + FLASH_FADE * 0.5))
        self.assertIsNone(flash_level(0, done, done + FLASH_FADE))

    def test_blend(self):
        self.assertEqual(blend("#ffffff", "#000000", 1), "#ffffff")
        self.assertEqual(blend("#ffffff", "#000000", 0), "#000000")
        self.assertEqual(blend("#ff0000", "#000000", 0.5), "#800000")


if __name__ == "__main__":
    unittest.main()
