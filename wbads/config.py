"""Настройки сервиса.

Читаются из переменных окружения и файла .env (см. .env.example).
Токен хранится ТОЛЬКО в .env, который не попадает в git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """Простой парсер .env — без внешних зависимостей.

    Переменные, уже заданные в окружении, имеют приоритет над файлом.
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


def write_token(token: str, env_path: Path = ENV_FILE,
                template: Path = ROOT / ".env.example") -> Path:
    """Сохраняет токен в .env, сохраняя остальные настройки.

    Если .env ещё нет — создаётся из шаблона, чтобы вместе с токеном
    приехали и комментарии с объяснениями настроек.
    """
    token = token.strip().strip('"').strip("'")
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
    db_raw = os.environ.get("WBADS_DB", "data/wbads.db")
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
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
