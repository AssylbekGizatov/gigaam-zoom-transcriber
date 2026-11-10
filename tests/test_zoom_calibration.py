import unittest

from gigaam_zoom.zoom_calibration import normalized_selection


class CalibrationTests(unittest.TestCase):
    def test_scaled_preview_is_client_relative_in_both_drag_directions(self):
        expected = (.1, .8, .4, .1)
        forward = normalized_selection((80, 480), (400, 540), (800, 600))
        backward = normalized_selection((400, 540), (80, 480), (800, 600))
        self.assertEqual(forward, expected)
        self.assertEqual(backward, expected)
        self.assertEqual(normalized_selection((160, 960), (800, 1080), (1600, 1200)), expected)

    def test_drag_beyond_preview_clamps_to_client(self):
        self.assertEqual(normalized_selection((-20, 80), (120, 140), (100, 100)), (0, .8, 1, .2))

    def test_accidental_click_empty_preview_and_nonfinite_coordinates_rejected(self):
        for start, end, size in [((20, 20), (21, 21), (100, 100)),
                                 ((0, 0), (50, 50), (0, 100)),
                                 ((float("nan"), 0), (50, 50), (100, 100)),
                                 ((0, 0), (float("inf"), 50), (100, 100))]:
            with self.subTest(start=start, end=end, size=size), self.assertRaises(ValueError):
                normalized_selection(start, end, size)


if __name__ == "__main__":
    unittest.main()
