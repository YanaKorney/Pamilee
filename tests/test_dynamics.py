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


class TestOrdersBasis(DynamicsCase):
    """WB показывает одно и то же событие тремя суммами и двумя
    счётчиками. «Правильного» среди них нет — есть тот, с которым
    человек сверяется в кабинете, и он обязан переключаться."""

    def order(self, date, *, srid="o1", with_disc=1000.0, finished=800.0,
              total=1500.0, cancelled=False, nm_id=777):
        self.conn.execute(
            "INSERT OR REPLACE INTO orders_raw (srid, date, last_change,"
            " nm_id, supplier_article, brand, subject, price_with_disc,"
            " finished_price, total_price, spp, is_cancel, collected_at)"
            " VALUES (?,?,?,?,'A','B','C',?,?,?,20,?,'now')",
            (srid, date, date, nm_id, with_disc, finished, total,
             1 if cancelled else 0))
        self.conn.commit()

    def basis(self, field="price_with_disc", cancelled=False):
        from wbads import api
        from wbads.config import Config

        api.handle_settings(self.conn, Config(db_path=self.path, token="t"),
                            {}, {"order_price": field,
                                 "orders_cancelled": cancelled})
        return api.handle_dynamics(self.conn, Config(db_path=self.path,
                                                     token="t"),
                                   {"from": ["2026-11-01"],
                                    "to": ["2026-11-01"]})

    def setUp(self):
        super().setUp()
        self.day("2026-11-01")
        self.order("2026-11-01", srid="a")
        self.order("2026-11-01", srid="b", cancelled=True)

    def test_default_is_the_price_after_the_seller_discount(self):
        rows = self.rows(self.basis())
        self.assertEqual(rows["total_orders"]["days"], [1.0])
        self.assertEqual(rows["total_revenue"]["days"], [1000.0])

    def test_switching_to_the_buyer_price(self):
        rows = self.rows(self.basis("finished_price"))
        self.assertEqual(rows["total_revenue"]["days"], [800.0])

    def test_counting_cancelled_orders(self):
        rows = self.rows(self.basis("price_with_disc", cancelled=True))
        self.assertEqual(rows["total_orders"]["days"], [2.0])
        self.assertEqual(rows["total_revenue"]["days"], [2000.0])

    def test_the_setting_survives_a_restart(self):
        """Настройка живёт в базе, а не в памяти вкладки."""
        from wbads import db

        self.basis("finished_price", cancelled=True)
        again = db.order_basis(db.connect(self.path))
        self.assertEqual(again, {"field": "finished_price", "cancelled": True})

    def test_junk_is_not_saved(self):
        from wbads import api, db
        from wbads.config import Config

        api.handle_settings(self.conn, Config(db_path=self.path, token="t"),
                            {}, {"order_price": "выдумка"})
        self.assertEqual(db.order_basis(self.conn)["field"], "price_with_disc")

    def test_comparison_lists_every_variant(self):
        """Таблица сверки — ответ на «а почему у меня в кабинете другое»:
        видно все шесть вариантов сразу."""
        data = self.basis()
        variants = data["orders_basis"]["compare"]["variants"]
        self.assertEqual(len(variants), 6)
        pairs = {(v["field"], v["cancelled"]): (v["orders"], v["revenue"])
                 for v in variants}
        self.assertEqual(pairs[("price_with_disc", False)], (1.0, 1000.0))
        self.assertEqual(pairs[("finished_price", False)], (1.0, 800.0))
        self.assertEqual(pairs[("total_price", True)], (2.0, 3000.0))

    def test_cabinet_report_follows_the_same_setting(self):
        """Общий ДРР считается из тех же заказов: расходиться вкладкам
        нельзя, иначе одно и то же число в программе разное."""
        from wbads import api
        from wbads.config import Config

        cfg = Config(db_path=self.path, token="t")
        api.handle_settings(self.conn, cfg, {}, {"order_price": "finished_price"})
        report = api.handle_report(self.conn, cfg, {"from": ["2026-11-01"],
                                                    "to": ["2026-11-01"]})
        self.assertEqual(report["orders"]["revenue"], 800.0)
        self.assertEqual(report["orders"]["price_field"], "finished_price")

    def test_cancelled_setting_reaches_the_cabinet_report(self):
        from wbads import api
        from wbads.config import Config

        cfg = Config(db_path=self.path, token="t")
        api.handle_settings(self.conn, cfg, {}, {"orders_cancelled": True})
        report = api.handle_report(self.conn, cfg, {"from": ["2026-11-01"],
                                                    "to": ["2026-11-01"]})
        self.assertEqual(report["orders"]["orders"], 2.0)
        self.assertTrue(report["orders"]["orders_cancelled"])


class TestBasisControls(unittest.TestCase):
    def setUp(self):
        from wbads.config import ROOT

        self.html = (ROOT / "wbads" / "web" / "index.html").read_text(
            encoding="utf-8")
        self.js = (ROOT / "wbads" / "web" / "app.js").read_text(encoding="utf-8")

    def test_switch_sits_above_the_table(self):
        self.assertIn('id="dyn-price"', self.html)
        self.assertIn('id="dyn-cancelled"', self.html)
        self.assertIn("function saveDynBasis", self.js)

    def test_comparison_can_be_opened(self):
        self.assertIn('id="dyn-compare"', self.html)
        self.assertIn("Сверить с кабинетом", self.html)
        self.assertIn("воронки продаж", self.js)

    def test_analytics_reloads_after_the_change(self):
        """Общий ДРР на первой вкладке считается из тех же заказов."""
        self.assertIn("if (state.report) load();", self.js)


class TestSalesFunnel(DynamicsCase):
    """Воронка продаж — единственный источник, который сходится с
    кабинетом. Метод заказов считает строки заказа, воронка — товары, и
    в этом была вся разница: 1273 строки против 1493 товаров за один и
    тот же день. Поэтому «Заказы кабинета» берутся из воронки."""

    def order(self, date, *, srid="o1", nm_id=777):
        self.conn.execute(
            "INSERT OR REPLACE INTO orders_raw (srid, date, last_change,"
            " nm_id, supplier_article, brand, subject, price_with_disc,"
            " finished_price, total_price, spp, is_cancel, collected_at)"
            " VALUES (?,?,?,?,'A','B','C',1000,800,1500,20,0,'now')",
            (srid, date, date, nm_id))
        self.conn.commit()

    def funnel(self, date, *, nm_id=777, orders=10, orders_sum=6460.0,
               opens=500, carts=80):
        db.upsert_funnel(self.conn, [{
            "date": date, "nm_id": nm_id, "orders": orders,
            "orders_sum": orders_sum, "buyouts": 4, "buyouts_sum": 2500.0,
            "cancels": 1, "cancels_sum": 646.0, "opens": opens,
            "carts": carts, "collected_at": "now"}])
        self.conn.commit()

    def table(self, date_from="2026-11-01", date_to="2026-11-01"):
        return dynamics.table(self.conn, date_from, date_to)

    def test_cabinet_orders_come_from_the_funnel(self):
        """Главное: в дашборде то же число, что в кабинете."""
        self.day("2026-11-01")
        self.order("2026-11-01", srid="a")
        self.funnel("2026-11-01", orders=10, orders_sum=6460.0)

        rows = self.rows(self.table())
        self.assertEqual(rows["total_orders"]["days"], [10.0])
        self.assertEqual(rows["total_revenue"]["days"], [6460.0])

    def test_the_funnel_wins_over_the_order_rows(self):
        """Выбора нет: за день, который есть в воронке, считает она.

        Строк заказов за тот же день может быть сколько угодно — они
        считают другое, и смешивать их внутри дня нельзя."""
        self.day("2026-11-01")
        for n in range(5):
            self.order("2026-11-01", srid=f"o{n}")
        self.funnel("2026-11-01", orders=10, orders_sum=6460.0)

        rows = self.rows(self.table())
        self.assertEqual(rows["total_orders"]["days"], [10.0])
        self.assertEqual(rows["total_revenue"]["days"], [6460.0])

    def test_days_without_the_funnel_keep_counting_by_orders(self):
        """Ноль вместо несобранного дня читался бы как провал продаж."""
        for day in ("2026-11-01", "2026-11-02"):
            self.day(day)
            self.order(day, srid=f"o{day}")
        self.funnel("2026-11-02", orders=10, orders_sum=6460.0)

        table = self.table("2026-11-01", "2026-11-02")
        rows = self.rows(table)
        self.assertEqual(rows["total_orders"]["days"], [1.0, 10.0])
        self.assertEqual(table["funnel_missing"], ["2026-11-01"])

    def test_the_general_drr_follows_the_funnel(self):
        """Общий ДРР считается от той же выручки, что показана рядом."""
        self.day("2026-11-01", spend=646.0)
        self.order("2026-11-01", srid="a")
        self.funnel("2026-11-01", orders=10, orders_sum=6460.0)

        rows = self.rows(self.table())
        self.assertAlmostEqual(rows["total_drr"]["days"][0], 10.0, places=3)

    def test_the_cabinet_funnel_rows_appear(self):
        """Переходы и корзины кабинета — то, чего в рекламе не видно."""
        self.day("2026-11-01")
        self.funnel("2026-11-01", opens=500, carts=80)

        rows = self.rows(self.table())
        self.assertEqual(rows["opens"]["days"], [500.0])
        self.assertEqual(rows["carts"]["days"], [80.0])
        self.assertAlmostEqual(rows["cr_cart_cabinet"]["days"][0], 16.0)

    def test_the_funnel_rows_are_hidden_without_data(self):
        self.day("2026-11-01")
        self.assertNotIn("opens", self.rows(self.table()))

    def test_a_period_total_adds_the_days_up(self):
        """Итог периода — сумма дней, а не последний день."""
        for day in ("2026-11-01", "2026-11-02"):
            self.day(day)
            self.funnel(day, orders=10, orders_sum=6460.0)

        rows = self.rows(self.table("2026-11-01", "2026-11-02"))
        self.assertEqual(rows["total_orders"]["total"], 20.0)
        self.assertEqual(rows["total_revenue"]["total"], 12920.0)

    def test_one_article_is_counted_alone(self):
        self.day("2026-11-01")
        self.funnel("2026-11-01", nm_id=777, orders=10, orders_sum=6460.0)
        self.funnel("2026-11-01", nm_id=888, orders=3, orders_sum=1000.0)

        table = dynamics.table(self.conn, "2026-11-01", "2026-11-01",
                               nm_id=777)
        self.assertEqual(self.rows(table)["total_orders"]["days"], [10.0])

    def test_a_repeated_collection_replaces_the_day(self):
        """WB досчитывает воронку раз в два часа: вечернее число меняется."""
        self.funnel("2026-11-01", orders=10, orders_sum=6460.0)
        self.funnel("2026-11-01", orders=14, orders_sum=9000.0)

        rows = db.funnel_by_day(self.conn, "2026-11-01", "2026-11-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders"], 14)

    def test_the_analytics_tab_counts_the_same_turnover(self):
        """Два экрана с разным оборотом — это два повода не верить ни
        одному. Вкладка «Аналитика» обязана брать воронку тоже."""
        from wbads import analytics

        self.day("2026-11-01", spend=646.0)
        self.order("2026-11-01", srid="a")
        self.funnel("2026-11-01", orders=10, orders_sum=6460.0)

        report = analytics.build_report(self.conn, "2026-11-01",
                                        "2026-11-01",
                                        analytics.Thresholds(), with_nm=False)
        self.assertEqual(report["orders"]["revenue"], 6460.0)
        self.assertEqual(report["orders"]["orders"], 10.0)
        self.assertAlmostEqual(report["orders"]["total_drr"], 10.0, places=3)

    def test_the_analytics_tab_falls_back_on_days_without_the_funnel(self):
        """За день без воронки оборот считается по заказам — иначе
        вчерашний день выглядел бы нулевым."""
        from wbads import analytics

        self.day("2026-11-01", spend=646.0)
        self.order("2026-11-01", srid="a")

        report = analytics.build_report(self.conn, "2026-11-01",
                                        "2026-11-01",
                                        analytics.Thresholds(), with_nm=False)
        self.assertEqual(report["orders"]["revenue"], 1000.0)
        self.assertEqual(report["orders"]["source"], "orders")


class TestFunnelNormalize(unittest.TestCase):
    """Разбор ответа WB: числа лежат внутри statistic.selected."""

    def row(self, raw):
        from wbads import collector

        return collector.normalize_funnel(raw, "2026-11-24", "now")

    def test_a_card_becomes_a_row(self):
        got = self.row({
            "product": {"nmId": 777, "title": "Чашка"},
            "statistic": {"selected": {
                "orderCount": 1493, "orderSum": 964171.0,
                "buyoutCount": 800, "buyoutSum": 500000.0,
                "cancelCount": 23, "cancelSum": 14000.0,
                "openCount": 50000, "cartCount": 3000}},
        })
        self.assertEqual(got["nm_id"], 777)
        self.assertEqual(got["orders"], 1493)
        self.assertEqual(got["orders_sum"], 964171.0)
        self.assertEqual(got["buyouts"], 800)
        self.assertEqual(got["cancels"], 23)
        self.assertEqual(got["opens"], 50000)
        self.assertEqual(got["carts"], 3000)

    def test_a_card_without_an_article_is_skipped(self):
        self.assertIsNone(self.row({"statistic": {"selected": {}}}))

    def test_missing_numbers_become_zero(self):
        got = self.row({"product": {"nmID": 777}})
        self.assertEqual(got["orders"], 0)
        self.assertEqual(got["orders_sum"], 0.0)

    def test_the_period_total_is_summed_from_the_cards(self):
        from wbads import reconcile

        total = reconcile.funnel_totals([
            {"statistic": {"selected": {"orderCount": 1000,
                                        "orderSum": 600000.0}}},
            {"statistic": {"selected": {"orderCount": 493,
                                        "orderSum": 364171.0}}},
        ])
        self.assertEqual(total["orders"], 1493)
        self.assertEqual(total["orders_sum"], 964171.0)
        self.assertEqual(total["cards"], 2)


class TestFunnelCollection(DynamicsCase):
    """Воронка отдаётся по одному дню за запрос, а запросов три в
    минуту. Значит, сбор обязан спрашивать только то, чего нет, и не
    запрашивать месяц за один раз."""

    def collect(self, start, end, **kw):
        from datetime import date as dt
        from unittest.mock import patch

        from wbads import collector

        asked: list[tuple[str, str]] = []

        def fake(self, s, e, on_progress=None, max_pages=1):
            asked.append((s, e))
            return [{"product": {"nmId": 777},
                     "statistic": {"selected": {"orderCount": 5,
                                                "orderSum": 500.0}}}]

        with patch.multiple("wbads.wb_client.WBAnalyticsClient",
                            sales_funnel=fake):
            result = collector.collect_funnel(
                self.conn, "token", dt.fromisoformat(start),
                dt.fromisoformat(end), **kw)
        return asked, result

    def test_a_day_is_one_request(self):
        asked, result = self.collect("2026-11-01", "2026-11-03")
        self.assertEqual(asked, [("2026-11-03", "2026-11-03"),
                                 ("2026-11-02", "2026-11-02"),
                                 ("2026-11-01", "2026-11-01")])
        self.assertEqual(result["funnel_days"], 3)

    def test_collected_days_are_not_asked_again(self):
        """Переспрашивать собранное — это потерянные минуты."""
        self.collect("2026-11-01", "2026-11-03")
        asked, _ = self.collect("2026-11-01", "2026-11-03")
        # Два свежих дня спрашиваем всегда: WB их ещё досчитывает.
        self.assertEqual(asked, [("2026-11-03", "2026-11-03"),
                                 ("2026-11-02", "2026-11-02")])

    def test_a_long_period_is_taken_in_parts(self):
        asked, result = self.collect("2026-11-01", "2026-11-30", max_days=3)
        self.assertEqual(len(asked), 3)
        self.assertEqual(asked[0], ("2026-11-30", "2026-11-30"))
        self.assertEqual(result["funnel_left"], 27)

    def test_the_numbers_land_in_the_base(self):
        self.collect("2026-11-01", "2026-11-01")
        rows = db.funnel_by_day(self.conn, "2026-11-01", "2026-11-01")
        self.assertEqual(rows[0]["orders"], 5)
        self.assertEqual(rows[0]["orders_sum"], 500.0)
