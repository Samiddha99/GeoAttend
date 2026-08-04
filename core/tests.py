from django.test import SimpleTestCase

from core.utils import haversine_m, parse_batch_label, pct, valid_coords


class UtilsTests(SimpleTestCase):
    def test_haversine_known_distance(self):
        # 0.001° of latitude ≈ 111 m
        d = haversine_m(22.5726, 88.3639, 22.5736, 88.3639)
        self.assertAlmostEqual(d, 111.2, delta=1.0)

    def test_haversine_zero(self):
        self.assertEqual(round(haversine_m(1, 1, 1, 1), 6), 0.0)

    def test_valid_coords(self):
        self.assertTrue(valid_coords(22.5, 88.3))
        self.assertFalse(valid_coords(95, 88.3))
        self.assertFalse(valid_coords("abc", 88.3))
        self.assertFalse(valid_coords(None, None))

    def test_parse_batch_label(self):
        self.assertEqual(parse_batch_label("2022-26"), (2022, 2026, "2022-26"))
        self.assertEqual(parse_batch_label(" 2021-2025 "), (2021, 2025, "2021-25"))
        self.assertIsNone(parse_batch_label("batch one"))
        self.assertIsNone(parse_batch_label("2026-22"))

    def test_pct(self):
        self.assertEqual(pct(3, 4), 75.0)
        self.assertEqual(pct(0, 0), 0.0)
