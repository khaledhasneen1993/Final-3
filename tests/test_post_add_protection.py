"""The removed averaging system must not remain active in Final-3."""

import unittest
from cascade_final2_paper import Final2PaperHunter


class RemovedScaleInTests(unittest.TestCase):
    def test_legacy_scale_methods_removed(self):
        for name in ("add_position", "partial_close", "apply_tight_stop"):
            self.assertFalse(hasattr(Final2PaperHunter, name))


if __name__ == "__main__":
    unittest.main()
