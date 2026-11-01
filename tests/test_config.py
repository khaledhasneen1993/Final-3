import unittest
import config
from cascade_final3_paper import Final3PaperHunter


class ConfigTests(unittest.TestCase):
    def test_only_current_strategy_is_exposed(self):
        self.assertEqual(config.HARD_STOP_PCT, 0.028)
        self.assertEqual(config.settings()["STOP_LOSS_PCT"], 0.028)
        self.assertEqual(config.settings()["partial_level_1"], 0.012)
        self.assertEqual(config.settings()["partial_level_2"], 0.02)
        for name in ["add_position", "apply_tight_stop", "try_open_reversal"]:
            self.assertFalse(hasattr(Final3PaperHunter, name))


if __name__ == "__main__":
    unittest.main()
