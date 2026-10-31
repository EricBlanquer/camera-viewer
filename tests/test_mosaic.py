import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app


class MosaicGridTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app.precompute_mosaics(2560, 1440)

    def _coverage(self, cells, spans):
        covered = sum(rowspan * colspan for _, _, rowspan, colspan in spans)
        return covered / (cells * cells)

    def _tiles_are_16_9(self, cells, spans, width, height):
        for _, _, rowspan, colspan in spans:
            tile_w = colspan / cells * width
            tile_h = rowspan / cells * height
            self.assertAlmostEqual(tile_w / tile_h, app.MOSAIC_TILE_ASPECT, delta=0.03)

    def test_six_cameras_fill_a_landscape_screen_with_one_big_tile(self) -> None:
        cells, spans = app.mosaic_grid(6, 2560, 1440)
        self.assertEqual(cells, 3)
        self.assertEqual(len(spans), 6)
        self.assertAlmostEqual(self._coverage(cells, spans), 1.0, delta=0.001)
        self._tiles_are_16_9(cells, spans, 2560, 1440)
        self.assertEqual(spans[0][2:], (2, 2))  # the largest tile comes first (camera order)
        self.assertEqual(sorted(s[2:] for s in spans), [(1, 1)] * 5 + [(2, 2)])

    def test_seven_cameras_fill_the_screen(self) -> None:
        cells, spans = app.mosaic_grid(7, 2560, 1440)
        self.assertEqual(cells, 4)
        self.assertEqual(len(spans), 7)
        self.assertAlmostEqual(self._coverage(cells, spans), 1.0, delta=0.001)
        self._tiles_are_16_9(cells, spans, 2560, 1440)
        self.assertEqual(sorted(s[2:] for s in spans), [(1, 1)] * 4 + [(2, 2)] * 3)

    def test_tiles_stay_inside_the_grid(self) -> None:
        for count in (4, 6, 7, 8, 9):
            cells, spans = app.mosaic_grid(count, 2560, 1440)
            for row, column, rowspan, colspan in spans:
                self.assertTrue(0 <= row and row + rowspan <= cells)
                self.assertTrue(0 <= column and column + colspan <= cells)

    def test_counts_without_a_clean_fill_fall_back(self) -> None:
        self.assertIsNone(app.mosaic_grid(1, 2560, 1440))
        self.assertIsNone(app.mosaic_grid(2, 2560, 1440))
        self.assertIsNone(app.mosaic_grid(3, 2560, 1440))
        self.assertIsNone(app.mosaic_grid(5, 2560, 1440))


if __name__ == "__main__":
    unittest.main()
