"""Сервисный слой: собирает отчёт из базы, метрик и правил.

Здесь нет обращений к API и нет HTML — только чистая аналитика,
которую одинаково используют и дашборд, и консольный отчёт.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from . import db
from .config import Thresholds
from .metrics import (
    RAW_KEYS,
    safe_div,
    build_series,
    compare,
    derive,
    empty_totals,
    shift_window,
    sum_rows,
)
from .rules import CRITICAL, OPPORTUNITY, WARNING, Context, diagnose

SEVERITY_RANK = {CRITICAL: 0, WARNING: 1, OPPORTUNITY: 2, "info": 3, "ok": 4, "idle": 5}


def default_period(conn: sqlite3.Connection, days: int = 7) -> tuple[str, str]:
    """Последние N дней, для которых в базе есть данные."""
    lo, hi = db.data_range(conn)
    end = date.fromisoformat(hi) if hi else date.today()
    start = end - timedelta(days=days - 1)
    if lo:
        start = max(start, date.fromisoformat(lo))
    return start.isoformat(), end.isoformat()


def load_thresholds(conn: sqlite3.Connection, base: Thresholds) -> Thresholds:
    """Пороги из .env, поверх — то, что пользователь поменял в дашборде."""
    stored = db.get_settings(conn)
    values = base.to_dict()
    for key, raw in stored.items():
        if key in values:
            try:
                values[key] = float(raw)
            except (TypeError, ValueError):
                continue
    return Thresholds(**values)


# ── общий ДРР: реклама против ВСЕХ заказов, а не только рекламных ────────

def _orders_index(conn: sqlite3.Connection, date_from: str, date_to: str,
                  price_field: str) -> dict[str, Any]:
    """Заказы кабинета за период: по артикулам, по дням и итогом.

    Рекламный ДРР считается от заказов, которые WB атрибутировал рекламе.
    Общий — от всех заказов артикула, включая органику. Второй показывает
    нагрузку рекламы на оборот, первый — эффективность самой открутки.
    """
    rows = db.orders_by_nm(conn, date_from, date_to, price_field)
    by_nm: dict[int, dict[str, float]] = {}
    by_nm_day: dict[tuple[int, str], float] = {}
    by_day: dict[str, float] = {}

    for row in rows:
        nm_id = int(row["nm_id"])
        day = str(row["date"])[:10]
        revenue = float(row["revenue"] or 0)
        orders = float(row["orders"] or 0)

        item = by_nm.setdefault(nm_id, {"orders": 0.0, "revenue": 0.0})
        item["orders"] += orders
        item["revenue"] += revenue
        by_nm_day[(nm_id, day)] = by_nm_day.get((nm_id, day), 0.0) + revenue
        by_day[day] = by_day.get(day, 0.0) + revenue

    return {
        "by_nm": by_nm,
        "by_nm_day": by_nm_day,
        "by_day": by_day,
        "totals": db.orders_totals(conn, date_from, date_to, price_field),
        "available": bool(rows),
    }


def _campaign_nm_spend(conn: sqlite3.Connection, date_from: str,
                       date_to: str) -> dict[int, dict[int, float]]:
    """Расход по «кампания → артикул» за период."""
    rows = conn.execute(
        "SELECT advert_id, nm_id, SUM(spend) AS spend FROM campaign_nm_daily"
        " WHERE date BETWEEN ? AND ? GROUP BY advert_id, nm_id",
        (date_from, date_to),
    )
    out: dict[int, dict[int, float]] = {}
    for row in rows:
        out.setdefault(int(row["advert_id"]), {})[int(row["nm_id"])] = float(row["spend"] or 0)
    return out


def _attributed_revenue(campaign_spend: Mapping[int, float],
                        article_spend: Mapping[int, float],
                        orders_by_nm: Mapping[int, Mapping[str, float]]) -> float:
    """Сколько общей выручки артикулов «приходится» на эту кампанию.

    Один артикул часто крутится в нескольких кампаниях. Если каждой отдать
    весь его оборот, сумма по кампаниям окажется больше кабинета, и общий
    ДРР по кампаниям будет занижен. Поэтому оборот артикула делится между
    кампаниями пропорционально тому, сколько каждая на него потратила.
    """
    total = 0.0
    for nm_id, spend in campaign_spend.items():
        article_total = article_spend.get(nm_id, 0.0)
        if article_total <= 0:
            continue
        share = spend / article_total
        total += float(orders_by_nm.get(nm_id, {}).get("revenue", 0.0)) * share
    return total


def _group_by_campaign(rows: Sequence[sqlite3.Row]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(int(row["advert_id"]), []).append(dict(row))
    return grouped


def _nm_summary(conn: sqlite3.Connection, advert_id: int, date_from: str,
                date_to: str, thresholds: Thresholds,
                orders_by_nm: Mapping[int, Mapping[str, float]] | None = None
                ) -> list[dict[str, Any]]:
    """Разбивка кампании по артикулам за период — кто внутри тянет вниз."""
    rows = db.nm_rows(conn, advert_id, date_from, date_to)
    by_nm: dict[int, dict[str, Any]] = {}
    for row in rows:
        nm_id = int(row["nm_id"])
        item = by_nm.setdefault(nm_id, {"nm_id": nm_id, "name": row["name"] or "", **empty_totals()})
        if row["name"]:
            item["name"] = row["name"]
        for key in RAW_KEYS:
            item[key] += float(row[key] or 0)

    orders_by_nm = orders_by_nm or {}
    items = []
    for item in by_nm.values():
        kpi = derive(item)
        nm_id = item["nm_id"]
        kpi["nm_id"] = nm_id
        kpi["name"] = item["name"] or f"Артикул {nm_id}"

        # На уровне артикула делить оборот не нужно: все его заказы — его.
        article = orders_by_nm.get(nm_id, {})
        kpi["total_orders"] = float(article.get("orders", 0.0))
        kpi["total_revenue"] = float(article.get("revenue", 0.0))
        kpi["total_drr"] = safe_div(kpi["spend"], kpi["total_revenue"]) * 100
        kpi["organic_orders"] = max(0.0, kpi["total_orders"] - kpi["orders"])
        kpi["organic_share"] = safe_div(kpi["organic_orders"], kpi["total_orders"]) * 100

        kpi["flag"] = _nm_flag(kpi, thresholds)
        items.append(kpi)

    items.sort(key=lambda x: x["spend"], reverse=True)
    return items


def _nm_flag(kpi: Mapping[str, float], t: Thresholds) -> str:
    """Светофор по артикулу внутри кампании."""
    if kpi["spend"] < t.min_spend / 3:
        return "quiet"
    if kpi["orders"] == 0:
        return "bad"
    if kpi["drr"] > t.target_drr * t.drr_critical_multiplier:
        return "bad"
    if kpi["drr"] > t.target_drr:
        return "watch"
    if kpi["drr"] <= t.target_drr * t.scale_up_ratio:
        return "star"
    return "ok"


def build_report(conn: sqlite3.Connection, date_from: str, date_to: str,
                 thresholds: Thresholds, with_nm: bool = True,
                 price_field: str = "price_with_disc") -> dict[str, Any]:
    """Главный отчёт: итоги, динамика и разбор каждой кампании."""
    prev_from, prev_to = shift_window(date_from, date_to)

    cur_rows = db.daily_rows(conn, date_from, date_to)
    prev_rows = db.daily_rows(conn, prev_from, prev_to)
    campaigns = {int(c["advert_id"]): dict(c) for c in db.list_campaigns(conn)}

    orders = _orders_index(conn, date_from, date_to, price_field)
    prev_orders = _orders_index(conn, prev_from, prev_to, price_field)
    nm_spend = _campaign_nm_spend(conn, date_from, date_to)
    prev_nm_spend = _campaign_nm_spend(conn, prev_from, prev_to)
    article_spend: dict[int, float] = {}
    for per_nm in nm_spend.values():
        for nm_id, spend in per_nm.items():
            article_spend[nm_id] = article_spend.get(nm_id, 0.0) + spend
    prev_article_spend: dict[int, float] = {}
    for per_nm in prev_nm_spend.values():
        for nm_id, spend in per_nm.items():
            prev_article_spend[nm_id] = prev_article_spend.get(nm_id, 0.0) + spend

    cur_by_campaign = _group_by_campaign(cur_rows)
    prev_by_campaign = _group_by_campaign(prev_rows)

    diagnoses: list[dict[str, Any]] = []
    seen = set(cur_by_campaign) | set(prev_by_campaign)
    for advert_id in seen:
        meta = campaigns.get(advert_id, {"advert_id": advert_id, "name": f"Кампания {advert_id}"})
        rows = cur_by_campaign.get(advert_id, [])
        series = build_series(rows, date_from, date_to)
        current = sum_rows(rows)
        previous = sum_rows(prev_by_campaign.get(advert_id, []))
        nm_items = _nm_summary(conn, advert_id, date_from, date_to, thresholds,
                               orders["by_nm"]) if with_nm else []

        ctx = Context(
            campaign=meta,
            series=series,
            current=current,
            previous=previous,
            thresholds=thresholds,
            nm_items=nm_items,
            total_revenue=_attributed_revenue(
                nm_spend.get(advert_id, {}), article_spend, orders["by_nm"]),
            prev_total_revenue=_attributed_revenue(
                prev_nm_spend.get(advert_id, {}), prev_article_spend, prev_orders["by_nm"]),
            orders_available=orders["available"],
        )
        diagnoses.append(diagnose(ctx))

    # сначала критичные, внутри — те, где на кону больше денег
    diagnoses.sort(key=lambda d: (
        SEVERITY_RANK.get(d["verdict"], 9),
        -d["metrics"]["spend"],
    ))

    totals_cur = sum_rows(cur_rows)
    totals_prev = sum_rows(prev_rows)

    series = build_series(cur_rows, date_from, date_to)
    for point in series:
        day_revenue = orders["by_day"].get(point["date"], 0.0)
        point["total_revenue"] = day_revenue
        point["total_drr"] = safe_div(point["spend"], day_revenue) * 100

    # Общий ДРР кабинета — вся реклама против всего оборота по заказам,
    # включая артикулы, которые вообще не рекламировались.
    account = {
        "available": orders["available"],
        "orders": orders["totals"]["orders"],
        "revenue": orders["totals"]["revenue"],
        "cancels": orders["totals"]["cancels"],
        "cancel_revenue": orders["totals"]["cancel_revenue"],
        "total_drr": safe_div(totals_cur["spend"], orders["totals"]["revenue"]) * 100,
        "prev_total_drr": safe_div(totals_prev["spend"], prev_orders["totals"]["revenue"]) * 100,
        "prev_revenue": prev_orders["totals"]["revenue"],
        "ad_share": safe_div(derive(totals_cur)["revenue"], orders["totals"]["revenue"]) * 100,
        "price_field": price_field,
    }

    return {
        "period": {
            "from": date_from,
            "to": date_to,
            "days": len(build_series([], date_from, date_to)),
            "prev_from": prev_from,
            "prev_to": prev_to,
        },
        "totals": derive(totals_cur),
        "previous": derive(totals_prev),
        "compare": compare(totals_cur, totals_prev),
        "series": series,
        "campaigns": diagnoses,
        "orders": account,
        "summary": _summary(diagnoses, derive(totals_cur), thresholds, account),
        "thresholds": thresholds.to_dict(),
        "meta": _meta(conn),
    }


def _summary(diagnoses: Sequence[Mapping[str, Any]], totals: Mapping[str, float],
             t: Thresholds, account: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Верхняя сводка: сколько кампаний в каждом состоянии и что на кону."""
    critical = [d for d in diagnoses if d["verdict"] == CRITICAL]
    warning = [d for d in diagnoses if d["verdict"] == WARNING]
    opportunity = [d for d in diagnoses if d["verdict"] == OPPORTUNITY]
    healthy = [d for d in diagnoses if d["verdict"] == "ok"]
    idle = [d for d in diagnoses if d["verdict"] == "idle"]

    # Деньги, которые уходят мимо цели: расход сверх целевого ДРР.
    # Когда собраны заказы кабинета, считаем от всего оборота, а не только
    # от рекламного: иначе переплата задваивается на органике, и сводка
    # начинает спорить с общим ДРР.
    orders_available = bool((account or {}).get("available"))

    def base_revenue(d: Mapping[str, Any]) -> float:
        if orders_available and d.get("total_revenue"):
            return float(d["total_revenue"])
        return float(d["metrics"]["revenue"])

    overspend = 0.0
    for d in critical + warning:
        overspend += max(0.0, d["metrics"]["spend"] - base_revenue(d) * t.target_drr / 100)

    # Сколько ещё можно открутить без выхода за цель по здоровым кампаниям
    headroom = 0.0
    for d in opportunity + healthy:
        headroom += max(0.0, base_revenue(d) * t.target_drr / 100 - d["metrics"]["spend"])

    return {
        "campaigns_total": len(diagnoses),
        "critical": len(critical),
        "warning": len(warning),
        "opportunity": len(opportunity),
        "healthy": len(healthy),
        "idle": len(idle),
        "money_at_risk": round(overspend, 2),
        "scale_headroom": round(headroom, 2),
        "risk_basis": "весь оборот" if orders_available else "выручка с рекламы",
        "target_drr": t.target_drr,
        "drr_gap": totals["drr"] - t.target_drr if totals["revenue"] else None,
        "total_drr": (account or {}).get("total_drr"),
        "orders_available": bool((account or {}).get("available")),
        "top_actions": _top_actions(critical + warning + opportunity),
    }


def _top_actions(diagnoses: Sequence[Mapping[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Короткий список «сделать сегодня» — по самым дорогим проблемам."""
    actions = []
    for d in diagnoses:
        if not d["findings"]:
            continue
        finding = d["findings"][0]
        actions.append({
            "advert_id": d["advert_id"],
            "campaign": d["name"],
            "severity": finding["severity"],
            "title": finding["title"],
            "action": finding["actions"][0] if finding["actions"] else "",
            "spend": d["metrics"]["spend"],
        })
    actions.sort(key=lambda a: (SEVERITY_RANK.get(a["severity"], 9), -a["spend"]))
    return actions[:limit]


def _meta(conn: sqlite3.Connection) -> dict[str, Any]:
    lo, hi = db.data_range(conn)
    last = db.last_collect(conn)
    balance = db.latest_balance(conn)
    return {
        "data_from": lo,
        "data_to": hi,
        "last_collect": dict(last) if last else None,
        "balance": dict(balance) if balance else None,
    }


def campaign_detail(conn: sqlite3.Connection, advert_id: int, date_from: str,
                    date_to: str, thresholds: Thresholds,
                    price_field: str = "price_with_disc") -> dict[str, Any] | None:
    """Подробный разбор одной кампании."""
    prev_from, prev_to = shift_window(date_from, date_to)
    rows = [dict(r) for r in db.daily_rows(conn, date_from, date_to, [advert_id])]
    prev = [dict(r) for r in db.daily_rows(conn, prev_from, prev_to, [advert_id])]
    meta_row = conn.execute(
        "SELECT * FROM campaigns WHERE advert_id=?", (advert_id,)
    ).fetchone()
    if not rows and not meta_row:
        return None
    meta = dict(meta_row) if meta_row else {"advert_id": advert_id}
    orders = _orders_index(conn, date_from, date_to, price_field)
    prev_orders = _orders_index(conn, prev_from, prev_to, price_field)
    nm_spend = _campaign_nm_spend(conn, date_from, date_to)
    prev_nm_spend = _campaign_nm_spend(conn, prev_from, prev_to)
    article_spend: dict[int, float] = {}
    for per_nm in nm_spend.values():
        for nm_id, spend in per_nm.items():
            article_spend[nm_id] = article_spend.get(nm_id, 0.0) + spend
    prev_article_spend: dict[int, float] = {}
    for per_nm in prev_nm_spend.values():
        for nm_id, spend in per_nm.items():
            prev_article_spend[nm_id] = prev_article_spend.get(nm_id, 0.0) + spend

    ctx = Context(
        campaign=meta,
        series=build_series(rows, date_from, date_to),
        current=sum_rows(rows),
        previous=sum_rows(prev),
        thresholds=thresholds,
        nm_items=_nm_summary(conn, advert_id, date_from, date_to, thresholds,
                             orders["by_nm"]),
        total_revenue=_attributed_revenue(nm_spend.get(advert_id, {}), article_spend,
                                          orders["by_nm"]),
        prev_total_revenue=_attributed_revenue(prev_nm_spend.get(advert_id, {}),
                                               prev_article_spend, prev_orders["by_nm"]),
        orders_available=orders["available"],
    )
    result = diagnose(ctx)
    result["period"] = {"from": date_from, "to": date_to}
    return result
