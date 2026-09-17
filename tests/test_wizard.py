"""Проверки мастера настройки: он рассчитан на человека, который
не работает с командной строкой, поэтому важно, чтобы он не ронял
данные и не оставлял токен в файле, уходящем в репозиторий."""

import io
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as cli  # noqa: E402
from wbads.config import (  # noqa: E402
    clear_template_token,
    token_from_template,
    token_in_template,
    write_token,
)


class TestTokenFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.tpl = self.dir / ".env.example"
        self.env = self.dir / ".env"

    def test_creates_env_from_template(self):
        """Новый .env наследует комментарии шаблона — они объясняют настройки."""
        self.tpl.write_text("# как настроить\nWB_API_TOKEN=\nWBADS_PORT=8000\n",
                            encoding="utf-8")
        write_token("abc", self.env, self.tpl)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("WB_API_TOKEN=abc", text)
        self.assertIn("# как настроить", text)
        self.assertIn("WBADS_PORT=8000", text)

    def test_replacing_token_keeps_other_settings(self):
        self.env.write_text("WB_API_TOKEN=old\nWBADS_PORT=9999\nWBADS_TARGET_DRR=12\n",
                            encoding="utf-8")
        write_token("new", self.env, self.tpl)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("WB_API_TOKEN=new", text)
        self.assertNotIn("old", text)
        self.assertIn("WBADS_PORT=9999", text)
        self.assertIn("WBADS_TARGET_DRR=12", text)

    def test_quotes_and_spaces_are_stripped(self):
        write_token('  "abc"  ', self.env, self.tpl)
        self.assertIn("WB_API_TOKEN=abc\n", self.env.read_text(encoding="utf-8"))

    def test_token_missing_in_env_is_appended(self):
        self.env.write_text("WBADS_PORT=8000\n", encoding="utf-8")
        write_token("abc", self.env, self.tpl)
        self.assertIn("WB_API_TOKEN=abc", self.env.read_text(encoding="utf-8"))

    def test_template_is_cleaned_but_kept_readable(self):
        self.tpl.write_text("# коммент\nWB_API_TOKEN=leaked\nWBADS_PORT=8000\n",
                            encoding="utf-8")
        self.assertEqual(token_from_template(self.tpl), "leaked")
        self.assertTrue(clear_template_token(self.tpl))
        self.assertFalse(token_in_template(self.tpl))
        text = self.tpl.read_text(encoding="utf-8")
        self.assertIn("# коммент", text)
        self.assertIn("WBADS_PORT=8000", text)

    def test_cleaning_clean_template_changes_nothing(self):
        self.tpl.write_text("WB_API_TOKEN=\n", encoding="utf-8")
        self.assertFalse(clear_template_token(self.tpl))


class TestSetupCommand(unittest.TestCase):
    def run_setup(self, typed_token: str, env_token: str = ""):
        """Возвращает код, вывод и содержимое .env — файл читаем до уборки
        временной папки, иначе к моменту проверки его уже нет."""
        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            tpl = Path(tmp) / ".env.example"
            tpl.write_text("WB_API_TOKEN=\n", encoding="utf-8")
            # Проверяем именно сохранение токена, поэтому обращение
            # к API подменяем целиком — оно проверено отдельно.
            with patch.dict("os.environ", {"WB_API_TOKEN": env_token}), \
                    patch("run.write_token", lambda t: write_token(t, env, tpl)), \
                    patch("run.token_from_template", lambda: token_from_template(tpl)), \
                    patch("run.clear_template_token", lambda: clear_template_token(tpl)), \
                    patch("builtins.input", lambda prompt="": typed_token), \
                    patch("run.cmd_check", lambda a, quiet_tail=False: 0), \
                    patch("run.token_from_file", lambda: ""), \
                    patch("run._ask", lambda q, default="д": False), \
                    redirect_stdout(buffer):
                code = cli.cmd_setup(Namespace())
            written = env.read_text(encoding="utf-8") if env.exists() else None
            return code, buffer.getvalue(), written

    def test_saves_typed_token(self):
        code, out, written = self.run_setup("eyJ.token.here")
        self.assertEqual(code, 0)
        self.assertIn("WB_API_TOKEN=eyJ.token.here", written)
        self.assertIn("никуда не отправляется", out)

    def test_empty_input_changes_nothing(self):
        """Человек передумал и нажал Enter — файлы не трогаем."""
        code, out, written = self.run_setup("")
        self.assertEqual(code, 1)
        self.assertIsNone(written)
        self.assertIn("demo", out)

    def test_prompt_shows_how_to_paste(self):
        """Раньше ввод был скрыт, и вставку нельзя было проверить глазами."""
        _, out, _ = self.run_setup("aaa.bbb.ccc")
        self.assertIn("виден на экране", out)
        self.assertIn("token.txt", out)


class TestStartWizard(unittest.TestCase):
    def test_declining_token_falls_back_to_demo(self):
        """Отказ от настройки не должен упираться в тупик — показываем демо."""
        served = []
        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            from wbads.config import Config, Thresholds
            cfg = Config(db_path=Path(tmp) / "w.db", token="", thresholds=Thresholds())
            with patch("run.load_config", lambda: cfg), \
                    patch("run.token_from_template", lambda: ""), \
                    patch("run._ask", lambda q, default="д": False), \
                    patch("run.cmd_serve", lambda a: served.append(a) or 0), \
                    redirect_stdout(buffer):
                code = cli.cmd_start(Namespace(days=30))
        out = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(len(served), 1)
        self.assertIn("демо-кабинет", out)
        self.assertIn("Шаг 1 из 3", out)

    def test_wizard_numbers_its_steps(self):
        """Человеку нужно понимать, сколько ещё осталось."""
        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            from wbads.config import Config, Thresholds
            cfg = Config(db_path=Path(tmp) / "w.db", token="", thresholds=Thresholds())
            with patch("run.load_config", lambda: cfg), \
                    patch("run.token_from_template", lambda: ""), \
                    patch("run._ask", lambda q, default="д": False), \
                    patch("run.cmd_serve", lambda a: 0), \
                    redirect_stdout(buffer):
                cli.cmd_start(Namespace(days=30))
        self.assertIn("Шаг 1 из 3", buffer.getvalue())


class TestEmptyEnvVar(unittest.TestCase):
    """Пустая переменная окружения не должна перекрывать .env."""

    def test_empty_variable_does_not_shadow_env_file(self):
        import os
        from wbads.config import load_env
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("WB_API_TOKEN=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"WB_API_TOKEN": ""}):
                load_env(env)
                self.assertEqual(os.environ["WB_API_TOKEN"], "from-file")

    def test_real_variable_still_wins(self):
        import os
        from wbads.config import load_env
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("WB_API_TOKEN=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"WB_API_TOKEN": "from-shell"}):
                load_env(env)
                self.assertEqual(os.environ["WB_API_TOKEN"], "from-shell")


class TestAsk(unittest.TestCase):
    def test_empty_answer_takes_default(self):
        with patch("builtins.input", lambda prompt="": ""):
            self.assertTrue(cli._ask("Продолжить?", default="д"))
            self.assertFalse(cli._ask("Продолжить?", default="н"))

    def test_russian_and_latin_yes(self):
        for answer in ("д", "да", "y", "yes", "1"):
            with patch("builtins.input", lambda prompt="", a=answer: a):
                self.assertTrue(cli._ask("Продолжить?"), answer)

    def test_no_answers(self):
        for answer in ("н", "нет", "n", "no"):
            with patch("builtins.input", lambda prompt="", a=answer: a):
                self.assertFalse(cli._ask("Продолжить?"), answer)

    def test_interrupt_is_treated_as_no(self):
        def boom(prompt=""):
            raise KeyboardInterrupt
        with patch("builtins.input", boom):
            self.assertFalse(cli._ask("Продолжить?"))


if __name__ == "__main__":
    unittest.main()


class TestTokenFromFile(unittest.TestCase):
    """Запасной путь: токен через token.txt. Нужен тем, у кого не выходит
    вставить текст в окно командной строки — а вставить в Блокнот умеет каждый."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "token.txt"

    def test_reads_and_deletes_the_file(self):
        from wbads.config import token_from_file
        self.path.write_text("abc.def.ghi", encoding="utf-8")
        self.assertEqual(token_from_file(self.path), "abc.def.ghi")
        self.assertFalse(self.path.exists(), "файл с токеном должен удаляться")

    def test_survives_what_notepad_adds(self):
        """Блокнот добавляет BOM, кавычки, перевод строки и пробелы."""
        from wbads.config import token_from_file
        self.path.write_text('\ufeff  "abc.def.ghi"  \n', encoding="utf-8")
        self.assertEqual(token_from_file(self.path), "abc.def.ghi")

    def test_strips_internal_line_breaks(self):
        """При копировании из браузера токен иногда приезжает с переносами."""
        from wbads.config import token_from_file
        self.path.write_text("abc.\ndef.\nghi\n", encoding="utf-8")
        self.assertEqual(token_from_file(self.path), "abc.def.ghi")

    def test_missing_file_gives_empty(self):
        from wbads.config import token_from_file
        self.assertEqual(token_from_file(self.path), "")

    def test_empty_file_is_kept(self):
        """Пустой файл не удаляем: человек, возможно, ещё не вставил токен."""
        from wbads.config import token_from_file
        self.path.write_text("   \n", encoding="utf-8")
        self.assertEqual(token_from_file(self.path), "")
        self.assertTrue(self.path.exists())


class TestVisibleTokenPrompt(unittest.TestCase):
    """Ввод токена виден на экране: скрытый ввод не давал понять,
    сработала ли вставка, и это оказалось главной причиной затыка."""

    def prompt(self, typed: str, platform: str = "win32") -> str:
        buffer = io.StringIO()
        with patch("builtins.input", lambda p="": typed), \
                patch("run.sys.platform", platform), \
                redirect_stdout(buffer):
            cli._ask_token()
        return buffer.getvalue()

    def test_explains_how_to_paste_on_windows(self):
        out = self.prompt("abc")
        self.assertIn("ПРАВОЙ кнопкой", out)
        self.assertIn("Ctrl+V", out)

    def test_offers_the_file_fallback(self):
        out = self.prompt("abc")
        self.assertIn("token.txt", out)
        self.assertIn("Блокнот", out)

    def test_says_the_token_will_be_visible(self):
        out = self.prompt("abc")
        self.assertIn("виден на экране", out)

    def test_empty_input_reads_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            drop = Path(tmp) / "token.txt"
            drop.write_text("aaa.bbb.ccc", encoding="utf-8")
            with patch("builtins.input", lambda p="": ""), \
                    patch("run.token_from_file", lambda: "aaa.bbb.ccc"), \
                    patch("run.TOKEN_DROP_FILE", drop), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cli._ask_token(), "aaa.bbb.ccc")


class TestPortFallback(unittest.TestCase):
    """Порт 8000 на Windows часто занят системой, и попытка его занять
    падает с WinError 10013. Программа обязана перейти на свободный,
    а не показывать простыню с ошибкой."""

    def test_busy_port_falls_back_to_free_one(self):
        import socket
        from http.server import BaseHTTPRequestHandler
        from wbads.api import _bind_server
        from wbads.config import Config, Thresholds

        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        busy = blocker.getsockname()[1]
        blocker.listen(1)
        self.addCleanup(blocker.close)

        cfg = Config(port=busy, thresholds=Thresholds())
        server, port = _bind_server(cfg, BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        self.assertNotEqual(port, busy)
        self.assertGreater(port, 0)

    def test_free_port_is_used_as_asked(self):
        from http.server import BaseHTTPRequestHandler
        from wbads.api import _bind_server
        from wbads.config import Config, Thresholds

        cfg = Config(port=8937, thresholds=Thresholds())
        server, port = _bind_server(cfg, BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        self.assertEqual(port, 8937)


class TestTokenDiagnostics(unittest.TestCase):
    """Разбор токена на месте: помогает понять причину отказа, не гадая."""

    @staticmethod
    def jwt(payload: dict) -> str:
        import base64
        import json
        enc = lambda d: base64.urlsafe_b64encode(  # noqa: E731
            json.dumps(d).encode()).decode().rstrip("=")
        return f"{enc({'alg': 'ES256'})}.{enc(payload)}.signature"

    def test_detects_sandbox_token(self):
        """Тестовый контур отвечает 403 сразу на всё — это надо назвать прямо."""
        from wbads.wb_client import describe_token
        import time
        info = describe_token(self.jwt({"exp": int(time.time()) + 8640, "t": True}))
        self.assertTrue(info["sandbox"])

    def test_detects_production_token(self):
        from wbads.wb_client import describe_token
        import time
        info = describe_token(self.jwt({"exp": int(time.time()) + 8640, "t": False}))
        self.assertFalse(info["sandbox"])

    def test_detects_expired_token(self):
        from wbads.wb_client import describe_token
        import time
        info = describe_token(self.jwt({"exp": int(time.time()) - 8640, "t": False}))
        self.assertTrue(info["expired"])

    def test_truncated_paste_is_named(self):
        """Обрезанная вставка — самая частая причина; её видно по длине."""
        from wbads.wb_client import describe_token
        info = describe_token("eyJhbGciOiJFUzI1NiJ9.eyJ")
        self.assertFalse(info["looks_like_jwt"])
        self.assertIsNotNone(info["error"])

    def test_empty_token(self):
        from wbads.wb_client import describe_token
        info = describe_token("")
        self.assertEqual(info["length"], 0)

    def test_never_returns_the_token_itself(self):
        """В диагностике не должно быть самого токена — её могут переслать."""
        from wbads.wb_client import describe_token
        import time
        token = self.jwt({"exp": int(time.time()) + 8640, "t": False, "sid": "s1"})
        info = describe_token(token)
        self.assertNotIn(token, str(info))
        self.assertLess(len(info["preview"]), 12)


class TestCrashReport(unittest.TestCase):
    def test_crash_prints_advice_not_traceback(self):
        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("run.ROOT", Path(tmp)), redirect_stdout(buffer):
                code = cli._report_crash(PermissionError("[WinError 10013] отказано"))
            out = buffer.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("Что-то пошло не так", out)
            self.assertIn("WinError 10013", out)
            self.assertNotIn("Traceback", out)
            self.assertTrue((Path(tmp) / "ошибка.txt").exists())
