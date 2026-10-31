import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app


class OptimalGridColumnsTest(unittest.TestCase):
    def test_landscape_screen_fills_width_without_a_centre_gap(self) -> None:
        # Six 16:9 views on a 16:9 screen: 3x2 ties 2x3 on tile size, prefer 3 columns to fill the width.
        self.assertEqual(app.optimal_grid_columns(6, 2560, 1440), 3)
        self.assertEqual(app.optimal_grid_columns(7, 2560, 1440), 3)

    def test_small_counts(self) -> None:
        self.assertEqual(app.optimal_grid_columns(1, 2560, 1440), 1)
        self.assertEqual(app.optimal_grid_columns(2, 2560, 1440), 2)
        self.assertEqual(app.optimal_grid_columns(4, 2560, 1440), 2)

    def test_degenerate_inputs(self) -> None:
        self.assertEqual(app.optimal_grid_columns(0, 2560, 1440), 1)
        self.assertEqual(app.optimal_grid_columns(5, 0, 0), 5)

    def test_portrait_screen_stacks(self) -> None:
        # On a tall screen a single column gives the largest 16:9 tiles.
        self.assertEqual(app.optimal_grid_columns(6, 1080, 1920), 1)


if __name__ == "__main__":
    unittest.main()
