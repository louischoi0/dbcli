import unittest

from dbcli.ui import FLASH_DIP, FLASH_FADE, FLASH_PERIOD, FLASH_TIME, blend, flash_level


class FlashTest(unittest.TestCase):
    def test_pulses_from_the_start(self):
        self.assertAlmostEqual(flash_level(0, None, 0), 1.0)  # lit at once
        self.assertAlmostEqual(flash_level(0, None, FLASH_PERIOD / 2), 1 - FLASH_DIP)  # the dip
        self.assertAlmostEqual(flash_level(0, None, FLASH_PERIOD), 1.0)  # and back, while it runs
        self.assertAlmostEqual(flash_level(0, 0, FLASH_PERIOD / 2), 1 - FLASH_DIP)  # also after

    def test_fades_out_in_the_last_second_then_goes(self):
        fade_at = FLASH_TIME - FLASH_FADE
        self.assertAlmostEqual(flash_level(0, 0, fade_at), 1.0)
        self.assertLess(flash_level(0, 0, fade_at + FLASH_FADE * 0.9), 0.05)
        self.assertIsNone(flash_level(0, 0, FLASH_TIME))
        self.assertEqual(FLASH_TIME, 3.0)

    def test_blend(self):
        self.assertEqual(blend("#ffffff", "#000000", 1), "#ffffff")
        self.assertEqual(blend("#ffffff", "#000000", 0), "#000000")
        self.assertEqual(blend("#ff0000", "#000000", 0.5), "#800000")


if __name__ == "__main__":
    unittest.main()
