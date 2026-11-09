"""Динамика по дням: все показатели разом, по группам, с тепловой картой.

Сводка по товарам отвечает на вопрос «какие цифры были в эти дни».
Здесь вопрос другой: «где за эти две недели что-то поехало». Поэтому
показателей много, они разбиты по смыслу, у каждого — форма тренда,
сравнение с прошлым периодом такой же длины и заливка ячеек от меньшего
к большему: взгляд находит аномалию до того, как человек прочтёт числа.

Строки появляются по данным: зоны показа — когда собрана статистика
поиска, заказы кабинета и цены — когда собраны заказы, выкупы — когда
собраны продажи, остатки — когда у токена есть категория «Аналитика».
Пустая строка с нулями сказала бы «этого не было», хотя правда в том,
что мы не спрашивали, — поэтому её просто нет.

Чего здесь нет и не будет, пока WB не отдаст отдельным методом:
контент-рейтинга карточки и прибыли. Прибыль упирается не в WB, а в
себестоимость: её знает только продавец, и без неё любое число будет
выдумкой.
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
        "title": "Выкупы",
        "rows": [
            {"key": "buyouts", "title": "Выкупы, шт", "unit": "шт",
             "good": "up", "sales": True},
            {"key": "buyout_revenue", "title": "Сумма выкупов, ₽",
             "unit": "₽", "good": "up", "sales": True},
            {"key": "for_pay", "title": "К перечислению, ₽", "unit": "₽",
             "good": "up", "sales": True},
            {"key": "cr_buyout", "title": "Конверсия в выкуп", "unit": "%",
             "good": "up", "sales": True},
            {"key": "drr_buyout", "title": "ДРР от выкупов", "unit": "%",
             "good": "down", "sales": True},
            {"key": "cps", "title": "CPS · цена выкупа", "unit": "₽",
             "good": "down", "sales": True},
            {"key": "returns", "title": "Возвраты, шт", "unit": "шт",
             "good": "down", "sales": True},
        ],
    },
    {
        "title": "Остатки",
        "rows": [
            # Ни у остатка, ни у запаса дней нет «хорошей» стороны:
            # пусто — нечего продавать, завал — деньги лежат на складе.
            {"key": "stock", "title": "Остатки на конец дня, шт",
             "unit": "шт", "good": None, "stocks": True},
            {"key": "in_way_to_client", "title": "В пути к клиенту, шт",
             "unit": "шт", "good": None, "stocks": True},
            {"key": "days_left", "title": "Осталось на N дней", "unit": "дн",
             "good": None, "stocks": True},
        ],
    },
    {
        "title": "Цена и СПП",
        "rows": [
            {"key": "price_before_spp", "title": "Цена до СПП", "unit": "₽",
             "good": None, "orders": True},
            {"key": "price_with_spp", "title": "Цена с СПП", "unit": "₽",
             "good": None, "orders": True},
            {"key": "spp_pct", "title": "СПП, %", "unit": "%",
             "good": None, "orders": True},
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
                            "total_revenue", "buyouts", "buyout_revenue",
                            "for_pay", "returns"}

# Складываются тоже, но строками не показываются: из них считаются
# средние цены за период. Складывать сами средние нельзя — день с одним
# заказом весил бы столько же, сколько день с сотней.
INGREDIENTS = ("price_orders", "before_spp", "with_spp", "spp_sum")

# Сколько дней смотрим назад, чтобы понять скорость продаж для запаса
# дней. Неделя сглаживает разницу между буднями и выходными.
SALES_WINDOW = 7

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
    has_sales = db.has_sales(conn)
    has_stocks = db.has_stocks(conn)

    groups = []
    for group in GROUPS:
        rows = []
        for spec in group["rows"]:
            if spec.get("zone") and not has_zones:
                continue
            if spec.get("orders") and not has_orders:
                continue
            if spec.get("sales") and not has_sales:
                continue
            if spec.get("stocks") and not has_stocks:
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
        "has_sales": has_sales,
        "has_stocks": has_stocks,
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
    sales = _by_nm_day(db.sales_by_day(conn, date_from, date_to), nm_id,
                       ("sales", "revenue", "for_pay", "returns"))
    stocks = _by_nm_day(db.stocks_by_day(conn, date_from, date_to), nm_id,
                        ("quantity", "in_way_to_client", "in_way_from_client"))

    days = daterange(date_from, date_to)
    out: dict[str, dict[str, float]] = {}
    for day in days:
        totals = raw.get(day) or {key: 0.0 for key in RAW_KEYS}
        out[day] = _derive_day(totals, search.get(day, 0.0),
                               cabinet.get(day, {}), sales.get(day, {}),
                               stocks.get(day, {}))

    _add_days_left(conn, out, days, nm_id, price_field)
    return out


def _by_nm_day(rows: Any, nm_id: int | None,
               fields: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """Строки «день + артикул» → суммы по дням для выбранного товара."""
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        if nm_id is not None and int(row["nm_id"] or 0) != nm_id:
            continue
        slot = out.setdefault(str(row["date"])[:10],
                              {field: 0.0 for field in fields})
        for field in fields:
            slot[field] += float(row[field] or 0)
    return out


def _add_days_left(conn: sqlite3.Connection,
                   values: dict[str, dict[str, float]], days: list[str],
                   nm_id: int | None, price_field: str) -> None:
    """Запас дней: остаток, делённый на продажи в день за неделю.

    Неделя, а не вчерашний день: по одному дню запас прыгает от двух
    дней до сорока и ничего не говорит. Если выкупы ещё не собраны,
    считаем по заказам кабинета — это ближе к правде, чем промолчать.
    """
    if not any(values[day].get("stock") for day in days):
        return

    # Неделя до начала периода тоже нужна: иначе у первых дней скорость
    # продаж считалась бы по двум-трём дням и запас выходил бы вдвое.
    start = daterange(days[0], days[-1])[0]
    back = _shift_back(start, SALES_WINDOW)
    sold = _by_nm_day(db.sales_by_day(conn, back, days[-1]), nm_id,
                      ("sales",))
    ordered = _by_nm_day(db.orders_by_nm(conn, back, days[-1], price_field),
                         nm_id, ("orders",))
    history = daterange(back, days[-1])

    for day in days:
        window = [d for d in history if d <= day][-SALES_WINDOW:]
        per_day = sum(float((sold.get(d) or {}).get("sales") or 0)
                      for d in window)
        if not per_day:
            per_day = sum(float((ordered.get(d) or {}).get("orders") or 0)
                          for d in window)
        rate = safe_div(per_day, len(window) or 1)
        values[day]["days_left"] = safe_div(values[day].get("stock") or 0, rate)


def _shift_back(day: str, days: int) -> str:
    from datetime import timedelta

    from .metrics import parse_date

    return (parse_date(day) - timedelta(days=days)).isoformat()


def _derive_day(totals: dict[str, float], search_views: float,
                cabinet: dict[str, float], sales: dict[str, float] | None = None,
                stocks: dict[str, float] | None = None) -> dict[str, float]:
    sales = sales or {}
    stocks = stocks or {}
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

    # Цены — средние по заказам дня: до скидки постоянного покупателя и
    # после неё. Отдельного метода «какая сегодня СПП» у WB нет, но
    # разница этих двух цен и есть СПП, причём та, что видел покупатель.
    count = float(cabinet.get("price_orders") or 0)
    before = float(cabinet.get("before_spp") or 0)
    after = float(cabinet.get("with_spp") or 0)
    values["price_before_spp"] = safe_div(before, count)
    values["price_with_spp"] = safe_div(after, count)
    values["spp_pct"] = (1 - safe_div(after, before)) * 100 if before else 0.0
    values["price_orders"] = count
    values["before_spp"] = before
    values["with_spp"] = after

    # СПП WB отдаёт процентом в каждом заказе — берём её, а не разницу
    # цен: в разницу попадают и другие скидки.
    spp_sum = float(cabinet.get("spp_sum") or 0)
    values["spp_sum"] = spp_sum
    if spp_sum:
        values["spp_pct"] = safe_div(spp_sum, count)

    # Выкупы. Заказ и выкуп — разные события, и конверсия между ними
    # считается по дню выкупа: иначе пришлось бы ждать, пока дойдут
    # все заказы недельной давности.
    buyouts = float(sales.get("sales") or 0)
    buyout_revenue = float(sales.get("revenue") or 0)
    values["buyouts"] = buyouts
    values["buyout_revenue"] = buyout_revenue
    values["for_pay"] = float(sales.get("for_pay") or 0)
    values["returns"] = float(sales.get("returns") or 0)
    values["cr_buyout"] = safe_div(buyouts, total_orders) * 100
    values["drr_buyout"] = safe_div(spend, buyout_revenue) * 100
    values["cps"] = safe_div(spend, buyouts)

    values["stock"] = float(stocks.get("quantity") or 0)
    values["in_way_to_client"] = float(stocks.get("in_way_to_client") or 0)
    values["days_left"] = 0.0
    return values


def _period_totals(values: dict[str, dict[str, float]],
                   days: list[str]) -> dict[str, float]:
    """Итог периода: количества складываем, отношения считаем из сумм.

    Среднее от дневных ДРР — это не ДРР периода: день с расходом 10 ₽
    весил бы в нём столько же, сколько день с расходом 10 000 ₽.
    """
    keys = set(SUMMABLE) | set(INGREDIENTS)
    sums = {key: 0.0 for key in keys}
    for day in days:
        for key in keys:
            sums[key] += float(values[day].get(key) or 0)

    totals = _derive_day(
        sums, sums["views_search"],
        {"orders": sums["total_orders"], "revenue": sums["total_revenue"],
         "price_orders": sums["price_orders"],
         "before_spp": sums["before_spp"], "with_spp": sums["with_spp"],
         "spp_sum": sums["spp_sum"]},
        {"sales": sums["buyouts"], "revenue": sums["buyout_revenue"],
         "for_pay": sums["for_pay"], "returns": sums["returns"]})
    totals["views_search"] = sums["views_search"]
    totals["views_other"] = sums["views_other"]

    # Остаток и запас дней не складываются: это состояние на день, а не
    # событие. За период показываем последний известный день.
    last = next((values[day] for day in reversed(days)
                 if values[day].get("stock")), None)
    if last:
        totals["stock"] = last["stock"]
        totals["in_way_to_client"] = last["in_way_to_client"]
        totals["days_left"] = last.get("days_left") or 0.0
    return totals


def _cabinet_orders(conn: sqlite3.Connection, date_from: str, date_to: str,
                    nm_id: int | None, price_field: str
                    ) -> dict[str, dict[str, float]]:
    """Заказы кабинета по дням: знаменатель общего ДРР и цены."""
    out: dict[str, dict[str, float]] = {}

    def slot(day: str) -> dict[str, float]:
        return out.setdefault(day, {"orders": 0.0, "revenue": 0.0,
                                    "price_orders": 0.0, "before_spp": 0.0,
                                    "with_spp": 0.0, "spp_sum": 0.0})

    for row in db.orders_by_nm(conn, date_from, date_to, price_field):
        if nm_id is not None and int(row["nm_id"] or 0) != nm_id:
            continue
        here = slot(str(row["date"])[:10])
        here["orders"] += float(row["orders"] or 0)
        here["revenue"] += float(row["revenue"] or 0)

    for row in db.prices_by_day(conn, date_from, date_to):
        if nm_id is not None and int(row["nm_id"] or 0) != nm_id:
            continue
        here = slot(str(row["date"])[:10])
        here["price_orders"] += float(row["orders"] or 0)
        here["before_spp"] += float(row["before_spp"] or 0)
        here["with_spp"] += float(row["with_spp"] or 0)
        here["spp_sum"] += float(row["spp_sum"] or 0)
    return out
