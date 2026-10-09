"""Сводки по товарам и по кампаниям: показатели по строкам, дни по колонкам.

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


class TestArticlePicker(unittest.TestCase):
    """Артикулов у кабинета полтысячи. Обычный выпадающий список не умеет
    строки поиска, и выбрать в нём товар можно только пролистыванием —
    это не работа. Проверяется устройство поля: сами действия (набор,
    стрелки, Escape) живут в браузере и проверены вручную."""

    def setUp(self):
        from wbads.config import ROOT

        self.html = (ROOT / "wbads" / "web" / "index.html").read_text(
            encoding="utf-8")
        self.js = (ROOT / "wbads" / "web" / "app.js").read_text(encoding="utf-8")

    def test_field_is_a_text_input_with_a_list(self):
        self.assertIn('id="daily-nm-input"', self.html)
        self.assertIn('role="combobox"', self.html)
        self.assertIn('id="daily-nm-list"', self.html)
        self.assertNotIn('<select id="daily-nm">', self.html,
                         "обычный список не умеет поиска")

    def test_rows_are_bare_article_numbers(self):
        """В строке только номер: ни названия, ни расхода. Так просили."""
        self.assertIn("setItems((data.articles || []).map", self.js)
        self.assertIn("String(a.nm_id)", self.js)
        self.assertNotIn("${name}${a.nm_id} — ${money(a.spend)}", self.js)

    def test_search_filters_by_digits(self):
        self.assertIn("c.items.filter((id) => id.includes(text))", self.js)

    def test_whole_cabinet_stays_reachable(self):
        self.assertIn("Весь кабинет", self.js)
        self.assertIn("combo-clear", self.html)

    def test_long_list_is_capped(self):
        """Полтысячи строк разом браузер рисует заметно, а пользы нет:
        нужный артикул находится набором двух-трёх цифр."""
        self.assertIn("COMBO_LIMIT", self.js)
        self.assertIn("Наберите ещё цифру", self.js)

    def test_keyboard_is_supported(self):
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(key, self.js, f"нет обработки {key}")


class TestZonesAddUpToTheTotal(DailyCase):
    """Подстроки зон обязаны сходиться со строкой «Показы». Иначе таблица
    врёт на глазах: сумма двух строк не равна строке над ними."""

    def search(self, date, *, advert_id=1, nm_id=777, views=0):
        self.conn.execute(
            "INSERT OR REPLACE INTO campaign_nm_search_daily (advert_id, date,"
            " nm_id, views, clicks, atbs, orders, shks, spend)"
            " VALUES (?,?,?,?,0,0,0,0,0)", (advert_id, date, nm_id, views))
        self.conn.commit()

    def test_search_plus_other_equals_views(self):
        self.day("2026-10-01", views=1000)
        self.search("2026-10-01", views=400)
        rows = self.rows(daily.daily_table(self.conn, "2026-10-01", "2026-10-01"))
        self.assertEqual(rows["views_search"]["days"][0], 400)
        self.assertEqual(rows["views_other"]["days"][0], 600)
        self.assertEqual(
            rows["views_search"]["days"][0] + rows["views_other"]["days"][0],
            rows["views"]["days"][0])

    def test_without_search_data_everything_is_other(self):
        """Пока поиск не собран, весь остаток нельзя выдавать за полки."""
        self.day("2026-10-01", views=1000)
        table = daily.daily_table(self.conn, "2026-10-01", "2026-10-01")
        self.assertFalse(table["has_zones"], "без данных поиска зон нет")

    def test_search_above_total_is_clamped(self):
        """Два метода WB считают по-своему. Показать больше ста процентов
        или отрицательный остаток хуже, чем прижать к границе."""
        self.day("2026-10-01", views=100)
        self.search("2026-10-01", views=150)
        rows = self.rows(daily.daily_table(self.conn, "2026-10-01", "2026-10-01"))
        self.assertEqual(rows["views_search"]["days"][0], 100)
        self.assertEqual(rows["views_other"]["days"][0], 0)

    def test_zones_follow_the_chosen_article(self):
        self.day("2026-10-01", nm_id=777, views=1000)
        self.day("2026-10-01", nm_id=888, advert_id=2, views=5000)
        self.search("2026-10-01", nm_id=777, views=400)
        self.search("2026-10-01", nm_id=888, advert_id=2, views=4000)
        mine = self.rows(daily.daily_table(self.conn, "2026-10-01",
                                           "2026-10-01", nm_id=777))
        self.assertEqual(mine["views_search"]["days"][0], 400)
        self.assertEqual(mine["views_other"]["days"][0], 600)

    def test_period_column_sums_the_zones(self):
        self.day("2026-10-01", views=1000)
        self.day("2026-10-02", views=2000)
        self.search("2026-10-01", views=400)
        self.search("2026-10-02", views=900)
        rows = self.rows(daily.daily_table(self.conn, "2026-10-01", "2026-10-02"))
        self.assertEqual(rows["views_search"]["total"], 1300)
        self.assertEqual(rows["views_other"]["total"], 1700)
        self.assertEqual(rows["views_search"]["total"] + rows["views_other"]["total"],
                         rows["views"]["total"])

    def test_export_skips_zones_until_search_is_collected(self):
        self.day("2026-10-01", views=1000)
        text = daily.to_csv(daily.daily_table(self.conn, "2026-10-01",
                                              "2026-10-01"))
        self.assertNotIn("прочее", text)

    def test_export_carries_zones_once_collected(self):
        self.day("2026-10-01", views=1000)
        self.search("2026-10-01", views=400)
        text = daily.to_csv(daily.daily_table(self.conn, "2026-10-01",
                                              "2026-10-01"))
        self.assertIn("в поиске", text)
        self.assertIn("прочее", text)

    def test_zone_rows_are_marked_as_sub_rows(self):
        self.assertTrue(all(row.get("zone") for row in daily.ZONE_ROWS))
        titles = [row["title"] for row in daily.ZONE_ROWS]
        self.assertEqual(titles[0], "в поиске")
        self.assertIn("прочее", titles[1])


class TestSearchStatsCollection(unittest.TestCase):
    """Показы в поиске собираются отдельным методом: статистика по
    поисковым кластерам. За день кластеров несколько, и каждый приходит
    своей строкой — нужна их сумма, а не сами кластеры."""

    ANSWER = [{
        "advertId": 123, "nmId": 777,
        "dailyStats": [
            {"date": "2026-10-01", "stat": {"views": 192, "clicks": 75,
                                            "atbs": 39, "orders": 9,
                                            "shks": 5, "spend": 108,
                                            "normQuery": "кластер А"}},
            {"date": "2026-10-01", "stat": {"views": 108, "clicks": 25,
                                            "atbs": 11, "orders": 3,
                                            "shks": 2, "spend": 42,
                                            "normQuery": "кластер Б"}},
            {"date": "2026-10-02", "stat": {"views": 50, "clicks": 10,
                                            "spend": 20}},
        ],
    }]

    def test_clusters_of_one_day_are_summed(self):
        from wbads.collector import normalize_search_stats

        rows = {r["date"]: r for r in normalize_search_stats(self.ANSWER)}
        self.assertEqual(rows["2026-10-01"]["views"], 300)
        self.assertEqual(rows["2026-10-01"]["clicks"], 100)
        self.assertEqual(rows["2026-10-01"]["spend"], 150.0)
        self.assertEqual(rows["2026-10-02"]["views"], 50)

    def test_campaign_and_article_are_kept(self):
        from wbads.collector import normalize_search_stats

        row = normalize_search_stats(self.ANSWER)[0]
        self.assertEqual(row["advert_id"], 123)
        self.assertEqual(row["nm_id"], 777)

    def test_pairs_come_from_collected_statistics(self):
        """Спрашивать про пары, которые не крутились, — значит тратить
        минуты на заведомо пустые ответы."""
        from wbads.collector import search_pairs

        pairs = search_pairs([
            {"advert_id": 1, "nm_id": 10, "views": 50},
            {"advert_id": 2, "nm_id": 20, "views": 900},
            {"advert_id": 2, "nm_id": 20, "views": 100},
        ])
        self.assertEqual(pairs[0], (2, 20), "крупные — первыми")
        self.assertEqual(len(pairs), 2, "повторы схлопываются")

    def test_junk_rows_are_skipped(self):
        from wbads.collector import normalize_search_stats, search_pairs

        self.assertEqual(normalize_search_stats([{"advertId": 0, "nmId": 0}]), [])
        self.assertEqual(search_pairs([{"advert_id": 0, "nm_id": 5}]), [])

    def test_request_splits_by_hundred_pairs(self):
        """Метод принимает не больше сотни пар за раз."""
        from wbads.wb_client import MAX_PAIRS_PER_SEARCH_CALL, WBAdvertClient

        client = WBAdvertClient.__new__(WBAdvertClient)
        client.token = "t"
        client.base_url = "x"
        client.timeout = 1
        client.max_retries = 1
        client.ca_bundle = ""
        client.used_fallback_bundle = False
        client._last_call = 0.0
        client._wait_turn = lambda on_progress=None, cooldown=0: None
        sent = []

        def request(method, path, payload=None, params=None, on_progress=None):
            sent.append((method, path, len(payload["items"])))
            return {"items": []}

        client._request = request
        client.search_stats([(1, i) for i in range(250)],
                            "2026-10-01", "2026-10-07")
        self.assertEqual([n for _, _, n in sent], [100, 100, 50])
        self.assertEqual(sent[0][0], "POST")
        self.assertEqual(sent[0][1], "/adv/v1/normquery/stats")

    def test_campaigns_without_clusters_are_not_an_error(self):
        """У части кампаний поисковых кластеров нет вовсе."""
        from wbads.wb_client import WBAdvertClient, WBError

        client = WBAdvertClient.__new__(WBAdvertClient)
        client.token = "t"
        client.base_url = "x"
        client.timeout = 1
        client.max_retries = 1
        client.ca_bundle = ""
        client.used_fallback_bundle = False
        client._last_call = 0.0
        client._wait_turn = lambda on_progress=None, cooldown=0: None
        client._request = lambda *a, **kw: (_ for _ in ()).throw(
            WBError("нет данных", 400))

        self.assertEqual(client.search_stats([(1, 2)], "2026-10-01",
                                             "2026-10-07"), [])


class TestCollectionIsReachableWithoutTheTerminal(unittest.TestCase):
    """Обновление программы не дополняет старые данные: зоны показа WB
    отдаёт отдельным запросом, и строки «в поиске» и «прочее» пустуют,
    пока сбор не прошёл заново. Человек, который работает двойным кликом,
    должен узнать об этом из таблицы и запустить сбор тем же кликом —
    иначе он смотрит на пустые строки и считает, что программа врёт.
    """

    def setUp(self):
        from wbads.config import ROOT

        self.root = ROOT
        self.js = (ROOT / "wbads" / "web" / "app.js").read_text(encoding="utf-8")

    def test_table_says_the_rows_are_not_collected_yet(self):
        self.assertIn("ещё не собраны", self.js)
        self.assertIn("SBOR-Windows.bat", self.js)

    def test_launchers_exist_for_both_systems(self):
        for name in ("SBOR-Windows.bat", "SBOR-Mac.command"):
            self.assertTrue((self.root / name).exists(), f"нет файла {name}")

    def test_launchers_run_the_collection(self):
        for name in ("SBOR-Windows.bat", "SBOR-Mac.command"):
            text = (self.root / name).read_text(encoding="utf-8")
            self.assertIn("run.py collect", text, f"{name} не запускает сбор")

    def test_windows_launcher_stays_ascii_with_crlf(self):
        """cmd.exe читает .bat в кодировке системы, а не в UTF-8: русская
        буква в самом файле превращает команду в мусор. Перевод строки
        тоже обязан быть виндовым."""
        raw = (self.root / "SBOR-Windows.bat").read_bytes()
        raw.decode("ascii")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_mac_launcher_is_executable(self):
        import os

        self.assertTrue(os.access(self.root / "SBOR-Mac.command", os.X_OK),
                        "без права на запуск двойной клик откроет редактор")


class TestCampaignSummary(DailyCase):
    """Один товар почти всегда крутится в нескольких кампаниях сразу.
    В общей сумме пропадает именно то, по чему принимают решение: ручная
    на поиске может кормить, пока автоматическая на рекомендациях жжёт.
    Поэтому по таблице на кампанию.
    """

    def campaign(self, advert_id, name, type_name, status_name="Идут показы"):
        self.conn.execute(
            "INSERT OR REPLACE INTO campaigns (advert_id, name, type,"
            " type_name, status, status_name, daily_budget, updated_at)"
            " VALUES (?,?,9,?,9,?,0,'now')",
            (advert_id, name, type_name, status_name))
        self.conn.commit()

    def setUp(self):
        super().setUp()
        self.campaign(1, "Поиск руками", "Аукцион, ручная ставка")
        self.campaign(2, "Рекомендации", "Аукцион, единая ставка")
        self.day("2026-10-01", advert_id=1, spend=100.0, revenue=2000.0,
                 views=500)
        self.day("2026-10-01", advert_id=2, spend=400.0, revenue=500.0,
                 views=900)

    def table(self):
        return daily.campaign_tables(self.conn, "2026-10-01", "2026-10-01", 777)

    def test_one_table_per_campaign(self):
        blocks = self.table()["campaigns"]
        self.assertEqual([b["advert_id"] for b in blocks], [2, 1],
                         "порядок задаёт расход: крупная кампания сверху")

    def test_rows_match_the_article_summary(self):
        """Строки те же и в том же порядке — человек не переучивается."""
        article = daily.daily_table(self.conn, "2026-10-01", "2026-10-01", 777)
        block = self.table()["campaigns"][0]
        self.assertEqual([r["key"] for r in block["rows"]],
                         [r["key"] for r in article["rows"]])

    def test_numbers_belong_to_their_own_campaign(self):
        blocks = {b["advert_id"]: self.rows(b) for b in self.table()["campaigns"]}
        self.assertEqual(blocks[1]["spend"]["total"], 100.0)
        self.assertEqual(blocks[2]["spend"]["total"], 400.0)
        # ДРР считается внутри кампании, а не делением на общую выручку.
        self.assertEqual(blocks[1]["drr"]["total"], 5.0)
        self.assertEqual(blocks[2]["drr"]["total"], 80.0)

    def test_campaign_type_comes_along(self):
        """Тип кампании важнее номера: «ручная» и «единая» — разные работы."""
        blocks = {b["advert_id"]: b for b in self.table()["campaigns"]}
        self.assertEqual(blocks[1]["type_name"], "Аукцион, ручная ставка")
        self.assertEqual(blocks[2]["name"], "Рекомендации")
        self.assertEqual(blocks[1]["status_name"], "Идут показы")

    def test_campaign_without_a_card_is_named_by_number(self):
        """Карточка кампании отдаётся не всегда, а строка без названия
        читается как пропажа данных."""
        self.day("2026-10-01", advert_id=3)
        blocks = {b["advert_id"]: b for b in self.table()["campaigns"]}
        self.assertEqual(blocks[3]["name"], "Кампания 3")

    def test_zones_are_counted_per_campaign(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO campaign_nm_search_daily (advert_id, date,"
            " nm_id, views, clicks, atbs, orders, shks, spend)"
            " VALUES (1,'2026-10-01',777,200,0,0,0,0,0)")
        self.conn.commit()
        blocks = {b["advert_id"]: b for b in self.table()["campaigns"]}
        self.assertTrue(blocks[1]["has_zones"])
        self.assertEqual(self.rows(blocks[1])["views_search"]["total"], 200.0)
        self.assertEqual(self.rows(blocks[1])["views_other"]["total"], 300.0)
        # У второй кампании поиск не спрашивали — выдумывать нечего.
        self.assertFalse(blocks[2]["has_zones"])

    def test_csv_separates_the_tables(self):
        """Слитые в один блок таблицы читаются как одна кампания с
        удвоенными числами."""
        text = daily.campaigns_to_csv(self.table(), "Артикул 777")
        self.assertIn("Артикул 777", text)
        self.assertIn("Кампания 2 · Рекомендации", text)
        self.assertIn("Кампания 1 · Поиск руками", text)
        self.assertEqual(text.count("Дата;"), 2, "шапка на каждую таблицу")


class TestCampaignSummaryApi(DailyCase):
    """Вкладка не должна встречать человека пустотой: артикул, который
    тратит больше всех, выбирается сам."""

    def setUp(self):
        super().setUp()
        self.day("2026-10-01", advert_id=1, nm_id=111, spend=50.0)
        self.day("2026-10-01", advert_id=2, nm_id=222, spend=900.0)

    def call(self, query):
        from wbads import api
        from wbads.config import Config

        cfg = Config(db_path=Path(self.tmp.name) / "t.db", token="t")
        return api.handle_campaign_daily(self.conn, cfg, query)

    def test_biggest_spender_is_chosen_by_default(self):
        data = self.call({"from": ["2026-10-01"], "to": ["2026-10-01"]})
        self.assertEqual(data["nm_id"], 222)
        self.assertEqual([b["advert_id"] for b in data["campaigns"]], [2])

    def test_chosen_article_wins(self):
        data = self.call({"from": ["2026-10-01"], "to": ["2026-10-01"],
                          "nm_id": ["111"]})
        self.assertEqual(data["nm_id"], 111)
        self.assertEqual([b["advert_id"] for b in data["campaigns"]], [1])

    def test_picker_lists_the_articles(self):
        data = self.call({"from": ["2026-10-01"], "to": ["2026-10-01"]})
        self.assertEqual([a["nm_id"] for a in data["articles"]], [222, 111])

    def test_empty_base_is_not_an_error(self):
        other = db.init_db(Path(self.tmp.name) / "empty.db")
        self.addCleanup(other.close)
        from wbads import api
        from wbads.config import Config
        data = api.handle_campaign_daily(
            other, Config(db_path=Path(self.tmp.name) / "empty.db", token="t"),
            {"from": ["2026-10-01"], "to": ["2026-10-01"]})
        self.assertIsNone(data["nm_id"])
        self.assertEqual(data["campaigns"], [])
        self.assertFalse(data["has_data"])


class TestTabsNameWhatTheyShow(unittest.TestCase):
    """«Сводка по дням» не отвечала на вопрос, по чему сводка. Теперь их
    две: по товарам и по кампаниям, — и названия обязаны их различать."""

    def setUp(self):
        from wbads.config import ROOT

        self.html = (ROOT / "wbads" / "web" / "index.html").read_text(
            encoding="utf-8")
        self.js = (ROOT / "wbads" / "web" / "app.js").read_text(encoding="utf-8")

    def test_both_tabs_exist(self):
        self.assertIn("Сводка по товарам", self.html)
        self.assertIn("Сводка по кампаниям", self.html)
        self.assertNotIn("Сводка по дням", self.html)
        self.assertNotIn("Сводка по дням", self.js)

    def test_campaign_tab_has_its_own_picker_and_period(self):
        for ident in ('id="camps-nm-input"', 'id="camps-seg"',
                      'id="camps-from"', 'id="camps-list"',
                      'id="camps-export"'):
            self.assertIn(ident, self.html, f"нет {ident}")

    def test_campaign_tab_is_loaded_when_opened(self):
        self.assertIn("if (view === 'camps') loadCamps();", self.js)
        self.assertIn("/api/campaign-daily?", self.js)

    def test_both_pickers_share_one_behaviour(self):
        """Две копии обработчика клавиш — верный способ однажды починить
        одну и забыть другую."""
        self.assertEqual(self.js.count("function makeCombo"), 1)
        self.assertIn("const dailyCombo = makeCombo", self.js)
        self.assertIn("const campsCombo = makeCombo", self.js)

    def test_campaign_picker_has_no_whole_cabinet_row(self):
        """Кампании показываются по одному товару: «весь кабинет» здесь
        означал бы таблицу на каждую из пятисот кампаний."""
        self.assertIn("allLabel: ''", self.js)
