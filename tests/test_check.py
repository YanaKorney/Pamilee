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
    "campaign_details": lambda self, ids: [{"advertId": ids[0]}],
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
                         "/adv/v1/promotion/adverts", "/adv/v2/fullstats",
                         "/api/v1/supplier/orders"):
            self.assertIn(endpoint, out)

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
        code, out = run_check({"balance": denied, "campaign_ids": denied})
        self.assertEqual(code, 1)
        self.assertIn("Закрыто методов рекламы: 2", out)
        self.assertIn("Продвижение", out)

    def test_partial_access_points_at_the_closed_method(self):
        """Кампании читаются, а статистика нет — видно ровно этот метод."""
        code, out = run_check({"fullstats": denied})
        self.assertEqual(code, 1)
        self.assertIn("Закрыто методов рекламы: 1", out)
        self.assertIn("✗ Статистика по дням", out)
        self.assertIn("Только на чтение", out)

    def test_missing_token_explains_where_to_get_it(self):
        buffer = io.StringIO()
        with patch.dict("os.environ", {"WB_API_TOKEN": ""}), redirect_stdout(buffer):
            code = cli.cmd_check(Namespace())
        out = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Доступ к API", out)
        self.assertIn("Продвижение", out)


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
                               "campaign_ids": self.tls_error},
                              orders={"orders": self.tls_error})
        self.assertEqual(code, 1)
        self.assertIn("НЕ про токен", out)
        self.assertIn("Дата и время на этом компьютере", out)
        self.assertNotIn("выпустите токен заново", out)
        self.assertNotIn("Тестовый контур", out)

    def test_tls_advice_printed_once_not_per_method(self):
        """Длинное объяснение не должно повторяться под каждым методом."""
        _, out = run_check({"balance": self.tls_error,
                            "campaign_ids": self.tls_error},
                           orders={"orders": self.tls_error})
        self.assertEqual(out.count("Дата и время на этом компьютере"), 1)

    def test_network_failure_suggests_checking_internet(self):
        code, out = run_check({"balance": self.network_error,
                               "campaign_ids": self.network_error},
                              orders={"orders": self.network_error})
        self.assertEqual(code, 1)
        self.assertIn("связаться с Wildberries", out)
        self.assertNotIn("Тестовый контур", out)

    def test_real_403_still_diagnoses_the_token(self):
        """А вот настоящий отказ доступа по-прежнему разбирает токен."""
        code, out = run_check({"balance": denied, "campaign_ids": denied},
                              orders={"orders": denied})
        self.assertEqual(code, 1)
        self.assertIn("Что записано в вашем токене", out)

    def test_offers_demo_while_connection_is_broken(self):
        _, out = run_check({"balance": self.tls_error,
                            "campaign_ids": self.tls_error},
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
        from unittest.mock import patch as p2
        fake = {"host": "advert-api.wildberries.ru",
                "issuer": "DigiCert Inc · DigiCert Global Root G2",
                "subject": "advert-api.wildberries.ru",
                "not_after": "Dec 31 23:59:59 2027 GMT",
                "expired": False, "error": None}
        buffer = io.StringIO()
        with p2("run.inspect_certificate", lambda *a, **k: fake), \
                redirect_stdout(buffer):
            cli._who_breaks_the_connection()
        out = buffer.getvalue()
        self.assertIn("не похоже на антивирус", out)
        self.assertIn("обновления Windows", out)


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
