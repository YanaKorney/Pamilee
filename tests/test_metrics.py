"""Проверки расчёта показателей."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wbads.metrics import (  # noqa: E402
    build_series, compare, derive, days_with_activity, moving_average,
    pct_change, safe_div, shift_window, trailing_zero_days, trend_slope,
)


class TestDerive(unittest.TestCase):
    def test_basic_kpi(self):
        kpi = derive({"views": 10000, "clicks": 200, "atbs": 20, "orders": 5,
                      "shks": 6, "spend": 1500, "revenue": 9000})
        self.assertAlmostEqual(kpi["ctr"], 2.0)
        self.assertAlmostEqual(kpi["cpc"], 7.5)
        self.assertAlmostEqual(kpi["cr_cart"], 10.0)
        self.assertAlmostEqual(kpi["cr_order"], 25.0)
        self.assertAlmostEqual(kpi["cpo"], 300.0)
        self.assertAlmostEqual(kpi["drr"], 1500 / 9000 * 100)
        self.assertAlmostEqual(kpi["roas"], 6.0)
        self.assertAlmostEqual(kpi["aov"], 1800.0)

    def test_zero_base_does_not_crash(self):
        """Нет показов и нет выручки — показатели просто нули, а не деление на ноль."""
        kpi = derive({"views": 0, "clicks": 0, "atbs": 0, "orders": 0,
                      "shks": 0, "spend": 0, "revenue": 0})
        self.assertEqual(kpi["ctr"], 0)
        self.assertEqual(kpi["drr"], 0)
        self.assertEqual(kpi["cpo"], 0)

    def test_spend_without_revenue_gives_zero_drr(self):
        """ДРР без выручки не определён — считаем нулём, а показываем прочерком."""
        kpi = derive({"views": 100, "clicks": 10, "atbs": 0, "orders": 0,
                      "shks": 0, "spend": 500, "revenue": 0})
        self.assertEqual(kpi["drr"], 0)
        self.assertEqual(kpi["roas"], 0)

    def test_safe_div(self):
        self.assertEqual(safe_div(10, 0), 0.0)
        self.assertEqual(safe_div(10, 0, default=1), 1)
        self.assertEqual(safe_div(10, 4), 2.5)


class TestWindows(unittest.TestCase):
    def test_previous_window_same_length(self):
        self.assertEqual(shift_window("2026-08-20", "2026-08-26"),
                         ("2026-08-13", "2026-08-19"))

    def test_single_day_window(self):
        self.assertEqual(shift_window("2026-08-26", "2026-08-26"),
                         ("2026-08-25", "2026-08-25"))

    def test_pct_change_without_base(self):
        self.assertIsNone(pct_change(10, 0))
        self.assertAlmostEqual(pct_change(150, 100), 50.0)
        self.assertAlmostEqual(pct_change(50, 100), -50.0)


class TestSeries(unittest.TestCase):
    def test_missing_days_become_zero_rows(self):
        """Дни без открутки обязаны попасть в ряд нулями — иначе тишину не увидеть."""
        rows = [{"date": "2026-08-20", "views": 100, "clicks": 5, "atbs": 1,
                 "orders": 1, "shks": 1, "spend": 50, "revenue": 900}]
        series = build_series(rows, "2026-08-20", "2026-08-24")
        self.assertEqual(len(series), 5)
        self.assertEqual(series[0]["views"], 100)
        self.assertEqual(series[4]["views"], 0)

    def test_same_day_rows_are_summed(self):
        rows = [
            {"date": "2026-08-20", "views": 100, "clicks": 5, "atbs": 1,
             "orders": 1, "shks": 1, "spend": 50, "revenue": 900},
            {"date": "2026-08-20", "views": 200, "clicks": 5, "atbs": 1,
             "orders": 1, "shks": 1, "spend": 50, "revenue": 900},
        ]
        series = build_series(rows, "2026-08-20", "2026-08-20")
        self.assertEqual(series[0]["views"], 300)
        self.assertEqual(series[0]["spend"], 100)

    def test_trailing_zero_days(self):
        series = build_series(
            [{"date": "2026-08-20", "views": 100, "clicks": 1, "atbs": 0,
              "orders": 0, "shks": 0, "spend": 10, "revenue": 0}],
            "2026-08-20", "2026-08-23")
        self.assertEqual(trailing_zero_days(series, "views"), 3)

    def test_days_with_activity(self):
        series = build_series(
            [{"date": "2026-08-20", "views": 1, "clicks": 0, "atbs": 0,
              "orders": 0, "shks": 0, "spend": 10, "revenue": 0}],
            "2026-08-20", "2026-08-22")
        self.assertEqual(days_with_activity(series), 1)


class TestTrend(unittest.TestCase):
    def test_slope_direction(self):
        self.assertGreater(trend_slope([1, 2, 3, 4, 5]), 0)
        self.assertLess(trend_slope([5, 4, 3, 2, 1]), 0)
        self.assertEqual(trend_slope([3, 3, 3]), 0)

    def test_slope_needs_two_points(self):
        self.assertEqual(trend_slope([7]), 0.0)

    def test_moving_average_smooths(self):
        self.assertEqual(moving_average([3, 3, 3], 3), [3.0, 3.0, 3.0])
        self.assertEqual(len(moving_average([1, 2, 3, 4], 2)), 4)


class TestCompare(unittest.TestCase):
    def test_compare_reports_delta(self):
        cur = {"views": 100, "clicks": 10, "atbs": 2, "orders": 1,
               "shks": 1, "spend": 200, "revenue": 1000}
        prev = {"views": 100, "clicks": 10, "atbs": 2, "orders": 1,
                "shks": 1, "spend": 100, "revenue": 1000}
        result = compare(cur, prev)
        self.assertAlmostEqual(result["spend"]["delta"], 100)
        self.assertAlmostEqual(result["spend"]["delta_pct"], 100.0)
        self.assertAlmostEqual(result["drr"]["current"], 20.0)
        self.assertAlmostEqual(result["drr"]["previous"], 10.0)


if __name__ == "__main__":
    unittest.main()
