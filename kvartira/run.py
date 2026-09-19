#!/usr/bin/env python3
"""Запуск приложения «Моя квартира».

Этот файл делает всё сам:
  1) проверяет версию Python;
  2) при первом запуске создаёт отдельное окружение и ставит библиотеки;
  3) создаёт файл настроек .env, если его ещё нет;
  4) поднимает сервер и открывает браузер.

Запускать так:  python3 run.py
Или просто двойным кликом по START-Mac.command / START-Windows.bat
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / ".venv"
REQUIREMENTS = BASE_DIR / "requirements.txt"
OPTIONAL_REQUIREMENTS = BASE_DIR / "requirements-optional.txt"
STAMP_FILE = VENV_DIR / ".requirements-stamp"
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"

MIN_PYTHON = (3, 10)
DEFAULT_PORT = 8000


# ── Печать ────────────────────────────────────────────────────────────────

def say(text: str = "") -> None:
    print(text, flush=True)


def title(text: str) -> None:
    say()
    say(text)
    say("─" * min(len(text), 60))


def fail(problem: str, what_to_do: str) -> None:
    """Печатает понятную ошибку и завершает работу."""
    say()
    say("╭" + "─" * 58 + "╮")
    say("│  Не получилось запустить программу" + " " * 23 + "│")
    say("╰" + "─" * 58 + "╯")
    say()
    say(f"Что случилось:  {problem}")
    say()
    say("Что сделать:")
    for line in what_to_do.strip().splitlines():
        say(f"  {line.strip()}")
    say()
    _wait_before_exit()
    sys.exit(1)


def _wait_before_exit() -> None:
    """Чтобы окно не закрылось мгновенно при запуске двойным кликом."""
    if sys.stdin and sys.stdin.isatty():
        try:
            input("Нажмите Enter, чтобы закрыть окно… ")
        except (EOFError, KeyboardInterrupt):
            pass


# ── Окружение ─────────────────────────────────────────────────────────────

def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def running_inside_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == venv_python().resolve()
    except OSError:
        return False


def requirements_stamp() -> str:
    data = b""
    for path in (REQUIREMENTS, OPTIONAL_REQUIREMENTS):
        if path.exists():
            data += path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def create_venv() -> None:
    title("Готовлю рабочее окружение")
    say("Это делается один раз и занимает примерно минуту.")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        fail(
            "Не найден сам Python.",
            "Установите Python 3.10 или новее с сайта python.org и запустите снова.",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["неизвестная причина"]
        fail(
            f"Не удалось создать рабочее окружение ({detail[0]}).",
            """
            1. На Windows: переустановите Python с python.org,
               на первом экране поставьте галочку «Add Python to PATH».
            2. На Linux: выполните в терминале
               sudo apt install python3-venv
            3. Запустите программу ещё раз.
            """,
        )


def install_requirements() -> None:
    title("Устанавливаю нужные библиотеки")
    say("Идёт загрузка — подождите, пожалуйста.")
    python = str(venv_python())

    subprocess.run(
        [python, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        capture_output=True, text=True,
    )

    # --prefer-binary: берём готовые сборки, а не собираем из исходников.
    # Сборка из исходников — самая частая причина, по которой установка
    # падает у человека без инструментов разработчика.
    result = subprocess.run(
        [python, "-m", "pip", "install", "--prefer-binary", "-r", str(REQUIREMENTS), "--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-4:]
        version = ".".join(str(n) for n in sys.version_info[:3])
        fail(
            "Не удалось скачать библиотеки.",
            f"""
            1. Проверьте, что интернет работает.
            2. Если используете VPN — попробуйте выключить его и запустить снова.
            3. У вас установлен Python {version}. Если это самая свежая версия,
               вышедшая недавно, готовых сборок для неё может ещё не быть.
               Поставьте Python 3.12 или 3.13 с сайта python.org, удалите
               папку .venv рядом с программой и запустите снова.
            4. Если не помогло, покажите разработчику эти строки:
            """
            + "\n".join(f"   {line}" for line in tail),
        )

    # Необязательное ставим отдельно и молча: не установилось — не беда.
    if OPTIONAL_REQUIREMENTS.exists():
        extra = subprocess.run(
            [python, "-m", "pip", "install", "--prefer-binary",
             "-r", str(OPTIONAL_REQUIREMENTS), "--quiet"],
            capture_output=True, text=True,
        )
        if extra.returncode != 0:
            say("Одна необязательная библиотека не установилась — это не помешает.")
            say("Не будут открываться только файлы формата WEBP.")

    STAMP_FILE.write_text(requirements_stamp(), encoding="utf-8")
    say("Готово.")


def prepare_environment() -> None:
    """Создаёт окружение и ставит библиотеки, если это ещё не сделано."""
    if not venv_python().exists():
        create_venv()
        install_requirements()
        return
    previous = STAMP_FILE.read_text(encoding="utf-8").strip() if STAMP_FILE.exists() else ""
    if previous != requirements_stamp():
        install_requirements()


def restart_inside_venv() -> None:
    python = str(venv_python())
    args = [python, str(BASE_DIR / "run.py"), *sys.argv[1:]]
    if os.name == "nt":
        raise SystemExit(subprocess.run(args).returncode)
    os.execv(python, args)


# ── Настройки ─────────────────────────────────────────────────────────────

def ensure_env_file() -> None:
    if ENV_FILE.exists() or not ENV_EXAMPLE.exists():
        return
    ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    title("Создан файл настроек .env")
    say("Пока он пустой — программа будет работать, но без AI-функций.")
    say("Как вписать ключи доступа, написано в README.md, раздел «Ключи».")


# ── Порт ──────────────────────────────────────────────────────────────────

def find_free_port(preferred: int) -> int:
    """Ищет свободный порт. Занятый порт — не повод падать."""
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    fail(
        "Все подходящие порты заняты.",
        "Закройте другие программы и запустите снова.",
    )
    return preferred  # сюда не дойдём, нужно только для проверки типов


def open_browser_later(url: str) -> None:
    def worker() -> None:
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:  # браузер мог не найтись — не повод останавливаться
            pass

    threading.Thread(target=worker, daemon=True).start()


# ── Запуск сервера ────────────────────────────────────────────────────────

def serve(port: int) -> None:
    import uvicorn  # импорт здесь: до установки библиотек его ещё нет

    from app import __version__
    from app.config import settings
    from app.db import init_db

    init_db()

    url = f"http://127.0.0.1:{port}"
    say()
    say("╭" + "─" * 58 + "╮")
    say("│  Моя квартира — визуализация дизайн-проекта" + " " * 15 + "│")
    say("╰" + "─" * 58 + "╯")
    say()
    say(f"  Откройте в браузере:   {url}")
    say("  Чтобы остановить:      закройте это окно или нажмите Ctrl+C")
    say()
    say(f"  Высота потолка по умолчанию: {settings.ceiling_height_mm} мм")
    plan_state = "настроено" if settings.plan_ready else "не настроено"
    image_state = "настроено" if settings.image_ready else "не настроено"
    say(f"  Чтение чертежа: {plan_state}   ·   Визуализации: {image_state}")
    say(f"  Версия: {__version__}")
    say()

    open_browser_later(url)
    uvicorn.run(
        "app.server:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def main() -> None:
    if sys.version_info < MIN_PYTHON:
        current = ".".join(str(n) for n in sys.version_info[:3])
        fail(
            f"Нужен Python 3.10 или новее, а установлен {current}.",
            """
            1. Откройте сайт python.org, раздел Downloads.
            2. Скачайте и установите свежую версию.
            3. На Windows поставьте галочку «Add Python to PATH».
            4. Запустите программу ещё раз.
            """,
        )

    use_venv = "--no-venv" not in sys.argv
    if use_venv and not running_inside_venv():
        os.chdir(BASE_DIR)
        prepare_environment()
        restart_inside_venv()
        return

    os.chdir(BASE_DIR)
    sys.path.insert(0, str(BASE_DIR))
    ensure_env_file()

    port = DEFAULT_PORT
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    port = find_free_port(port)

    try:
        serve(port)
    except KeyboardInterrupt:
        say()
        say("Программа остановлена. Все данные сохранены.")


if __name__ == "__main__":
    main()
