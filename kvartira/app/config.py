"""Настройки приложения и пути к файлам.

Всё, что можно менять, лежит в файле .env рядом с run.py.
Здесь только чтение этих настроек и разумные значения по умолчанию.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Пути ──────────────────────────────────────────────────────────────────
# BASE_DIR — папка kvartira/, в которой лежит run.py
BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "app.db"
ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"


def load_env(path: Path = ENV_PATH) -> None:
    """Читает .env и кладёт значения в переменные окружения.

    Свой простой разбор вместо отдельной библиотеки: формат КЛЮЧ=значение,
    строки с # игнорируются. Уже заданные переменные окружения не перетираем.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)).replace(",", "."))
    except ValueError:
        return default


class Settings:
    """Текущие настройки. Создаётся один раз при старте."""

    def __init__(self) -> None:
        load_env()

        # ── Сервер ────────────────────────────────────────────────────────
        self.host: str = _get("APP_HOST", "127.0.0.1")
        self.port: int = _get_int("APP_PORT", 8000)

        # ── Значения по умолчанию для квартиры ────────────────────────────
        # Высота потолка и толщины стен — в миллиметрах.
        # Все размеры в приложении хранятся в целых миллиметрах: так не
        # накапливается ошибка округления и не путаются метры с сантиметрами.
        self.ceiling_height_mm: int = _get_int("CEILING_HEIGHT_MM", 2900)
        self.outer_wall_mm: int = _get_int("OUTER_WALL_MM", 250)
        self.inner_wall_mm: int = _get_int("INNER_WALL_MM", 120)
        self.window_sill_mm: int = _get_int("WINDOW_SILL_MM", 800)
        self.window_top_mm: int = _get_int("WINDOW_TOP_MM", 2300)
        self.door_height_mm: int = _get_int("DOOR_HEIGHT_MM", 2100)

        # ── AI: чтение чертежа ────────────────────────────────────────────
        # Пусто = сервис не настроен. Приложение при этом работает,
        # просто кнопка «Разобрать план» скажет, что нужен ключ.
        self.plan_api_key: str = _get("PLAN_API_KEY")
        self.plan_base_url: str = _get("PLAN_BASE_URL", "https://api.anthropic.com")
        self.plan_model: str = _get("PLAN_MODEL", "claude-opus-5")

        # ── AI: создание визуализаций ─────────────────────────────────────
        self.image_api_key: str = _get("IMAGE_API_KEY")
        self.image_base_url: str = _get(
            "IMAGE_BASE_URL", "https://generativelanguage.googleapis.com"
        )
        self.image_model: str = _get("IMAGE_MODEL", "gemini-3.1-flash-image")

        # ── Защита от лишних трат ─────────────────────────────────────────
        self.daily_limit_usd: float = _get_float("DAILY_LIMIT_USD", 3.0)

    @property
    def plan_ready(self) -> bool:
        return bool(self.plan_api_key)

    @property
    def image_ready(self) -> bool:
        return bool(self.image_api_key)


settings = Settings()


def ensure_dirs() -> None:
    """Создаёт папки для данных и логов, если их ещё нет."""
    for path in (DATA_DIR, PROJECTS_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)
