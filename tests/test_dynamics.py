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

    def test_heat_is_green_and_red_by_direction(self):
        """Зелёный — лучше обычного для строки, красный — хуже. Сторона
        берётся из самого показателя, иначе у ДРР и у выручки рост
        покрасился бы одинаково."""
        self.assertIn("--heat-good-rgb", self.css)
        self.assertIn("--heat-bad-rgb", self.css)
        self.assertIn(".dgrid.dyn .heat-bad-3", self.css)
        self.assertIn("(row.good === 'up') === up", self.js)

    def test_rows_without_a_good_side_stay_one_hue(self):
        """У расхода «больше» не значит «хуже»: красить его красным —
        значит сказать то, чего мы не проверяли."""
        self.assertIn("if (!row.good) {", self.js)
        self.assertIn(".dgrid.dyn .heat-5", self.css)

    def test_colour_is_measured_from_the_median(self):
        """Среднее сдвигает один выброс — а выбросы мы и ищем."""
        self.assertIn("function median", self.js)
        self.assertIn("const middle = median(values)", self.js)

    def test_legend_says_what_the_colours_mean(self):
        self.assertIn("лучше обычного", self.js)
        self.assertIn("heat-chip", self.js)
        self.assertIn(".heat-chip", self.css)

    def test_trend_shows_shape_and_number(self):
        self.assertIn("sparkline(row.days", self.js)
        self.assertIn("function dynDelta", self.js)

    def test_delta_colour_never_travels_alone(self):
        """Цвет только вместе со знаком и стрелкой."""
        self.assertIn("${up ? '↑' : '↓'}", self.js)
        self.assertIn("signed(row.delta_pct)", self.js)

    def test_row_opens_a_chart_with_the_previous_period(self):
        """Один показатель — свои единицы и прошлый период второй линией."""
        self.assertIn("function toggleDynMetric", self.js)
        self.assertIn("Прошлый период", self.js)
        self.assertIn("if (!indexed) {", self.js)

    def test_several_metrics_share_one_chart(self):
        """Ради этого вкладку и смотрят: видно ли, что одно поехало
        вслед за другим."""
        self.assertIn("dyn.picked", self.js)
        self.assertIn("function dynChips", self.js)
        self.assertIn("aria-pressed", self.js)

    def test_different_scales_are_indexed_not_double_axed(self):
        """Вторая ось — самый частый способ соврать графиком: показы в
        сотнях тысяч превратили бы ДРР в прямую у нуля."""
        self.assertIn("const indexed = rows.length > 1", self.js)
        self.assertIn("(v / base) * 100", self.js)
        self.assertIn("100% — среднее значение", self.js)

    def test_tooltip_keeps_the_real_numbers(self):
        """Процент от среднего — не та цифра, по которой принимают
        решение."""
        self.assertIn("tipValues: row.days", self.js)
        self.assertIn("s.tipValues || s.values", self.js)

    def test_colour_follows_the_metric_not_its_place(self):
        """Снял одну линию — остальные не перекрашиваются."""
        self.assertIn("function takeSlot", self.js)
        self.assertIn("dyn.slots[key]", self.js)

    def test_lines_are_capped(self):
        """Больше шести линий не различить ни по цвету, ни глазом."""
        self.assertIn("DYN_MAX_LINES = 6", self.js)
        self.assertIn("--series-6", self.css)

    def test_chart_has_a_legend_for_two_series(self):
        self.assertIn("legend-item", self.js)

    def test_keyboard_reaches_the_chart(self):
        self.assertIn("tr.tabIndex = 0", self.js)
        self.assertIn("e.key === 'Enter'", self.js)


class TestBuyouts(DynamicsCase):
    """Заказ и выкуп — разные события, и разрыв между ними в днях как
    раз то, ради чего выкупы смотрят: реклама привела заказ, а деньги
    пришли (или не пришли) позже."""

    def sale(self, date, *, nm_id=777, finished=1200.0, for_pay=1000.0,
             returned=False, key=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO sales_raw (srid, sale_id, date,"
            " last_change, nm_id, supplier_article, brand, subject, warehouse,"
            " region, total_price, discount_percent, price_with_disc,"
            " finished_price, for_pay, spp, is_return, collected_at)"
            " VALUES (?,?,?,?,?,'A','B','C','W','R',2000,20,1500,?,?,20,?,'now')",
            (key or f"{date}-{nm_id}-{finished}-{returned}",
             "R1" if returned else "S1", date, date, nm_id, finished, for_pay,
             1 if returned else 0))
        self.conn.commit()

    def test_rows_appear_only_with_sales(self):
        self.day("2026-11-01")
        self.assertNotIn("buyouts",
                         self.rows(dynamics.table(self.conn, "2026-11-01",
                                                  "2026-11-01")))
        self.sale("2026-11-01")
        self.assertIn("buyouts",
                      self.rows(dynamics.table(self.conn, "2026-11-01",
                                               "2026-11-01")))

    def test_money_and_count(self):
        self.day("2026-11-01", spend=600.0)
        self.sale("2026-11-01", finished=1200.0, for_pay=1000.0, key="a")
        self.sale("2026-11-01", finished=800.0, for_pay=700.0, key="b")
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["buyouts"]["days"], [2.0])
        self.assertEqual(rows["buyout_revenue"]["days"], [2000.0])
        self.assertEqual(rows["for_pay"]["days"], [1700.0])
        self.assertEqual(rows["cps"]["days"], [300.0])
        self.assertEqual(rows["drr_buyout"]["days"], [30.0])

    def test_returns_are_their_own_row(self):
        """Возврат приходит через недели. Вычитая его из дня, в котором
        он приехал, мы испортили бы оба дня."""
        self.day("2026-11-01")
        self.sale("2026-11-01", key="s")
        self.sale("2026-11-01", returned=True, key="r")
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["buyouts"]["days"], [1.0])
        self.assertEqual(rows["returns"]["days"], [1.0])

    def test_conversion_to_buyout_uses_cabinet_orders(self):
        self.day("2026-11-01")
        self.order("2026-11-01", srid="o1")
        self.order("2026-11-01", srid="o2")
        self.sale("2026-11-01")
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["cr_buyout"]["days"], [50.0])

    def test_period_total_is_not_an_average_of_days(self):
        self.day("2026-11-01", spend=100.0)
        self.day("2026-11-02", spend=900.0)
        self.sale("2026-11-01", finished=1000.0, key="a")
        self.sale("2026-11-02", finished=1000.0, key="b")
        drr = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-02")
                        )["drr_buyout"]
        self.assertEqual(drr["days"], [10.0, 90.0])
        self.assertEqual(drr["total"], 50.0)


class TestStocks(DynamicsCase):
    """Метод остатков отдаёт снимок «что лежит сейчас». Истории у него
    нет, и ряд по дням набирается нашими снимками."""

    def stock(self, date, *, nm_id=777, quantity=40, in_way=5, warehouse=1):
        self.conn.execute(
            "INSERT OR REPLACE INTO stocks_daily (date, nm_id, warehouse_id,"
            " warehouse, region, quantity, in_way_to_client,"
            " in_way_from_client, collected_at)"
            " VALUES (?,?,?,'Коледино','ЦФО',?,?,0,'now')",
            (date, nm_id, warehouse, quantity, in_way))
        self.conn.commit()

    def test_rows_appear_only_with_stocks(self):
        self.day("2026-11-01")
        self.assertNotIn("stock", self.rows(
            dynamics.table(self.conn, "2026-11-01", "2026-11-01")))
        self.stock("2026-11-01")
        self.assertIn("stock", self.rows(
            dynamics.table(self.conn, "2026-11-01", "2026-11-01")))

    def test_warehouses_are_summed(self):
        self.day("2026-11-01")
        self.stock("2026-11-01", quantity=40, warehouse=1)
        self.stock("2026-11-01", quantity=10, warehouse=2)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["stock"]["days"], [50.0])

    def test_stock_is_not_summed_over_the_period(self):
        """Остаток — состояние на день, а не событие. Сумма остатков за
        две недели — это число, которого не существует."""
        self.day("2026-11-01")
        self.day("2026-11-02")
        self.stock("2026-11-01", quantity=40)
        self.stock("2026-11-02", quantity=30)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-02"))
        self.assertEqual(rows["stock"]["days"], [40.0, 30.0])
        self.assertEqual(rows["stock"]["total"], 30.0, "итог — последний день")

    def test_days_left_counts_from_the_week(self):
        """По одному дню запас прыгает от двух дней до сорока и ничего
        не говорит."""
        for day in ("2026-11-0%d" % i for i in range(1, 8)):
            self.day(day)
            self.conn.execute(
                "INSERT OR REPLACE INTO sales_raw (srid, sale_id, date,"
                " last_change, nm_id, supplier_article, brand, subject,"
                " warehouse, region, total_price, discount_percent,"
                " price_with_disc, finished_price, for_pay, spp, is_return,"
                " collected_at) VALUES (?,?,?,?,777,'A','B','C','W','R',"
                "2000,20,1500,1200,1000,20,0,'now')",
                (f"s{day}", "S1", day, day))
        self.conn.commit()
        self.stock("2026-11-07", quantity=14)
        rows = self.rows(dynamics.table(self.conn, "2026-11-07", "2026-11-07"))
        # Неделя, одна продажа в день → запас ровно на четырнадцать дней.
        self.assertEqual(rows["days_left"]["days"], [14.0])

    def test_no_stock_means_no_guess(self):
        self.day("2026-11-01")
        self.stock("2026-11-01", quantity=0)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["days_left"]["days"], [0.0])


class TestSppComesFromWb(DynamicsCase):
    """СПП WB отдаёт процентом в каждом заказе. Считать её разницей цен
    можно, но в разницу попадут и другие скидки."""

    def test_spp_is_taken_from_the_order(self):
        self.day("2026-11-01")
        self.conn.execute(
            "INSERT OR REPLACE INTO orders_raw (srid, date, last_change,"
            " nm_id, supplier_article, brand, subject, price_with_disc,"
            " finished_price, total_price, spp, is_cancel, collected_at)"
            " VALUES ('x','2026-11-01','2026-11-01',777,'A','B','C',"
            "1500,1200,2000,17,0,'now')")
        self.conn.commit()
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["spp_pct"]["days"], [17.0])
        self.assertEqual(rows["price_before_spp"]["days"], [1500.0])
        self.assertEqual(rows["price_with_spp"]["days"], [1200.0])

    def test_without_wb_spp_the_price_gap_is_used(self):
        """У заказов, собранных прежней версией, поля СПП нет вовсе."""
        self.day("2026-11-01")
        self.conn.execute(
            "INSERT OR REPLACE INTO orders_raw (srid, date, last_change,"
            " nm_id, supplier_article, brand, subject, price_with_disc,"
            " finished_price, total_price, spp, is_cancel, collected_at)"
            " VALUES ('x','2026-11-01','2026-11-01',777,'A','B','C',"
            "1000,800,2000,0,0,'now')")
        self.conn.commit()
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["spp_pct"]["days"], [20.0])


class TestItemRating(DynamicsCase):
    """«Оценка товара» — то, что в кабинете на одноимённой странице.
    Как и остатки, это снимок «что сейчас»: истории у метода нет."""

    def rating(self, date, *, nm_id=777, rating=4.8, feedback_rating=4.9,
               feedbacks=120):
        self.conn.execute(
            "INSERT OR REPLACE INTO item_rating_daily (date, nm_id, rating,"
            " feedback_rating, feedback_count, collected_at)"
            " VALUES (?,?,?,?,?,'now')",
            (date, nm_id, rating, feedback_rating, feedbacks))
        self.conn.commit()

    def test_rows_appear_only_with_ratings(self):
        self.day("2026-11-01")
        self.assertNotIn("rating", self.rows(
            dynamics.table(self.conn, "2026-11-01", "2026-11-01")))
        self.rating("2026-11-01")
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["rating"]["days"], [4.8])
        self.assertEqual(rows["feedback_rating"]["days"], [4.9])
        self.assertEqual(rows["feedback_count"]["days"], [120.0])

    def test_cabinet_rating_is_averaged_over_items(self):
        """Средневзвешенная по отзывам была бы оценкой магазина, где
        товар с тысячей отзывов заглушает десять остальных."""
        self.day("2026-11-01")
        self.rating("2026-11-01", nm_id=1, rating=5.0, feedbacks=1000)
        self.rating("2026-11-01", nm_id=2, rating=3.0, feedbacks=2)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01"))
        self.assertEqual(rows["rating"]["days"], [4.0])
        # Отзывы — штуки, их складывают.
        self.assertEqual(rows["feedback_count"]["days"], [1002.0])

    def test_one_article_sees_only_its_own(self):
        self.day("2026-11-01", nm_id=777)
        self.rating("2026-11-01", nm_id=777, rating=4.2)
        self.rating("2026-11-01", nm_id=888, rating=2.0)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-01",
                                        nm_id=777))
        self.assertEqual(rows["rating"]["days"], [4.2])

    def test_rating_is_not_summed_over_the_period(self):
        self.day("2026-11-01")
        self.day("2026-11-02")
        self.rating("2026-11-01", rating=4.5)
        self.rating("2026-11-02", rating=4.7)
        rows = self.rows(dynamics.table(self.conn, "2026-11-01", "2026-11-02"))
        self.assertEqual(rows["rating"]["days"], [4.5, 4.7])
        self.assertEqual(rows["rating"]["total"], 4.7, "итог — последний день")

    def test_rating_is_shown_with_tenths(self):
        """Оценка 4,8, округлённая до 5, перестаёт быть оценкой."""
        from wbads.config import ROOT

        js = (ROOT / "wbads" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("row.unit === 'балл'", js)
        self.assertIn("toFixed(2)", js)


class TestRatingRequestMatchesWb(unittest.TestCase):
    """Метод «Оценка товара» требователен к телу запроса: без периода,
    поля сортировки и смещения он отвечает отказом, а не данными."""

    def client(self, capture):
        from wbads.wb_client import WBAnalyticsClient

        client = WBAnalyticsClient.__new__(WBAnalyticsClient)
        client.token = "t"
        client.base_url = "x"
        client.timeout = 1
        client.max_retries = 1
        client.ca_bundle = ""
        client.used_fallback_bundle = False
        client._last_call = 0.0
        client._wait_turn = lambda on_progress=None, cooldown=0: None
        client._request = capture
        return client

    def test_body_carries_period_order_and_offset(self):
        sent = {}

        def capture(method, path, payload=None, on_progress=None):
            sent.update({"method": method, "path": path, "payload": payload})
            return {"data": {"items": []}}

        self.client(capture).item_rating("2026-11-13")
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["path"], "/api/analytics/v2/item-rating")
        self.assertEqual(sent["payload"]["currentPeriod"],
                         {"start": "2026-11-13", "end": "2026-11-13"})
        self.assertEqual(sent["payload"]["orderBy"],
                         {"field": "feedbackCount", "mode": "desc"})
        self.assertEqual(sent["payload"]["offset"], 0)

    def test_one_day_period_makes_the_review_count_daily(self):
        """WB отдаёт отзывы приростом за период. Период в один день —
        и это честный дневной ряд, а не нарастающий итог."""
        sent = []

        def capture(method, path, payload=None, on_progress=None):
            sent.append(payload)
            return {"data": {"items": []}}

        self.client(capture).item_rating("2026-11-13")
        self.assertEqual(sent[0]["currentPeriod"]["start"],
                         sent[0]["currentPeriod"]["end"])


class TestRatingFieldsAreUnwrapped(unittest.TestCase):
    """Оценка по отзывам и число отзывов приходят объектами, а не
    числами. Записать в базу словарь — значит получить мусор в таблице
    и узнать об этом через неделю."""

    def test_objects_are_unwrapped(self):
        from wbads import collector

        row = collector.normalize_rating({
            "nmId": 777,
            "rating": 10,
            "feedbackRating": {"current": 4.87, "percentile": 1.7,
                               "dynamics": 0.31},
            "feedbackCount": {"current": 12, "dynamics": 9},
        }, "2026-11-13", "now")
        self.assertEqual(row["rating"], 10.0)
        self.assertEqual(row["feedback_rating"], 4.87)
        self.assertEqual(row["feedback_count"], 12)

    def test_plain_numbers_still_work(self):
        """Если WB однажды отдаст число вместо объекта — не падаем."""
        from wbads import collector

        row = collector.normalize_rating({
            "nmID": 777, "rating": 4.5, "feedbackRating": 4.6,
            "feedbackCount": 3}, "2026-11-13", "now")
        self.assertEqual(row["feedback_rating"], 4.6)
        self.assertEqual(row["feedback_count"], 3)

    def test_a_row_without_an_article_is_skipped(self):
        from wbads import collector

        self.assertIsNone(collector.normalize_rating({"rating": 5},
                                                     "2026-11-13", "now"))
