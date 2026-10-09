"""Сводки в виде таблицы «показатели × дни».

Форма взята из таблицы, которую менеджер ведёт руками: строки — показатели,
колонки — дни. Привычный для аналитики вид — наоборот, строка на день, и
соблазн «сделать правильно» здесь надо задавить: человек читает свои цифры
определённым движением глаза, и менять его ради чужой нормы — значит
заставлять переучиваться там, где учиться нечему.

Две сводки из одного кода. «По товарам» — весь кабинет или один артикул:
правки делают в кампании, а смотрят на товар. «По кампаниям» — тот же
артикул, но по таблице на каждую его кампанию: ручная на поиске и
автоматическая на рекомендациях живут по-разному, и в общей сумме это
различие пропадает ровно там, где по нему принимают решение.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from .metrics import RAW_KEYS, daterange, derive, safe_div

# Строки таблицы — в том порядке и под теми названиями, в каких их читает
# человек. «Заказы в шт» и «Заказы в руб» — это одно и то же событие,
# измеренное двумя способами, и стоять они должны рядом.
# Зоны показа. Отдельного поля «зона» в статистике WB нет, и выдумывать
# его нельзя. Зато есть статистика по поисковым кластерам — это по
# определению поиск. Всё остальное — полки, каталог, карточка — WB не
# разделяет, и делить их догадками значило бы выдавать предположение за
# отчёт. Поэтому ровно две строки: точный поиск и честное «прочее».
ZONE_ROWS: list[dict[str, Any]] = [
    {"key": "views_search", "title": "в поиске", "unit": "шт", "zone": True},
    {"key": "views_other", "title": "прочее: полки, каталог, карточка",
     "unit": "шт", "zone": True},
]

ROWS: list[dict[str, Any]] = [
    {"key": "views", "title": "Показы", "unit": "шт"},
    *ZONE_ROWS,
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
SUMMABLE = {"views", "clicks", "atbs", "orders", "shks", "spend", "revenue",
            "views_search", "views_other"}


def _search_by_day(conn: sqlite3.Connection, date_from: str, date_to: str,
                   nm_id: int | None, advert_id: int | None = None
                   ) -> dict[str, float]:
    """Показы в поиске по дням — из статистики поисковых кластеров."""
    params: list[Any] = [date_from, date_to]
    where = ""
    if nm_id is not None:
        where += " AND nm_id = ?"
        params.append(nm_id)
    if advert_id is not None:
        where += " AND advert_id = ?"
        params.append(advert_id)
    return {str(row["date"])[:10]: float(row["views"] or 0)
            for row in conn.execute(
                "SELECT date, SUM(views) AS views FROM campaign_nm_search_daily"
                " WHERE date BETWEEN ? AND ?" + where + " GROUP BY date",
                params)}


def _split_zones(total_views: float, search_views: float) -> dict[str, float]:
    """Делит показы дня на поиск и прочее.

    Считается от того же итога, что стоит в строке «Показы»: иначе
    подстроки не сходятся с ней, и таблица врёт на глазах. Поиск при этом
    обрезается итогом, а остаток — нулём: два метода WB считают по-своему,
    и показывать человеку больше ста процентов или отрицательные показы
    хуже, чем показать расхождение прижатым к границе.
    """
    search = max(0.0, min(float(search_views or 0), float(total_views or 0)))
    return {
        "views_search": search,
        "views_other": max(0.0, float(total_views or 0) - search),
    }


def daily_table(conn: sqlite3.Connection, date_from: str, date_to: str,
                nm_id: int | None = None) -> dict[str, Any]:
    """Таблица за период: строки — показатели, колонки — дни.

    Итог за период считается из сумм, а не усреднением дневных значений.
    Среднее от дневных ДРР — это не ДРР периода: день с расходом 10 ₽ весит
    в нём столько же, сколько день с расходом 10 000 ₽.
    """
    table = _table(_by_day(conn, date_from, date_to, nm_id),
                   _search_by_day(conn, date_from, date_to, nm_id),
                   daterange(date_from, date_to))
    return {"period": {"from": date_from, "to": date_to,
                       "days": len(table["dates"])},
            "nm_id": nm_id, **table}


def campaign_tables(conn: sqlite3.Connection, date_from: str, date_to: str,
                    nm_id: int) -> dict[str, Any]:
    """Те же таблицы, но по одной на каждую кампанию этого артикула.

    Один товар почти всегда крутится сразу в нескольких кампаниях, и
    складывать их в одну таблицу — значит прятать именно то, по чему
    принимается решение: ручная на поиске может кормить, пока
    автоматическая на рекомендациях жжёт. Поэтому по таблице на кампанию,
    от крупной к мелкой — порядок задаёт расход, а не номер.
    """
    days = daterange(date_from, date_to)
    blocks: list[dict[str, Any]] = []
    for camp in _campaigns_of(conn, date_from, date_to, nm_id):
        advert_id = camp["advert_id"]
        blocks.append({
            **camp,
            **_table(_by_day(conn, date_from, date_to, nm_id,
                             advert_id=advert_id),
                     _search_by_day(conn, date_from, date_to, nm_id,
                                    advert_id=advert_id),
                     days),
        })
    return {
        "period": {"from": date_from, "to": date_to, "days": len(days)},
        "nm_id": nm_id,
        "dates": days,
        "campaigns": blocks,
        "has_data": any(block["has_data"] for block in blocks),
    }


def _campaigns_of(conn: sqlite3.Connection, date_from: str, date_to: str,
                  nm_id: int) -> list[dict[str, Any]]:
    """Кампании, в которых этот артикул крутился за период."""
    rows = conn.execute(
        "SELECT d.advert_id AS advert_id, SUM(d.spend) AS spend,"
        " SUM(d.views) AS views, MAX(c.name) AS name,"
        " MAX(c.type_name) AS type_name, MAX(c.status_name) AS status_name"
        " FROM campaign_nm_daily d"
        " LEFT JOIN campaigns c ON c.advert_id = d.advert_id"
        " WHERE d.nm_id = ? AND d.date BETWEEN ? AND ?"
        " GROUP BY d.advert_id ORDER BY SUM(d.spend) DESC, d.advert_id",
        (nm_id, date_from, date_to),
    ).fetchall()

    return [{
        "advert_id": int(row["advert_id"]),
        # Без карточки кампании названия нет — опознаём по номеру, как
        # и везде: пустая строка на месте названия читается как пропажа.
        "name": str(row["name"] or "") or f"Кампания {int(row['advert_id'])}",
        "type_name": str(row["type_name"] or ""),
        "status_name": str(row["status_name"] or ""),
    } for row in rows]


def _table(totals_by_day: dict[str, dict[str, float]],
           search_by_day: dict[str, float],
           days: Sequence[str]) -> dict[str, Any]:
    """Собирает строки таблицы из дневных сумм. Общее для обеих сводок."""
    zone_keys = [row["key"] for row in ZONE_ROWS]
    period_totals = {key: 0.0 for key in RAW_KEYS}
    zone_totals = {key: 0.0 for key in zone_keys}
    columns: list[dict[str, Any]] = []
    for day in days:
        raw = totals_by_day.get(day, {key: 0.0 for key in RAW_KEYS})
        for key in RAW_KEYS:
            period_totals[key] += raw[key]
        values = derive(raw)
        zones = _split_zones(raw["views"], search_by_day.get(day, 0.0))
        for key in zone_keys:
            value = float(zones.get(key) or 0)
            values[key] = value
            zone_totals[key] += value
        columns.append({"date": day, "values": values})

    period = derive(period_totals)
    period.update(zone_totals)
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
        "dates": list(days),
        "rows": rows,
        "has_data": any(period_totals[key] for key in RAW_KEYS),
        # Зоны показываем, только если статистика поиска вообще собрана.
        # Иначе «прочее» равнялось бы всем показам — и выглядело бы как
        # утверждение «в поиске не было ни одного показа», хотя на деле
        # мы просто не спрашивали.
        "has_zones": bool(search_by_day),
    }


def _by_day(conn: sqlite3.Connection, date_from: str, date_to: str,
            nm_id: int | None, advert_id: int | None = None
            ) -> dict[str, dict[str, float]]:
    """Сырые суммы по дням: по кабинету, по артикулу или по его кампании."""
    sums = ", ".join(f"SUM({k}) AS {k}" for k in RAW_KEYS)
    if nm_id is None:
        sql = (f"SELECT date, {sums} FROM campaign_daily"
               " WHERE date BETWEEN ? AND ? GROUP BY date")
        params: list[Any] = [date_from, date_to]
    else:
        where = " AND advert_id = ?" if advert_id is not None else ""
        sql = (f"SELECT date, {sums} FROM campaign_nm_daily WHERE nm_id = ?"
               " AND date BETWEEN ? AND ?" + where + " GROUP BY date")
        params = [nm_id, date_from, date_to]
        if advert_id is not None:
            params.append(advert_id)

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
    import io

    buffer = io.StringIO()
    writer = _writer(buffer)
    if title:
        writer.writerow([title])
    _write_table(writer, table)
    return buffer.getvalue()


def campaigns_to_csv(data: dict[str, Any], title: str = "") -> str:
    """Сводка по кампаниям файлом: таблицы одна под другой.

    Разделять их пустой строкой и шапкой обязательно — слитые в один
    блок, они читаются как одна кампания с удвоенными числами.
    """
    import io

    buffer = io.StringIO()
    writer = _writer(buffer)
    if title:
        writer.writerow([title])
    for block in data.get("campaigns") or []:
        writer.writerow([])
        writer.writerow([_campaign_label(block)])
        _write_table(writer, block)
    return buffer.getvalue()


def _campaign_label(block: dict[str, Any]) -> str:
    parts = [str(block.get("name") or ""), str(block.get("type_name") or ""),
             str(block.get("status_name") or "")]
    head = f"Кампания {block.get('advert_id')}"
    return " · ".join([head] + [p for p in parts if p])


def _writer(buffer: Any) -> Any:
    import csv

    return csv.writer(buffer, delimiter=";")


def _write_table(writer: Any, table: dict[str, Any]) -> None:
    def dec(value: float, digits: int = 2) -> str:
        return f"{value:.{digits}f}".replace(".", ",")

    writer.writerow(["Дата"] + [_day_label(d) for d in table["dates"]]
                    + ["За период"])
    for row in table["rows"]:
        # Зоны без собранного поиска показывать нельзя: «прочее» равнялось
        # бы всем показам и читалось как утверждение, которого мы не делали.
        if row.get("zone") and not table.get("has_zones"):
            continue
        digits = 0 if row["unit"] == "шт" else 2
        writer.writerow([row["title"]]
                        + [dec(v, digits) for v in row["days"]]
                        + [dec(row["total"], digits)])


def _day_label(iso: str) -> str:
    """Дата в виде, который Excel не превратит в число неизвестного года."""
    parts = iso.split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else iso
