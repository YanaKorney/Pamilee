"""Диагностика существует ровно для одного: показать все проблемы за один
запуск, а не по одной за раз.

Каждая ошибка, всплывавшая по одной, стоила человеку скачивания, запуска и
переписки. Поэтому у диагностики два обязательства, и оба проверяются здесь:
ни одна проба не отменяет остальные, и по каждому методу видно не только
«работает / не работает», но и ФОРМУ ОТВЕТА — при переезде метода ломается
обычно она, а не доступ.
"""

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wbads import diagnose  # noqa: E402

TOKEN = "header.payload.signature"


def http_error(code: int, body: bytes = b"", headers: dict | None = None):
    return urllib.error.HTTPError("https://advert-api.wildberries.ru/x", code,
                                  "Error", headers or {}, io.BytesIO(body))


class TestShapeIsReported(unittest.TestCase):
    """Форма ответа — главное, что даёт диагностика. «200, ноль записей»
    не отличает «данных нет» от «поля переименовали»; список полей отличает."""

    def test_fullstats_shape_names_the_fields(self):
        shape = diagnose.describe_shape([{
            "advertId": 22161678, "sum": 661.0, "views": 1373, "clicks": 139,
            "days": [{"date": "2025-09-07T00:00:00Z", "sum": 378.49,
                      "apps": [{"appType": 1, "nms": [{"nmId": 1, "sum": 10.19}]}]}],
        }])
        self.assertIn("advertId", shape)
        self.assertIn("days →", shape, "вложенные дни обязаны быть видны")
        self.assertIn("date", shape)

    def test_card_shape_shows_the_wrapper(self):
        """Карточки приходят завёрнутыми в объект — это надо видеть."""
        shape = diagnose.describe_shape({"adverts": [{"id": 1, "settings": {}}]})
        self.assertIn("объект с полями: adverts", shape)
        self.assertIn("adverts →", shape)

    def test_empty_answers_are_distinguished(self):
        self.assertIn("пустой ответ", diagnose.describe_shape(None))
        self.assertEqual(diagnose.describe_shape([]), "пустой список")

    def test_records_are_counted_inside_the_wrapper(self):
        self.assertEqual(diagnose.count_records({"adverts": [1, 2, 3]}), 3)
        self.assertEqual(diagnose.count_records([1, 2]), 2)
        self.assertEqual(diagnose.count_records(None), None)

    def test_plural_forms_are_right(self):
        self.assertIn("1 записи", diagnose.describe_shape([{"a": 1}]))
        self.assertIn("2 записей", diagnose.describe_shape([{"a": 1}, {"a": 2}]))


class TestNothingStopsTheDiagnosis(unittest.TestCase):
    """Смысл команды — полный отчёт при любом состоянии кабинета."""

    def run_all(self, answer):
        with patch("wbads.diagnose.raw_request", answer), \
                patch("wbads.diagnose.inspect_certificate",
                      lambda *a, **kw: {"issuer": "Let's Encrypt", "error": None,
                                        "not_after": "Oct 31", "expired": False}):
            return diagnose.run_diagnostics(TOKEN)

    def test_every_method_is_probed_even_if_the_first_one_fails(self):
        def always_denied(url, token, method="GET", ca_bundle="", timeout=60):
            f = diagnose.Finding(label="", method=method, url=url, status=403)
            return f

        report = self.run_all(always_denied)
        self.assertEqual(len(report["findings"]), len(diagnose.PROBES),
                         "отказ первой пробы не отменяет остальные")
        self.assertEqual(len(report["problems"]), len(diagnose.PROBES))
        text = diagnose.render(report)
        for label, *_ in diagnose.PROBES:
            self.assertIn(label, text, f"{label} обязан попасть в отчёт")

    def test_a_crashing_probe_does_not_crash_the_report(self):
        """Диагностика нужна именно тогда, когда что-то сломано непредвиденно."""
        calls = {"n": 0}

        def flaky(url, token, method="GET", ca_bundle="", timeout=60):
            calls["n"] += 1
            f = diagnose.Finding(label="", method=method, url=url)
            if calls["n"] == 1:
                f.error = "RuntimeError: нежданное"
            else:
                f.status = 200
                f.records = 1
                f.shape = "список из 1 записи"
                f.parsed = []
            return f

        report = self.run_all(flaky)
        self.assertEqual(len(report["findings"]), len(diagnose.PROBES))
        self.assertIn("нежданное", diagnose.render(report))

    def test_report_survives_a_broken_token(self):
        def ok(url, token, method="GET", ca_bundle="", timeout=60):
            f = diagnose.Finding(label="", method=method, url=url, status=200)
            f.records, f.shape, f.parsed = 1, "объект", {}
            return f

        with patch("wbads.diagnose.raw_request", ok), \
                patch("wbads.diagnose.inspect_certificate",
                      lambda *a, **kw: {"error": "нет связи"}):
            report = diagnose.run_diagnostics("это-не-токен")
        text = diagnose.render(report)
        self.assertIn("не похоже на токен", text)
        self.assertIn("Методы WB", text, "отчёт обязан дойти до конца")


class TestVerdictsAreHonest(unittest.TestCase):
    """Оценка пробы должна называть причину, а не «что-то не так»."""

    def grade(self, **kwargs):
        expect = kwargs.pop("expect_data", True)
        finding = diagnose.Finding(label="x", method="GET", url="u", **kwargs)
        diagnose._grade(finding, expect)
        return finding

    def test_404_points_at_a_moved_method(self):
        finding = self.grade(status=404)
        self.assertEqual(finding.level, "fail")
        self.assertIn("перенёс адрес", finding.note)

    def test_429_is_a_pause_not_a_denial(self):
        finding = self.grade(status=429)
        self.assertEqual(finding.level, "warn")
        self.assertIn("подождать", finding.note)

    def test_403_points_at_the_token(self):
        self.assertEqual(self.grade(status=403).level, "fail")

    def test_200_without_records_is_a_warning_not_a_tick(self):
        """Пустой ответ нельзя показывать галочкой: именно так пропали
        и мёртвый адрес метода, и молчащая статистика."""
        finding = self.grade(status=200, records=0)
        self.assertEqual(finding.level, "warn")
        self.assertIn("форма ответа другая", finding.note)

    def test_200_with_records_is_a_tick(self):
        self.assertEqual(self.grade(status=200, records=5).level, "ok")

    def test_balance_may_legitimately_have_no_records(self):
        self.assertEqual(self.grade(status=200, records=0,
                                    expect_data=False).level, "ok")


class TestRetiredAddressesAreChecked(unittest.TestCase):
    """404 на отключённом адресе — правильный ответ, и это надо показывать
    как норму, иначе отчёт пугает там, где всё в порядке."""

    def test_dead_address_is_a_tick(self):
        def by_url(url, token, method="GET", ca_bundle="", timeout=60):
            f = diagnose.Finding(label="", method=method, url=url)
            f.status = 404 if method == "POST" else 200
            if method == "GET":
                f.records, f.parsed = 1, {}
            return f

        with patch("wbads.diagnose.raw_request", by_url), \
                patch("wbads.diagnose.inspect_certificate",
                      lambda *a, **kw: {"error": None, "issuer": "x"}):
            report = diagnose.run_diagnostics(TOKEN)

        self.assertTrue(report["retired"], "старые адреса обязаны проверяться")
        for finding in report["retired"]:
            self.assertEqual(finding.level, "ok")
            self.assertIn("как и ожидалось", finding.note)

    def test_unreachable_address_is_not_read_as_a_revival(self):
        def nothing(url, token, method="GET", ca_bundle="", timeout=60):
            f = diagnose.Finding(label="", method=method, url=url)
            f.error = "нет связи"
            return f

        with patch("wbads.diagnose.raw_request", nothing), \
                patch("wbads.diagnose.inspect_certificate",
                      lambda *a, **kw: {"error": None, "issuer": "x"}):
            report = diagnose.run_diagnostics(TOKEN)
        for finding in report["retired"]:
            self.assertIn("проверить не удалось", finding.note)
            self.assertNotIn("None", finding.note)


class TestProbesUseRealCampaigns(unittest.TestCase):
    """Спрашивать статистику у выдуманных номеров бессмысленно: ответ
    «данных нет» ничего не расскажет. Берём живые из списка кампаний."""

    def test_live_ids_prefer_active_and_recent(self):
        ids = diagnose._live_ids({"adverts": [
            {"status": 8, "advert_list": [{"advertId": 1, "changeTime": "2026-09-23"}]},
            {"status": 9, "advert_list": [{"advertId": 2, "changeTime": "2026-05-01"},
                                          {"advertId": 3, "changeTime": "2026-09-22"}]},
            {"status": 11, "advert_list": [{"advertId": 4, "changeTime": "2026-09-01"}]},
        ]})
        self.assertNotIn(1, ids, "отменённую спрашивать незачем")
        self.assertEqual(ids[0], 3, "свежая — первой")

    def test_live_ids_tolerate_junk(self):
        self.assertEqual(diagnose._live_ids(None), [])
        self.assertEqual(diagnose._live_ids({"adverts": None}), [])

    def test_stats_query_carries_ids_and_a_legal_period(self):
        from datetime import date
        from wbads.wb_client import MAX_STATS_DAYS

        query = diagnose._query_for("stats", [10, 20, 30])
        self.assertIn("ids=10,20,30", query)
        begin = query.split("beginDate=")[1].split("&")[0]
        end = query.split("endDate=")[1]
        days = (date.fromisoformat(end) - date.fromisoformat(begin)).days
        self.assertLess(days, MAX_STATS_DAYS, "WB не примет период длиннее месяца")
        self.assertLess(date.fromisoformat(end), date.today(),
                        "за сегодня статистики ещё нет")

    def test_no_ids_means_no_query_rather_than_a_broken_one(self):
        self.assertEqual(diagnose._query_for("stats", []), "")
        self.assertEqual(diagnose._query_for("ids", []), "")


class TestRateLimitHeadersAreKept(unittest.TestCase):
    """По заголовкам лимитов видно, что WB считает, — это и объясняет 429."""

    def test_only_limit_headers_are_kept(self):
        kept = diagnose._rate_headers({
            "X-RateLimit-Remaining": "0",
            "Retry-After": "20",
            "Content-Type": "application/json",
            "Set-Cookie": "секрет",
        })
        self.assertEqual(set(kept), {"X-RateLimit-Remaining", "Retry-After"})
        self.assertNotIn("Set-Cookie", kept, "лишнего в отчёт попадать не должно")


class TestWBOwnWordsReachTheReport(unittest.TestCase):
    """Текст отказа от WB — самое полезное, что есть в ответе. Именно он
    назвал причину, которую мы до того месяц угадывали."""

    def test_json_error_is_extracted(self):
        self.assertEqual(
            diagnose._first_line('{"detail":"rate limit exceeded"}'),
            "rate limit exceeded")

    def test_plain_text_survives(self):
        self.assertIn("Please consult",
                      diagnose._first_line("Please consult the docs"))

    def test_empty_body_is_empty(self):
        self.assertEqual(diagnose._first_line(""), "")


class TestReportIsSelfContained(unittest.TestCase):
    """Отчёт отправляют целиком, не поясняя ничего словами."""

    def test_saved_file_has_everything_needed(self):
        import tempfile

        def ok(url, token, method="GET", ca_bundle="", timeout=60):
            f = diagnose.Finding(label="", method=method, url=url, status=200)
            f.records, f.shape, f.parsed = 2, "список из 2 записей", []
            f.headers = {"X-RateLimit-Remaining": "2"}
            return f

        with patch("wbads.diagnose.raw_request", ok), \
                patch("wbads.diagnose.inspect_certificate",
                      lambda *a, **kw: {"issuer": "Let's Encrypt", "error": None,
                                        "not_after": "Oct 31", "expired": False}):
            report = diagnose.run_diagnostics(TOKEN)
            with tempfile.TemporaryDirectory() as tmp:
                path = diagnose.save(report, Path(tmp))
                text = path.read_text(encoding="utf-8")

        self.assertEqual(path.name, "диагностика.txt")
        for needed in ("Компьютер", "Токен", "Защищённое соединение",
                       "Методы WB", "Отключённые адреса", "ИТОГ"):
            self.assertIn(needed, text, f"в отчёте нет раздела «{needed}»")
        self.assertNotIn(TOKEN, text, "сам токен в файл попадать не должен")

    def test_token_value_never_leaks_into_the_report(self):
        """Файл отправляют по почте — значение токена в нём недопустимо."""
        secret = "eyJhbGciOiJFUzI1NiJ9.eyJzIjo0MjE0fQ.подпись-секрет"

        def ok(url, token, method="GET", ca_bundle="", timeout=60):
            f = diagnose.Finding(label="", method=method, url=url, status=200)
            f.records, f.parsed = 1, {}
            return f

        with patch("wbads.diagnose.raw_request", ok), \
                patch("wbads.diagnose.inspect_certificate",
                      lambda *a, **kw: {"error": None, "issuer": "x"}):
            report = diagnose.run_diagnostics(secret)
        text = diagnose.render(report)
        self.assertNotIn(secret, text)
        self.assertNotIn("подпись-секрет", text)


if __name__ == "__main__":
    unittest.main()
