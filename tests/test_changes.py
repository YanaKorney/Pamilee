"""Журнал изменений и замер того, что из них вышло.

Число «эффект» опаснее отсутствия числа: по нему принимают решения. Здесь
проверяется не то, что расчёт выдаёт цифры, а то, что он не выдаёт цифры,
которые означают не то, чем кажутся, — и честно говорит, когда судить рано.
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wbads import changes, db  # noqa: E402


def day(offset: int, base: str = "2026-06-15") -> str:
    return (date.fromisoformat(base) + timedelta(days=offset)).isoformat()


class JournalCase(unittest.TestCase):
    """Общая заготовка: артикул с ровной статистикой по дням."""

    NM = 555000111
    ADVERT = 4242

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init_db(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.conn.close)

    def stats(self, offset: int, *, views=1000, clicks=20, atbs=4, orders=2,
              spend=200.0, revenue=2000.0, nm_id=None, advert_id=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO campaign_nm_daily"
            " (advert_id, date, nm_id, name, views, clicks, atbs, orders,"
            "  shks, spend, revenue) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (advert_id or self.ADVERT, day(offset), nm_id or self.NM, "Товар",
             views, clicks, atbs, orders, orders, spend, revenue))
        self.conn.commit()

    def fill(self, offsets, **kwargs):
        for offset in offsets:
            self.stats(offset, **kwargs)


class TestWritingDown(JournalCase):
    """Запись должна быть быстрой, но не бессмысленной."""

    def test_change_is_stored_and_listed(self):
        changes.add(self.conn, day(0), "снизила ставку до 120", nm_id=self.NM)
        rows = changes.listing(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "снизила ставку до 120")
        self.assertEqual(rows[0]["nm_id"], self.NM)

    def test_change_without_a_subject_is_refused(self):
        """Запись, не привязанная ни к товару, ни к кампании, нечем мерить."""
        with self.assertRaises(ValueError) as caught:
            changes.add(self.conn, day(0), "что-то поменяла")
        self.assertIn("артикул или кампанию", str(caught.exception))

    def test_empty_text_is_refused(self):
        with self.assertRaises(ValueError):
            changes.add(self.conn, day(0), "   ", nm_id=self.NM)

    def test_unreadable_date_is_refused(self):
        with self.assertRaises(ValueError):
            changes.add(self.conn, "позавчера", "ставка", nm_id=self.NM)

    def test_listing_is_newest_first(self):
        changes.add(self.conn, day(-5), "старая", nm_id=self.NM)
        changes.add(self.conn, day(0), "свежая", nm_id=self.NM)
        rows = changes.listing(self.conn)
        self.assertEqual(rows[0]["text"], "свежая")

    def test_repeated_import_does_not_double_records(self):
        """Повторный перенос той же таблицы не должен плодить дубли."""
        batch = [{"date": day(0), "nm_id": self.NM, "text": "почистила запросы"}]
        self.assertEqual(changes.add_many(self.conn, batch), 1)
        self.assertEqual(changes.add_many(self.conn, batch), 0)
        self.assertEqual(len(changes.listing(self.conn)), 1)

    def test_delete_removes_the_record(self):
        change_id = changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        self.assertTrue(changes.delete(self.conn, change_id))
        self.assertEqual(changes.listing(self.conn), [])


class TestWindowsAreHonest(JournalCase):
    """Окна сравнения — то место, где эффект легче всего подделать."""

    def test_the_day_of_the_change_is_excluded(self):
        """Правку вносят посреди дня: этот день наполовину старый."""
        self.fill(range(-7, 8))
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertEqual(result["before_period"][1], day(-1))
        self.assertEqual(result["after_period"][0], day(1))

    def test_next_change_cuts_the_window(self):
        """Иначе первая правка присвоит себе результат второй."""
        self.fill(range(-7, 8))
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        changes.add(self.conn, day(3), "запросы", nm_id=self.NM)
        first = [c for c in changes.listing(self.conn) if c["date"] == day(0)][0]
        result = changes.effect(self.conn, first, today=day(7))
        self.assertEqual(result["after_period"][1], day(2),
                         "окно обязано кончиться накануне следующей правки")
        self.assertTrue(result["cut_by_next"])

    def test_previous_change_cuts_the_window_too(self):
        self.fill(range(-7, 8))
        changes.add(self.conn, day(-2), "первая", nm_id=self.NM)
        changes.add(self.conn, day(0), "вторая", nm_id=self.NM)
        second = [c for c in changes.listing(self.conn) if c["date"] == day(0)][0]
        result = changes.effect(self.conn, second, today=day(7))
        self.assertEqual(result["before_period"][0], day(-1))

    def test_changes_on_other_articles_do_not_cut_anything(self):
        """Правка по другому товару к этому отношения не имеет."""
        self.fill(range(-7, 8))
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        changes.add(self.conn, day(2), "чужая правка", nm_id=999999)
        mine = [c for c in changes.listing(self.conn)
                if c["nm_id"] == self.NM][0]
        result = changes.effect(self.conn, mine, today=day(7))
        self.assertEqual(result["after_period"][1], day(7))
        self.assertFalse(result["cut_by_next"])

    def test_window_never_runs_past_the_data(self):
        self.fill(range(-7, 3))
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(2))
        self.assertEqual(result["after_period"][1], day(2))


class TestComparisonIsPerDay(JournalCase):
    """Самое опасное место: окна почти всегда разной длины."""

    def test_unequal_windows_do_not_fake_an_effect(self):
        """Семь одинаковых дней до и три одинаковых после — эффекта нет.

        Если сравнивать суммы, «после» окажется втрое меньше, и правка
        будет выглядеть провалом, хотя не изменилось ровно ничего.
        """
        self.fill(range(-7, 0))
        self.fill(range(1, 4))
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(3))
        self.assertEqual(result["before_days"], 7)
        self.assertEqual(result["after_days"], 3)
        for metric in result["metrics"]:
            self.assertAlmostEqual(
                metric["delta_pct"] or 0, 0, places=6,
                msg=f"{metric['title']} не менялся, а разница показана "
                    f"как {metric['delta_pct']}")
            # «neutral» — у расхода, направление которого не оценивается.
            self.assertIn(metric["direction"], ("same", "neutral"),
                          f"{metric['title']} показан как {metric['direction']}")

    def test_missing_days_do_not_dilute_the_average(self):
        """Несобранные дни не должны занижать среднее.

        Делить на длину окна нельзя: если сбор захватил не весь период,
        показатель упадёт ровно на долю несобранного.
        """
        self.fill([-7, -6])          # в окне «до» есть только два дня
        self.fill(range(1, 8))
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertEqual(result["before_days"], 2)
        views = [m for m in result["metrics"] if m["key"] == "views"][0]
        self.assertAlmostEqual(views["before"], 1000, places=6)

    def test_real_improvement_is_seen(self):
        self.fill(range(-7, 0), spend=400.0, revenue=2000.0)
        self.fill(range(1, 8), spend=200.0, revenue=2000.0)
        changes.add(self.conn, day(0), "снизила ставку", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        drr = [m for m in result["metrics"] if m["key"] == "drr"][0]
        self.assertAlmostEqual(drr["before"], 20.0, places=4)
        self.assertAlmostEqual(drr["after"], 10.0, places=4)
        self.assertEqual(drr["direction"], "better")
        self.assertIn("ДРР снизился", result["verdict"])
        self.assertIn(",", result["verdict"], "дробная часть — через запятую")

    def test_rising_spend_is_not_called_bad_on_its_own(self):
        """Рост расхода сам по себе ни плох, ни хорош: важно, что он принёс."""
        self.fill(range(-7, 0), spend=100.0)
        self.fill(range(1, 8), spend=300.0)
        changes.add(self.conn, day(0), "подняла бюджет", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        spend = [m for m in result["metrics"] if m["key"] == "spend"][0]
        self.assertEqual(spend["direction"], "neutral")

    def test_noise_under_one_percent_is_not_a_result(self):
        self.fill(range(-7, 0), views=1000)
        self.fill(range(1, 8), views=1005)
        changes.add(self.conn, day(0), "мелочь", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        views = [m for m in result["metrics"] if m["key"] == "views"][0]
        self.assertEqual(views["direction"], "same")


class TestItSaysWhenItCannotJudge(JournalCase):
    """Бодрые проценты по одному дню хуже честного «рано»."""

    def test_two_days_after_is_too_early(self):
        self.fill(range(-7, 0))
        self.fill(range(1, 3))
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(2))
        self.assertFalse(result["ready"])
        self.assertIn("мало", result["verdict"])

    def test_change_made_today_says_so(self):
        self.fill(range(-7, 1))
        changes.add(self.conn, day(0), "только что", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(0))
        self.assertIn("ещё не виден", result["verdict"])
        self.assertEqual(result["metrics"], [])

    def test_no_data_at_all_is_not_shown_as_zero_effect(self):
        """Пустая база не должна выглядеть как «ничего не изменилось»."""
        changes.add(self.conn, day(0), "ставка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertIn("Не с чем сравнивать", result["verdict"])
        self.assertEqual(result["metrics"], [])

    def test_crowded_days_are_counted(self):
        """Когда в один день правят десять кампаний, сдвиг общий."""
        for i in range(5):
            changes.add(self.conn, day(0), f"правка {i}", nm_id=100 + i)
        self.assertEqual(changes.crowding(self.conn, day(0)), 5)


class TestCampaignLevelChanges(JournalCase):
    """Правку делают в кампании, а следят за товаром — нужны обе связки."""

    def test_campaign_change_is_measured_on_the_campaign(self):
        self.fill(range(-7, 8))
        self.conn.executemany(
            "INSERT OR REPLACE INTO campaign_daily"
            " (advert_id, date, views, clicks, atbs, orders, shks, spend,"
            "  revenue, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(self.ADVERT, day(o), 1000, 20, 4, 2, 2,
              400.0 if o < 0 else 200.0, 2000.0, "now")
             for o in range(-7, 8)])
        self.conn.commit()
        changes.add(self.conn, day(0), "снизила ставку", advert_id=self.ADVERT)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        drr = [m for m in result["metrics"] if m["key"] == "drr"][0]
        self.assertAlmostEqual(drr["before"], 20.0, places=4)
        self.assertAlmostEqual(drr["after"], 10.0, places=4)

    def test_article_inside_a_campaign_narrows_the_measurement(self):
        """Указаны и товар, и кампания — меряем их пересечение."""
        self.fill(range(-7, 8), spend=200.0)
        self.fill(range(-7, 8), advert_id=9999, spend=5000.0)
        changes.add(self.conn, day(0), "ставка",
                    nm_id=self.NM, advert_id=self.ADVERT)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        spend = [m for m in result["metrics"] if m["key"] == "spend"][0]
        self.assertAlmostEqual(spend["before"], 200.0, places=4,
                               msg="чужая кампания не должна попадать в замер")


if __name__ == "__main__":
    unittest.main()


class TestVerdictDoesNotOverpromise(JournalCase):
    """Фраза словами обязана говорить только о заметном. В таблице точные
    числа видны и так, а «ДРР снизился с 24,9% до 24,6%» обещает результат
    там, где это обычное дневное колебание."""

    def test_tiny_drr_move_is_not_called_a_result(self):
        self.fill(range(-7, 0), spend=249.0, revenue=1000.0)
        self.fill(range(1, 8), spend=246.0, revenue=1000.0)
        changes.add(self.conn, day(0), "мелкая правка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertEqual(result["verdict"], "Заметных сдвигов в метриках нет.")

    def test_big_drr_move_is_stated(self):
        self.fill(range(-7, 0), spend=400.0, revenue=1000.0)
        self.fill(range(1, 8), spend=200.0, revenue=1000.0)
        changes.add(self.conn, day(0), "крупная правка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertIn("ДРР снизился", result["verdict"])


class TestToneMatchesTheWords(JournalCase):
    """Цвет плашки и её текст обязаны говорить одно и то же: зелёная
    плашка под фразой «заметных сдвигов нет» — это два разных мнения
    о результате, и человек поверит цвету."""

    def test_tiny_move_is_not_painted_as_a_win(self):
        self.fill(range(-7, 0), spend=249.0, revenue=1000.0)
        self.fill(range(1, 8), spend=246.0, revenue=1000.0)
        changes.add(self.conn, day(0), "мелкая правка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertEqual(result["tone"], "none")
        self.assertIn("нет", result["verdict"])

    def test_real_win_is_green(self):
        self.fill(range(-7, 0), spend=400.0, revenue=1000.0)
        self.fill(range(1, 8), spend=200.0, revenue=1000.0)
        changes.add(self.conn, day(0), "крупная правка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertEqual(result["tone"], "good")

    def test_real_loss_is_red(self):
        self.fill(range(-7, 0), spend=200.0, revenue=1000.0)
        self.fill(range(1, 8), spend=400.0, revenue=1000.0)
        changes.add(self.conn, day(0), "неудачная правка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertEqual(result["tone"], "bad")

    def test_early_verdicts_are_neutral(self):
        self.fill(range(-7, 0))
        self.fill([1])
        changes.add(self.conn, day(0), "вчерашняя правка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(1))
        self.assertEqual(result["tone"], "early")

    def test_nothing_to_compare_is_neutral_too(self):
        changes.add(self.conn, day(0), "правка", nm_id=self.NM)
        result = changes.effect(self.conn, changes.listing(self.conn)[0],
                                today=day(7))
        self.assertEqual(result["tone"], "early")
