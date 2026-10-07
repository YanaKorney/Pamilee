"""Сводка по дням: показатели по строкам, дни по колонкам.

Главная опасность этой таблицы — колонка «За период». Половина строк в
ней это отношения (ДРР, CPC, CTR, конверсии), и ни складывать их по
строке, ни усреднять нельзя: день с расходом в десять рублей весил бы в
среднем столько же, сколько день с расходом в десять тысяч. Здесь это и
проверяется в первую очередь.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import isolate  # noqa: E402,F401 — должен идти до wbads: пути берутся при импорте

from wbads import daily, db  # noqa: E402


class DailyCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init_db(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.conn.close)

    def day(self, date, *, advert_id=1, nm_id=777, views=1000, clicks=20,
            atbs=4, orders=2, spend=200.0, revenue=2000.0):
        self.conn.execute(
            "INSERT OR REPLACE INTO campaign_daily (advert_id, date, views,"
            " clicks, atbs, orders, shks, spend, revenue, collected_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,'now')",
            (advert_id, date, views, clicks, atbs, orders, orders, spend, revenue))
        self.conn.execute(
            "INSERT OR REPLACE INTO campaign_nm_daily (advert_id, date, nm_id,"
            " name, views, clicks, atbs, orders, shks, spend, revenue)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (advert_id, date, nm_id, "Товар", views, clicks, atbs, orders,
             orders, spend, revenue))
        self.conn.commit()

    def rows(self, table):
        return {row["key"]: row for row in table["rows"]}


class TestShapeMatchesTheTemplate(DailyCase):
    """Форма взята из таблицы, которую менеджер ведёт руками. Привычная
    для аналитики форма обратная — строка на день, — но переучивать
    человека там, где учиться нечему, незачем."""

    def test_rows_are_metrics_and_columns_are_days(self):
        self.day("2026-10-01")
        self.day("2026-10-02")
        table = daily.daily_table(self.conn, "2026-10-01", "2026-10-02")
        self.assertEqual(table["dates"], ["2026-10-01", "2026-10-02"])
        for row in table["rows"]:
            self.assertEqual(len(row["days"]), 2,
                             f"{row['title']}: по значению на каждый день")

    def test_every_metric_from_the_sample_is_present(self):
        titles = [row["title"] for row in
                  daily.daily_table(self.conn, "2026-10-01", "2026-10-01")["rows"]]
        for needed in ("Показы", "Заказы в шт", "Заказы в руб",
                       "Затраты на рекламу", "ДРР", "CPC", "CPO", "CTR",
                       "Конверсия в корзину", "Конверсия в заказ"):
            self.assertIn(needed, titles)

    def test_days_without_spend_are_zeros_not_gaps(self):
        """Пропуск в календаре скрыл бы «три дня не крутилось»."""
        self.day("2026-10-01")
        self.day("2026-10-05")
        table = daily.daily_table(self.conn, "2026-10-01", "2026-10-05")
        self.assertEqual(len(table["dates"]), 5)
        self.assertEqual(self.rows(table)["views"]["days"][2], 0)


class TestPeriodColumnIsNotASum(DailyCase):
    """Самое опасное место таблицы."""

    def test_counts_and_money_add_up(self):
        self.day("2026-10-01", views=1000, spend=100.0, revenue=1000.0)
        self.day("2026-10-02", views=3000, spend=300.0, revenue=3000.0)
        rows = self.rows(daily.daily_table(self.conn, "2026-10-01", "2026-10-02"))
        self.assertEqual(rows["views"]["total"], 4000)
        self.assertEqual(rows["spend"]["total"], 400)

    def test_drr_is_weighted_by_money_not_averaged(self):
        """День с расходом 10 ₽ не должен весить столько же, сколько день
        с расходом 10 000 ₽."""
        self.day("2026-10-01", spend=10.0, revenue=1000.0)      # ДРР 1%
        self.day("2026-10-02", spend=10000.0, revenue=10000.0)  # ДРР 100%
        rows = self.rows(daily.daily_table(self.conn, "2026-10-01", "2026-10-02"))
        naive = (1.0 + 100.0) / 2
        self.assertAlmostEqual(rows["drr"]["total"], 10010 / 11000 * 100, places=4)
        self.assertNotAlmostEqual(rows["drr"]["total"], naive, places=1)

    def test_ratios_are_marked_as_such(self):
        """Колонка «За период» иначе читается как сумма строки."""
        rows = self.rows(daily.daily_table(self.conn, "2026-10-01", "2026-10-01"))
        self.assertTrue(rows["drr"]["is_ratio"])
        self.assertTrue(rows["ctr"]["is_ratio"])
        self.assertFalse(rows["views"]["is_ratio"])
        self.assertFalse(rows["spend"]["is_ratio"])

    def test_cpc_and_cpo_are_weighted_too(self):
        self.day("2026-10-01", clicks=10, orders=1, spend=100.0)
        self.day("2026-10-02", clicks=90, orders=9, spend=900.0)
        rows = self.rows(daily.daily_table(self.conn, "2026-10-01", "2026-10-02"))
        self.assertAlmostEqual(rows["cpc"]["total"], 1000 / 100, places=4)
        self.assertAlmostEqual(rows["cpo"]["total"], 1000 / 10, places=4)

    def test_empty_period_does_not_divide_by_zero(self):
        table = daily.daily_table(self.conn, "2026-10-01", "2026-10-03")
        self.assertFalse(table["has_data"])
        for row in table["rows"]:
            self.assertEqual(row["total"], 0)


class TestOneArticle(DailyCase):
    """Правки делают в кампании, а смотрят на товар, и один товар часто
    крутится сразу в нескольких кампаниях."""

    def test_article_sees_only_its_own_numbers(self):
        self.day("2026-10-01", nm_id=777, spend=100.0)
        self.day("2026-10-01", nm_id=888, advert_id=2, spend=900.0)
        mine = self.rows(daily.daily_table(self.conn, "2026-10-01",
                                           "2026-10-01", nm_id=777))
        self.assertEqual(mine["spend"]["total"], 100)

    def test_article_sums_across_its_campaigns(self):
        self.day("2026-10-01", nm_id=777, advert_id=1, spend=100.0)
        self.day("2026-10-01", nm_id=777, advert_id=2, spend=250.0)
        mine = self.rows(daily.daily_table(self.conn, "2026-10-01",
                                           "2026-10-01", nm_id=777))
        self.assertEqual(mine["spend"]["total"], 350)

    def test_whole_cabinet_is_the_default(self):
        self.day("2026-10-01", nm_id=777, spend=100.0)
        self.day("2026-10-01", nm_id=888, advert_id=2, spend=900.0)
        table = daily.daily_table(self.conn, "2026-10-01", "2026-10-01")
        self.assertIsNone(table["nm_id"])
        self.assertEqual(self.rows(table)["spend"]["total"], 1000)


class TestArticleList(DailyCase):
    """Человек помнит товар по обороту, а не по месту в алфавите."""

    def test_biggest_spender_comes_first(self):
        self.day("2026-10-01", nm_id=111, advert_id=1, spend=50.0)
        self.day("2026-10-01", nm_id=222, advert_id=2, spend=500.0)
        found = daily.articles(self.conn, "2026-10-01", "2026-10-01")
        self.assertEqual([a["nm_id"] for a in found], [222, 111])

    def test_articles_outside_the_period_are_not_listed(self):
        self.day("2026-09-01", nm_id=111)
        found = daily.articles(self.conn, "2026-10-01", "2026-10-07")
        self.assertEqual(found, [])

    def test_names_from_orders_win_over_the_ad_name(self):
        """В заказах лежат предмет и артикул продавца — понятнее номера."""
        self.day("2026-10-01", nm_id=111)
        found = daily.articles(self.conn, "2026-10-01", "2026-10-01",
                               names={111: "Шампунь · ART-1"})
        self.assertEqual(found[0]["name"], "Шампунь · ART-1")


class TestExport(DailyCase):
    """Выгрузку открывают в Excel и доделывают руками."""

    def test_csv_keeps_the_same_shape(self):
        self.day("2026-10-01", views=1000)
        self.day("2026-10-02", views=2000)
        text = daily.to_csv(
            daily.daily_table(self.conn, "2026-10-01", "2026-10-02"),
            "Весь кабинет")
        lines = text.splitlines()
        self.assertEqual(lines[0], "Весь кабинет")
        self.assertIn("01.10.2026", lines[1], "дату Excel не должен съесть")
        self.assertIn("За период", lines[1])
        self.assertTrue(any(line.startswith("Показы;") for line in lines))

    def test_decimal_comma_for_russian_excel(self):
        self.day("2026-10-01", spend=12.5, revenue=100.0)
        text = daily.to_csv(daily.daily_table(self.conn, "2026-10-01",
                                              "2026-10-01"))
        self.assertIn("12,50", text)
        self.assertNotIn("12.50", text)


if __name__ == "__main__":
    unittest.main()


class TestArticleBreakdownFromWB(unittest.TestCase):
    """Разбивка по артикулам приходит в поле «nms». В старом методе WB
    оно называлось «nm», и при переходе на v3 это осталось незамеченным:
    ответ приходил, дни сохранялись, а товары молча терялись. Видно было
    только по следствиям — пустой список товаров в сводке, пустые
    «артикулы внутри кампании» и «не с чем сравнивать» в журнале."""

    # Форма ответа GET /adv/v3/fullstats — как в спецификации WB.
    V3 = {
        "advertId": 22161678,
        "days": [{
            "date": "2026-10-01T00:00:00Z",
            "views": 784, "clicks": 75, "atbs": 2, "orders": 1, "shks": 1,
            "sum": 378.49, "sum_price": 5000.0,
            "apps": [
                {"appType": 1, "views": 300, "clicks": 30, "atbs": 1,
                 "orders": 0, "shks": 0, "sum": 150.0, "sum_price": 0.0,
                 "nms": [{"nmId": 221725278, "name": "постер 2", "views": 300,
                          "clicks": 30, "atbs": 1, "orders": 0, "shks": 0,
                          "sum": 150.0, "sum_price": 0.0}]},
                {"appType": 32, "views": 484, "clicks": 45, "atbs": 1,
                 "orders": 1, "shks": 1, "sum": 228.49, "sum_price": 5000.0,
                 "nms": [{"nmId": 221725278, "name": "постер 2", "views": 484,
                          "clicks": 45, "atbs": 1, "orders": 1, "shks": 1,
                          "sum": 228.49, "sum_price": 5000.0}]},
            ],
        }],
    }

    def test_articles_are_read_from_the_v3_answer(self):
        from wbads.collector import normalize_stats

        _, nm_rows = normalize_stats(self.V3, "now")
        self.assertTrue(nm_rows, "разбивка по артикулам обязана появиться")
        self.assertEqual(nm_rows[0]["nm_id"], 221725278)
        self.assertEqual(nm_rows[0]["name"], "постер 2")

    def test_platforms_are_summed_into_one_article(self):
        """Один товар встречается в нескольких площадках за день."""
        from wbads.collector import normalize_stats

        _, nm_rows = normalize_stats(self.V3, "now")
        self.assertEqual(len(nm_rows), 1)
        self.assertEqual(nm_rows[0]["views"], 784)
        self.assertAlmostEqual(nm_rows[0]["spend"], 378.49, places=2)
        self.assertAlmostEqual(nm_rows[0]["revenue"], 5000.0, places=2)

    def test_article_totals_match_the_day(self):
        """Расхождение здесь означало бы, что сводка по товару и сводка
        по кабинету показывают разные деньги за один и тот же день."""
        from wbads.collector import normalize_stats

        days, nm_rows = normalize_stats(self.V3, "now")
        self.assertAlmostEqual(sum(r["spend"] for r in nm_rows),
                               days[0]["spend"], places=2)
        self.assertEqual(sum(r["views"] for r in nm_rows), days[0]["views"])

    def test_old_field_name_still_works(self):
        """У кого-то в базе могли остаться ответы старого вида."""
        from wbads.collector import normalize_stats

        old = {"advertId": 1, "days": [{"date": "2026-10-01", "apps": [
            {"nm": [{"nmId": 555, "views": 10, "clicks": 1, "sum": 5.0}]}]}]}
        _, nm_rows = normalize_stats(old, "now")
        self.assertEqual(nm_rows[0]["nm_id"], 555)
