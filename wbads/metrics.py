"""Расчёт показателей рекламы и их динамики.

Основные показатели (в терминах кабинета WB):
    показы (views) → клики (clicks) → в корзину (atbs) → заказы (orders)
    расход (spend, руб.) и выручка с рекламы (revenue = сумма заказов, руб.)

Производные:
    CTR      = клики / показы × 100
    CPC      = расход / клики                  — цена клика
    CPM      = расход / показы × 1000          — цена тысячи показов
    CR в корзину = в корзину / клики × 100     — качество карточки
    CR в заказ   = заказы / в корзину × 100    — цена, отзывы, сроки доставки
    CPO      = расход / заказы                 — цена заказа
    ДРР      = расход / выручка × 100          — главный показатель здоровья
    ROAS     = выручка / расход                — сколько рублей на рубль рекламы
    Средний чек = выручка / заказы
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

RAW_KEYS = ("views", "clicks", "atbs", "orders", "shks", "spend", "revenue")


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """Деление, которое не падает на нуле: нет базы — нет показателя."""
    try:
        return a / b if b else default
    except (TypeError, ZeroDivisionError):
        return default


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def daterange(date_from: str, date_to: str) -> list[str]:
    start, end = parse_date(date_from), parse_date(date_to)
    days = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]


def shift_window(date_from: str, date_to: str) -> tuple[str, str]:
    """Предыдущий период такой же длины — для сравнения «стало / было»."""
    start, end = parse_date(date_from), parse_date(date_to)
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return prev_start.isoformat(), prev_end.isoformat()


def empty_totals() -> dict[str, float]:
    return {k: 0.0 for k in RAW_KEYS}


def sum_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Складывает сырые метрики по набору дней."""
    totals = empty_totals()
    for row in rows:
        for key in RAW_KEYS:
            totals[key] += float(row[key] or 0)
    return totals


def derive(totals: Mapping[str, float]) -> dict[str, float]:
    """Считает производные показатели поверх сырых сумм."""
    views = float(totals.get("views") or 0)
    clicks = float(totals.get("clicks") or 0)
    atbs = float(totals.get("atbs") or 0)
    orders = float(totals.get("orders") or 0)
    spend = float(totals.get("spend") or 0)
    revenue = float(totals.get("revenue") or 0)

    out = {k: float(totals.get(k) or 0) for k in RAW_KEYS}
    out.update(
        ctr=safe_div(clicks, views) * 100,
        cpc=safe_div(spend, clicks),
        cpm=safe_div(spend, views) * 1000,
        cr_cart=safe_div(atbs, clicks) * 100,
        cr_order=safe_div(orders, atbs) * 100,
        cr_click_order=safe_div(orders, clicks) * 100,
        cpo=safe_div(spend, orders),
        drr=safe_div(spend, revenue) * 100,
        roas=safe_div(revenue, spend),
        aov=safe_div(revenue, orders),
    )
    return out


def pct_change(current: float, previous: float) -> float | None:
    """Изменение в процентах. None — если сравнивать не с чем (база 0)."""
    if previous in (0, None):
        return None
    return (current - previous) / abs(previous) * 100


def compare(current: Mapping[str, float],
            previous: Mapping[str, float]) -> dict[str, dict[str, float | None]]:
    """По каждому показателю: сейчас, было, изменение в % и в абсолюте."""
    cur, prev = derive(current), derive(previous)
    result: dict[str, dict[str, float | None]] = {}
    for key in cur:
        result[key] = {
            "current": cur[key],
            "previous": prev[key],
            "delta": cur[key] - prev[key],
            "delta_pct": pct_change(cur[key], prev[key]),
        }
    return result


def build_series(rows: Sequence[Mapping[str, Any]], date_from: str,
                 date_to: str) -> list[dict[str, Any]]:
    """Ряд по дням со сквозным календарём: дни без открутки — нули, а не пропуски.

    Это принципиально: «показов не было три дня» видно, только если день
    присутствует в ряду с нулём.
    """
    by_date: dict[str, dict[str, float]] = {}
    for row in rows:
        day = str(row["date"])[:10]
        acc = by_date.setdefault(day, empty_totals())
        for key in RAW_KEYS:
            acc[key] += float(row[key] or 0)

    series = []
    for day in daterange(date_from, date_to):
        totals = by_date.get(day, empty_totals())
        point = derive(totals)
        point["date"] = day
        series.append(point)
    return series


def moving_average(values: Sequence[float], window: int = 3) -> list[float]:
    """Скользящее среднее — сглаживает суточные скачки в графиках."""
    out: list[float] = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1): i + 1]
        out.append(sum(chunk) / len(chunk) if chunk else 0.0)
    return out


def trend_slope(values: Sequence[float]) -> float:
    """Наклон линейного тренда (метод наименьших квадратов).

    Положительный — показатель растёт по ходу периода, отрицательный — падает.
    Нужен, чтобы отличить «плохо и ухудшается» от «плохо, но выправляется».
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return safe_div(num, den)


def trailing_zero_days(series: Sequence[Mapping[str, Any]], key: str = "views") -> int:
    """Сколько последних дней подряд показатель равен нулю."""
    count = 0
    for point in reversed(series):
        if float(point.get(key) or 0) > 0:
            break
        count += 1
    return count


def days_with_activity(series: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for p in series if float(p.get("spend") or 0) > 0)
