"""Динамика по дням: все показатели разом, по группам, с тепловой картой.

Сводка по товарам отвечает на вопрос «какие цифры были в эти дни».
Здесь вопрос другой: «где за эти две недели что-то поехало». Поэтому
показателей много, они разбиты по смыслу, у каждого — форма тренда,
сравнение с прошлым периодом такой же длины и заливка ячеек от меньшего
к большему: взгляд находит аномалию до того, как человек прочтёт числа.

Чего здесь нет и не будет, пока WB не отдаст: остатков, цены до СПП,
контент-рейтинга, выкупов и прибыли. Их показывают чужие сервисы,
которым продавец отдаёт отдельный доступ к кабинету; выдумывать их из
рекламной статистики — значит показать число, которого не существует.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .daily import raw_by_day, search_views_by_day
from .metrics import RAW_KEYS, daterange, derive, safe_div, shift_window

# Показатели по группам. «good» — в какую сторону хорошо: по нему
# красится только стрелка изменения, и всегда рядом со знаком, потому
# что одним цветом смысл не передают. None — там, где роста и падения
# нет «хорошего»: расход сам по себе ни плох, ни хорош.
GROUPS: list[dict[str, Any]] = [
    {
        "title": "Воронка",
        "rows": [
            {"key": "views", "title": "Показы", "unit": "шт", "good": "up"},
            {"key": "views_search", "title": "в поиске", "unit": "шт",
             "good": "up", "zone": True},
            {"key": "views_other", "title": "прочее: полки, каталог, карточка",
             "unit": "шт", "good": "up", "zone": True},
            {"key": "clicks", "title": "Клики", "unit": "шт", "good": "up"},
            {"key": "ctr", "title": "CTR", "unit": "%", "good": "up"},
            {"key": "atbs", "title": "Корзины", "unit": "шт", "good": "up"},
            {"key": "cr_cart", "title": "Конверсия в корзину", "unit": "%",
             "good": "up"},
            {"key": "orders", "title": "Заказы с рекламы, шт", "unit": "шт",
             "good": "up"},
            {"key": "cr_order", "title": "Конверсия в заказ", "unit": "%",
             "good": "up"},
        ],
    },
    {
        "title": "Деньги",
        "rows": [
            {"key": "revenue", "title": "Заказы с рекламы, ₽", "unit": "₽",
             "good": "up"},
            {"key": "spend", "title": "Расход на рекламу", "unit": "₽",
             "good": None},
            {"key": "drr", "title": "ДРР от рекламных заказов", "unit": "%",
             "good": "down"},
            {"key": "aov", "title": "Средний чек", "unit": "₽", "good": "up"},
            {"key": "total_orders", "title": "Заказы кабинета, шт",
             "unit": "шт", "good": "up", "orders": True},
            {"key": "total_revenue", "title": "Заказы кабинета, ₽",
             "unit": "₽", "good": "up", "orders": True},
            {"key": "total_drr", "title": "ДРР от всех заказов", "unit": "%",
             "good": "down", "orders": True},
        ],
    },
    {
        "title": "Цена рекламы",
        "rows": [
            {"key": "cpm", "title": "CPM · цена 1000 показов", "unit": "₽",
             "good": "down"},
            {"key": "cpc", "title": "CPC · цена клика", "unit": "₽",
             "good": "down"},
            {"key": "cpo", "title": "CPO · цена заказа", "unit": "₽",
             "good": "down"},
            {"key": "views_per_order", "title": "Показов на заказ",
             "unit": "шт", "good": "down"},
            {"key": "clicks_per_order", "title": "Кликов на заказ",
             "unit": "шт", "good": "down"},
        ],
    },
]

# Показатели, которые складываются по дням. Остальные — отношения, и
# итог периода у них считается из сумм, а не усреднением дней.
SUMMABLE = set(RAW_KEYS) | {"views_search", "views_other", "total_orders",
                            "total_revenue"}

# Ниже этого изменения стрелку не красим: 3% между двумя неделями —
# это обычное дыхание данных, а не сдвиг.
NOTABLE_PCT = 5.0


def table(conn: sqlite3.Connection, date_from: str, date_to: str,
          nm_id: int | None = None,
          price_field: str = "price_with_disc") -> dict[str, Any]:
    """Таблица «показатели × дни» с трендом и прошлым периодом."""
    days = daterange(date_from, date_to)
    prev_from, prev_to = shift_window(date_from, date_to)
    prev_days = daterange(prev_from, prev_to)

    values = _values_by_day(conn, date_from, date_to, nm_id, price_field)
    prev_values = _values_by_day(conn, prev_from, prev_to, nm_id, price_field)

    totals = _period_totals(values, days)
    prev_totals = _period_totals(prev_values, prev_days)
    has_zones = any(values[day].get("views_search") for day in days) or any(
        prev_values[day].get("views_search") for day in prev_days)
    has_orders = db.has_orders(conn)

    groups = []
    for group in GROUPS:
        rows = []
        for spec in group["rows"]:
            if spec.get("zone") and not has_zones:
                continue
            if spec.get("orders") and not has_orders:
                continue
            rows.append(_row(spec, values, days, prev_values, prev_days,
                             totals, prev_totals))
        if rows:
            groups.append({"title": group["title"], "rows": rows})

    return {
        "period": {"from": date_from, "to": date_to, "days": len(days)},
        "previous": {"from": prev_from, "to": prev_to},
        "nm_id": nm_id,
        "dates": days,
        "prev_dates": prev_days,
        "groups": groups,
        "has_data": any(totals[key] for key in RAW_KEYS),
        "has_zones": has_zones,
        "has_orders": has_orders,
    }


def _row(spec: dict[str, Any], values: dict[str, dict[str, float]],
         days: list[str], prev_values: dict[str, dict[str, float]],
         prev_days: list[str], totals: dict[str, float],
         prev_totals: dict[str, float]) -> dict[str, Any]:
    key = spec["key"]
    series = [round(float(values[day].get(key) or 0), 4) for day in days]
    prev_series = [round(float(prev_values[day].get(key) or 0), 4)
                   for day in prev_days]
    total = float(totals.get(key) or 0)
    prev_total = float(prev_totals.get(key) or 0)

    return {
        **{k: v for k, v in spec.items() if k != "orders"},
        "days": series,
        "prev_days": prev_series,
        "total": round(total, 4),
        "prev_total": round(prev_total, 4),
        "delta_pct": _delta_pct(total, prev_total),
        "is_ratio": key not in SUMMABLE,
        # Пустую строку прятать нельзя — ноль это тоже ответ, — но
        # заливать её тепловой картой незачем.
        "has_data": any(series),
    }


def _delta_pct(current: float, previous: float) -> float | None:
    """Изменение к прошлому периоду. None — если сравнивать не с чем."""
    if not previous:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def _values_by_day(conn: sqlite3.Connection, date_from: str, date_to: str,
                   nm_id: int | None, price_field: str
                   ) -> dict[str, dict[str, float]]:
    """Все показатели по каждому дню периода, включая пустые дни."""
    raw = raw_by_day(conn, date_from, date_to, nm_id)
    search = search_views_by_day(conn, date_from, date_to, nm_id)
    cabinet = _cabinet_orders(conn, date_from, date_to, nm_id, price_field)

    out: dict[str, dict[str, float]] = {}
    for day in daterange(date_from, date_to):
        totals = raw.get(day) or {key: 0.0 for key in RAW_KEYS}
        out[day] = _derive_day(totals, search.get(day, 0.0),
                               cabinet.get(day, {}))
    return out


def _derive_day(totals: dict[str, float], search_views: float,
                cabinet: dict[str, float]) -> dict[str, float]:
    values = derive(totals)

    views = float(totals.get("views") or 0)
    orders = float(totals.get("orders") or 0)
    spend = float(totals.get("spend") or 0)
    clicks = float(totals.get("clicks") or 0)

    # Поиск обрезаем общим числом показов, а остаток — нулём: два метода
    # WB считают по-своему, и показывать больше ста процентов хуже, чем
    # прижать расхождение к границе.
    found = max(0.0, min(float(search_views or 0), views))
    values["views_search"] = found
    values["views_other"] = max(0.0, views - found)

    values["views_per_order"] = safe_div(views, orders)
    values["clicks_per_order"] = safe_div(clicks, orders)

    total_orders = float(cabinet.get("orders") or 0)
    total_revenue = float(cabinet.get("revenue") or 0)
    values["total_orders"] = total_orders
    values["total_revenue"] = total_revenue
    values["total_drr"] = safe_div(spend, total_revenue) * 100
    return values


def _period_totals(values: dict[str, dict[str, float]],
                   days: list[str]) -> dict[str, float]:
    """Итог периода: количества складываем, отношения считаем из сумм.

    Среднее от дневных ДРР — это не ДРР периода: день с расходом 10 ₽
    весил бы в нём столько же, сколько день с расходом 10 000 ₽.
    """
    sums = {key: 0.0 for key in SUMMABLE}
    for day in days:
        for key in SUMMABLE:
            sums[key] += float(values[day].get(key) or 0)

    totals = _derive_day(sums, sums["views_search"],
                         {"orders": sums["total_orders"],
                          "revenue": sums["total_revenue"]})
    totals["views_search"] = sums["views_search"]
    totals["views_other"] = sums["views_other"]
    return totals


def _cabinet_orders(conn: sqlite3.Connection, date_from: str, date_to: str,
                    nm_id: int | None, price_field: str
                    ) -> dict[str, dict[str, float]]:
    """Заказы кабинета по дням — знаменатель общего ДРР."""
    out: dict[str, dict[str, float]] = {}
    for row in db.orders_by_nm(conn, date_from, date_to, price_field):
        if nm_id is not None and int(row["nm_id"] or 0) != nm_id:
            continue
        day = str(row["date"])[:10]
        slot = out.setdefault(day, {"orders": 0.0, "revenue": 0.0})
        slot["orders"] += float(row["orders"] or 0)
        slot["revenue"] += float(row["revenue"] or 0)
    return out
