"""Проверки сквозного пути: сбор → база → отчёт → выгрузка."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wbads import analytics, db, demo  # noqa: E402
from wbads.api import handle_export, handle_meta, handle_report  # noqa: E402
from wbads.collector import normalize_campaign, normalize_stats  # noqa: E402
from wbads.config import Config, Thresholds  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_campaign_fields_mapped(self):
        row = normalize_campaign(
            {"advertId": 42, "name": "  Кампания  ", "type": 9, "status": 11,
             "dailyBudget": 1500}, "2026-08-26T10:00:00")
        self.assertEqual(row["advert_id"], 42)
        self.assertEqual(row["name"], "Кампания")
        self.assertEqual(row["type_name"], "Аукцион")
        self.assertEqual(row["status_name"], "На паузе")
        self.assertEqual(row["daily_budget"], 1500.0)

    def test_stats_sum_across_platforms(self):
        """Один артикул приходит в нескольких площадках — их нужно сложить."""
        raw = {"advertId": 7, "days": [{
            "date": "2026-08-26T00:00:00+03:00",
            "views": 100, "clicks": 5, "atbs": 2, "orders": 1, "shks": 1,
            "sum": 45.5, "sum_price": 1200,
            "apps": [
                {"appType": 1, "nm": [{"nmId": 9, "name": "Товар", "views": 60,
                                       "clicks": 3, "atbs": 1, "orders": 1,
                                       "shks": 1, "sum": 27.3, "sum_price": 1200}]},
                {"appType": 32, "nm": [{"nmId": 9, "name": "Товар", "views": 40,
                                        "clicks": 2, "atbs": 1, "orders": 0,
                                        "shks": 0, "sum": 18.2, "sum_price": 0}]},
            ],
        }]}
        daily, nm = normalize_stats(raw, "now")
        self.assertEqual(daily[0]["date"], "2026-08-26")
        self.assertEqual(daily[0]["spend"], 45.5)
        self.assertEqual(daily[0]["revenue"], 1200.0)
        self.assertEqual(len(nm), 1)
        self.assertEqual(nm[0]["views"], 100)
        self.assertEqual(nm[0]["spend"], 45.5)

    def test_missing_fields_do_not_crash(self):
        daily, nm = normalize_stats({"advertId": 1, "days": [{"date": "2026-08-26"}]}, "now")
        self.assertEqual(daily[0]["views"], 0)
        self.assertEqual(nm, [])


class TestPipeline(unittest.TestCase):
    """Демо-данные проходят весь путь и дают осмысленный отчёт."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "test.db"
        cls.conn = db.init_db(cls.db_path)
        demo.generate(cls.conn, days=45)
        cls.cfg = Config(db_path=cls.db_path, thresholds=Thresholds())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_demo_fills_database(self):
        lo, hi = db.data_range(self.conn)
        self.assertIsNotNone(lo)
        self.assertEqual(len(db.list_campaigns(self.conn)), len(demo.CAMPAIGNS))

    def test_repeated_collect_does_not_duplicate(self):
        """Повторный сбор за те же дни обновляет строки, а не плодит их."""
        before = self.conn.execute("SELECT COUNT(*) FROM campaign_daily").fetchone()[0]
        demo.generate(self.conn, days=45)
        after = self.conn.execute("SELECT COUNT(*) FROM campaign_daily").fetchone()[0]
        self.assertEqual(before, after)

    def test_report_structure(self):
        date_from, date_to = analytics.default_period(self.conn, 7)
        report = analytics.build_report(self.conn, date_from, date_to, Thresholds())
        self.assertEqual(len(report["series"]), 7)
        self.assertGreater(report["totals"]["spend"], 0)
        self.assertIn("summary", report)
        self.assertEqual(report["period"]["prev_to"],
                         (analytics.date.fromisoformat(date_from) -
                          analytics.timedelta(days=1)).isoformat())

    def test_report_finds_planted_problems(self):
        """В демо намеренно заложены больные кампании — движок обязан их найти."""
        date_from, date_to = analytics.default_period(self.conn, 7)
        report = analytics.build_report(self.conn, date_from, date_to, Thresholds())
        found = {c["advert_id"]: {f["code"] for f in c["findings"]}
                 for c in report["campaigns"]}
        self.assertIn("spend_without_orders", found[21400103])  # свеча: нет остатка
        self.assertIn("ctr_drop", found[21400104])              # термокружка: фото
        self.assertIn("cr_cart_low", found[21400105])           # плед: карточка
        self.assertIn("no_impressions", found[21400107])        # ночник: баланс
        self.assertIn("cpc_spike", found[21400102])             # худи: аукцион
        self.assertGreater(report["summary"]["critical"], 0)

    def test_campaigns_sorted_by_urgency(self):
        date_from, date_to = analytics.default_period(self.conn, 7)
        report = analytics.build_report(self.conn, date_from, date_to, Thresholds())
        self.assertEqual(report["campaigns"][0]["verdict"], "critical")

    def test_campaign_detail_has_articles(self):
        date_from, date_to = analytics.default_period(self.conn, 7)
        detail = analytics.campaign_detail(self.conn, 21400105, date_from,
                                           date_to, Thresholds())
        self.assertIsNotNone(detail)
        self.assertGreaterEqual(len(detail["nm_items"]), 2)
        self.assertTrue(all("drr" in item for item in detail["nm_items"]))

    def test_campaign_detail_missing_returns_none(self):
        date_from, date_to = analytics.default_period(self.conn, 7)
        self.assertIsNone(analytics.campaign_detail(self.conn, 999999, date_from,
                                                    date_to, Thresholds()))

    def test_thresholds_persist_in_settings(self):
        db.set_setting(self.conn, "target_drr", "8")
        loaded = analytics.load_thresholds(self.conn, Thresholds())
        self.assertEqual(loaded.target_drr, 8.0)
        db.set_setting(self.conn, "target_drr", "15")

    def test_settings_ignore_broken_values(self):
        db.set_setting(self.conn, "target_drr", "не число")
        loaded = analytics.load_thresholds(self.conn, Thresholds())
        self.assertEqual(loaded.target_drr, 15.0)
        db.set_setting(self.conn, "target_drr", "15")


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "api.db"
        cls.conn = db.init_db(cls.db_path)
        demo.generate(cls.conn, days=30)
        cls.cfg = Config(db_path=cls.db_path, thresholds=Thresholds())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_report_endpoint_respects_days(self):
        report = handle_report(self.conn, self.cfg, {"days": ["14"]})
        self.assertEqual(len(report["series"]), 14)

    def test_report_endpoint_accepts_explicit_dates(self):
        _, hi = db.data_range(self.conn)
        report = handle_report(self.conn, self.cfg, {"from": [hi], "to": [hi]})
        self.assertEqual(report["period"]["from"], hi)
        self.assertEqual(len(report["series"]), 1)

    def test_reversed_dates_are_swapped(self):
        lo, hi = db.data_range(self.conn)
        report = handle_report(self.conn, self.cfg, {"from": [hi], "to": [lo]})
        self.assertEqual(report["period"]["from"], lo)

    def test_meta_endpoint(self):
        meta = handle_meta(self.conn, self.cfg, {})
        self.assertTrue(meta["has_data"])
        self.assertEqual(meta["campaigns"], len(demo.CAMPAIGNS))

    def test_csv_export(self):
        content, filename = handle_export(self.conn, self.cfg, {"days": ["7"]})
        lines = content.splitlines()
        self.assertTrue(filename.endswith(".csv"))
        self.assertIn("Кампания", lines[0])
        self.assertEqual(len(lines), len(demo.CAMPAIGNS) + 1)
        self.assertIn(";", lines[1])  # разделитель, который понимает русский Excel

    def test_csv_has_full_funnel(self):
        """В выгрузке должна быть вся воронка, а не только деньги и ДРР."""
        content, _ = handle_export(self.conn, self.cfg, {"days": ["7"]})
        header = content.splitlines()[0]
        for column in ("Показы", "CTR %", "Клики", "В корзине", "CR в корзину %",
                       "Заказы", "CR в заказ %", "CR клик-заказ %"):
            self.assertIn(column, header)

    def test_csv_uses_comma_decimal(self):
        """Русский Excel не понимает точку как разделитель дробной части."""
        content, _ = handle_export(self.conn, self.cfg, {"days": ["7"]})
        row = content.splitlines()[1].split(";")
        self.assertNotIn(".", "".join(row[6:20]))


if __name__ == "__main__":
    unittest.main()
