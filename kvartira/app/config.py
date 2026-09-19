"""Настройки приложения и пути к файлам.

Всё, что можно менять, лежит в файле .env рядом с run.py.
Здесь только чтение этих настроек и разумные значения по умолчанию.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# ── Пути ──────────────────────────────────────────────────────────────────
# Программа живёт в одной папке, а ваши данные — в другой, в «Документах».
# Так обновление программы не задевает проекты: скачали новую версию,
# распаковали, запустили — ключ и проекты на месте.

# BASE_DIR — папка kvartira/, в которой лежит run.py
BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"

HOME_FOLDER_NAME = "Моя квартира"


def _documents_dir() -> Path:
    """Папка «Документы» пользователя, а если её нет — домашняя папка."""
    home = Path.home()
    for name in ("Documents", "Документы", "Мои документы"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home


def resolve_app_home() -> Path:
    """Где лежат данные. Можно задать переменной окружения APP_HOME."""
    override = os.environ.get("APP_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return _documents_dir() / HOME_FOLDER_NAME


APP_HOME = resolve_app_home()
DATA_DIR = APP_HOME / "data"
PROJECTS_DIR = DATA_DIR / "projects"
LOGS_DIR = APP_HOME / "logs"
DB_PATH = DATA_DIR / "app.db"
ENV_PATH = APP_HOME / ".env"


def migrate_from_program_folder() -> list[str]:
    """Переносит настройки и данные из старого места — из папки программы.

    Раньше всё лежало рядом с run.py и пропадало при обновлении.
    Переносим бережно: сначала копия, и только после удачной копии
    старое удаляется. Уже перенесённое не трогаем.
    """
    moved: list[str] = []

    old_env = BASE_DIR / ".env"
    if old_env.is_file() and not ENV_PATH.exists():
        APP_HOME.mkdir(parents=True, exist_ok=True)
        ENV_PATH.write_bytes(old_env.read_bytes())
        old_env.unlink()
        moved.append("настройки с ключом доступа")

    old_data = BASE_DIR / "data"
    if old_data.is_dir() and not DATA_DIR.exists():
        APP_HOME.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old_data, DATA_DIR)
        shutil.rmtree(old_data, ignore_errors=True)
        moved.append("проекты и загруженные планы")

    old_logs = BASE_DIR / "logs"
    if old_logs.is_dir() and not LOGS_DIR.exists():
        shutil.rmtree(old_logs, ignore_errors=True)

    return moved


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
        self.plan_base_url: str = _get("PLAN_BASE_URL", "https://api.aitunnel.ru/v1")
        self.plan_model: str = _get("PLAN_MODEL", "claude-opus-5")

        # ── AI: создание визуализаций ─────────────────────────────────────
        self.image_api_key: str = _get("IMAGE_API_KEY")
        self.image_base_url: str = _get("IMAGE_BASE_URL", "https://api.aitunnel.ru/v1")
        self.image_model: str = _get("IMAGE_MODEL", "flux.2-pro")

        # ── Цены сервиса, чтобы считать расходы в рублях ──────────────────
        # Берутся из каталога вашего сервиса. Значения по умолчанию —
        # для Claude Opus 5 и FLUX.2 в AITunnel на сентябрь 2026 года.
        self.price_in_rub_per_million: float = _get_float("PRICE_IN_RUB_PER_MILLION", 100)
        self.price_out_rub_per_million: float = _get_float("PRICE_OUT_RUB_PER_MILLION", 5000)
        self.price_image_rub: float = _get_float("PRICE_IMAGE_RUB", 10.71)

        # ── Защита от лишних трат ─────────────────────────────────────────
        self.daily_limit_rub: float = _get_float("DAILY_LIMIT_RUB", 300.0)

    @property
    def plan_ready(self) -> bool:
        return bool(self.plan_api_key)

    @property
    def image_ready(self) -> bool:
        return bool(self.image_api_key)


# Переезд со старого расположения делается до чтения настроек.
MIGRATED = migrate_from_program_folder()

settings = Settings()


def ensure_dirs() -> None:
    """Создаёт папки для данных и логов, если их ещё нет."""
    for path in (DATA_DIR, PROJECTS_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)
