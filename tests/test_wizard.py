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
                    patch("run.getpass.getpass", lambda prompt="": typed_token), \
                    patch("run.cmd_check", lambda a, quiet_tail=False: 0), \
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

    def test_prompt_warns_that_input_is_hidden(self):
        """Скрытый ввод пугает: «я вставила, а ничего не появилось»."""
        _, out, _ = self.run_setup("abc")
        self.assertIn("НЕ появятся", out)


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
