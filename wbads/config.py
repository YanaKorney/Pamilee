"""Настройки сервиса.

Читаются из переменных окружения и файла .env (см. .env.example).
Токен хранится ТОЛЬКО в .env, который не попадает в git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Версия программы. Папок с программой у человека накапливается несколько,
# и запускается не всегда свежая: «обновил, а нового не видно» означало
# обычно запуск старой копии. Угадывать это в переписке — потерянный день,
# поэтому версия и папка видны и в окне, и в дашборде.
#
# Дата сборки, а не номер: человеку она говорит больше.
APP_VERSION = "2026-10-10"

# Данные живут ОТДЕЛЬНО от программы — в домашней папке пользователя.
#
# Иначе каждое обновление их стирает: человек скачивает новый архив,
# распаковывает в новую папку, переносит туда токен — и теряет базу
# вместе с журналом изменений и собранной статистикой, потому что они
# лежали внутри старой папки. Ровно так и произошло: журнал на 797
# записей исчез при первом же обновлении.
#
# Папка названа по-русски и лежит на виду: её должно быть легко найти,
# чтобы скопировать на другой компьютер или положить в резервную копию.
DATA_HOME = Path(os.environ.get("WBADS_HOME")
                 or (Path.home() / "Аналитика рекламы WB"))

# Старое место. Читается, если там что-то есть: у тех, кто уже работает,
# данные не должны пропасть из-за переезда.
LEGACY_DB = ROOT / "data" / "wbads.db"
LEGACY_ENV = ROOT / ".env"

ENV_FILE = DATA_HOME / ".env"


def ensure_data_home() -> Path:
    """Создаёт папку данных и переносит туда всё из старого места."""
    try:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Нет доступа к домашней папке — работаем по-старому, рядом
        # с программой. Лучше так, чем не запуститься вовсе.
        return ROOT
    return DATA_HOME


# Значение, которое шаблон прописывал в .env сам. Это не выбор человека,
# а моя оплошность: строка возвращала базу внутрь папки программы и
# отменяла переезд для всех, кто когда-либо сохранял токен.
TEMPLATE_DB_VALUE = "data/wbads.db"


def heal_pinned_database(env_path: Path | None = None) -> bool:
    """Убирает из .env путь к базе, который прописал шаблон.

    Трогаем только точное значение из шаблона: если человек задал свой
    путь осознанно, он должен остаться. Отличить одно от другого больше
    нечем, и ошибаться надо в сторону сохранения чужого выбора.
    """
    path = env_path or ENV_FILE
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    cleaned, changed = [], False
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("WBADS_DB="):
            value = stripped.partition("=")[2].strip().strip('"').strip("'")
            if value == TEMPLATE_DB_VALUE:
                cleaned.append("# " + raw + "   # убрано: база живёт отдельно")
                changed = True
                continue
        cleaned.append(raw)

    if changed:
        try:
            path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
        except OSError:
            return False
    return changed


def migrate_from_program_folder() -> list[str]:
    """Переносит базу и токен из папки программы в папку данных.

    Возвращает, что именно переехало, — человеку надо об этом сказать:
    файлы пропали не сами по себе.
    """
    moved: list[str] = []
    home = ensure_data_home()
    if home == ROOT:
        return moved

    for source, target, title in (
        (LEGACY_DB, home / "wbads.db", "база данных"),
        (LEGACY_ENV, home / ".env", "настройки с токеном"),
    ):  # noqa: E501
        if not source.exists() or target.exists():
            continue
        try:
            target.write_bytes(source.read_bytes())
            source.unlink()
            moved.append(title)
        except OSError:
            continue

    # Старые .env несут в себе путь к базе из шаблона. Пока он там, вся
    # затея с отдельной папкой не работает.
    if heal_pinned_database():
        moved.append("путь к базе в настройках")
    return moved


def load_env(path: Path | None = None) -> None:
    """Простой парсер .env — без внешних зависимостей.

    Переменные, уже заданные в окружении, имеют приоритет над файлом.
    """
    if path is None:
        # Сначала новое место, потом старое: у того, кто ещё не обновился,
        # токен лежит рядом с программой и обязан продолжать работать.
        for candidate in (ENV_FILE, LEGACY_ENV):
            if candidate.exists():
                load_env(candidate)
        return
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Пустая переменная окружения не должна затенять значение из .env:
        # иначе однажды экспортированный пустой WB_API_TOKEN навсегда
        # перекрыл бы только что сохранённый токен.
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = value


def token_in_template(path: Path = ROOT / ".env.example") -> bool:
    """Не вписан ли настоящий токен в шаблон вместо .env.

    Файл .env.example отслеживается git и уезжает в репозиторий — токен в нём
    утечёт при первом же push. Сервис его оттуда не читает, поэтому ошибка
    выглядит как «токен не найден», хотя он вроде бы вписан. Ловим явно.
    """
    if not path.exists():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "WB_API_TOKEN" and value.strip().strip('"').strip("'"):
            return True
    return False


# Файл-подсказка для вставки токена ищется рядом с программой: человек
# кладёт его туда, где видит ярлыки, а не в папку данных.
TOKEN_DROP_FILE = ROOT / "token.txt"


def token_from_file(path: Path = TOKEN_DROP_FILE) -> str:
    """Забирает токен из файла token.txt и сразу удаляет его.

    Запасной путь для тех, у кого не получается вставить текст в окно
    командной строки: вставить в Блокнот умеет каждый. Файл удаляем,
    чтобы токен не лежал в открытом виде рядом с программой.
    """
    if not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""

    # Из Блокнота нередко приезжают кавычки, перевод строки и лишние пробелы
    token = raw.strip().strip('"').strip("'").strip()
    token = "".join(token.split())
    if token:
        try:
            path.unlink()
        except OSError:
            pass
    return token


def write_token(token: str, env_path: Path | None = None,
                template: Path = ROOT / ".env.example") -> Path:
    """Сохраняет токен в .env, сохраняя остальные настройки.

    Если .env ещё нет — создаётся из шаблона, чтобы вместе с токеном
    приехали и комментарии с объяснениями настроек.
    """
    token = token.strip().strip('"').strip("'")
    if env_path is None:
        ensure_data_home()
        env_path = ENV_FILE
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif template.exists():
        lines = template.read_text(encoding="utf-8").splitlines()
    else:
        lines = ["WB_API_TOKEN="]

    replaced = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith("WB_API_TOKEN="):
            lines[i] = f"WB_API_TOKEN={token}"
            replaced = True
            break
    if not replaced:
        lines.append(f"WB_API_TOKEN={token}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def clear_template_token(template: Path = ROOT / ".env.example") -> bool:
    """Убирает токен из шаблона: этот файл уходит в репозиторий."""
    if not template.exists():
        return False
    lines = template.read_text(encoding="utf-8").splitlines()
    changed = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith("WB_API_TOKEN=") and raw.split("=", 1)[1].strip():
            lines[i] = "WB_API_TOKEN="
            changed = True
    if changed:
        template.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def token_from_template(template: Path = ROOT / ".env.example") -> str:
    """Достаёт токен, ошибочно вписанный в шаблон, чтобы перенести его в .env."""
    if not template.exists():
        return ""
    for raw in template.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "WB_API_TOKEN":
            return value.strip().strip('"').strip("'")
    return ""


def _num(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


@dataclass
class Thresholds:
    """Пороги, по которым кампания признаётся проблемной.

    Значения по умолчанию — рабочий ориентир для товара с нормальной маржой.
    Меняются в .env или прямо в дашборде (сохраняются в таблицу settings).
    """

    # Целевой ДРР, %. Выше — предупреждение, выше ×critical_multiplier — критично.
    target_drr: float = 15.0
    drr_critical_multiplier: float = 1.5
    # Кампании с расходом ниже этого за период не разбираем — статистики мало.
    min_spend: float = 300.0
    # Расход без единого заказа, после которого это уже критично.
    spend_no_orders: float = 1500.0
    # Рост CPC неделя к неделе, %, после которого предупреждаем.
    cpc_growth: float = 30.0
    # Падение CTR неделя к неделе, %.
    ctr_drop: float = 30.0
    # Падение показов неделя к неделе, %.
    views_drop: float = 50.0
    # Рост расхода неделя к неделе, %, при котором ждём роста заказов.
    spend_growth: float = 40.0
    # Абсолютный минимум CTR, % (ниже — карточка не цепляет в выдаче).
    ctr_floor: float = 1.0
    # Конверсия клик → корзина, % (ниже — проблема карточки, а не рекламы).
    cr_cart_floor: float = 5.0
    # Конверсия корзина → заказ, % (ниже — проблема цены/отзывов/доставки).
    cr_order_floor: float = 20.0
    # Дней подряд без показов у активной кампании.
    silent_days: float = 3.0
    # Доля дневного бюджета, при которой считаем, что кампания упирается в лимит.
    budget_hit_ratio: float = 0.95
    # ДРР ниже target × этого коэффициента — кандидат на масштабирование.
    scale_up_ratio: float = 0.6

    @classmethod
    def from_env(cls) -> "Thresholds":
        return cls(
            target_drr=_num("WBADS_TARGET_DRR", 15.0),
            min_spend=_num("WBADS_MIN_SPEND", 300.0),
            spend_no_orders=_num("WBADS_SPEND_NO_ORDERS", 1500.0),
        )

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# Какую цену заказа считать выручкой при расчёте общего ДРР.
#   price_with_disc — сумма после скидки продавца (сопоставимо с рекламным отчётом)
#   finished_price  — что фактически заплатил покупатель, с учётом СПП
#   total_price     — цена до скидок
ORDER_PRICE_FIELDS = ("price_with_disc", "finished_price", "total_price")


@dataclass
class Config:
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "wbads.db")
    token: str = ""
    port: int = 8000
    # Путь к своему набору корневых сертификатов. Нужен, когда в хранилище
    # системы устаревший корень и проверка цепочки срывается на нём.
    ca_bundle: str = ""
    order_price_field: str = "price_with_disc"
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def has_token(self) -> bool:
        return bool(self.token.strip())


def load_config() -> Config:
    load_env()
    db_raw = os.environ.get("WBADS_DB", "")
    if db_raw:
        db_path = Path(db_raw)
        if not db_path.is_absolute():
            db_path = ROOT / db_path
    else:
        # Постоянное место — главное. Старое рядом с программой берём,
        # только если постоянного ещё нет: иначе программа пишет в одну
        # базу, а читает другую, и человек видит то пустой журнал, то
        # полный, не понимая, от чего это зависит.
        home_db = ensure_data_home() / "wbads.db"
        db_path = home_db if home_db.exists() or not LEGACY_DB.exists() \
            else LEGACY_DB
    price_field = os.environ.get("WBADS_ORDER_PRICE", "price_with_disc").strip()
    if price_field not in ORDER_PRICE_FIELDS:
        price_field = "price_with_disc"
    return Config(
        db_path=db_path,
        token=os.environ.get("WB_API_TOKEN", ""),
        port=int(_num("WBADS_PORT", 8000)),
        ca_bundle=(os.environ.get("WBADS_CA_BUNDLE")
                   or os.environ.get("SSL_CERT_FILE") or "").strip(),
        order_price_field=price_field,
        thresholds=Thresholds.from_env(),
    )


# Где искать базу от прежней установки. Человек распаковывает архив туда,
# куда скачал, поэтому смотрим в привычные места, а не просим вспомнить путь.
def search_roots() -> list[Path]:
    home = Path.home()
    roots = [ROOT.parent, home]
    for name in ("Downloads", "Загрузки", "Desktop", "Рабочий стол",
                 "Documents", "Документы", "OneDrive"):
        roots.append(home / name)
    seen, unique = set(), []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def find_other_databases(current: Path, limit: int = 12) -> list[dict[str, object]]:
    """Ищет базы прежних установок и рассказывает, что в каждой.

    Размер файла ни о чём не говорит человеку, а «812 записей журнала и
    30 дней статистики» говорит всё. Поэтому каждая найденная база
    открывается на чтение и пересчитывается.
    """
    import sqlite3

    found: list[dict[str, object]] = []
    seen: set[Path] = set()
    try:
        current = current.resolve()
    except OSError:
        pass

    for root in search_roots():
        try:
            # Вглубь не лезем: база лежит в папке программы, в её data/
            # или в папке данных — дальше второго уровня её не бывает.
            # Архив с гитхаба распаковывается во вложенную папку, и
            # человек нередко кладёт её ещё в одну — «новая папка».
            # Поэтому три уровня, но не больше: глубже начинается обход
            # всего диска.
            candidates = []
            for pattern in ("wbads.db", "*/wbads.db", "*/data/wbads.db",
                            "*/*/wbads.db", "*/*/data/wbads.db"):
                candidates += list(root.glob(pattern))
        except OSError:
            continue
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved == current or resolved in seen:
                continue
            seen.add(resolved)
            info = describe_database(resolved)
            if info:
                found.append(info)
            if len(found) >= limit:
                return found
    return found


def describe_database(path: Path) -> dict[str, object] | None:
    """Что лежит в файле базы. None — если это не наша база."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "campaign_daily" not in tables:
            return None
        counts = {}
        for key, sql in (
            ("changes", "SELECT COUNT(*) FROM changes"),
            ("days", "SELECT COUNT(DISTINCT date) FROM campaign_daily"),
            ("articles", "SELECT COUNT(DISTINCT nm_id) FROM campaign_nm_daily"),
            ("campaigns", "SELECT COUNT(*) FROM campaigns"),
            ("orders", "SELECT COUNT(*) FROM orders_raw"),
        ):
            try:
                counts[key] = int(conn.execute(sql).fetchone()[0] or 0)
            except sqlite3.Error:
                counts[key] = 0
        last = ""
        try:
            last = str(conn.execute(
                "SELECT MAX(date) FROM campaign_daily").fetchone()[0] or "")
        except sqlite3.Error:
            pass
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()

    return {"path": path, "last_day": last, **counts}


def find_previous_tokens(current: Path | None = None) -> list[dict[str, object]]:
    """Ищет токен в прежних установках программы.

    Перенос из папки программы помогает только тому, кто обновляется
    поверх старой папки. Человек, который распаковывает архив в новую, —
    а именно так и написано во всех моих инструкциях — остаётся и без
    токена, и без базы: переносить в его новой папке нечего.
    """
    found: list[dict[str, object]] = []
    seen: set[Path] = set()
    current = (current or ENV_FILE)
    try:
        current = current.resolve()
    except OSError:
        pass

    for root in search_roots():
        candidates: list[Path] = []
        for pattern in (".env", "*/.env", "*/*/.env",
                        "token.txt", "*/token.txt", "*/*/token.txt"):
            try:
                candidates += list(root.glob(pattern))
            except OSError:
                continue
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved == current or resolved in seen:
                continue
            seen.add(resolved)
            token = _token_in_file(resolved)
            if token:
                found.append({"path": resolved, "token": token})
    return found


def _token_in_file(path: Path) -> str:
    """Достаёт токен из .env или token.txt. Пустая строка — если его нет."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if path.name == "token.txt":
        candidate = text.strip().strip('"').strip("'")
        return candidate if _looks_like_token(candidate) else ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("WB_API_TOKEN="):
            continue
        candidate = line.partition("=")[2].strip().strip('"').strip("'")
        if _looks_like_token(candidate):
            return candidate
    return ""


def _looks_like_token(value: str) -> bool:
    """Токен WB — длинный JWT из трёх частей. Заготовка в шаблоне — нет."""
    value = (value or "").strip()
    return len(value) > 80 and value.count(".") == 2 and " " not in value
