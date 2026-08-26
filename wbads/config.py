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
        if key and key not in os.environ:
            os.environ[key] = value


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


@dataclass
class Config:
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "wbads.db")
    token: str = ""
    port: int = 8000
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
    return Config(
        db_path=db_path,
        token=os.environ.get("WB_API_TOKEN", ""),
        port=int(_num("WBADS_PORT", 8000)),
        thresholds=Thresholds.from_env(),
    )
