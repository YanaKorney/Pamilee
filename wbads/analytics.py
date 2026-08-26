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


def _group_by_campaign(rows: Sequence[sqlite3.Row]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(int(row["advert_id"]), []).append(dict(row))
    return grouped


def _nm_summary(conn: sqlite3.Connection, advert_id: int, date_from: str,
                date_to: str, thresholds: Thresholds) -> list[dict[str, Any]]:
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

    items = []
    for item in by_nm.values():
        kpi = derive(item)
        kpi["nm_id"] = item["nm_id"]
        kpi["name"] = item["name"] or f"Артикул {item['nm_id']}"
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
                 thresholds: Thresholds, with_nm: bool = True) -> dict[str, Any]:
    """Главный отчёт: итоги, динамика и разбор каждой кампании."""
    prev_from, prev_to = shift_window(date_from, date_to)

    cur_rows = db.daily_rows(conn, date_from, date_to)
    prev_rows = db.daily_rows(conn, prev_from, prev_to)
    campaigns = {int(c["advert_id"]): dict(c) for c in db.list_campaigns(conn)}

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
        nm_items = _nm_summary(conn, advert_id, date_from, date_to, thresholds) if with_nm else []

        ctx = Context(
            campaign=meta,
            series=series,
            current=current,
            previous=previous,
            thresholds=thresholds,
            nm_items=nm_items,
        )
        diagnoses.append(diagnose(ctx))

    # сначала критичные, внутри — те, где на кону больше денег
    diagnoses.sort(key=lambda d: (
        SEVERITY_RANK.get(d["verdict"], 9),
        -d["metrics"]["spend"],
    ))

    totals_cur = sum_rows(cur_rows)
    totals_prev = sum_rows(prev_rows)

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
        "series": build_series(cur_rows, date_from, date_to),
        "campaigns": diagnoses,
        "summary": _summary(diagnoses, derive(totals_cur), thresholds),
        "thresholds": thresholds.to_dict(),
        "meta": _meta(conn),
    }


def _summary(diagnoses: Sequence[Mapping[str, Any]], totals: Mapping[str, float],
             t: Thresholds) -> dict[str, Any]:
    """Верхняя сводка: сколько кампаний в каждом состоянии и что на кону."""
    critical = [d for d in diagnoses if d["verdict"] == CRITICAL]
    warning = [d for d in diagnoses if d["verdict"] == WARNING]
    opportunity = [d for d in diagnoses if d["verdict"] == OPPORTUNITY]
    healthy = [d for d in diagnoses if d["verdict"] == "ok"]
    idle = [d for d in diagnoses if d["verdict"] == "idle"]

    # Деньги, которые уходят мимо цели: расход сверх целевого ДРР
    # по кампаниям, где что-то не так.
    overspend = 0.0
    for d in critical + warning:
        m = d["metrics"]
        target_spend = m["revenue"] * t.target_drr / 100
        overspend += max(0.0, m["spend"] - target_spend)

    # Сколько ещё можно открутить без выхода за цель по здоровым кампаниям
    headroom = 0.0
    for d in opportunity + healthy:
        m = d["metrics"]
        headroom += max(0.0, m["revenue"] * t.target_drr / 100 - m["spend"])

    return {
        "campaigns_total": len(diagnoses),
        "critical": len(critical),
        "warning": len(warning),
        "opportunity": len(opportunity),
        "healthy": len(healthy),
        "idle": len(idle),
        "money_at_risk": round(overspend, 2),
        "scale_headroom": round(headroom, 2),
        "target_drr": t.target_drr,
        "drr_gap": totals["drr"] - t.target_drr if totals["revenue"] else None,
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
                    date_to: str, thresholds: Thresholds) -> dict[str, Any] | None:
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
    ctx = Context(
        campaign=meta,
        series=build_series(rows, date_from, date_to),
        current=sum_rows(rows),
        previous=sum_rows(prev),
        thresholds=thresholds,
        nm_items=_nm_summary(conn, advert_id, date_from, date_to, thresholds),
    )
    result = diagnose(ctx)
    result["period"] = {"from": date_from, "to": date_to}
    return result
