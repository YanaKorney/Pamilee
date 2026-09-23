"""Сбор данных из кабинета WB в локальную базу.

Запускается вручную или по расписанию. Каждый запуск дописывает историю:
статистика за уже сохранённые дни обновляется, новая — добавляется.
Так у вас копится динамика, которой в кабинете нет.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable, Sequence

from . import db
from .rules import STATUS_NAMES, TYPE_NAMES
from .wb_client import (
    STATS_GIVE_UP,
    WBAdvertClient,
    WBError,
    WBStatisticsClient,
    _order_key,
)

Progress = Callable[[str], None]


def _log(on_progress: Progress | None, message: str) -> None:
    if on_progress:
        on_progress(message)


def _day(value: Any) -> str:
    """WB отдаёт дату как «2026-08-26T00:00:00+03:00» — берём только дату."""
    return str(value)[:10]


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    return int(round(_num(value)))


def normalize_campaign(raw: dict[str, Any], now: str) -> dict[str, Any]:
    """Карточка кампании из API → строка нашей таблицы."""
    type_code = _int(raw.get("type"))
    status_code = _int(raw.get("status"))
    advert_id = _int(raw.get("advertId"))
    return {
        "advert_id": advert_id,
        # Без карточки названия нет — опознаём кампанию по номеру
        "name": (raw.get("name") or "").strip() or f"Кампания {advert_id}",
        "type": type_code,
        "type_name": TYPE_NAMES.get(type_code, f"Тип {type_code}"),
        "status": status_code,
        "status_name": STATUS_NAMES.get(status_code, f"Статус {status_code}"),
        "daily_budget": _num(raw.get("dailyBudget")),
        "create_time": raw.get("createTime"),
        "change_time": raw.get("changeTime"),
        "start_time": raw.get("startTime"),
        "end_time": raw.get("endTime"),
        "updated_at": now,
    }


def normalize_stats(raw: dict[str, Any], now: str
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ответ fullstats по одной кампании → строки по дням и по артикулам.

    Соответствие полей WB нашим:
        sum → расход, sum_price → выручка с рекламы,
        atbs → добавления в корзину, shks → заказано штук.
    """
    advert_id = _int(raw.get("advertId"))
    daily: list[dict[str, Any]] = []
    nm_daily: list[dict[str, Any]] = []

    for day_row in raw.get("days") or []:
        day = _day(day_row.get("date"))
        daily.append({
            "advert_id": advert_id, "date": day,
            "views": _int(day_row.get("views")),
            "clicks": _int(day_row.get("clicks")),
            "atbs": _int(day_row.get("atbs")),
            "orders": _int(day_row.get("orders")),
            "shks": _int(day_row.get("shks")),
            "spend": round(_num(day_row.get("sum")), 2),
            "revenue": round(_num(day_row.get("sum_price")), 2),
            "collected_at": now,
        })

        # Внутри дня статистика разбита по площадкам (apps), а внутри — по артикулам.
        # Один и тот же артикул встречается в нескольких площадках, поэтому суммируем.
        per_nm: dict[int, dict[str, Any]] = {}
        for app in day_row.get("apps") or []:
            for nm in app.get("nm") or []:
                nm_id = _int(nm.get("nmId"))
                if not nm_id:
                    continue
                item = per_nm.setdefault(nm_id, {
                    "advert_id": advert_id, "date": day, "nm_id": nm_id,
                    "name": (nm.get("name") or "").strip(),
                    "views": 0, "clicks": 0, "atbs": 0, "orders": 0,
                    "shks": 0, "spend": 0.0, "revenue": 0.0,
                })
                if nm.get("name"):
                    item["name"] = str(nm["name"]).strip()
                item["views"] += _int(nm.get("views"))
                item["clicks"] += _int(nm.get("clicks"))
                item["atbs"] += _int(nm.get("atbs"))
                item["orders"] += _int(nm.get("orders"))
                item["shks"] += _int(nm.get("shks"))
                item["spend"] += _num(nm.get("sum"))
                item["revenue"] += _num(nm.get("sum_price"))
        for item in per_nm.values():
            item["spend"] = round(item["spend"], 2)
            item["revenue"] = round(item["revenue"], 2)
            nm_daily.append(item)

    return daily, nm_daily


def normalize_order(raw: dict[str, Any], now: str) -> dict[str, Any] | None:
    """Строка заказа из API статистики → строка нашей таблицы.

    Если у заказа нет ни srid, ни номера — пропускаем: без устойчивого ключа
    повторный сбор наплодит дублей.
    """
    key = _order_key(raw)
    if not key or key.startswith("None:"):
        return None

    total = _num(raw.get("totalPrice"))
    discount = _num(raw.get("discountPercent"))
    # priceWithDisc WB отдаёт не всегда — тогда считаем сами
    price_with_disc = _num(raw.get("priceWithDisc")) or round(total * (1 - discount / 100), 2)

    return {
        "srid": key,
        "date": _day(raw.get("date")),
        "last_change": raw.get("lastChangeDate"),
        "nm_id": _int(raw.get("nmId")),
        "supplier_article": (raw.get("supplierArticle") or "").strip(),
        "brand": (raw.get("brand") or "").strip(),
        "subject": (raw.get("subject") or "").strip(),
        "warehouse": (raw.get("warehouseName") or "").strip(),
        "region": (raw.get("regionName") or "").strip(),
        "total_price": total,
        "discount_percent": discount,
        "price_with_disc": price_with_disc,
        "finished_price": _num(raw.get("finishedPrice")),
        "is_cancel": 1 if raw.get("isCancel") else 0,
        "cancel_date": raw.get("cancelDate"),
        "collected_at": now,
    }


# Статистика есть только у кампаний, которые хотя бы раз откручивались.
# Запрашивать её у остальных — это впустую потраченные минуты ожидания,
# потому что метод отдаётся раз в минуту.
STATUSES_WITH_STATS = {7, 9, 11}   # завершена, идут показы, на паузе


def campaigns_worth_asking(rows: Sequence[dict[str, Any]],
                           date_from: str) -> list[int]:
    """Отбирает кампании, у которых может быть статистика за период."""
    chosen: list[int] = []
    for row in rows:
        if row.get("status") not in STATUSES_WITH_STATS:
            continue
        # Кампания, законченная до начала периода, ничего не добавит
        end = _day(row.get("end_time")) if row.get("end_time") else ""
        if end and end < date_from:
            continue
        if row.get("advert_id"):
            chosen.append(int(row["advert_id"]))
    return chosen


def collect_orders(conn: sqlite3.Connection, token: str, days: int = 30,
                   end: date | None = None, on_progress: Progress | None = None,
                   ca_bundle: str = "") -> dict[str, Any]:
    """Забирает заказы кабинета — знаменатель для общего ДРР.

    Требует у токена категорию «Статистика». Метод отдаётся раз в минуту,
    поэтому сбор небыстрый; зато повторные запуски дёшевы — WB возвращает
    только то, что изменилось.
    """
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    now = datetime.now().isoformat(timespec="seconds")

    client = WBStatisticsClient(token, ca_bundle=ca_bundle)
    _log(on_progress, f"Забираем заказы с {start.isoformat()}…")
    raw_rows = client.orders(start.isoformat(), on_progress=on_progress)

    rows = [normalize_order(r, now) for r in raw_rows]
    rows = [r for r in rows if r and r["nm_id"]]
    # Заказы старше запрошенного периода WB тоже присылает — они приезжают
    # из-за поздних изменений статуса. Оставляем: история от этого только полнее.
    db.upsert_orders(conn, rows)
    conn.commit()

    cancels = sum(1 for r in rows if r["is_cancel"])
    _log(on_progress, f"Заказов сохранено: {len(rows)} (из них отменённых {cancels}).")
    return {"orders": len(rows), "cancels": cancels, "date_from": start.isoformat()}


def collect(conn: sqlite3.Connection, token: str, days: int = 30,
            end: date | None = None, advert_ids: Sequence[int] | None = None,
            on_progress: Progress | None = None,
            with_orders: bool = True, ca_bundle: str = "") -> dict[str, Any]:
    """Забирает кампании, баланс, статистику рекламы и заказы кабинета."""
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    date_from, date_to = start.isoformat(), end.isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    client = WBAdvertClient(token, ca_bundle=ca_bundle)
    log_id = db.start_collect(conn, "wb-api", now, date_from, date_to)
    conn.commit()

    try:
        _log(on_progress, "Забираем баланс кабинета…")
        try:
            balance = client.balance()
            db.save_balance(conn, now, balance["balance"], balance["bonus"], balance["net"])
        except WBError as exc:
            # Баланс — приятное дополнение; без него сбор продолжаем
            _log(on_progress, f"Баланс получить не вышло: {exc}")

        _log(on_progress, "Получаем список кампаний…")
        index = client.campaign_index()
        if advert_ids:
            wanted = set(advert_ids)
            index = [row for row in index if row["advertId"] in wanted]
        ids = [row["advertId"] for row in index]
        if not ids:
            db.finish_collect(conn, log_id, datetime.now().isoformat(timespec="seconds"),
                              0, 0, "ok", "В кабинете нет рекламных кампаний")
            conn.commit()
            return {"campaigns": 0, "rows": 0, "message": "В кабинете нет рекламных кампаний"}

        _log(on_progress, f"Кампаний найдено: {len(ids)}. Забираем названия…")

        # Названия и бюджеты приходят отдельным методом, и у части кабинетов
        # он отвечает 404. Это не повод останавливать сбор: тип и статус
        # уже есть в списке кампаний, а без названия кампания опознаётся
        # по номеру. Поэтому карточки — только дополнение к основе.
        details_by_id: dict[int, dict[str, Any]] = {}
        try:
            for raw in client.campaign_details(ids, on_progress=on_progress):
                advert_id = _int(raw.get("advertId"))
                if advert_id:
                    details_by_id[advert_id] = raw
        except WBError as exc:
            _log(on_progress, f"Названия получить не вышло: {exc}")

        campaign_rows = [
            normalize_campaign({**row, **details_by_id.get(row["advertId"], {})}, now)
            for row in index
        ]
        campaign_rows = [r for r in campaign_rows if r["advert_id"]]
        db.upsert_campaigns(conn, campaign_rows)
        conn.commit()
        _log(on_progress, f"Кампаний сохранено: {len(campaign_rows)}"
                          f" (с названиями: {len(details_by_id)}).")

        # Спрашиваем статистику только у тех, у кого она может быть:
        # каждая лишняя пачка — минута ожидания.
        ask_ids = campaigns_worth_asking(campaign_rows, date_from)
        skipped = len(campaign_rows) - len(ask_ids)
        if skipped:
            _log(on_progress, f"Пропускаю {skipped} кампаний: не запускались"
                              " или закончились до начала периода.")

        _log(on_progress, f"Забираем статистику по {len(ask_ids)} кампаниям "
                          f"за {date_from} — {date_to}.")
        _log(on_progress, "Метод медленный: WB отдаёт его раз в минуту.")

        # Карточки и статистика запрашиваются одним и тем же способом (POST).
        # Если карточки не отдались ни по одной кампании, дело почти наверняка
        # не в данных, а в самом способе запроса — и тогда перебирать пачки
        # статистики по минуте каждая значит впустую забрать полчаса у
        # человека. Делаем пару проб и останавливаемся с внятным ответом.
        stats_budget = STATS_GIVE_UP
        if getattr(client, "details_all_404", False):
            stats_budget = 2
            _log(on_progress, "Карточки кампаний не отдались ни по одной. "
                              "Проверю статистику парой запросов, а не перебором "
                              "по минуте на пачку.")

        stats = client.fullstats(ask_ids, date_from, date_to,
                                 on_progress=on_progress,
                                 give_up_after=stats_budget)

        daily_all: list[dict[str, Any]] = []
        nm_all: list[dict[str, Any]] = []
        for raw in stats:
            daily, nm_daily = normalize_stats(raw, now)
            daily_all.extend(daily)
            nm_all.extend(nm_daily)

        db.upsert_daily(conn, daily_all)
        db.upsert_nm_daily(conn, nm_all)
        conn.commit()

        # Заказы нужны для общего ДРР. Категории «Статистика» может не быть —
        # тогда сбор рекламы всё равно считается успешным.
        orders_result: dict[str, Any] = {}
        if with_orders:
            try:
                orders_result = collect_orders(conn, token, days=days, end=end,
                                               on_progress=on_progress,
                                               ca_bundle=ca_bundle)
            except WBError as exc:
                _log(on_progress, f"Заказы получить не вышло: {exc}")
                _log(on_progress, "Общий ДРР считаться не будет, рекламный — будет.")

        db.finish_collect(conn, log_id, datetime.now().isoformat(timespec="seconds"),
                          len(campaign_rows), len(daily_all))
        conn.commit()

        if not daily_all:
            _log(on_progress, "Статистики за период WB не отдал ни по одной кампании.")
            _log(on_progress, "Заказы при этом собраны — общий ДРР считаться будет.")
        _log(on_progress, f"Готово: {len(campaign_rows)} кампаний, "
                          f"{len(daily_all)} дней статистики.")
        return {
            "campaigns": len(campaign_rows),
            "rows": len(daily_all),
            "nm_rows": len(nm_all),
            "orders": orders_result.get("orders", 0),
            "date_from": date_from,
            "date_to": date_to,
        }

    except Exception as exc:
        db.finish_collect(conn, log_id, datetime.now().isoformat(timespec="seconds"),
                          0, 0, "error", str(exc))
        conn.commit()
        raise
