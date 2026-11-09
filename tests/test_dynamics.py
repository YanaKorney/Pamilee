"""Динамика по дням: много показателей разом, тренд и прошлый период.

Эта таблица отвечает на вопрос «где за две недели что-то поехало», и
соврать в ней можно двумя способами: усреднить отношения по дням
(тогда день с расходом в десять рублей весит как день с десятью
тысячами) и показать показатель, которого WB не отдаёт. Здесь
проверяется и то, и другое.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import isolate  # noqa: E402,F401 — должен идти до wbads: пути берутся при импорте

from wbads import db, dynamics  # noqa: E402


class DynamicsCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)

    def day(self, date, *, advert_id=1, nm_id=777, views=1000, clicks=20,
            atbs=5, orders=2, spend=200.0, revenue=2000.0):
        for table in ("campaign_daily", "campaign_nm_daily"):
            if table == "campaign_daily":
                self.conn.execute(
                    "INSERT OR REPLACE INTO campaign_daily (advert_id, date,"
                    " views, clicks, atbs, orders, shks, spend, revenue,"
                    " collected_at) VALUES (?,?,?,?,?,?,?,?,?,'now')",
                    (advert_id, date, views, clicks, atbs, orders, orders,
                     spend, revenue))
            else:
                self.conn.execute(
                    "INSERT OR REPLACE INTO campaign_nm_daily (advert_id,"
                    " date, nm_id, name, views, clicks, atbs, orders, shks,"
                    " spend, revenue) VALUES (?,?,?,'Товар',?,?,?,?,?,?,?)",
                    (advert_id, date, nm_id, views, clicks, atbs, orders,
                     orders, spend, revenue))
        self.conn.commit()

    def order(self, date, *, nm_id=777, price=1500.0, srid=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO orders_raw (srid, date, last_change,"
            " nm_id, supplier_article, brand, subject, price_with_disc,"
            " finished_price, total_price, is_cancel, collected_at)"
            " VALUES (?,?,?,?,'ART','Бренд','Предмет',?,?,?,0,'now')",
            (srid or f"{date}-{nm_id}-{price}", date, date, nm_id,
             price, price, price))
        self.conn.commit()

    def rows(self, table):
        out = {}
        for group in table["groups"]:
            for row in group["rows"]:
                out[row["key"]] = row
        return out


class TestShape(DynamicsCase):

    def test_groups_are_named_and_ordered(self):
        self.day("2026-11-01")
        table = dynamics.table(self.conn, "2026-11-01", "2026-11-01")
        self.assertEqual([g["title"] for g in table["groups"]],
                         ["Воронка", "Деньги", "Цена рекламы"])

    def test_every_row_has_a_value_for_every_day(self):
        """Пустой день — это ноль, а не пропуск: иначе строка короче
        шапки и числа разъезжаются с датами."""
        self.day("2026-11-01")
        table = dynamics.table(self.conn, "2026-11-01", "2026-11-03")
        for row in self.rows(table).values():
            self.assertEqual(len(row["days"]), 3, row["title"])

    def test_previous_period_is_the_same_length(self):
        self.day("2026-11-01")
        table = dynamics.table(self.conn, "2026-11-01", "2026-11-07")
        self.assertEqual(table["previous"],
                         {"from": "2026-10-25", "to": "2026-10-31"})
        for row in self.rows(table).values():
            self.assertEqual(len(row["prev_days"]), 7, row["title"])


class TestRatiosAreNotAveraged(DynamicsCase):
    """Главная ловушка таблицы «показатели × дни»."""

    def test_period_drr_comes_from_sums(self):
        # День дешёвый и день дорогой: среднее дневных ДРР дало бы 30%,
        # правда — 20,5%.
        self.day("2026-11-01", spend=10.0, revenue=100.0)      # 10%
        self.day("2026-11-02", spend=200.0, revenue=400.0)     # 50%
        table = dynamics.table(self.conn, "2026-11-01", "2026-11-02")
        drr = self.rows(table)["drr"]
        self.assertEqual(drr["days"], [10.0, 50.0])
        self.assertAlmostEqual(drr["total"], 42.0, places=1)

    def test_counts_are_summed(self):
        self.day("2026-11-01", views=1000)
        self.day("2026-11-02", views=1500)
        self.assertEqual(
            self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-02"))
            ["views"]["total"], 2500.0)


class TestTrend(DynamicsCase):

    def test_delta_compares_with_the_previous_window(self):
        self.day("2026-10-31", views=100)
        self.day("2026-11-01", views=150)
        table = dynamics.table(self.conn, "2026-11-01", "2026-11-01")
        self.assertEqual(self.rows(table)["views"]["delta_pct"], 50.0)

    def test_no_base_is_not_zero(self):
        """Прошлого периода нет — «−100%» было бы выдумкой."""
        self.day("2026-11-01", views=150)
        table = dynamics.table(self.conn, "2026-11-01", "2026-11-01")
        self.assertIsNone(self.rows(table)["views"]["delta_pct"])

    def test_direction_is_declared_for_colouring(self):
        """Для ДРР рост — плохо, для выручки — хорошо. Без этого поля
        стрелку пришлось бы красить наугад."""
        self.day("2026-11-01")
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["drr"]["good"], "down")
        self.assertEqual(rows["revenue"]["good"], "up")
        # У расхода хорошей стороны нет: тратить больше само по себе ни
        # плохо, ни хорошо.
        self.assertIsNone(rows["spend"]["good"])


class TestOnlyWhatWbGives(DynamicsCase):

    def test_zones_appear_only_when_search_is_collected(self):
        self.day("2026-11-01")
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertNotIn("views_search", rows)

        self.conn.execute(
            "INSERT OR REPLACE INTO campaign_nm_search_daily (advert_id, date,"
            " nm_id, views, clicks, atbs, orders, shks, spend)"
            " VALUES (1,'2026-11-01',777,400,0,0,0,0,0)")
        self.conn.commit()
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["views_search"]["days"], [400.0])
        self.assertEqual(rows["views_other"]["days"], [600.0])

    def test_cabinet_rows_appear_only_with_orders(self):
        self.day("2026-11-01")
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertNotIn("total_orders", rows)

        self.order("2026-11-01", price=1500.0)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["total_orders"]["days"], [1.0])
        self.assertEqual(rows["total_revenue"]["days"], [1500.0])
        # Общий ДРР — расход, делённый на все заказы, а не на рекламные.
        self.assertAlmostEqual(rows["total_drr"]["days"][0], 200 / 1500 * 100,
                               places=2)

    def test_cabinet_orders_follow_the_chosen_article(self):
        self.day("2026-11-01", nm_id=777)
        self.order("2026-11-01", nm_id=777, price=1000.0)
        self.order("2026-11-01", nm_id=888, price=5000.0)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01",
                                        nm_id=777))
        self.assertEqual(rows["total_revenue"]["days"], [1000.0])

    def test_no_invented_metrics(self):
        """Остатков, цены до СПП, выкупов и прибыли WB в рекламной
        статистике не отдаёт — и выдумывать их нельзя."""
        self.day("2026-11-01")
        keys = set(self.rows(dynamics.table(self.conn, "2026-11-01",
                                            "2026-11-01")))
        for absent in ("stock", "buyouts", "profit", "spp", "rating"):
            self.assertNotIn(absent, keys)


class TestPerOrderMetrics(DynamicsCase):

    def test_views_and_clicks_per_order(self):
        self.day("2026-11-01", views=1000, clicks=20, orders=2)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["views_per_order"]["days"], [500.0])
        self.assertEqual(rows["clicks_per_order"]["days"], [10.0])

    def test_no_orders_means_no_division(self):
        """Делить на ноль заказов нельзя, и «бесконечность» в таблице
        хуже прочерка."""
        self.day("2026-11-01", orders=0)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["views_per_order"]["days"], [0.0])


class TestApi(DynamicsCase):

    def call(self, query):
        from wbads import api
        from wbads.config import Config

        return api.handle_dynamics(self.conn, Config(db_path=self.path,
                                                     token="t"), query)

    def test_articles_come_with_the_table(self):
        self.day("2026-11-01", nm_id=777)
        data = self.call({"from": ["2026-11-01"], "to": ["2026-11-01"]})
        self.assertEqual([a["nm_id"] for a in data["articles"]], [777])
        self.assertIsNone(data["nm_id"])

    def test_one_article_is_filtered(self):
        self.day("2026-11-01", nm_id=777, views=1000)
        self.day("2026-11-01", advert_id=2, nm_id=888, views=5000)
        data = self.call({"from": ["2026-11-01"], "to": ["2026-11-01"],
                          "nm_id": ["777"]})
        rows = self.rows(data)
        self.assertEqual(data["nm_id"], 777)
        self.assertEqual(rows["views"]["days"], [1000.0])


class TestTabStructure(unittest.TestCase):
    """Поведение проверено в браузере; здесь — что вкладка собрана из
    того, из чего задумана."""

    def setUp(self):
        from wbads.config import ROOT

        self.html = (ROOT / "wbads" / "web" / "index.html").read_text(
            encoding="utf-8")
        self.js = (ROOT / "wbads" / "web" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "wbads" / "web" / "styles.css").read_text(
            encoding="utf-8")

    def test_tab_exists_and_loads(self):
        self.assertIn('data-view="dyn"', self.html)
        self.assertIn("Динамика по дням", self.html)
        self.assertIn("if (view === 'dyn') loadDyn();", self.js)
        self.assertIn("/api/dynamics?", self.js)

    def test_heat_map_can_be_turned_off(self):
        self.assertIn('id="dyn-heat"', self.html)
        self.assertIn("dyn.heat", self.js)

    def test_heat_is_one_hue_by_magnitude(self):
        """Заливка — это «сколько», а не «хорошо»: смысл одним цветом не
        передают, и зелёно-красная шкала сказала бы то, чего мы не
        проверяли."""
        self.assertIn("--heat-rgb", self.css)
        self.assertIn(".dgrid.dyn .heat-5", self.css)
        self.assertNotIn("heat-good", self.css)

    def test_trend_shows_shape_and_number(self):
        self.assertIn("sparkline(row.days", self.js)
        self.assertIn("function dynDelta", self.js)

    def test_delta_colour_never_travels_alone(self):
        """Цвет только вместе со знаком и стрелкой."""
        self.assertIn("${up ? '↑' : '↓'}", self.js)
        self.assertIn("signed(row.delta_pct)", self.js)

    def test_row_opens_a_chart_with_the_previous_period(self):
        self.assertIn("function toggleDynChart", self.js)
        self.assertIn("'Этот период'", self.js)
        self.assertIn("Прошлый период", self.js)

    def test_chart_has_a_legend_for_two_series(self):
        self.assertIn("legend-item", self.js)

    def test_keyboard_reaches_the_chart(self):
        self.assertIn("tr.tabIndex = 0", self.js)
        self.assertIn("e.key === 'Enter'", self.js)
