"""Проверки общего ДРР: сбор заказов, распределение оборота и правила."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wbads import analytics, db, demo  # noqa: E402
from wbads.analytics import _attributed_revenue  # noqa: E402
from wbads.collector import normalize_order  # noqa: E402
from wbads.config import Config, Thresholds  # noqa: E402
from wbads.metrics import build_series, sum_rows  # noqa: E402
from wbads.rules import Context, diagnose  # noqa: E402

DATE_FROM, DATE_TO = "2026-08-20", "2026-08-26"


def order(srid: str, day: str = "2026-08-20", nm_id: int = 1,
          price: float = 1000.0, cancel: bool = False) -> dict:
    return {
        "srid": srid, "date": f"{day}T10:00:00", "lastChangeDate": f"{day}T11:00:00",
        "nmId": nm_id, "supplierArticle": "ART", "brand": "B", "subject": "S",
        "warehouseName": "Коледино", "regionName": "Москва",
        "totalPrice": price * 2, "discountPercent": 50, "priceWithDisc": price,
        "finishedPrice": price * 0.9, "isCancel": cancel,
    }


class TestNormalizeOrder(unittest.TestCase):
    def test_maps_fields(self):
        row = normalize_order(order("a1"), "now")
        self.assertEqual(row["srid"], "a1")
        self.assertEqual(row["date"], "2026-08-20")   # время отброшено
        self.assertEqual(row["price_with_disc"], 1000.0)
        self.assertEqual(row["is_cancel"], 0)

    def test_computes_price_when_wb_omits_it(self):
        """priceWithDisc приходит не всегда — считаем из цены и скидки."""
        raw = order("a2")
        del raw["priceWithDisc"]
        row = normalize_order(raw, "now")
        self.assertEqual(row["price_with_disc"], 1000.0)  # 2000 × (1 − 50%)

    def test_falls_back_to_order_number_without_srid(self):
        raw = order("")
        raw.pop("srid")
        raw["gNumber"] = "777"
        raw["barcode"] = "bc"
        row = normalize_order(raw, "now")
        self.assertEqual(row["srid"], "777:1:bc")

    def test_rejects_row_without_any_key(self):
        self.assertIsNone(normalize_order({"date": "2026-08-20"}, "now"))


class TestOrderStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init_db(Path(self.tmp.name) / "o.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def save(self, raws):
        db.upsert_orders(self.conn, [normalize_order(r, "now") for r in raws])
        self.conn.commit()

    def test_repeated_collect_does_not_duplicate(self):
        self.save([order("a1"), order("a2")])
        self.save([order("a1"), order("a2")])
        totals = db.orders_totals(self.conn, DATE_FROM, DATE_TO)
        self.assertEqual(totals["orders"], 2)

    def test_cancellation_arriving_later_is_applied(self):
        """Заказ отменили на следующий день — сумма должна уменьшиться.

        Ради этого заказы и хранятся построчно, а не агрегатами.
        """
        self.save([order("a1", price=1000), order("a2", price=1000)])
        self.assertEqual(db.orders_totals(self.conn, DATE_FROM, DATE_TO)["revenue"], 2000)

        self.save([order("a1", price=1000, cancel=True)])
        totals = db.orders_totals(self.conn, DATE_FROM, DATE_TO)
        self.assertEqual(totals["revenue"], 1000)
        self.assertEqual(totals["orders"], 1)
        self.assertEqual(totals["cancels"], 1)

    def test_cancelled_orders_excluded_from_nm_rollup(self):
        self.save([order("a1", nm_id=5), order("a2", nm_id=5, cancel=True)])
        rows = db.orders_by_nm(self.conn, DATE_FROM, DATE_TO)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders"], 1)

    def test_price_field_is_selectable(self):
        self.save([order("a1", price=1000)])
        with_disc = db.orders_totals(self.conn, DATE_FROM, DATE_TO, "price_with_disc")
        finished = db.orders_totals(self.conn, DATE_FROM, DATE_TO, "finished_price")
        self.assertEqual(with_disc["revenue"], 1000)
        self.assertEqual(finished["revenue"], 900)

    def test_unknown_price_field_falls_back(self):
        """Подстановка чужого имени столбца не должна доходить до SQL."""
        self.save([order("a1", price=1000)])
        totals = db.orders_totals(self.conn, DATE_FROM, DATE_TO, "1; DROP TABLE orders_raw")
        self.assertEqual(totals["revenue"], 1000)


class TestAttribution(unittest.TestCase):
    """Оборот артикула делится между кампаниями по их расходу на него."""

    def test_single_campaign_takes_whole_revenue(self):
        result = _attributed_revenue({1: 100.0}, {1: 100.0}, {1: {"revenue": 5000.0}})
        self.assertAlmostEqual(result, 5000.0)

    def test_two_campaigns_split_by_spend(self):
        article_spend = {1: 100.0}
        orders = {1: {"revenue": 5000.0}}
        first = _attributed_revenue({1: 75.0}, article_spend, orders)
        second = _attributed_revenue({1: 25.0}, article_spend, orders)
        self.assertAlmostEqual(first, 3750.0)
        self.assertAlmostEqual(second, 1250.0)
        # Сумма долей равна обороту артикула — иначе общий ДРР был бы занижен
        self.assertAlmostEqual(first + second, 5000.0)

    def test_article_without_spend_is_skipped(self):
        self.assertEqual(_attributed_revenue({1: 0.0}, {1: 0.0}, {1: {"revenue": 900.0}}), 0.0)


def week(**kw):
    from wbads.metrics import daterange
    base = {"views": 0, "clicks": 0, "atbs": 0, "orders": 0,
            "shks": 0, "spend": 0.0, "revenue": 0.0}
    return [{**base, **kw, "date": d} for d in daterange(DATE_FROM, DATE_TO)]


def ctx(rows, total_revenue, orders_available=True, thresholds=None):
    return Context(
        campaign={"advert_id": 1, "name": "Тест", "status": 9, "type": 8},
        series=build_series(rows, DATE_FROM, DATE_TO),
        current=sum_rows(rows), previous=sum_rows([]),
        thresholds=thresholds or Thresholds(),
        total_revenue=total_revenue, orders_available=orders_available,
    )


class TestTotalDrrRules(unittest.TestCase):
    def codes(self, c):
        return {f["code"] for f in diagnose(c)["findings"]}

    def test_ads_lift_organic_when_total_drr_is_fine(self):
        """Рекламный ДРР 40%, общий 8% — кампанию резать нельзя."""
        rows = week(views=10000, clicks=200, atbs=20, orders=5,
                    spend=400.0, revenue=1000.0)       # рекламный ДРР 40%
        found = self.codes(ctx(rows, total_revenue=35000.0))  # общий ≈ 8%
        self.assertIn("ads_lift_organic", found)
        self.assertNotIn("total_drr_high", found)

    def test_total_drr_high_when_organic_does_not_save_it(self):
        rows = week(views=10000, clicks=200, atbs=20, orders=5,
                    spend=400.0, revenue=1000.0)
        found = self.codes(ctx(rows, total_revenue=1200.0))  # общий ≈ 233%
        self.assertIn("total_drr_high", found)
        self.assertNotIn("ads_lift_organic", found)

    def test_no_organic_flagged(self):
        """Оборот почти весь рекламный — без рекламы продажи встанут."""
        rows = week(views=10000, clicks=200, atbs=20, orders=5,
                    spend=400.0, revenue=4000.0)
        found = self.codes(ctx(rows, total_revenue=4200.0))  # органика 5%
        self.assertIn("no_organic", found)

    def test_silent_without_orders_data(self):
        """Без категории «Статистика» правила по обороту молчат, а не врут."""
        rows = week(views=10000, clicks=200, atbs=20, orders=5,
                    spend=400.0, revenue=1000.0)
        found = self.codes(ctx(rows, total_revenue=0.0, orders_available=False))
        for code in ("total_drr_high", "ads_lift_organic", "no_organic"):
            self.assertNotIn(code, found)


class TestReportWithOrders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "r.db"
        cls.conn = db.init_db(cls.path)
        demo.generate(cls.conn, days=40)
        cls.cfg = Config(db_path=cls.path, thresholds=Thresholds())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def report(self):
        date_from, date_to = analytics.default_period(self.conn, 7)
        return analytics.build_report(self.conn, date_from, date_to, Thresholds())

    def test_account_total_drr_is_lower_than_ad_drr(self):
        """Органика увеличивает знаменатель, поэтому общий ДРР ниже рекламного."""
        r = self.report()
        self.assertTrue(r["orders"]["available"])
        self.assertLess(r["orders"]["total_drr"], r["totals"]["drr"])

    def test_account_total_drr_matches_formula(self):
        r = self.report()
        expected = r["totals"]["spend"] / r["orders"]["revenue"] * 100
        self.assertAlmostEqual(r["orders"]["total_drr"], expected, places=6)

    def test_campaign_total_revenue_does_not_exceed_account(self):
        """Доли кампаний не должны в сумме превышать оборот кабинета."""
        r = self.report()
        attributed = sum(c["total_revenue"] for c in r["campaigns"])
        self.assertLessEqual(round(attributed, 2), round(r["orders"]["revenue"], 2) + 1)

    def test_out_of_stock_article_has_no_orders(self):
        """У свечи кончился остаток: расход есть, заказов нет вообще."""
        date_from, date_to = analytics.default_period(self.conn, 7)
        detail = analytics.campaign_detail(self.conn, 21400103, date_from,
                                           date_to, Thresholds())
        self.assertGreater(detail["metrics"]["spend"], 0)
        self.assertEqual(detail["total_revenue"], 0)
        for item in detail["nm_items"]:
            self.assertEqual(item["total_orders"], 0)

    def test_articles_carry_organic_share(self):
        date_from, date_to = analytics.default_period(self.conn, 7)
        detail = analytics.campaign_detail(self.conn, 21400101, date_from,
                                           date_to, Thresholds())
        item = detail["nm_items"][0]
        self.assertGreater(item["total_orders"], item["orders"])
        self.assertGreater(item["organic_share"], 0)

    def test_money_at_risk_uses_total_revenue(self):
        """Переплата считается от всего оборота — иначе спорит с общим ДРР."""
        r = self.report()
        self.assertEqual(r["summary"]["risk_basis"], "весь оборот")


class TestReportWithoutOrders(unittest.TestCase):
    """Токен без категории «Статистика»: реклама считается, общий ДРР — нет."""

    def test_report_works_and_hides_total_drr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "noorders.db"
            conn = db.init_db(path)
            demo.generate(conn, days=30)
            conn.execute("DELETE FROM orders_raw")
            conn.commit()

            date_from, date_to = analytics.default_period(conn, 7)
            report = analytics.build_report(conn, date_from, date_to, Thresholds())
            self.assertFalse(report["orders"]["available"])
            self.assertEqual(report["orders"]["revenue"], 0)
            self.assertGreater(report["totals"]["spend"], 0)
            self.assertEqual(report["summary"]["risk_basis"], "выручка с рекламы")
            for campaign in report["campaigns"]:
                self.assertFalse(campaign["orders_available"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
