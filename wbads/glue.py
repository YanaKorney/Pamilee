"""ДРР склейки: несколько артикулов, посчитанных как один товар.

В кабинете склейка — это одна карточка, на которой сидят несколько
артикулов: цвета, размеры, объёмы. WB считает их порознь, и по
отдельности каждый выглядит странно: реклама крутится на один цвет, а
заказы приходят на соседний, потому что покупатель пришёл по рекламе и
переключил цвет внутри карточки. ДРР такого артикула — число ни о чём:
у одного расход без заказов, у другого заказы без расхода.

Правда здесь считается только целиком: весь расход склейки против всего
её оборота. Поэтому человек отмечает галочками, что входит в склейку, а
программа складывает — и показывает заодно вклад каждого артикула,
чтобы было видно, кто тянет расход, а кто собирает заказы.

Оборот берётся общим правилом из cabinet: день, который есть в воронке
продаж, считает воронка. Иначе ДРР склейки не сошёлся бы с ДРР в других
вкладках, а два разных числа про одно и то же — повод не верить обоим.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Sequence

from . import cabinet, db
from .metrics import RAW_KEYS, daterange, derive, safe_div, shift_window


def table(conn: sqlite3.Connection, date_from: str, date_to: str,
          nm_ids: Iterable[int],
          price_field: str = "price_with_disc",
          include_cancelled: bool = False) -> dict[str, Any]:
    """Итог склейки за период, вклад каждого товара и ряд по дням."""
    ids = sorted({int(nm) for nm in nm_ids if int(nm or 0) > 0})
    prev_from, prev_to = shift_window(date_from, date_to)

    current = _period(conn, date_from, date_to, ids, price_field,
                      include_cancelled)
    previous = _period(conn, prev_from, prev_to, ids, price_field,
                       include_cancelled)

    names = _names(conn, ids)
    return {
        "period": {"from": date_from, "to": date_to,
                   "days": len(daterange(date_from, date_to))},
        "previous": {"from": prev_from, "to": prev_to},
        "nm_ids": ids,
        "names": names,
        "totals": current["totals"],
        "prev_totals": previous["totals"],
        "items": _items(current, previous, ids, names),
        "series": _series(conn, current, date_from, date_to),
        "source": current["source"],
        "funnel_days": current["funnel_days"],
        "has_data": bool(current["totals"]["spend"]
                         or current["totals"]["total_revenue"]),
        # Товары, по которым за период нет ни открутки, ни заказов:
        # молча проглотить их нельзя — человек отметил их не случайно.
        "silent": [nm for nm in ids
                   if not current["raw_by_nm"].get(nm)
                   and not current["cabinet_by_nm"].get(nm)],
        "orders_basis": {"field": price_field,
                         "cancelled": bool(include_cancelled)},
    }


def _period(conn: sqlite3.Connection, date_from: str, date_to: str,
            ids: Sequence[int], price_field: str,
            include_cancelled: bool) -> dict[str, Any]:
    """Всё, что нужно про склейку за один период."""
    raw_by_nm = _raw_by_nm(conn, date_from, date_to, ids)
    got = cabinet.order_cells(conn, date_from, date_to, price_field,
                              include_cancelled, nm_ids=ids)
    cabinet_by_nm = cabinet.by_nm(got["cells"])

    raw_total = {key: 0.0 for key in RAW_KEYS}
    for values in raw_by_nm.values():
        for key in RAW_KEYS:
            raw_total[key] += values[key]
    account = cabinet.totals(got["cells"])

    return {
        "raw_by_nm": raw_by_nm,
        "cabinet_by_nm": cabinet_by_nm,
        "cells": got["cells"],
        "source": got["source"],
        "funnel_days": got["funnel_days"],
        "totals": _totals(raw_total, account),
    }


def _totals(raw: dict[str, float], account: dict[str, float]
            ) -> dict[str, float]:
    """Показатели склейки: рекламные и общие в одной таблице.

    Отношения считаются из сумм, а не усреднением по товарам: артикул с
    расходом в сто рублей не должен весить столько же, сколько артикул
    с расходом в сто тысяч.
    """
    values = derive(raw)
    spend = float(raw.get("spend") or 0)
    values["total_orders"] = account.get("orders", 0.0)
    values["total_revenue"] = account.get("revenue", 0.0)
    values["opens"] = account.get("opens", 0.0)
    values["carts"] = account.get("carts", 0.0)
    values["total_drr"] = safe_div(spend, values["total_revenue"]) * 100
    # Какую долю оборота склейки принесла реклама. Остальное —
    # органика, и именно это отличает склейку от отдельного артикула:
    # реклама на один цвет вытягивает заказы всей карточки.
    values["ad_share"] = safe_div(values["revenue"],
                                  values["total_revenue"]) * 100
    values["cr_cart_cabinet"] = safe_div(values["carts"],
                                         values["opens"]) * 100
    return values


def _items(current: dict[str, Any], previous: dict[str, Any],
           ids: Sequence[int], names: dict[int, str]) -> list[dict[str, Any]]:
    """Вклад каждого артикула — от крупного расхода к мелкому."""
    total_spend = sum(v["spend"] for v in current["raw_by_nm"].values())
    total_revenue = sum(v["revenue"] for v in
                        current["cabinet_by_nm"].values())

    out = []
    for nm_id in ids:
        raw = current["raw_by_nm"].get(nm_id) or {k: 0.0 for k in RAW_KEYS}
        account = (current["cabinet_by_nm"].get(nm_id)
                   or {key: 0.0 for key in cabinet.CELL_KEYS})
        was = previous["cabinet_by_nm"].get(nm_id) or {}
        values = _totals(raw, account)
        out.append({
            "nm_id": nm_id,
            "name": names.get(nm_id, ""),
            **{key: round(values[key], 4) for key in (
                "views", "clicks", "atbs", "orders", "spend", "revenue",
                "ctr", "cr_order", "cpo", "drr", "aov",
                "total_orders", "total_revenue", "total_drr", "ad_share")},
            "spend_share": round(safe_div(raw["spend"], total_spend) * 100, 2),
            "revenue_share": round(
                safe_div(account["revenue"], total_revenue) * 100, 2),
            "prev_total_revenue": round(float(was.get("revenue") or 0), 2),
        })
    out.sort(key=lambda row: (-row["spend"], -row["total_revenue"]))
    return out


def _series(conn: sqlite3.Connection, current: dict[str, Any],
            date_from: str, date_to: str) -> list[dict[str, Any]]:
    """Ряд по дням со сквозным календарём — для графика."""
    raw = _raw_by_day(conn, date_from, date_to, current["raw_by_nm"].keys())
    account = cabinet.by_day(current["cells"])

    out = []
    for day in daterange(date_from, date_to):
        totals = raw.get(day) or {key: 0.0 for key in RAW_KEYS}
        got = account.get(day) or {key: 0.0 for key in cabinet.CELL_KEYS}
        values = _totals(totals, got)
        out.append({
            "date": day,
            "spend": round(values["spend"], 2),
            "revenue": round(values["revenue"], 2),
            "total_revenue": round(values["total_revenue"], 2),
            "orders": round(values["orders"], 2),
            "total_orders": round(values["total_orders"], 2),
            "drr": round(values["drr"], 2),
            "total_drr": round(values["total_drr"], 2),
        })
    return out


def _raw_by_nm(conn: sqlite3.Connection, date_from: str, date_to: str,
               ids: Sequence[int]) -> dict[int, dict[str, float]]:
    """Рекламные суммы по каждому артикулу склейки."""
    if not ids:
        return {}
    sums = ", ".join(f"SUM({key}) AS {key}" for key in RAW_KEYS)
    holes = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT nm_id, {sums} FROM campaign_nm_daily"
        f" WHERE date BETWEEN ? AND ? AND nm_id IN ({holes})"
        " GROUP BY nm_id", [date_from, date_to, *ids])
    return {int(row["nm_id"]): {key: float(row[key] or 0) for key in RAW_KEYS}
            for row in rows}


def _raw_by_day(conn: sqlite3.Connection, date_from: str, date_to: str,
                ids: Iterable[int]) -> dict[str, dict[str, float]]:
    """Рекламные суммы склейки по дням."""
    ids = list(ids)
    if not ids:
        return {}
    sums = ", ".join(f"SUM({key}) AS {key}" for key in RAW_KEYS)
    holes = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT date, {sums} FROM campaign_nm_daily"
        f" WHERE date BETWEEN ? AND ? AND nm_id IN ({holes})"
        " GROUP BY date", [date_from, date_to, *ids])
    return {str(row["date"])[:10]:
            {key: float(row[key] or 0) for key in RAW_KEYS} for row in rows}


def _names(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, str]:
    """Названия товаров: из рекламы, а если там пусто — из заказов."""
    if not ids:
        return {}
    holes = ",".join("?" * len(ids))
    out: dict[int, str] = {}
    for row in conn.execute(
            f"SELECT nm_id, MAX(name) AS name FROM campaign_nm_daily"
            f" WHERE nm_id IN ({holes}) GROUP BY nm_id", list(ids)):
        name = str(row["name"] or "").strip()
        if name:
            out[int(row["nm_id"])] = name
    missing = [nm for nm in ids if nm not in out]
    if missing:
        holes = ",".join("?" * len(missing))
        for row in conn.execute(
                "SELECT nm_id, MAX(supplier_article) AS article,"
                " MAX(subject) AS subject FROM orders_raw"
                f" WHERE nm_id IN ({holes}) GROUP BY nm_id", missing):
            name = (str(row["article"] or "").strip()
                    or str(row["subject"] or "").strip())
            if name:
                out[int(row["nm_id"])] = name
    return out


def catalog(conn: sqlite3.Connection, date_from: str, date_to: str
            ) -> list[dict[str, Any]]:
    """Все товары периода — список, в котором человек ставит галочки.

    Берём и рекламировавшиеся, и просто продававшиеся: в склейку входит
    вся карточка, а рекламируется часто только один её артикул.
    """
    rows: dict[int, dict[str, Any]] = {}

    for row in conn.execute(
            "SELECT nm_id, SUM(spend) AS spend, SUM(revenue) AS revenue,"
            " MAX(name) AS name FROM campaign_nm_daily"
            " WHERE date BETWEEN ? AND ? GROUP BY nm_id",
            (date_from, date_to)):
        rows[int(row["nm_id"])] = {
            "nm_id": int(row["nm_id"]),
            "name": str(row["name"] or ""),
            "spend": round(float(row["spend"] or 0), 2),
            "ad_revenue": round(float(row["revenue"] or 0), 2),
            "revenue": 0.0,
            "orders": 0.0,
        }

    got = cabinet.order_cells(conn, date_from, date_to)
    for nm_id, cell in cabinet.by_nm(got["cells"]).items():
        slot = rows.setdefault(nm_id, {
            "nm_id": nm_id, "name": "", "spend": 0.0, "ad_revenue": 0.0,
            "revenue": 0.0, "orders": 0.0})
        slot["revenue"] = round(cell["revenue"], 2)
        slot["orders"] = round(cell["orders"], 2)

    names = _names(conn, list(rows))
    for nm_id, slot in rows.items():
        if not slot["name"]:
            slot["name"] = names.get(nm_id, "")

    out = list(rows.values())
    # По обороту, а не по номеру: человек ищет товар глазами и помнит
    # его по деньгам, а не по месту в списке.
    out.sort(key=lambda row: (-row["revenue"], -row["spend"]))
    return out


def to_csv(data: dict[str, Any], title: str = "") -> str:
    """Склейка таблицей — её открывают в Excel и считают дальше."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")

    def dec(value: float, digits: int = 2) -> str:
        return f"{value:.{digits}f}".replace(".", ",")

    period = data["period"]
    if title:
        writer.writerow([title])
    writer.writerow([f"Склейка за {period['from']} — {period['to']}"])
    writer.writerow([])

    totals = data["totals"]
    writer.writerow(["Показатель", "Значение"])
    for label, value in (
        ("Расход на рекламу, ₽", dec(totals["spend"])),
        ("Заказы с рекламы, ₽", dec(totals["revenue"])),
        ("ДРР рекламный, %", dec(totals["drr"])),
        ("Весь оборот склейки, ₽", dec(totals["total_revenue"])),
        ("ДРР общий, %", dec(totals["total_drr"])),
        ("Доля рекламы в обороте, %", dec(totals["ad_share"])),
        ("Заказов всего, шт", f"{totals['total_orders']:.0f}"),
    ):
        writer.writerow([label, value])

    writer.writerow([])
    writer.writerow(["Артикул", "Товар", "Расход, ₽", "Доля расхода, %",
                     "Заказы с рекламы, ₽", "ДРР рекламный, %",
                     "Весь оборот, ₽", "Доля оборота, %", "ДРР общий, %"])
    for item in data["items"]:
        writer.writerow([
            item["nm_id"], item["name"], dec(item["spend"]),
            dec(item["spend_share"]), dec(item["revenue"]),
            dec(item["drr"]), dec(item["total_revenue"]),
            dec(item["revenue_share"]), dec(item["total_drr"]),
        ])
    return buffer.getvalue()
