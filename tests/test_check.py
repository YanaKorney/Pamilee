"""Проверки диагностики токена: команда должна показывать, какой именно
метод закрыт, а не общее «не работает»."""

import io
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as cli  # noqa: E402
from wbads.config import token_in_template  # noqa: E402
from wbads.wb_client import WBError  # noqa: E402

FULL_ACCESS = {
    "balance": lambda self: {"balance": 100.0, "bonus": 0.0, "net": 100.0},
    "campaign_ids": lambda self: [1, 2],
    "campaign_index": lambda self: [{"advertId": 1, "type": 8, "status": 9},
                                    {"advertId": 2, "type": 9, "status": 9}],
    "campaign_details": lambda self, ids, on_progress=None: [{"advertId": ids[0]}],
    "fullstats": lambda self, ids, a, b, on_progress=None: [{"advertId": ids[0], "days": []}],
}

# Заказы живут в отдельном клиенте: их подменяем отдельно, иначе тест
# полезет в сеть за настоящими данными.
ORDERS_OK = {"orders": lambda self, d, on_progress=None, max_pages=1: [{"srid": "x"}]}


def denied(*args, **kwargs):
    raise WBError("Доступ запрещён (403).", 403)


def run_check(overrides: dict, orders: dict | None = None) -> tuple[int, str]:
    methods = {**FULL_ACCESS, **overrides}
    buffer = io.StringIO()
    with patch.dict("os.environ", {"WB_API_TOKEN": "test-token"}), \
            patch.multiple("wbads.wb_client.WBAdvertClient", **methods), \
            patch.multiple("wbads.wb_client.WBStatisticsClient", **{**ORDERS_OK, **(orders or {})}), \
            redirect_stdout(buffer):
        code = cli.cmd_check(Namespace())
    return code, buffer.getvalue()


class TestCheck(unittest.TestCase):
    def test_full_access_passes(self):
        code, out = run_check({})
        self.assertEqual(code, 0)
        self.assertIn("считаются оба ДРР", out)
        self.assertEqual(out.count("✓"), 5)  # четыре метода рекламы плюс заказы
        self.assertNotIn("✗", out)

    def test_names_every_endpoint_it_needs(self):
        """Пользователь должен видеть, куда именно сервис ходит."""
        _, out = run_check({})
        for endpoint in ("/adv/v1/balance", "/adv/v1/promotion/count",
                         "/api/advert/v2/adverts", "/adv/v3/fullstats",
                         "/api/v1/supplier/orders"):
            self.assertIn(endpoint, out)
        # Старые адреса WB отключил осенью 2025 — они не должны вернуться.
        for gone in ("/adv/v1/promotion/adverts", "/adv/v2/fullstats"):
            self.assertNotIn(gone, out)

    def test_missing_statistics_does_not_fail_the_check(self):
        """Нет категории «Статистика» — реклама всё равно работает.

        Проверка не должна падать: рекламный ДРР считается, общий — нет,
        и об этом сказано прямо.
        """
        code, out = run_check({}, orders={"orders": denied})
        self.assertEqual(code, 0)
        self.assertIn("✗ Заказы кабинета", out)
        self.assertIn("общий — нет", out)
        self.assertIn("Статистика", out)

    def test_closed_category_reports_failure(self):
        code, out = run_check({"balance": denied, "campaign_index": denied})
        self.assertEqual(code, 1)
        self.assertIn("Закрыто методов рекламы: 2", out)
        self.assertIn("Продвижение", out)

    def test_partial_access_points_at_the_closed_method(self):
        """Кампании читаются, а статистика нет — видно ровно этот метод."""
        code, out = run_check({"fullstats": denied})
        self.assertEqual(code, 1)
        self.assertIn("Закрыто методов рекламы: 1", out)
        self.assertIn("✗ Статистика по дням", out)
        # Все методы теперь GET, поэтому совет «выпустите токен без галочки
        # Только на чтение» стал неверным: читающий токен их не закрывает.
        self.assertNotIn("Только на чтение", out)
        self.assertIn("Категория «Продвижение» у токена есть", out)

    def test_missing_token_explains_where_to_get_it(self):
        buffer = io.StringIO()
        with patch.dict("os.environ", {"WB_API_TOKEN": ""}), redirect_stdout(buffer):
            code = cli.cmd_check(Namespace())
        out = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Доступ к API", out)
        self.assertIn("Продвижение", out)


class TestNotFoundIsNotDenial(unittest.TestCase):
    """404 означает «запрос дошёл, прав хватило, но отдавать нечего».
    Считать это отказом доступа — значит гнать человека проверять
    категорию токена, которая на самом деле на месте."""

    @staticmethod
    def not_found(*args, **kwargs):
        from wbads.wb_client import WBError
        raise WBError("Метод не найден (404).", 404)

    def test_empty_data_does_not_fail_the_check(self):
        code, out = run_check({"campaign_details": self.not_found,
                               "fullstats": self.not_found})
        self.assertEqual(code, 0)
        self.assertIn("⚠", out)
        self.assertIn("Данных за проверяемый период нет", out)
        self.assertIn("Сбор запускайте", out)

    def test_does_not_blame_the_token_category(self):
        """Баланс и список кампаний прошли — значит, категория есть."""
        _, out = run_check({"campaign_details": self.not_found,
                            "fullstats": self.not_found})
        self.assertNotIn("Проверьте в кабинете", out)
        self.assertNotIn("Что записано в вашем токене", out)

    def test_real_denial_after_partial_success_does_not_blame_the_category(self):
        """Часть методов прошла, часть отказала — категория ни при чём."""
        code, out = run_check({"campaign_details": denied, "fullstats": denied})
        self.assertEqual(code, 1)
        self.assertIn("Категория «Продвижение» у токена есть", out)
        self.assertIn("поддержку", out)

    def test_stats_window_is_in_the_past(self):
        """За сегодня статистики может не быть — просим прошедшие дни."""
        seen = {}

        def capture(self_, ids, date_from, date_to, on_progress=None):
            seen["from"], seen["to"] = date_from, date_to
            return []

        run_check({"fullstats": capture})
        from datetime import date, timedelta
        self.assertEqual(seen["to"], (date.today() - timedelta(days=1)).isoformat())
        self.assertLess(seen["from"], seen["to"])


class TestBatchSplitting(unittest.TestCase):
    """WB отвечает 404 на всю пачку, если статистики нет хотя бы по части
    кампаний в ней. Пропускать пачку целиком — значит терять рабочие
    кампании, которые в ней были: ровно так и вышло на кабинете из 511
    кампаний, где не собралось вообще ничего. Пачка должна дробиться."""

    def client(self, with_data: set, strict: bool = True):
        """Поддельный WB. strict=True — 404, если данных нет хоть по одной."""
        from wbads.wb_client import WBAdvertClient, WBError
        client = WBAdvertClient.__new__(WBAdvertClient)
        client.token = "t"
        client.base_url = "x"
        client.timeout = 1
        client.max_retries = 1
        client._last_call = 0.0
        client.ca_bundle = ""
        client.used_fallback_bundle = False
        client._wait_turn = lambda on_progress=None, cooldown=0: None
        counter = {"requests": 0}

        def request(method, path, payload=None, params=None, on_progress=None):
            counter["requests"] += 1
            # Новые методы WB принимают ID списком в адресе запроса.
            ids = [int(i) for i in (params or {}).get("ids", "").split(",") if i]
            hit = [i for i in ids if i in with_data]
            missing = not all(i in with_data for i in ids) if strict else not hit
            if missing:
                raise WBError("404", 404)
            rows = [{"advertId": i, "days": []} for i in hit]
            if path.endswith("fullstats"):
                return rows
            return {"adverts": [{"id": i, "settings": {"name": f"Кампания {i}"}}
                                for i in hit]}

        client._request = request
        client.requests = counter
        return client

    def test_splitting_recovers_campaigns_that_have_data(self):
        client = self.client(set(range(170, 200)))
        result = client.fullstats(list(range(200)), "2026-08-25", "2026-09-23")
        self.assertGreater(len(result), 0,
                           "дробление обязано вытащить кампании со статистикой")

    def test_lenient_api_collects_everything_it_has(self):
        """Если WB мягче и 404 только на полностью пустую пачку —
        собраться должно всё."""
        client = self.client(set(range(120)), strict=False)
        result = client.fullstats(list(range(511)), "2026-08-25", "2026-09-23")
        self.assertEqual(len(result), 120)

    def test_request_budget_is_respected(self):
        """Каждый запрос — минута ожидания, поэтому их число ограничено."""
        client = self.client(set())
        client.fullstats(list(range(511)), "2026-08-25", "2026-09-23")
        from wbads.wb_client import MAX_STATS_REQUESTS
        self.assertLessEqual(client.requests["requests"], MAX_STATS_REQUESTS)

    def test_empty_batch_does_not_starve_the_rest(self):
        """Пустая пачка не должна съедать бюджет, дробясь вглубь:
        половины уходят в конец очереди, а не в начало."""
        client = self.client(set(range(400, 440)))
        result = client.fullstats(list(range(440)), "2026-08-25", "2026-09-23")
        self.assertGreater(len(result), 0,
                           "данные в конце списка обязаны быть найдены")

    def test_details_are_split_too(self):
        client = self.client(set(range(60, 80)))
        result = client.campaign_details(list(range(80)))
        self.assertGreater(len(result), 0)

    def test_gives_up_early_when_names_are_never_returned(self):
        """У части кабинетов метод названий молчит по всем кампаниям.
        Дробить до каждой из 511 — это около тысячи пустых запросов:
        долго для человека и грубо по отношению к API. Названия
        необязательны, поэтому после нескольких попыток сдаёмся."""
        from wbads.wb_client import DETAIL_GIVE_UP
        client = self.client(set())
        client.campaign_details(list(range(511)))
        self.assertLessEqual(client.requests["requests"], DETAIL_GIVE_UP + 1)

    def test_working_cabinet_still_gets_all_names(self):
        """Ранняя сдача не должна мешать кабинету, где названия есть."""
        client = self.client(set(range(511)))
        result = client.campaign_details(list(range(511)))
        self.assertEqual(len(result), 511)

    def test_detail_requests_are_capped(self):
        from wbads.wb_client import MAX_DETAIL_REQUESTS
        client = self.client(set(range(100)))
        client.campaign_details(list(range(400)))
        self.assertLessEqual(client.requests["requests"], MAX_DETAIL_REQUESTS)

    def test_real_denial_still_propagates(self):
        """403 глотать нельзя — это настоящая проблема."""
        from wbads.wb_client import WBError
        client = self.client(set())
        client._request = lambda *a, **kw: (_ for _ in ()).throw(WBError("403", 403))
        with self.assertRaises(WBError) as caught:
            client.campaign_details([1])
        self.assertEqual(caught.exception.status, 403)


class TestCardsAreOptional(unittest.TestCase):
    """Названия кампаний приходят отдельным методом, и у части кабинетов
    он отвечает 404 по каждой кампании. Останавливать из-за этого весь
    сбор нельзя: тип и статус есть в списке кампаний, а без названия
    кампания опознаётся по номеру."""

    def test_type_and_status_come_from_the_campaign_list(self):
        from wbads.wb_client import WBAdvertClient
        client = WBAdvertClient.__new__(WBAdvertClient)
        client._request = lambda *a, **kw: {"adverts": [
            {"type": 8, "status": 9, "advert_list": [{"advertId": 101}]},
            {"type": 9, "status": 11, "advert_list": [{"advertId": 102}]},
        ]}
        index = client.campaign_index()
        self.assertEqual(len(index), 2)
        self.assertEqual(index[0]["type"], 8)
        self.assertEqual(index[1]["status"], 11)

    def test_campaign_without_a_card_is_named_by_number(self):
        from wbads.collector import normalize_campaign
        row = normalize_campaign({"advertId": 21400101, "type": 8, "status": 9}, "now")
        self.assertEqual(row["name"], "Кампания 21400101")
        self.assertEqual(row["type_name"], "Автоматическая")
        self.assertEqual(row["status_name"], "Идут показы")

    def test_card_enriches_the_name_when_available(self):
        from wbads.collector import normalize_campaign
        row = normalize_campaign({"advertId": 1, "type": 8, "status": 9,
                                  "name": "Авто · Худи", "dailyBudget": 2500}, "now")
        self.assertEqual(row["name"], "Авто · Худи")
        self.assertEqual(row["daily_budget"], 2500.0)

    def test_collection_completes_without_any_cards(self):
        """Сквозная проверка: кабинет, где метод названий молчит."""
        import tempfile
        from unittest.mock import patch as p2
        from wbads import collector, db
        from wbads.wb_client import WBAdvertClient, WBStatisticsClient

        index = [{"advertId": i, "type": 8, "status": 9} for i in range(1, 6)]
        methods = {
            "balance": lambda self: {"balance": 0.0, "bonus": 0.0, "net": 100.0},
            "campaign_index": lambda self: index,
            "campaign_details": lambda self, ids, on_progress=None: [],
            "fullstats": lambda self, ids, a, b, on_progress=None, max_requests=40, give_up_after=15: [
                {"advertId": 1, "days": [{"date": "2026-09-20", "views": 100,
                                          "clicks": 5, "sum": 50.0, "sum_price": 900.0}]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init_db(Path(tmp) / "c.db")
            with p2.multiple("wbads.wb_client.WBAdvertClient", **methods), \
                    p2.multiple("wbads.wb_client.WBStatisticsClient",
                                orders=lambda self, d, on_progress=None, max_pages=12: []):
                result = collector.collect(conn, "token", days=30)
            self.assertEqual(result["campaigns"], 5, "кампании обязаны сохраниться")
            self.assertGreater(result["rows"], 0, "статистика обязана собраться")
            saved = conn.execute("SELECT name FROM campaigns LIMIT 1").fetchone()[0]
            self.assertTrue(saved.startswith("Кампания "))
            conn.close()


class TestCheckDoesNotFakeSuccess(unittest.TestCase):
    """Сбор проглатывает 404 внутри себя, поэтому пустой ответ метода
    нельзя показывать как успех — иначе проверка объявляет рабочим то,
    что не отдаёт ничего."""

    def test_empty_cards_are_flagged_not_ticked(self):
        code, out = run_check({"campaign_details": lambda self, ids, on_progress=None: []})
        self.assertEqual(code, 0)
        self.assertIn("⚠ Названия кампаний", out)
        self.assertIn("у вашего кабинета этот метод молчит", out)
        self.assertIn("Сбор запускайте", out)

    def test_empty_stats_are_flagged_too(self):
        code, out = run_check(
            {"fullstats": lambda self, i, a, b, on_progress=None, max_requests=40, give_up_after=15: []})
        self.assertEqual(code, 0)
        self.assertIn("⚠ Статистика по дням", out)


class TestCampaignFilter(unittest.TestCase):
    """Статистику спрашиваем только у кампаний, которые могли откручиваться:
    каждая лишняя пачка — минута ожидания."""

    def test_keeps_campaigns_that_ran(self):
        from wbads.collector import campaigns_worth_asking
        rows = [
            {"advert_id": 1, "status": 9, "end_time": None},
            {"advert_id": 2, "status": 11, "end_time": None},
            {"advert_id": 3, "status": 7, "end_time": "2026-09-10T00:00:00"},
        ]
        self.assertEqual(campaigns_worth_asking(rows, "2026-08-25"), [1, 2, 3])

    def test_drops_never_started_and_deleted(self):
        from wbads.collector import campaigns_worth_asking
        rows = [
            {"advert_id": 4, "status": 4, "end_time": None},    # готова, не шла
            {"advert_id": 5, "status": -1, "end_time": None},   # удаляется
        ]
        self.assertEqual(campaigns_worth_asking(rows, "2026-08-25"), [])

    def test_drops_campaigns_finished_before_the_period(self):
        from wbads.collector import campaigns_worth_asking
        rows = [{"advert_id": 6, "status": 7, "end_time": "2025-01-01T00:00:00"}]
        self.assertEqual(campaigns_worth_asking(rows, "2026-08-25"), [])


class TestNetworkVsAuth(unittest.TestCase):
    """Сетевой сбой и просроченный сертификат не имеют отношения к токену.
    Раньше программа сваливала их в кучу с отказом доступа и советовала
    перевыпустить исправный токен — это уводило в сторону."""

    @staticmethod
    def tls_error(*args, **kwargs):
        from wbads.wb_client import WBError, explain_tls_error
        raise WBError(
            explain_tls_error("certificate verify failed: certificate has expired"),
            kind="tls",
        )

    @staticmethod
    def network_error(*args, **kwargs):
        from wbads.wb_client import WBError
        raise WBError("Не удалось связаться с Wildberries.\nПроверьте интернет.",
                      kind="network")

    def test_tls_failure_does_not_blame_the_token(self):
        code, out = run_check({"balance": self.tls_error,
                               "campaign_index": self.tls_error},
                              orders={"orders": self.tls_error})
        self.assertEqual(code, 1)
        self.assertIn("НЕ про токен", out)
        self.assertIn("Дата и время на этом компьютере", out)
        self.assertNotIn("выпустите токен заново", out)
        self.assertNotIn("Тестовый контур", out)

    def test_tls_advice_printed_once_not_per_method(self):
        """Длинное объяснение не должно повторяться под каждым методом."""
        _, out = run_check({"balance": self.tls_error,
                            "campaign_index": self.tls_error},
                           orders={"orders": self.tls_error})
        self.assertEqual(out.count("Дата и время на этом компьютере"), 1)

    def test_network_failure_suggests_checking_internet(self):
        code, out = run_check({"balance": self.network_error,
                               "campaign_index": self.network_error},
                              orders={"orders": self.network_error})
        self.assertEqual(code, 1)
        self.assertIn("связаться с Wildberries", out)
        self.assertNotIn("Тестовый контур", out)

    def test_real_403_still_diagnoses_the_token(self):
        """А вот настоящий отказ доступа по-прежнему разбирает токен."""
        code, out = run_check({"balance": denied, "campaign_index": denied},
                              orders={"orders": denied})
        self.assertEqual(code, 1)
        self.assertIn("Что записано в вашем токене", out)

    def test_offers_demo_while_connection_is_broken(self):
        _, out = run_check({"balance": self.tls_error,
                            "campaign_index": self.tls_error},
                           orders={"orders": self.tls_error})
        self.assertIn("demo", out)


class TestTlsExplanation(unittest.TestCase):
    def test_shows_the_machine_clock(self):
        """Сбитые дата и время — самая частая причина «сертификат просрочен»."""
        from datetime import datetime
        from wbads.wb_client import explain_tls_error
        text = explain_tls_error("certificate has expired")
        self.assertIn(datetime.now().strftime("%d.%m.%Y"), text)

    def test_keeps_the_technical_reason(self):
        from wbads.wb_client import explain_tls_error
        text = explain_tls_error("certificate verify failed: certificate has expired")
        self.assertIn("certificate verify failed", text)

    def test_mentions_antivirus_and_proxy(self):
        from wbads.wb_client import explain_tls_error
        text = explain_tls_error("certificate has expired")
        self.assertIn("Антивирус", text)


class TestInterceptorRecognition(unittest.TestCase):
    """По имени выдавшего сертификат видно, кто вклинился в соединение.
    Это превращает гадание «антивирус? прокси? сайт?» в прямой ответ."""

    def test_recognises_known_antiviruses(self):
        from wbads.wb_client import name_interceptor
        cases = {
            "Doctor Web, Ltd. · Dr.Web Custom CA": "Dr.Web",
            "Kaspersky Lab · Kaspersky Anti-Virus Personal Root": "Kaspersky",
            "ESET, spol. s r.o. · ESET SSL Filter CA": "ESET",
            "AVAST Software · avast! Web/Mail Shield Root": "Avast",
        }
        for issuer, expected in cases.items():
            self.assertIn(expected, name_interceptor(issuer) or "", issuer)

    def test_recognises_corporate_gateways(self):
        from wbads.wb_client import name_interceptor
        self.assertIn("Zscaler", name_interceptor("Zscaler Inc · Zscaler Root CA") or "")

    def test_real_certificate_authority_is_not_flagged(self):
        """Настоящий удостоверяющий центр не должен объявляться перехватчиком."""
        from wbads.wb_client import name_interceptor
        for issuer in ("DigiCert Inc · DigiCert Global Root G2",
                       "Let's Encrypt · R3",
                       "GlobalSign nv-sa · GlobalSign Root CA"):
            self.assertIsNone(name_interceptor(issuer), issuer)

    def test_empty_issuer_is_safe(self):
        from wbads.wb_client import name_interceptor
        self.assertIsNone(name_interceptor(None))
        self.assertIsNone(name_interceptor(""))


class TestCertificateInspection(unittest.TestCase):
    def test_unreachable_host_reports_error_without_raising(self):
        """Диагностика не должна сама падать, если узел недоступен."""
        from wbads.wb_client import inspect_certificate
        info = inspect_certificate("nonexistent.invalid", timeout=3)
        self.assertIsNotNone(info["error"])
        self.assertIsNone(info["issuer"])

    def test_names_the_culprit_in_check_output(self):
        from unittest.mock import patch as p2
        fake = {"host": "advert-api.wildberries.ru",
                "issuer": "Doctor Web, Ltd. · Dr.Web Custom CA",
                "subject": "advert-api.wildberries.ru",
                "not_after": "Dec 31 23:59:59 2027 GMT",
                "expired": False, "error": None}
        buffer = io.StringIO()
        with p2("run.inspect_certificate", lambda *a, **k: fake), \
                redirect_stdout(buffer):
            cli._who_breaks_the_connection()
        out = buffer.getvalue()
        self.assertIn("Dr.Web", out)
        self.assertIn("исключения python.exe", out)

    def test_unknown_issuer_points_at_missing_root(self):
        """Срок сертификата разобрать не вышло — общий совет про корни."""
        from unittest.mock import patch as p2
        fake = {"host": "advert-api.wildberries.ru",
                "issuer": "DigiCert Inc · DigiCert Global Root G2",
                "subject": "advert-api.wildberries.ru",
                "not_after": None, "expired": None, "error": None}
        buffer = io.StringIO()
        with p2("run.inspect_certificate", lambda *a, **k: fake), \
                redirect_stdout(buffer):
            cli._who_breaks_the_connection()
        out = buffer.getvalue()
        self.assertIn("не похоже на антивирус", out)
        self.assertIn("обновления Windows", out)

    def test_expired_site_certificate_is_reported_as_such(self):
        """А если просрочен сам сертификат сайта — это надо сказать прямо."""
        from unittest.mock import patch as p2
        fake = {"host": "advert-api.wildberries.ru",
                "issuer": "Let's Encrypt · YE1",
                "subject": "advert-api.wildberries.ru",
                "not_after": "Jan 01 00:00:00 2020 GMT",
                "expired": True, "error": None}
        buffer = io.StringIO()
        with p2("run.inspect_certificate", lambda *a, **k: fake), \
                redirect_stdout(buffer):
            cli._who_breaks_the_connection()
        self.assertIn("СРОК ВЫШЕЛ", buffer.getvalue())


class TestTrustStoreFallback(unittest.TestCase):
    """Устаревший корень в хранилище системы — отдельная причина, и лечится
    она не отключением антивируса, а свежим набором корней."""

    def test_verification_stays_full_with_custom_bundle(self):
        """Подмена набора корней не должна ослаблять проверку."""
        import ssl
        from wbads.wb_client import build_ssl_context, certifi_bundle
        for bundle in ("", certifi_bundle() or ""):
            ctx = build_ssl_context(bundle)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED, bundle)
            self.assertTrue(ctx.check_hostname, bundle)

    def test_missing_bundle_path_falls_back_to_system(self):
        import ssl
        from wbads.wb_client import build_ssl_context
        ctx = build_ssl_context("/нет/такого/файла.pem")
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_valid_site_certificate_points_at_the_system_store(self):
        """Сертификат сайта не просрочен, а проверка ругается — значит, корень."""
        from unittest.mock import patch as p2
        fake = {"host": "advert-api.wildberries.ru",
                "issuer": "Let's Encrypt · YE1",
                "subject": "advert-api.wildberries.ru",
                "not_after": "Oct 31 04:51:24 2026 GMT",
                "expired": False, "error": None}
        buffer = io.StringIO()
        # Ветку задаём явно: без набора корней — предложение его поставить
        with p2("run.inspect_certificate", lambda *a, **k: fake), \
                p2("run.certifi_bundle", lambda: None), \
                p2("run._ask", lambda q, default="д": False), \
                redirect_stdout(buffer):
            cli._who_breaks_the_connection()
        out = buffer.getvalue()
        self.assertIn("НЕ просрочен", out)
        self.assertIn("pip install certifi", out)
        self.assertIn("ISRG Root X1", out)
        # Про антивирус здесь говорить нечего — он ни при чём
        self.assertNotIn("исключения python.exe", out)

    def test_ca_bundle_is_read_from_environment(self):
        import os
        from unittest.mock import patch as p2
        from wbads.config import load_config
        with p2.dict(os.environ, {"WBADS_CA_BUNDLE": "/tmp/my-roots.pem"}):
            self.assertEqual(load_config().ca_bundle, "/tmp/my-roots.pem")

    def test_standard_ssl_cert_file_is_honoured(self):
        """SSL_CERT_FILE — общепринятая переменная, её тоже уважаем."""
        import os
        from unittest.mock import patch as p2
        from wbads.config import load_config
        with p2.dict(os.environ, {"WBADS_CA_BUNDLE": "", "SSL_CERT_FILE": "/tmp/roots.pem"}):
            self.assertEqual(load_config().ca_bundle, "/tmp/roots.pem")


class TestCertifiOffer(unittest.TestCase):
    """Набор корней ставится тем же Python, которым запущена программа.
    На Windows «python» и «py» часто указывают на разные установки,
    и поставленный вручную набор оказывается не в той."""

    def test_manual_command_names_this_interpreter(self):
        from unittest.mock import patch as p2
        buffer = io.StringIO()
        with p2("run.certifi_bundle", lambda: None), \
                p2("run._ask", lambda q, default="д": False), \
                redirect_stdout(buffer):
            cli._offer_certificate_bundle()
        out = buffer.getvalue()
        self.assertIn(sys.executable, out)
        self.assertIn("несколько Python", out)

    def test_installs_when_user_agrees(self):
        from unittest.mock import patch as p2
        called = []
        buffer = io.StringIO()
        with p2("run.certifi_bundle", lambda: None), \
                p2("run._ask", lambda q, default="д": True), \
                p2("run._install_certifi", lambda: called.append(1) or True), \
                redirect_stdout(buffer):
            cli._offer_certificate_bundle()
        self.assertEqual(len(called), 1)
        self.assertIn("запустите проверку ещё раз", buffer.getvalue())

    def test_already_installed_moves_on_to_windows_store(self):
        """Набор есть, а не помогло — предлагать ставить его снова бессмысленно."""
        from unittest.mock import patch as p2
        buffer = io.StringIO()
        with p2("run.certifi_bundle", lambda: "/some/cacert.pem"), redirect_stdout(buffer):
            cli._offer_certificate_bundle()
        out = buffer.getvalue()
        self.assertIn("уже установлен", out)
        self.assertIn("ISRG Root X1", out)
        self.assertNotIn("Поставить его прямо сейчас", out)

    def test_warns_against_the_retired_root(self):
        """DST Root CA X3 — это и есть просроченный корень, его брать нельзя."""
        from unittest.mock import patch as p2
        buffer = io.StringIO()
        with p2("run.certifi_bundle", lambda: "/some/cacert.pem"), redirect_stdout(buffer):
            cli._offer_certificate_bundle()
        self.assertIn("DST Root CA X3", buffer.getvalue())
        self.assertIn("retired", buffer.getvalue())

    def test_install_uses_sys_executable(self):
        """Ставим ровно тем интерпретатором, который работает сейчас."""
        from unittest.mock import patch as p2
        seen = {}

        class Result:
            returncode = 0
            stdout = stderr = ""

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return Result()

        with p2("subprocess.run", fake_run), \
                p2("run.certifi_bundle", lambda: "/some/cacert.pem"), \
                redirect_stdout(io.StringIO()):
            cli._install_certifi()
        self.assertEqual(seen["cmd"][0], sys.executable)
        self.assertIn("certifi", seen["cmd"])

    def test_failed_install_reports_without_raising(self):
        from unittest.mock import patch as p2

        class Result:
            returncode = 1
            stdout = ""
            stderr = "ERROR: не вышло"

        buffer = io.StringIO()
        with p2("subprocess.run", lambda cmd, **kw: Result()), redirect_stdout(buffer):
            self.assertFalse(cli._install_certifi())
        self.assertIn("не удалась", buffer.getvalue())


class TestClockPlausibility(unittest.TestCase):
    """Порядок советов при сбое сертификата зависит от того, похожи ли часы
    на верные: гонять человека в настройки времени, когда дата в порядке,
    значит уводить его от настоящей причины."""

    @staticmethod
    def token(days_left: int) -> str:
        import base64
        import json
        import time
        enc = lambda d: base64.urlsafe_b64encode(  # noqa: E731
            json.dumps(d).encode()).decode().rstrip("=")
        payload = {"exp": int(time.time()) + days_left * 86400, "t": False}
        return f"{enc({'alg': 'ES256'})}.{enc(payload)}.signature"

    def test_fresh_token_means_clock_is_fine(self):
        from wbads.wb_client import clock_looks_plausible
        self.assertTrue(clock_looks_plausible(self.token(166)))

    def test_long_expired_token_means_clock_is_suspect(self):
        from wbads.wb_client import clock_looks_plausible
        self.assertFalse(clock_looks_plausible(self.token(-500)))

    def test_absurdly_distant_expiry_is_suspect(self):
        """Токены WB не выпускают на годы вперёд — значит, часы сбиты назад."""
        from wbads.wb_client import clock_looks_plausible
        self.assertFalse(clock_looks_plausible(self.token(3000)))

    def test_unreadable_token_gives_no_verdict(self):
        from wbads.wb_client import clock_looks_plausible
        self.assertIsNone(clock_looks_plausible("мусор"))

    def test_plausible_clock_points_at_antivirus(self):
        from wbads.wb_client import explain_tls_error
        text = explain_tls_error("certificate has expired", self.token(166))
        self.assertIn("похожа на правильную", text)
        self.assertNotIn("часы на компьютере сбиты", text)

    def test_broken_clock_points_at_the_clock(self):
        from wbads.wb_client import explain_tls_error
        text = explain_tls_error("certificate has expired", self.token(-500))
        self.assertIn("часы на компьютере сбиты", text)
        self.assertIn("Установить время автоматически", text)

    def test_suggests_the_recheck_file(self):
        """После правки настроек нужен быстрый способ перепроверить."""
        from wbads.wb_client import explain_tls_error
        text = explain_tls_error("certificate has expired", self.token(166))
        self.assertIn("CHECK-Windows.bat", text)


class TestTokenInTemplate(unittest.TestCase):
    """Токен, вписанный в .env.example, обязан быть замечен.

    Файл отслеживается git: незамеченный токен уедет в репозиторий,
    а сервис при этом будет говорить «токен не найден».
    """

    def write(self, text: str) -> Path:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / ".env.example"
        path.write_text(text, encoding="utf-8")
        return path

    def test_detects_filled_token(self):
        path = self.write("# комментарий\nWB_API_TOKEN=eyJhbGciOiJFUzI1NiJ9.abc\n")
        self.assertTrue(token_in_template(path))

    def test_empty_template_is_fine(self):
        path = self.write("# комментарий\nWB_API_TOKEN=\nWBADS_PORT=8000\n")
        self.assertFalse(token_in_template(path))

    def test_quoted_token_is_detected(self):
        path = self.write('WB_API_TOKEN="eyJhbGciOiJFUzI1NiJ9.abc"\n')
        self.assertTrue(token_in_template(path))

    def test_commented_out_token_is_not_a_leak(self):
        path = self.write("# WB_API_TOKEN=eyJhbGciOiJFUzI1NiJ9.abc\nWB_API_TOKEN=\n")
        self.assertFalse(token_in_template(path))

    def test_missing_file_is_fine(self):
        self.assertFalse(token_in_template(Path("/nope/.env.example")))

    def test_shipped_template_is_empty(self):
        """Шаблон в репозитории не должен содержать токен — ни при каких правках."""
        shipped = Path(__file__).resolve().parent.parent / ".env.example"
        self.assertFalse(token_in_template(shipped))


if __name__ == "__main__":
    unittest.main()


class TestBrokenMethodIsNotWaitedOut(unittest.TestCase):
    """Кабинет, где POST-методы отвечают 404 на всё подряд. Перебирать в
    таком кабинете пачки статистики по минуте каждая — значит забрать у
    человека полчаса и ничего не собрать."""

    def test_error_body_from_wb_is_shown(self):
        """WB объясняет отказ в теле ответа. Выбрасывать это объяснение —
        значит гадать там, где ответ уже написан."""
        import io
        import urllib.error
        from wbads.wb_client import _read_error_body

        def http_error(body: bytes):
            return urllib.error.HTTPError(
                "https://x", 404, "Not Found", {}, io.BytesIO(body))

        self.assertEqual(
            _read_error_body(http_error(b'{"error":"campaign not found"}')),
            "campaign not found")
        self.assertEqual(_read_error_body(http_error(b"plain text")), "plain text")
        self.assertEqual(_read_error_body(http_error(b"")), "")

    def test_details_report_that_the_method_is_silent(self):
        """Карточки, не отдавшиеся ни по одной кампании, — сигнал для сбора."""
        client = TestBatchSplitting().client(set())
        client.campaign_details(list(range(60)))
        self.assertTrue(client.details_all_404,
                        "молчащий метод карточек обязан быть помечен")

    def test_details_that_worked_are_not_flagged(self):
        client = TestBatchSplitting().client(set(range(60)))
        client.campaign_details(list(range(60)))
        self.assertFalse(client.details_all_404)

    def test_collect_probes_stats_instead_of_grinding(self):
        """Если карточки молчат, статистику пробуем парой запросов."""
        import tempfile
        from unittest.mock import patch as p2
        from wbads import collector, db

        seen: dict[str, int] = {}

        def fullstats(self, ids, a, b, on_progress=None, max_requests=40,
                      give_up_after=15):
            seen["budget"] = give_up_after
            return []

        index = [{"advertId": i, "type": 8, "status": 9} for i in range(1, 6)]

        def details(self, ids, on_progress=None):
            self.details_all_404 = True
            return []

        methods = {
            "balance": lambda self: {"balance": 0.0, "bonus": 0.0, "net": 100.0},
            "campaign_index": lambda self: index,
            "campaign_details": details,
            "fullstats": fullstats,
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init_db(Path(tmp) / "c.db")
            with p2.multiple("wbads.wb_client.WBAdvertClient", **methods), \
                    p2.multiple("wbads.wb_client.WBStatisticsClient",
                                orders=lambda self, d, on_progress=None, max_pages=12: []):
                collector.collect(conn, "token", days=30)
            conn.close()
        self.assertLessEqual(seen["budget"], 2,
                             "в сломанном кабинете нельзя ждать по минуте на пачку")

    def test_healthy_cabinet_keeps_the_full_budget(self):
        import tempfile
        from unittest.mock import patch as p2
        from wbads import collector, db
        from wbads.wb_client import STATS_GIVE_UP

        seen: dict[str, int] = {}

        def fullstats(self, ids, a, b, on_progress=None, max_requests=40,
                      give_up_after=15):
            seen["budget"] = give_up_after
            return []

        index = [{"advertId": i, "type": 8, "status": 9} for i in range(1, 6)]
        methods = {
            "balance": lambda self: {"balance": 0.0, "bonus": 0.0, "net": 100.0},
            "campaign_index": lambda self: index,
            "campaign_details": lambda self, ids, on_progress=None: [
                {"advertId": 1, "name": "Кампания"}],
            "fullstats": fullstats,
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init_db(Path(tmp) / "c.db")
            with p2.multiple("wbads.wb_client.WBAdvertClient", **methods), \
                    p2.multiple("wbads.wb_client.WBStatisticsClient",
                                orders=lambda self, d, on_progress=None, max_pages=12: []):
                collector.collect(conn, "token", days=30)
            conn.close()
        self.assertEqual(seen["budget"], STATS_GIVE_UP)

    def test_redirect_keeps_the_method(self):
        """Перенаправление, превращающее POST в GET, — классическая причина
        404 только на POST-методах."""
        import urllib.request
        from wbads.wb_client import _KeepMethodRedirect

        handler = _KeepMethodRedirect()
        original = urllib.request.Request(
            "https://advert-api.wildberries.ru/adv/v3/fullstats",
            data=b"[]", headers={"Authorization": "t"}, method="POST")
        moved = handler.redirect_request(
            original, None, 301, "Moved",
            {}, "https://advert-api.wildberries.ru/adv/v3/fullstats/")
        self.assertEqual(moved.get_method(), "POST", "метод обязан сохраниться")
        self.assertEqual(moved.data, b"[]")
        self.assertTrue(handler.redirected, "факт перенаправления обязан быть виден")


class TestCheckShowsWhatWBSaid(unittest.TestCase):
    """Пустой ответ без объяснения оставляет человека ни с чем.
    Причину, которую написал сам WB, надо показать."""

    def test_reason_is_printed_next_to_the_warning(self):
        from wbads.wb_client import WBError

        def silent(self, ids, on_progress=None):
            self.last_404_message = "Ответ WB: campaign not found"
            return []

        code, out = run_check({"campaign_details": silent})
        self.assertEqual(code, 0)
        self.assertIn("campaign not found", out)


class TestActualRequestsMatchWBToday(unittest.TestCase):
    """Осенью 2025 WB отключил два метода продвижения, и программа месяц
    ходила на мёртвые адреса: 404 выглядел как «нет данных». Здесь
    зафиксировано, куда и каким способом уходят запросы, — чтобы такая
    подмена больше не проходила молча.

    Старое → новое:
        POST /adv/v1/promotion/adverts → GET /api/advert/v2/adverts
        POST /adv/v2/fullstats         → GET /adv/v3/fullstats
    """

    def client(self):
        from wbads.wb_client import WBAdvertClient
        client = WBAdvertClient.__new__(WBAdvertClient)
        client.token = "t"
        client.base_url = "https://advert-api.wildberries.ru"
        client.timeout = 1
        client.max_retries = 1
        client._last_call = 0.0
        client.ca_bundle = ""
        client.used_fallback_bundle = False
        client._wait_turn = lambda on_progress=None, cooldown=0: None
        calls: list[dict] = []

        def request(method, path, payload=None, params=None, on_progress=None):
            calls.append({"method": method, "path": path,
                          "payload": payload, "params": params})
            return [] if path.endswith("fullstats") else {"adverts": []}

        client._request = request
        client.calls = calls
        return client

    def test_stats_go_to_v3_as_a_get_with_dates_in_the_query(self):
        client = self.client()
        client.fullstats([11, 22], "2026-08-25", "2026-09-23")
        call = client.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["path"], "/adv/v3/fullstats")
        self.assertIsNone(call["payload"], "у GET не должно быть тела")
        self.assertEqual(call["params"], {"ids": "11,22",
                                          "beginDate": "2026-08-25",
                                          "endDate": "2026-09-23"})

    def test_stats_ask_no_more_than_fifty_campaigns_at_once(self):
        """Новый метод принимает максимум 50 ID в запросе."""
        client = self.client()
        client.fullstats(list(range(120)), "2026-08-25", "2026-09-23")
        for call in client.calls:
            self.assertLessEqual(len(call["params"]["ids"].split(",")), 50)

    def test_period_longer_than_a_month_is_trimmed_not_rejected(self):
        """Больше 31 дня метод не принимает — период надо обрезать самим."""
        client = self.client()
        client.fullstats([1], "2026-06-01", "2026-09-23")
        params = client.calls[0]["params"]
        from datetime import date
        begin = date.fromisoformat(params["beginDate"])
        end = date.fromisoformat(params["endDate"])
        self.assertLess((end - begin).days, 31)
        self.assertEqual(params["endDate"], "2026-09-23", "конец периода не сдвигаем")

    def test_cards_go_to_the_new_address_as_a_get(self):
        client = self.client()
        client.campaign_details([11, 22])
        call = client.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["path"], "/api/advert/v2/adverts")
        self.assertIsNone(call["payload"], "у GET не должно быть тела")
        self.assertEqual(call["params"], {"ids": "11,22"})

    def test_new_card_shape_is_understood(self):
        """Новый метод отдаёт другую форму ответа: id вместо advertId,
        название внутри settings. Программа должна читать именно её."""
        from wbads.wb_client import _advert_cards
        cards = _advert_cards({"adverts": [{
            "id": 28150154,
            "status": 11,
            "bid_type": "manual",
            "settings": {"name": "Кампания от 28.08.2025 ",
                         "payment_type": "cpc"},
            "timestamps": {"updated": "2025-09-10T10:14:58.475499+03:00"},
        }]})
        self.assertEqual(cards, [{
            "advertId": 28150154,
            "name": "Кампания от 28.08.2025",
            "status": 11,
            "bid_type": "manual",
            "payment_type": "cpc",
            "changeTime": "2025-09-10T10:14:58.475499+03:00",
        }])

    def test_bid_type_reaches_the_campaign_name(self):
        """«Аукцион, ручная ставка» говорит менеджеру больше, чем «Аукцион»."""
        from wbads.collector import normalize_campaign
        row = normalize_campaign({"advertId": 1, "type": 9, "status": 9,
                                  "name": "Платья", "bid_type": "manual"}, "now")
        self.assertEqual(row["type_name"], "Аукцион, ручная ставка")
        unified = normalize_campaign({"advertId": 2, "type": 9, "status": 9,
                                      "bid_type": "unified"}, "now")
        self.assertEqual(unified["type_name"], "Аукцион, единая ставка")
        plain = normalize_campaign({"advertId": 3, "type": 8, "status": 9}, "now")
        self.assertEqual(plain["type_name"], "Автоматическая")

    def test_stats_wait_twenty_seconds_not_a_minute(self):
        """Новый метод отдаётся 3 раза в минуту, а не раз в минуту:
        511 кампаний — это минуты ожидания, а не полчаса."""
        from wbads.wb_client import STATS_COOLDOWN, MAX_IDS_PER_STATS_CALL
        self.assertLessEqual(STATS_COOLDOWN, 25)
        batches = -(-511 // MAX_IDS_PER_STATS_CALL)
        self.assertLessEqual(batches * STATS_COOLDOWN, 5 * 60,
                             "весь кабинет должен собираться за считаные минуты")


class TestRateLimitDoesNotKillTheCollection(unittest.TestCase):
    """429 — это «подожди», а не «нельзя». Сбор из-за него падал целиком,
    хотя 511 кампаний уже лежали в базе, а заказы даже не начинались."""

    def test_pause_comes_from_wb_not_from_a_guess(self):
        import io
        import urllib.error
        from wbads.wb_client import MAX_RETRY_PAUSE, _retry_after

        def error(headers):
            return urllib.error.HTTPError(
                "https://x", 429, "Too Many Requests", headers, io.BytesIO(b""))

        self.assertEqual(_retry_after(error({"X-RateLimit-Retry": "20"}), 2.0), 21)
        self.assertEqual(_retry_after(error({"Retry-After": "45"}), 2.0), 46)
        # Без заголовка остаётся своя пауза, но не бесконечная.
        self.assertEqual(_retry_after(error({}), 2.0), 2.0)
        self.assertEqual(_retry_after(error({"Retry-After": "99999"}), 2.0),
                         MAX_RETRY_PAUSE)
        self.assertEqual(_retry_after(error({"Retry-After": "мусор"}), 4.0), 4.0)

    def test_stats_retry_the_same_batch_instead_of_dropping_it(self):
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
        calls = {"n": 0}

        def request(method, path, payload=None, params=None, on_progress=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise WBError("Слишком часто (429)", 429)
            return [{"advertId": int(i), "days": []}
                    for i in params["ids"].split(",")]

        client._request = request
        result = client.fullstats([1, 2], "2026-08-25", "2026-09-23")
        self.assertEqual(calls["n"], 2, "пачку обязаны повторить, а не бросить")
        self.assertEqual(len(result), 2, "данные обязаны дойти после паузы")

    def test_stats_give_up_quietly_if_the_limit_holds(self):
        """Если лимит не отпускает, сбор останавливается, а не падает."""
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
            WBError("Слишком часто (429)", 429))

        said: list[str] = []
        result = client.fullstats([1, 2], "2026-08-25", "2026-09-23",
                                  on_progress=said.append)
        self.assertEqual(result, [])
        self.assertTrue(any("остальное доберём" in line for line in said),
                        f"человеку нужно сказать, что делать дальше: {said}")

    def test_collection_survives_a_failing_stats_method(self):
        """Кампании сохранены, заказы собраны — падать незачем."""
        import tempfile
        from unittest.mock import patch as p2
        from wbads import collector, db
        from wbads.wb_client import WBError

        orders_called = {"yes": False}

        def orders(self, d, on_progress=None, max_pages=12):
            orders_called["yes"] = True
            return []

        index = [{"advertId": i, "type": 8, "status": 9} for i in range(1, 6)]
        methods = {
            "balance": lambda self: {"balance": 0.0, "bonus": 0.0, "net": 100.0},
            "campaign_index": lambda self: index,
            "campaign_details": lambda self, ids, on_progress=None: [],
            "fullstats": lambda self, *a, **kw: (_ for _ in ()).throw(
                WBError("Слишком часто (429)", 429)),
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init_db(Path(tmp) / "c.db")
            with p2.multiple("wbads.wb_client.WBAdvertClient", **methods), \
                    p2.multiple("wbads.wb_client.WBStatisticsClient", orders=orders):
                result = collector.collect(conn, "token", days=30)
            conn.close()
        self.assertEqual(result["campaigns"], 5, "кампании обязаны сохраниться")
        self.assertTrue(orders_called["yes"], "заказы обязаны собраться")

    def test_the_check_does_not_steal_the_collectors_turn(self):
        """Проверка доступа и сбор — два клиента в одном запуске. Лимит же
        у WB один на кабинет, поэтому паузу надо помнить между ними."""
        import time
        from wbads.wb_client import WBAdvertClient

        from wbads.wb_client import _RATE_CLOCK
        self.addCleanup(_RATE_CLOCK.clear)
        first = WBAdvertClient("token")
        first._last_call = time.monotonic()
        second = WBAdvertClient("token")
        self.assertGreater(second._last_call, 0.0,
                           "второй клиент обязан знать, когда запрашивал первый")


class TestExhaustedRetriesKeepTheRealReason(unittest.TestCase):
    """После исчерпания повторов программа винила интернет, даже когда WB
    внятно ответил «слишком часто». Из-за этого 429 не опознавался как
    лимит частоты и ронял весь сбор."""

    def test_rate_limit_stays_a_rate_limit(self):
        import io
        import urllib.error
        from unittest.mock import patch as p2
        from wbads.wb_client import WBAdvertClient, WBError

        client = WBAdvertClient("token", max_retries=2)

        def always_429(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://x", 429, "Too Many Requests",
                {"X-RateLimit-Retry": "0"},
                io.BytesIO(b'{"detail":"rate limit exceeded"}'))

        with p2("urllib.request.OpenerDirector.open", always_429), \
                p2("time.sleep", lambda *_: None):
            with self.assertRaises(WBError) as caught:
                client._request("GET", "/adv/v3/fullstats")

        self.assertEqual(caught.exception.status, 429,
                         "код ответа обязан дожить до вызывающего кода")
        self.assertEqual(caught.exception.kind, "http")
        self.assertNotIn("подключение к интернету", str(caught.exception),
                         "лимит частоты не имеет отношения к интернету")
        self.assertIn("rate limit exceeded", str(caught.exception))


class TestProbeAsksLiveCampaigns(unittest.TestCase):
    """Проверять статистику на первых пяти кампаниях по списку — значит
    часто попадать на остановленные год назад и получать честное «данных
    нет» как предупреждение. Спрашиваем у тех, у кого открутка правдоподобна."""

    def test_active_and_recent_campaigns_go_first(self):
        picked = cli._likely_to_have_stats([
            {"advertId": 1, "status": 8, "changeTime": "2026-09-01"},   # отменена
            {"advertId": 2, "status": 9, "changeTime": "2026-05-01"},   # активна
            {"advertId": 3, "status": 4, "changeTime": "2026-09-20"},   # не запускалась
            {"advertId": 4, "status": 11, "changeTime": "2026-09-22"},  # на паузе
            {"advertId": 5, "status": 7, "changeTime": "2026-09-10"},   # завершена
        ])
        self.assertEqual(picked, [4, 5, 2],
                         "нужны статусы 9/11/7, свежие — первыми")

    def test_falls_back_to_whatever_there_is(self):
        self.assertEqual(cli._likely_to_have_stats(
            [{"advertId": 1, "status": 8}]), [])

    def test_check_probes_the_live_ones(self):
        asked: dict[str, object] = {}

        def fullstats(self, ids, a, b, on_progress=None, max_requests=40,
                      give_up_after=15):
            asked["ids"] = list(ids)
            return [{"advertId": ids[0], "days": []}]

        index = [
            {"advertId": 100, "type": 8, "status": 8, "changeTime": "2026-09-01"},
            {"advertId": 200, "type": 9, "status": 9, "changeTime": "2026-09-22"},
        ]
        run_check({"campaign_index": lambda self: index, "fullstats": fullstats})
        self.assertEqual(asked["ids"], [200],
                         "отменённую кампанию спрашивать незачем")


class TestNetworkModeIsDeliberate(unittest.TestCase):
    """Дашборд показывает обороты, расходы и названия кампаний, а пароля у
    него нет. Поэтому по умолчанию он слушает только свой компьютер, а
    выход в сеть включается отдельно — и вместе с кодом доступа."""

    def handler(self, code=""):
        """Заготовка обработчика без сети — проверяем только правила входа."""
        from wbads.api import DashboardHandler

        handler = DashboardHandler.__new__(DashboardHandler)
        handler.access_code = code
        handler.headers = {}
        handler.client_address = ("192.168.1.50", 51234)
        return handler

    def test_without_a_code_nothing_is_asked(self):
        """На своём компьютере спрашивать у себя пароль незачем."""
        self.assertTrue(self.handler()._authorized({}))

    def test_wrong_code_is_refused(self):
        self.assertFalse(self.handler("7391")._authorized({"code": ["0000"]}))
        self.assertFalse(self.handler("7391")._authorized({}))

    def test_right_code_passes_in_the_address(self):
        handler = self.handler("7391")
        self.assertTrue(handler._authorized({"code": ["7391"]}))
        self.assertTrue(handler._authorized({"код": ["7391"]}))

    def test_remembered_code_passes(self):
        handler = self.handler("7391")
        handler.headers = {"Cookie": "theme=dark; wbads_code=7391"}
        self.assertTrue(handler._authorized({}))

    def test_a_similar_cookie_does_not_pass(self):
        handler = self.handler("7391")
        handler.headers = {"Cookie": "wbads_code=739"}
        self.assertFalse(handler._authorized({}))

    def test_own_computer_is_not_asked_for_the_code(self):
        """Тот, кто сидит за этим компьютером, видит код в окне программы.
        Спрашивать его там же — значит мешать хозяину."""
        handler = self.handler("7391")
        handler.client_address = ("127.0.0.1", 51234)
        self.assertTrue(handler._authorized({}))

    def test_server_stays_on_localhost_by_default(self):
        """Самая важная проверка: случайно в сеть выйти нельзя."""
        import inspect
        from wbads.api import _bind_server

        signature = inspect.signature(_bind_server)
        self.assertEqual(signature.parameters["host"].default, "127.0.0.1")

    def test_network_mode_generates_a_code_when_none_given(self):
        """Пустое поле «придумайте пароль» человек пропускает, а сервер
        при этом уже в сети. Поэтому код придумываем сами."""
        from unittest.mock import patch as p2

        seen = {}

        def fake_serve(cfg, open_browser=True, network=False, access_code=""):
            seen["network"] = network
            seen["code"] = access_code

        with p2("run.serve", fake_serve), \
                p2("wbads.db.data_range", lambda conn: ("2026-09-01", "2026-09-20")):
            cli.cmd_serve(Namespace(port=None, no_browser=True,
                                    network=True, code=""))
        self.assertTrue(seen["network"])
        self.assertTrue(seen["code"], "в сети код обязателен")
        self.assertEqual(len(seen["code"]), 4)

    def test_local_mode_has_no_code_at_all(self):
        from unittest.mock import patch as p2

        seen = {}

        def fake_serve(cfg, open_browser=True, network=False, access_code=""):
            seen["code"] = access_code

        with p2("run.serve", fake_serve), \
                p2("wbads.db.data_range", lambda conn: ("2026-09-01", "2026-09-20")):
            cli.cmd_serve(Namespace(port=None, no_browser=True,
                                    network=False, code=""))
        self.assertEqual(seen["code"], "")
