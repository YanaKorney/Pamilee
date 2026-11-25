"""ДРР склейки: несколько артикулов, посчитанных как один товар.

Соврать здесь можно двумя способами, и оба выглядят правдоподобно.
Первый — усреднить ДРР по товарам: тогда артикул с расходом в сто
рублей весит столько же, сколько артикул с расходом в сто тысяч.
Второй — посчитать оборот склейки не так, как его считает кабинет, и
получить вкладку, которая спорит с другими вкладками той же программы.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import isolate  # noqa: E402,F401 — должен идти до wbads: пути берутся при импорте

from wbads import db, glue  # noqa: E402


class GlueCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)

    def ad(self, date, nm_id, *, advert_id=1, views=1000, clicks=20,
           atbs=5, orders=2, spend=200.0, revenue=2000.0, name=""):
        self.conn.execute(
            "INSERT OR REPLACE INTO campaign_nm_daily (advert_id, date,"
            " nm_id, name, views, clicks, atbs, orders, shks, spend, revenue)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (advert_id, date, nm_id, name or f"Товар {nm_id}", views, clicks,
             atbs, orders, orders, spend, revenue))
        self.conn.commit()

    def order(self, date, nm_id, *, srid="o1", price=1000.0):
        self.conn.execute(
            "INSERT OR REPLACE INTO orders_raw (srid, date, last_change,"
            " nm_id, supplier_article, brand, subject, price_with_disc,"
            " finished_price, total_price, spp, is_cancel, collected_at)"
            " VALUES (?,?,?,?,'A','B','Кружка',?,?,?,0,0,'now')",
            (srid, date, date, nm_id, price, price, price))
        self.conn.commit()

    def funnel(self, date, nm_id, *, orders=10, orders_sum=10000.0):
        db.upsert_funnel(self.conn, [{
            "date": date, "nm_id": nm_id, "orders": orders,
            "orders_sum": orders_sum, "buyouts": 0, "buyouts_sum": 0.0,
            "cancels": 0, "cancels_sum": 0.0, "opens": 100, "carts": 20,
            "collected_at": "now"}])
        self.conn.commit()

    def table(self, ids, date_from="2026-11-01", date_to="2026-11-02"):
        return glue.table(self.conn, date_from, date_to, ids)


class TestTheWholeGlue(GlueCase):
    """Главное: склейка считается целиком, а не по кусочкам."""

    def setUp(self):
        super().setUp()
        # Реклама крутится на чёрный, заказы приходят и чёрному, и
        # серому: внутри карточки покупатель переключает цвет.
        self.ad("2026-11-01", 111, spend=1000.0, revenue=4000.0)
        self.funnel("2026-11-01", 111, orders=10, orders_sum=6000.0)
        self.funnel("2026-11-01", 222, orders=5, orders_sum=4000.0)

    def test_spend_and_turnover_add_up(self):
        totals = self.table([111, 222])["totals"]
        self.assertEqual(totals["spend"], 1000.0)
        self.assertEqual(totals["total_revenue"], 10000.0)
        self.assertAlmostEqual(totals["total_drr"], 10.0)

    def test_the_lonely_article_drags_the_drr_down(self):
        """Один артикул склейки выглядит вдвое хуже целой склейки."""
        alone = self.table([111])["totals"]
        whole = self.table([111, 222])["totals"]
        self.assertAlmostEqual(alone["total_drr"], 16.6667, places=3)
        self.assertAlmostEqual(whole["total_drr"], 10.0)

    def test_the_article_without_spend_shows_no_drr_of_its_own(self):
        """У серого расхода нет — и собственный ДРР у него не считается."""
        items = {row["nm_id"]: row for row in self.table([111, 222])["items"]}
        self.assertEqual(items[222]["spend"], 0.0)
        self.assertEqual(items[222]["total_drr"], 0.0)
        self.assertEqual(items[222]["total_revenue"], 4000.0)

    def test_the_shares_say_who_spends_and_who_collects(self):
        items = {row["nm_id"]: row for row in self.table([111, 222])["items"]}
        self.assertEqual(items[111]["spend_share"], 100.0)
        self.assertEqual(items[111]["revenue_share"], 60.0)
        self.assertEqual(items[222]["revenue_share"], 40.0)

    def test_the_ad_share_of_the_turnover(self):
        """Сколько оборота принесла реклама, а сколько пришло само."""
        totals = self.table([111, 222])["totals"]
        self.assertAlmostEqual(totals["ad_share"], 40.0)

    def test_items_go_from_the_biggest_spender_down(self):
        self.ad("2026-11-01", 333, spend=5000.0, revenue=1000.0)
        order = [row["nm_id"] for row in self.table([111, 222, 333])["items"]]
        self.assertEqual(order[0], 333)

    def test_an_article_without_anything_is_named(self):
        """Отметили товар, а по нему тишина — это надо сказать."""
        data = self.table([111, 222, 999])
        self.assertEqual(data["silent"], [999])

    def test_nothing_picked_is_not_an_error(self):
        data = self.table([])
        self.assertEqual(data["totals"]["spend"], 0.0)
        self.assertFalse(data["has_data"])


class TestRatiosComeFromSums(GlueCase):
    """Отношения считаются из сумм, а не усреднением по дням и товарам."""

    def test_the_drr_is_not_an_average_of_days(self):
        # День дорогой и день дешёвый: среднее дневных ДРР дало бы 30%,
        # правда — 20%.
        self.ad("2026-11-01", 111, spend=100.0, revenue=200.0)
        self.ad("2026-11-02", 111, spend=100.0, revenue=800.0)

        totals = self.table([111])["totals"]
        self.assertAlmostEqual(totals["drr"], 20.0)

    def test_the_drr_is_not_an_average_of_articles(self):
        self.ad("2026-11-01", 111, spend=10.0, revenue=10.0)
        self.ad("2026-11-01", 222, spend=990.0, revenue=9990.0)

        totals = self.table([111, 222])["totals"]
        self.assertAlmostEqual(totals["drr"], 10.0, places=2)


class TestTheSameTurnoverEverywhere(GlueCase):
    """Оборот склейки обязан считаться тем же правилом, что и везде."""

    def test_the_funnel_wins_over_the_order_rows(self):
        self.ad("2026-11-01", 111, spend=100.0)
        for n in range(7):
            self.order("2026-11-01", 111, srid=f"o{n}", price=1000.0)
        self.funnel("2026-11-01", 111, orders=10, orders_sum=6000.0)

        data = self.table([111])
        self.assertEqual(data["totals"]["total_revenue"], 6000.0)
        self.assertEqual(data["source"], "funnel")

    def test_days_without_the_funnel_count_by_orders(self):
        self.ad("2026-11-01", 111, spend=100.0)
        self.order("2026-11-02", 111, srid="o1", price=1500.0)

        data = self.table([111])
        self.assertEqual(data["totals"]["total_revenue"], 1500.0)
        self.assertEqual(data["source"], "orders")

    def test_the_same_number_as_the_analytics_tab(self):
        """Две вкладки с разным оборотом — повод не верить обеим."""
        from wbads import analytics

        self.ad("2026-11-01", 111, spend=1000.0)
        self.funnel("2026-11-01", 111, orders=10, orders_sum=6000.0)

        mine = self.table([111])["totals"]["total_revenue"]
        report = analytics.build_report(self.conn, "2026-11-01", "2026-11-02",
                                        analytics.Thresholds(), with_nm=False)
        self.assertEqual(mine, report["orders"]["revenue"])


class TestSeries(GlueCase):

    def test_every_day_of_the_period_is_in_the_row(self):
        """День без открутки — ноль в ряду, а не пропуск."""
        self.ad("2026-11-01", 111, spend=100.0, revenue=1000.0)
        series = self.table([111], "2026-11-01", "2026-11-03")["series"]
        self.assertEqual([p["date"] for p in series],
                         ["2026-11-01", "2026-11-02", "2026-11-03"])
        self.assertEqual(series[1]["spend"], 0.0)

    def test_the_day_drr_counts_the_whole_glue(self):
        self.ad("2026-11-01", 111, spend=600.0)
        self.funnel("2026-11-01", 111, orders=2, orders_sum=2000.0)
        self.funnel("2026-11-01", 222, orders=4, orders_sum=4000.0)

        day = self.table([111, 222], "2026-11-01", "2026-11-01")["series"][0]
        self.assertAlmostEqual(day["total_drr"], 10.0)


class TestCatalog(GlueCase):
    """Список для галочек: в склейку входит вся карточка, а крутится
    часто один её артикул."""

    def test_an_article_without_ads_is_still_offered(self):
        self.ad("2026-11-01", 111, spend=100.0)
        self.order("2026-11-01", 222, srid="o1", price=900.0)

        found = {row["nm_id"] for row in
                 glue.catalog(self.conn, "2026-11-01", "2026-11-02")}
        self.assertEqual(found, {111, 222})

    def test_the_biggest_turnover_goes_first(self):
        self.ad("2026-11-01", 111, spend=100.0)
        self.funnel("2026-11-01", 111, orders=1, orders_sum=100.0)
        self.funnel("2026-11-01", 222, orders=1, orders_sum=9000.0)

        rows = glue.catalog(self.conn, "2026-11-01", "2026-11-02")
        self.assertEqual(rows[0]["nm_id"], 222)

    def test_the_name_comes_from_the_orders_when_ads_are_silent(self):
        self.order("2026-11-01", 222, srid="o1")
        rows = {row["nm_id"]: row for row in
                glue.catalog(self.conn, "2026-11-01", "2026-11-02")}
        self.assertEqual(rows[222]["name"], "A")


class TestSavedGlues(GlueCase):
    """Склейку собирают один раз, а смотрят каждую неделю."""

    def test_a_glue_survives_a_restart(self):
        db.save_glue(self.conn, "Худи", [222, 111, 111])
        self.conn.commit()

        again = db.list_glue(db.connect(self.path))
        self.assertEqual(again[0]["name"], "Худи")
        self.assertEqual(again[0]["nm_ids"], [111, 222])

    def test_the_same_name_replaces_the_set(self):
        db.save_glue(self.conn, "Худи", [111])
        db.save_glue(self.conn, "Худи", [222, 333])
        rows = db.list_glue(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nm_ids"], [222, 333])

    def test_a_glue_without_a_name_is_refused(self):
        with self.assertRaises(ValueError):
            db.save_glue(self.conn, "   ", [111])

    def test_a_glue_without_articles_is_refused(self):
        with self.assertRaises(ValueError):
            db.save_glue(self.conn, "Худи", [])

    def test_forgetting_a_glue(self):
        db.save_glue(self.conn, "Худи", [111])
        self.assertTrue(db.delete_glue(self.conn, "Худи"))
        self.assertEqual(db.list_glue(self.conn), [])
        self.assertFalse(db.delete_glue(self.conn, "Худи"))


class TestApi(GlueCase):

    def handle(self, query):
        from wbads import api
        from wbads.config import Config

        return api.handle_glue(self.conn, Config(db_path=self.path, token="t"),
                               query)

    def test_the_tab_gets_everything_in_one_request(self):
        """Отдельный запрос на список и на склейку — мигающая вкладка."""
        self.ad("2026-11-01", 111, spend=100.0)
        db.save_glue(self.conn, "Худи", [111])
        self.conn.commit()

        data = self.handle({"from": ["2026-11-01"], "to": ["2026-11-02"],
                            "nm": ["111"]})
        self.assertEqual(data["nm_ids"], [111])
        self.assertTrue(data["catalog"])
        self.assertEqual(data["saved"][0]["name"], "Худи")

    def test_articles_can_come_as_one_string_or_many(self):
        from wbads import api

        self.assertEqual(api._glue_ids({"nm": ["11,22"]}), [11, 22])
        self.assertEqual(api._glue_ids({"nm": ["11", "22"]}), [11, 22])
        self.assertEqual(api._glue_ids({"nm": ["вздор"]}), [])

    def test_the_csv_holds_the_totals_and_the_articles(self):
        from wbads import api
        from wbads.config import Config

        self.ad("2026-11-01", 111, spend=1000.0, revenue=4000.0, name="Худи")
        self.funnel("2026-11-01", 111, orders=10, orders_sum=10000.0)

        text, filename = api.handle_glue_csv(
            self.conn, Config(db_path=self.path, token="t"),
            {"from": ["2026-11-01"], "to": ["2026-11-02"], "nm": ["111"],
             "name": ["Худи"]})
        self.assertTrue(filename.endswith(".csv"))
        self.assertIn("ДРР общий, %", text)
        self.assertIn("10,00", text)
        self.assertIn("Худи", text)


if __name__ == "__main__":
    unittest.main()
