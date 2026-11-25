"""Заказы кабинета по «артикул + день» — одно правило на всю программу.

Чисел про заказы у WB два, и они разные. Метод заказов отдаёт строки
заказа: один заказ на три товара — три строки. Воронка продаж считает
товары, и именно её числа стоят в кабинете на странице «Этапы воронки
продаж». Разница доходит до двадцати процентов, поэтому вопрос «откуда
берём оборот» решается один раз и одинаково везде: вкладка «Аналитика»,
«Динамика по дням» и «ДРР склейки» обязаны показывать один и тот же
оборот за один и тот же период.

Правило: день, который есть в воронке, считает воронка — целиком.
Смешивать внутри дня числа двух методов нельзя: получился бы оборот,
которого не было. Дни, которых в воронке ещё нет, считаются по строкам
заказов: ноль вместо несобранного дня читался бы как провал продаж.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from . import db

# Что лежит в каждой клетке «артикул + день».
CELL_KEYS = ("orders", "revenue", "opens", "carts")


def order_cells(conn: sqlite3.Connection, date_from: str, date_to: str,
                price_field: str = "price_with_disc",
                include_cancelled: bool = False,
                nm_ids: Iterable[int] | None = None) -> dict[str, Any]:
    """Заказы кабинета по «артикул + день» за период.

    Возвращает клетки, дни, закрытые воронкой, и дни, где её не хватило.
    Переходы в карточку и корзины бывают только у воронки: у метода
    заказов их нет совсем, и за такие дни там нули.
    """
    wanted = ({int(nm) for nm in nm_ids} if nm_ids is not None else None)
    cells: dict[tuple[int, str], dict[str, float]] = {}

    def slot(nm_id: int, day: str) -> dict[str, float]:
        return cells.setdefault((nm_id, day),
                                {key: 0.0 for key in CELL_KEYS})

    for row in db.orders_by_nm(conn, date_from, date_to, price_field,
                               include_cancelled):
        nm_id = int(row["nm_id"] or 0)
        if wanted is not None and nm_id not in wanted:
            continue
        cell = slot(nm_id, str(row["date"])[:10])
        cell["orders"] += float(row["orders"] or 0)
        cell["revenue"] += float(row["revenue"] or 0)

    funnel = [row for row in db.funnel_by_day(conn, date_from, date_to)
              if wanted is None or int(row["nm_id"] or 0) in wanted]
    covered = {str(row["date"])[:10] for row in funnel}
    if covered:
        # День заменяется целиком — вместе с артикулами, которых в
        # воронке за этот день нет: раз WB их не показал, значит заказов
        # по ним он не считает.
        cells = {key: cell for key, cell in cells.items()
                 if key[1] not in covered}
        for row in funnel:
            cell = slot(int(row["nm_id"] or 0), str(row["date"])[:10])
            cell["orders"] += float(row["orders"] or 0)
            cell["revenue"] += float(row["orders_sum"] or 0)
            cell["opens"] += float(row["opens"] or 0)
            cell["carts"] += float(row["carts"] or 0)

    return {"cells": cells, "funnel_days": sorted(covered),
            "source": "funnel" if covered else "orders"}


def by_day(cells: dict[tuple[int, str], dict[str, float]]
           ) -> dict[str, dict[str, float]]:
    """Те же клетки, свёрнутые по дням."""
    out: dict[str, dict[str, float]] = {}
    for (_, day), cell in cells.items():
        slot = out.setdefault(day, {key: 0.0 for key in CELL_KEYS})
        for key in CELL_KEYS:
            slot[key] += cell[key]
    return out


def by_nm(cells: dict[tuple[int, str], dict[str, float]]
          ) -> dict[int, dict[str, float]]:
    """Те же клетки, свёрнутые по артикулам."""
    out: dict[int, dict[str, float]] = {}
    for (nm_id, _), cell in cells.items():
        slot = out.setdefault(nm_id, {key: 0.0 for key in CELL_KEYS})
        for key in CELL_KEYS:
            slot[key] += cell[key]
    return out


def totals(cells: dict[tuple[int, str], dict[str, float]]) -> dict[str, float]:
    """Итог за весь период."""
    out = {key: 0.0 for key in CELL_KEYS}
    for cell in cells.values():
        for key in CELL_KEYS:
            out[key] += cell[key]
    return out
