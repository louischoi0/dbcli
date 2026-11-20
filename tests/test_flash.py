import unittest

from dbcli.ui import FLASH_BLINK, FLASH_HOLD, flash_on


class FlashTest(unittest.TestCase):
    def test_lit_while_running_and_for_the_hold(self):
        self.assertTrue(flash_on(None, 99))  # still running
        self.assertTrue(flash_on(10, 10))
        self.assertTrue(flash_on(10, 10 + FLASH_HOLD - 0.1))

    def test_then_blinks_three_times_and_goes(self):
        states = [flash_on(10, 10 + FLASH_HOLD + 0.01 + FLASH_BLINK * k) for k in range(7)]
        self.assertEqual(states, [False, True, False, True, False, True, None])


if __name__ == "__main__":
    unittest.main()
