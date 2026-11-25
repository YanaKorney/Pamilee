"""Сверка заказов за один день: что отдал WB и что лежит у нас.

«В дашборде одно, в кабинете другое» — вопрос, на который нельзя
отвечать догадками. Поэтому программа спрашивает у WB заказы ровно за
один день (отбор по дате заказа, а не по дате изменения), считает их
всеми способами сразу и кладёт рядом то, что хранится в базе.

Дальше человек открывает в кабинете «Этапы воронки продаж» за тот же
день и видит, какая строка совпала. Если не совпала ни одна — значит
дело не в способе счёта, а в полноте сбора, и это тоже видно: строк от
WB будет больше, чем у нас в базе.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from . import db

# Цены, которыми WB описывает один и тот же заказ.
PRICE_FIELDS = (
    ("price_with_disc", "priceWithDisc", "без СПП — после скидки продавца"),
    ("finished_price", "finishedPrice", "с СПП — что заплатил покупатель"),
    ("total_price", "totalPrice", "до скидок"),
)


def from_wb(rows: Sequence[dict[str, Any]], day: str) -> dict[str, Any]:
    """Считает присланные WB строки всеми способами сразу.

    Строки, у которых дата не совпала с запрошенным днём, отбрасываются:
    WB изредка досылает соседние дни, и в сверке они всё испортят.
    """
    mine = [row for row in rows if str(row.get("date") or "")[:10] == day]

    keys: dict[str, int] = {}
    for row in mine:
        key = str(row.get("srid") or "").strip()
        keys[key] = keys.get(key, 0) + 1
    duplicates = sum(count - 1 for count in keys.values() if count > 1)

    cancelled = [row for row in mine if row.get("isCancel")]
    variants = []
    for field, wb_name, title in PRICE_FIELDS:
        for include in (False, True):
            picked = mine if include else [r for r in mine if not r.get("isCancel")]
            variants.append({
                "field": field,
                "title": title,
                "cancelled": include,
                "orders": len(picked),
                "revenue": round(sum(_num(r.get(wb_name)) for r in picked), 2),
            })

    return {
        "day": day,
        "rows": len(mine),
        "unique": len(keys),
        "duplicates": duplicates,
        "cancelled": len(cancelled),
        "other_days": len(rows) - len(mine),
        "variants": variants,
    }


# Поля воронки продаж — так их называет WB в своём ответе.
FUNNEL_FIELDS = (
    ("orders", "orderCount"),
    ("orders_sum", "orderSum"),
    ("buyouts", "buyoutCount"),
    ("buyouts_sum", "buyoutSum"),
    ("cancels", "cancelCount"),
    ("cancels_sum", "cancelSum"),
    ("opens", "openCount"),
    ("carts", "cartCount"),
)


def funnel_totals(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Складывает карточки воронки в итог за день.

    Воронка считает товары, а не строки заказа, поэтому её итог и есть
    то число, которое человек видит в кабинете. Складываем сами: WB
    отдаёт её постранично и общей строки в ответе нет.
    """
    total: dict[str, Any] = {name: 0.0 for name, _ in FUNNEL_FIELDS}
    total["cards"] = len(items)
    for item in items:
        got = ((item.get("statistic") or {}).get("selected")) or {}
        for name, wb_name in FUNNEL_FIELDS:
            total[name] += _num(got.get(wb_name))
    for name, _ in FUNNEL_FIELDS:
        total[name] = round(total[name], 2)
    return total


def from_base(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    """То же самое, но по нашей базе."""
    row = conn.execute(
        "SELECT COUNT(*) AS rows,"
        " SUM(CASE WHEN is_cancel = 1 THEN 1 ELSE 0 END) AS cancelled"
        " FROM orders_raw WHERE date = ?", (day,)).fetchone()

    variants = []
    for field, _, title in PRICE_FIELDS:
        for include in (False, True):
            keep = "" if include else " AND is_cancel = 0"
            got = conn.execute(
                f"SELECT COUNT(*) AS orders, SUM({field}) AS revenue"
                f" FROM orders_raw WHERE date = ?{keep}", (day,)).fetchone()
            variants.append({
                "field": field,
                "title": title,
                "cancelled": include,
                "orders": float(got["orders"] or 0),
                "revenue": round(float(got["revenue"] or 0), 2),
            })

    return {
        "day": day,
        "rows": int(row["rows"] or 0),
        "cancelled": int(row["cancelled"] or 0),
        "variants": variants,
        "basis": db.order_basis(conn),
    }


def verdict(wb: dict[str, Any], base: dict[str, Any]) -> list[str]:
    """Что из этого следует — словами, а не кодами.

    Три разных беды выглядят в таблице одинаково, и различить их должна
    программа: способ счёта, потерянные при сборе строки и повторы srid,
    из-за которых заказ в базе один, а у WB их два.
    """
    said: list[str] = []

    # Сравнивать надо с тем, что база вообще способна хранить: строки с
    # одинаковым srid она по устройству сливает в одну, и считать это
    # потерей при сборе — значит отправить человека пересобирать зря.
    if wb["unique"] > base["rows"]:
        said.append(
            f"WB отдал за этот день {wb['unique']} заказов с разными "
            f"номерами, а в базе их {base['rows']}. Значит, дело не в "
            "способе счёта: часть заказов не доехала при сборе. Запустите "
            "сбор заново (SBOR-Windows.bat) и повторите сверку.")
    elif base["rows"] > wb["unique"]:
        said.append(
            f"В базе {base['rows']} заказов, а WB сейчас отдал "
            f"{wb['unique']}. Так бывает, когда заказы отменили и WB "
            "перестал их присылать: в базе они остаются с пометкой отмены.")

    if wb["duplicates"]:
        said.append(
            f"Строк с повторяющимся номером заказа (srid): "
            f"{wb['duplicates']}. Это один номер на несколько товаров. "
            "База хранит такие строки как одну, поэтому заказов у нас "
            f"ровно на {wb['duplicates']} меньше, чем товаров в кабинете. "
            "Если расхождение с воронкой примерно такое — причина эта.")

    if wb["cancelled"]:
        said.append(
            f"Отменённых заказов за день: {wb['cancelled']}. Если в кабинете "
            "их число входит в «Заказали товаров», поставьте галочку "
            "«учитывать отменённые» в сводке по дням.")

    if not said:
        said.append("Строки сошлись: в базе ровно то, что отдаёт WB. "
                    "Значит, расхождение с кабинетом — в способе счёта, "
                    "и нужный вариант есть в таблице выше.")
    return said


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
