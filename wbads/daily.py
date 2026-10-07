"""Сводка по дням в виде таблицы «показатели × дни».

Форма взята из таблицы, которую менеджер ведёт руками: строки — показатели,
колонки — дни. Привычный для аналитики вид — наоборот, строка на день, и
соблазн «сделать правильно» здесь надо задавить: человек читает свои цифры
определённым движением глаза, и менять его ради чужой нормы — значит
заставлять переучиваться там, где учиться нечему.

Считается либо по всему кабинету, либо по одному артикулу. Артикул —
естественная единица работы: правки делают в кампании, а смотрят на товар,
и один товар часто крутится сразу в нескольких кампаниях.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from .metrics import RAW_KEYS, daterange, derive, safe_div

# Строки таблицы — в том порядке и под теми названиями, в каких их читает
# человек. «Заказы в шт» и «Заказы в руб» — это одно и то же событие,
# измеренное двумя способами, и стоять они должны рядом.
ROWS: list[dict[str, Any]] = [
    {"key": "views", "title": "Показы", "unit": "шт"},
    {"key": "clicks", "title": "Клики", "unit": "шт"},
    {"key": "orders", "title": "Заказы в шт", "unit": "шт"},
    {"key": "revenue", "title": "Заказы в руб", "unit": "₽"},
    {"key": "spend", "title": "Затраты на рекламу", "unit": "₽"},
    {"key": "drr", "title": "ДРР", "unit": "%"},
    {"key": "cpc", "title": "CPC", "unit": "₽"},
    {"key": "cpo", "title": "CPO", "unit": "₽"},
    {"key": "ctr", "title": "CTR", "unit": "%"},
    {"key": "cr_cart", "title": "Конверсия в корзину", "unit": "%"},
    {"key": "cr_order", "title": "Конверсия в заказ", "unit": "%"},
]

# Показатели, которые складываются по дням. Остальные — отношения, и
# складывать их нельзя ни при каких обстоятельствах.
SUMMABLE = {"views", "clicks", "atbs", "orders", "shks", "spend", "revenue"}


def daily_table(conn: sqlite3.Connection, date_from: str, date_to: str,
                nm_id: int | None = None) -> dict[str, Any]:
    """Таблица за период: строки — показатели, колонки — дни.

    Итог за период считается из сумм, а не усреднением дневных значений.
    Среднее от дневных ДРР — это не ДРР периода: день с расходом 10 ₽ весит
    в нём столько же, сколько день с расходом 10 000 ₽.
    """
    totals_by_day = _by_day(conn, date_from, date_to, nm_id)
    days = daterange(date_from, date_to)

    period_totals = {key: 0.0 for key in RAW_KEYS}
    columns: list[dict[str, Any]] = []
    for day in days:
        raw = totals_by_day.get(day, {key: 0.0 for key in RAW_KEYS})
        for key in RAW_KEYS:
            period_totals[key] += raw[key]
        columns.append({"date": day, "values": derive(raw)})

    period = derive(period_totals)
    rows = []
    for spec in ROWS:
        key = spec["key"]
        rows.append({
            **spec,
            "days": [round(float(col["values"].get(key) or 0), 4)
                     for col in columns],
            "total": round(float(period.get(key) or 0), 4),
            # Отношения в колонке «за период» не суммируются, и это надо
            # показать: иначе колонка читается как сумма строки.
            "is_ratio": key not in SUMMABLE,
        })

    return {
        "period": {"from": date_from, "to": date_to, "days": len(days)},
        "nm_id": nm_id,
        "dates": days,
        "rows": rows,
        "has_data": any(period_totals[key] for key in RAW_KEYS),
    }


def _by_day(conn: sqlite3.Connection, date_from: str, date_to: str,
            nm_id: int | None) -> dict[str, dict[str, float]]:
    """Сырые суммы по дням: по кабинету или по одному артикулу."""
    if nm_id is None:
        sql = ("SELECT date, " + ", ".join(f"SUM({k}) AS {k}" for k in RAW_KEYS)
               + " FROM campaign_daily WHERE date BETWEEN ? AND ? GROUP BY date")
        params: list[Any] = [date_from, date_to]
    else:
        sql = ("SELECT date, " + ", ".join(f"SUM({k}) AS {k}" for k in RAW_KEYS)
               + " FROM campaign_nm_daily WHERE nm_id = ?"
                 " AND date BETWEEN ? AND ? GROUP BY date")
        params = [nm_id, date_from, date_to]

    return {str(row["date"])[:10]: {key: float(row[key] or 0) for key in RAW_KEYS}
            for row in conn.execute(sql, params)}


def articles(conn: sqlite3.Connection, date_from: str, date_to: str,
             names: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """Артикулы, которые крутились за период, — от крупных к мелким.

    По алфавиту или по номеру искать нечего: человек помнит товар по
    обороту, а не по месту в списке.
    """
    rows = conn.execute(
        "SELECT nm_id, SUM(spend) AS spend, SUM(revenue) AS revenue,"
        " MAX(name) AS name FROM campaign_nm_daily"
        " WHERE date BETWEEN ? AND ? GROUP BY nm_id ORDER BY SUM(spend) DESC",
        (date_from, date_to),
    ).fetchall()

    out = []
    for row in rows:
        nm_id = int(row["nm_id"])
        title = (names or {}).get(nm_id) or str(row["name"] or "")
        out.append({
            "nm_id": nm_id,
            "name": title,
            "spend": round(float(row["spend"] or 0), 2),
            "revenue": round(float(row["revenue"] or 0), 2),
            "drr": round(safe_div(float(row["spend"] or 0),
                                  float(row["revenue"] or 0)) * 100, 2),
        })
    return out


def to_csv(table: dict[str, Any], title: str = "") -> str:
    """Та же таблица файлом — её открывают в Excel и доделывают руками."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")

    def dec(value: float, digits: int = 2) -> str:
        return f"{value:.{digits}f}".replace(".", ",")

    if title:
        writer.writerow([title])
    writer.writerow(["Дата"] + [_day_label(d) for d in table["dates"]]
                    + ["За период"])
    for row in table["rows"]:
        digits = 0 if row["unit"] == "шт" else 2
        writer.writerow([row["title"]]
                        + [dec(v, digits) for v in row["days"]]
                        + [dec(row["total"], digits)])
    return buffer.getvalue()


def _day_label(iso: str) -> str:
    """Дата в виде, который Excel не превратит в число неизвестного года."""
    parts = iso.split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else iso
