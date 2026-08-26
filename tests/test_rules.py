"""Проверки движка диагностики: правило должно срабатывать тогда,
когда проблема есть, и молчать, когда её нет."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wbads.config import Thresholds  # noqa: E402
from wbads.metrics import build_series  # noqa: E402
from wbads.rules import CRITICAL, Context, diagnose  # noqa: E402

DATE_FROM, DATE_TO = "2026-08-20", "2026-08-26"


def day(date, **kw):
    base = {"date": date, "views": 0, "clicks": 0, "atbs": 0,
            "orders": 0, "shks": 0, "spend": 0.0, "revenue": 0.0}
    base.update(kw)
    return base


def week(**kw):
    """Семь одинаковых дней — ровный период без сюрпризов."""
    from wbads.metrics import daterange
    return [day(d, **kw) for d in daterange(DATE_FROM, DATE_TO)]


def make(rows, previous=None, status=9, daily_budget=0.0, thresholds=None):
    from wbads.metrics import sum_rows
    return Context(
        campaign={"advert_id": 1, "name": "Тест", "status": status,
                  "type": 8, "daily_budget": daily_budget},
        series=build_series(rows, DATE_FROM, DATE_TO),
        current=sum_rows(rows),
        previous=sum_rows(previous or []),
        thresholds=thresholds or Thresholds(),
    )


def codes(ctx):
    return {f["code"] for f in diagnose(ctx)["findings"]}


class TestSpendWithoutOrders(unittest.TestCase):
    def test_fires_when_money_burns_with_no_orders(self):
        rows = week(views=5000, clicks=100, atbs=3, orders=0, spend=400.0)
        self.assertIn("spend_without_orders", codes(make(rows)))

    def test_silent_when_orders_exist(self):
        rows = week(views=5000, clicks=100, atbs=10, orders=3,
                    spend=400.0, revenue=6000.0)
        self.assertNotIn("spend_without_orders", codes(make(rows)))

    def test_silent_below_spend_threshold(self):
        """Потратили мало — тревожить рано."""
        rows = week(views=200, clicks=5, atbs=0, orders=0, spend=20.0)
        self.assertNotIn("spend_without_orders", codes(make(rows)))


class TestDrr(unittest.TestCase):
    def test_critical_above_target_times_multiplier(self):
        # ДРР = 3500 / 10000 = 35% при цели 15% → критично
        rows = week(views=5000, clicks=100, atbs=20, orders=5,
                    spend=500.0, revenue=1428.6)
        self.assertIn("drr_critical", codes(make(rows)))

    def test_warning_between_target_and_critical(self):
        # ДРР ≈ 18% — выше цели 15%, но ниже 22.5%
        rows = week(views=5000, clicks=100, atbs=20, orders=5,
                    spend=180.0, revenue=1000.0)
        found = codes(make(rows))
        self.assertIn("drr_high", found)
        self.assertNotIn("drr_critical", found)

    def test_silent_when_within_target(self):
        rows = week(views=5000, clicks=100, atbs=20, orders=5,
                    spend=100.0, revenue=1000.0)
        found = codes(make(rows))
        self.assertNotIn("drr_high", found)
        self.assertNotIn("drr_critical", found)

    def test_target_is_configurable(self):
        """При жёсткой цели 10% те же цифры уже проблема."""
        rows = week(views=5000, clicks=100, atbs=20, orders=5,
                    spend=120.0, revenue=1000.0)
        self.assertNotIn("drr_high", codes(make(rows)))
        strict = Thresholds(target_drr=10.0)
        self.assertIn("drr_high", codes(make(rows, thresholds=strict)))


class TestFunnel(unittest.TestCase):
    def test_low_cart_conversion(self):
        rows = week(views=20000, clicks=500, atbs=5, orders=1,
                    spend=300.0, revenue=2000.0)
        self.assertIn("cr_cart_low", codes(make(rows)))

    def test_low_order_conversion(self):
        rows = week(views=20000, clicks=500, atbs=100, orders=5,
                    spend=300.0, revenue=5000.0)
        self.assertIn("cr_order_low", codes(make(rows)))

    def test_healthy_funnel_is_silent(self):
        rows = week(views=20000, clicks=440, atbs=44, orders=15,
                    spend=300.0, revenue=9000.0)
        found = codes(make(rows))
        self.assertNotIn("cr_cart_low", found)
        self.assertNotIn("cr_order_low", found)


class TestDynamics(unittest.TestCase):
    def test_cpc_spike(self):
        current = week(views=10000, clicks=100, atbs=10, orders=3,
                       spend=1500.0, revenue=9000.0)   # CPC 15
        previous = week(views=10000, clicks=100, atbs=10, orders=3,
                        spend=800.0, revenue=9000.0)   # CPC 8
        self.assertIn("cpc_spike", codes(make(current, previous)))

    def test_ctr_drop(self):
        current = week(views=10000, clicks=50, atbs=5, orders=2,
                       spend=500.0, revenue=4000.0)
        previous = week(views=10000, clicks=200, atbs=20, orders=8,
                        spend=500.0, revenue=16000.0)
        self.assertIn("ctr_drop", codes(make(current, previous)))

    def test_views_collapse(self):
        current = week(views=2000, clicks=40, atbs=4, orders=2,
                       spend=400.0, revenue=3000.0)
        previous = week(views=20000, clicks=400, atbs=40, orders=20,
                        spend=4000.0, revenue=30000.0)
        self.assertIn("views_collapse", codes(make(current, previous)))

    def test_spend_up_orders_flat(self):
        current = week(views=20000, clicks=400, atbs=40, orders=10,
                       spend=1000.0, revenue=8000.0)
        previous = week(views=10000, clicks=200, atbs=20, orders=10,
                        spend=500.0, revenue=8000.0)
        self.assertIn("spend_up_orders_flat", codes(make(current, previous)))


class TestSilence(unittest.TestCase):
    def test_active_campaign_without_impressions(self):
        """Статус «идут показы», а показов нет — это критично."""
        from wbads.metrics import daterange
        days = daterange(DATE_FROM, DATE_TO)
        rows = [day(d, views=3000, clicks=60, atbs=6, orders=2,
                    spend=400.0, revenue=3000.0) for d in days[:2]]
        rows += [day(d) for d in days[2:]]
        result = diagnose(make(rows))
        found = {f["code"] for f in result["findings"]}
        self.assertIn("no_impressions", found)
        self.assertEqual(result["verdict"], CRITICAL)

    def test_paused_campaign_is_not_alarmed(self):
        rows = [day(d) for d in
                ["2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23",
                 "2026-08-24", "2026-08-25", "2026-08-26"]]
        result = diagnose(make(rows, status=11))
        self.assertNotIn("no_impressions", {f["code"] for f in result["findings"]})
        self.assertEqual(result["verdict"], "idle")


class TestOpportunity(unittest.TestCase):
    def test_scale_up_when_better_than_target(self):
        # ДРР 5% при цели 15% → есть куда расти
        rows = week(views=10000, clicks=200, atbs=20, orders=8,
                    spend=500.0, revenue=10000.0)
        result = diagnose(make(rows))
        self.assertIn("scale_up", {f["code"] for f in result["findings"]})
        self.assertEqual(result["verdict"], "opportunity")

    def test_budget_cap_flagged_only_when_profitable(self):
        """Упирается в бюджет и при этом в цели — это возможность, а не проблема."""
        rows = week(views=10000, clicks=200, atbs=20, orders=8,
                    spend=1000.0, revenue=12000.0)
        self.assertIn("budget_capped", codes(make(rows, daily_budget=1000.0)))

    def test_budget_cap_silent_when_drr_bad(self):
        rows = week(views=10000, clicks=200, atbs=20, orders=8,
                    spend=1000.0, revenue=2000.0)
        self.assertNotIn("budget_capped", codes(make(rows, daily_budget=1000.0)))


class TestRootCause(unittest.TestCase):
    def test_drr_finding_points_at_the_real_cause(self):
        """Если проблема в корзине, ДРР-совет не должен звать снижать ставку."""
        rows = week(views=20000, clicks=500, atbs=5, orders=1,
                    spend=800.0, revenue=2000.0)
        result = diagnose(make(rows))
        drr = next(f for f in result["findings"] if f["code"].startswith("drr_"))
        self.assertIn("карточк", drr["actions"][0].lower())


class TestVerdict(unittest.TestCase):
    def test_healthy_campaign_scores_full_health(self):
        rows = week(views=10000, clicks=220, atbs=25, orders=9,
                    spend=500.0, revenue=5000.0)
        result = diagnose(make(rows))
        self.assertEqual(result["health"], 100)
        self.assertEqual(result["verdict"], "ok")

    def test_critical_lowers_health(self):
        rows = week(views=5000, clicks=100, atbs=3, orders=0, spend=400.0)
        self.assertLess(diagnose(make(rows))["health"], 60)

    def test_findings_sorted_by_severity(self):
        rows = week(views=5000, clicks=100, atbs=3, orders=0, spend=400.0)
        findings = diagnose(make(rows))["findings"]
        severities = [f["severity"] for f in findings]
        self.assertEqual(severities[0], CRITICAL)

    def test_every_finding_carries_actions(self):
        """Находка без рекомендации бесполезна — их не должно быть."""
        rows = week(views=5000, clicks=100, atbs=3, orders=0, spend=400.0)
        for finding in diagnose(make(rows))["findings"]:
            self.assertTrue(finding["actions"], finding["code"])


if __name__ == "__main__":
    unittest.main()
