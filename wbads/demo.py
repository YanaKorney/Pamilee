"""Демо-данные: правдоподобный кабинет с намеренно «больными» кампаниями.

Нужен, чтобы пройти весь путь и увидеть дашборд ещё до получения токена WB.
У каждой кампании свой сюжет — та самая ситуация, которую сервис должен
поймать и объяснить. Данные детерминированы (фиксированный seed), поэтому
демо у всех выглядит одинаково.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from . import db

SEED = 20260826

# Как читать base: сколько показов в день и какие конверсии по воронке.
# story(i, n) — множители по дням: i — индекс дня, n — всего дней.
# Последние 7 дней = «текущая неделя», предыдущие 7 = «прошлая», по ним
# и строится сравнение в отчёте.

def _last_week(i: int, n: int) -> bool:
    return i >= n - 7


def _prev_week(i: int, n: int) -> bool:
    return n - 14 <= i < n - 7


CAMPAIGNS: list[dict[str, Any]] = [
    {
        "advert_id": 21400101,
        "name": "Авто · Худи оверсайз",
        "type": 8, "status": 9, "daily_budget": 2500,
        "articles": [(184203311, "Худи оверсайз, чёрный", 0.55),
                     (184203312, "Худи оверсайз, молочный", 0.45)],
        "base": dict(views=14000, ctr=0.021, cr_cart=0.11, cr_order=0.34, cpc=9.0, price=2390),
        # Сюжет: здоровая кампания, упирается в дневной бюджет — есть куда расти
        "story": lambda i, n: dict(views=1.0, cpc=1.0, cr_order=1.0, cr_cart=1.0, ctr=1.0),
        "cap_budget": True,
    },
    {
        "advert_id": 21400102,
        "name": "Аукцион · Худи оверсайз (поиск)",
        "type": 9, "status": 9, "daily_budget": 12000,
        "articles": [(184203311, "Худи оверсайз, чёрный", 0.4),
                     (184203313, "Худи оверсайз, серый", 0.6)],
        "base": dict(views=21000, ctr=0.018, cr_cart=0.09, cr_order=0.26, cpc=11.0, price=2390),
        # Сюжет: аукцион разогрелся — клик подорожал почти вдвое, ДРР улетел
        "story": lambda i, n: dict(
            cpc=1.85 if _last_week(i, n) else (1.15 if _prev_week(i, n) else 1.0),
            cr_order=0.85 if _last_week(i, n) else 1.0,
            views=1.0, cr_cart=1.0, ctr=1.0,
        ),
    },
    {
        "advert_id": 21400103,
        "name": "Авто · Свеча ароматическая",
        "type": 8, "status": 9, "daily_budget": 3000,
        "articles": [(191887420, "Свеча ароматическая «Ваниль»", 1.0)],
        "base": dict(views=9000, ctr=0.016, cr_cart=0.08, cr_order=0.28, cpc=8.5, price=690),
        # Сюжет: товар закончился на складах — показы и клики идут, заказов нет
        "story": lambda i, n: dict(
            cr_order=0.0 if _last_week(i, n) else 1.0,
            cr_cart=0.45 if _last_week(i, n) else 1.0,
            views=1.0, cpc=1.0, ctr=1.0,
        ),
    },
    {
        "advert_id": 21400104,
        "name": "Аукцион · Термокружка 500 мл",
        "type": 9, "status": 9, "daily_budget": 9000,
        "articles": [(177451209, "Термокружка 500 мл, сталь", 0.7),
                     (177451210, "Термокружка 500 мл, чёрная", 0.3)],
        "base": dict(views=26000, ctr=0.022, cr_cart=0.10, cr_order=0.30, cpc=7.5, price=1290),
        # Сюжет: поменяли главное фото — CTR обвалился, платим за показы впустую
        "story": lambda i, n: dict(
            ctr=0.45 if _last_week(i, n) else 1.0,
            views=1.05, cpc=1.0, cr_cart=1.0, cr_order=1.0,
        ),
    },
    {
        "advert_id": 21400105,
        "name": "Авто · Плед плюшевый 200×220",
        "type": 8, "status": 9, "daily_budget": 8000,
        "articles": [(165330944, "Плед плюшевый, бежевый", 0.6),
                     (165330945, "Плед плюшевый, графит", 0.4)],
        "base": dict(views=17000, ctr=0.025, cr_cart=0.032, cr_order=0.33, cpc=8.0, price=1750),
        # Сюжет: кликают охотно, а в корзину не кладут — вопрос к карточке
        "story": lambda i, n: dict(views=1.0, cpc=1.0, ctr=1.0, cr_cart=1.0, cr_order=1.0),
    },
    {
        "advert_id": 21400106,
        "name": "Аукцион · Кроссовки беговые",
        "type": 9, "status": 9, "daily_budget": 10000,
        "articles": [(203118876, "Кроссовки беговые, 40-45", 1.0)],
        "base": dict(views=19000, ctr=0.019, cr_cart=0.12, cr_order=0.11, cpc=13.0, price=4290),
        # Сюжет: кладут в корзину и не выкупают — цена/сроки доставки
        "story": lambda i, n: dict(views=1.0, cpc=1.0, ctr=1.0, cr_cart=1.0, cr_order=1.0),
    },
    {
        "advert_id": 21400107,
        "name": "Авто · Ночник детский «Луна»",
        "type": 8, "status": 9, "daily_budget": 3000,
        "articles": [(158990233, "Ночник детский «Луна»", 1.0)],
        "base": dict(views=7000, ctr=0.023, cr_cart=0.10, cr_order=0.31, cpc=7.0, price=990),
        # Сюжет: кончился баланс кабинета — 4 дня тишины при статусе «идут показы»
        "story": lambda i, n: dict(
            views=0.0 if i >= n - 4 else 1.0,
            cpc=1.0, ctr=1.0, cr_cart=1.0, cr_order=1.0,
        ),
    },
    {
        "advert_id": 21400108,
        "name": "Аукцион · Рюкзак городской",
        "type": 9, "status": 9, "daily_budget": 2600,
        "articles": [(199204411, "Рюкзак городской, 20 л", 0.65),
                     (199204412, "Рюкзак городской, 25 л", 0.35)],
        "base": dict(views=15000, ctr=0.020, cr_cart=0.10, cr_order=0.31, cpc=8.5, price=2190),
        # Сюжет: ровная рабочая кампания, которую держит дневной лимит
        "story": lambda i, n: dict(views=1.0, cpc=1.0, ctr=1.0, cr_cart=1.0, cr_order=1.0),
        "cap_budget": True,
    },
    {
        "advert_id": 21400109,
        "name": "Авто · Носки, набор 5 пар",
        "type": 8, "status": 9, "daily_budget": 600,
        "articles": [(144870551, "Носки хлопок, набор 5 пар", 1.0)],
        "base": dict(views=350, ctr=0.014, cr_cart=0.08, cr_order=0.30, cpc=5.5, price=590),
        # Сюжет: ставка ниже рынка — кампания висит активной, но не откручивается
        "story": lambda i, n: dict(views=0.35, cpc=1.0, ctr=1.0, cr_cart=1.0, cr_order=1.0),
    },
    {
        "advert_id": 21400110,
        "name": "Аукцион · Гель для душа 750 мл",
        "type": 9, "status": 9, "daily_budget": 12000,
        "articles": [(212556033, "Гель для душа 750 мл", 1.0)],
        "base": dict(views=12000, ctr=0.021, cr_cart=0.09, cr_order=0.29, cpc=9.5, price=740),
        # Сюжет: подняли бюджет — расход вырос, заказы стоят на месте
        "story": lambda i, n: dict(
            views=1.75 if _last_week(i, n) else 1.0,
            cpc=1.25 if _last_week(i, n) else 1.0,
            cr_order=0.55 if _last_week(i, n) else 1.0,
            ctr=1.0, cr_cart=1.0,
        ),
    },
    {
        "advert_id": 21400111,
        "name": "Аукцион · Зимняя шапка (архив)",
        "type": 9, "status": 11, "daily_budget": 1500,
        "articles": [(133445566, "Шапка вязаная, шерсть", 1.0)],
        "base": dict(views=6000, ctr=0.020, cr_cart=0.10, cr_order=0.30, cpc=8.0, price=1190),
        # Сюжет: кампания на паузе — в отчёте должна быть, но без паники
        "story": lambda i, n: dict(
            views=0.0 if i >= n - 20 else 1.0,
            cpc=1.0, ctr=1.0, cr_cart=1.0, cr_order=1.0,
        ),
    },
]


def _jitter(rng: random.Random, low: float = 0.85, high: float = 1.15) -> float:
    return rng.uniform(low, high)


def _weekday_factor(day: date) -> float:
    """Выходные на WB обычно чуть активнее буднего вторника."""
    return {0: 0.97, 1: 0.95, 2: 0.98, 3: 1.0, 4: 1.06, 5: 1.12, 6: 1.08}[day.weekday()]


def generate(conn: sqlite3.Connection, days: int = 60,
             end: date | None = None) -> dict[str, int]:
    """Наполняет базу демо-историей за `days` дней до `end` включительно."""
    rng = random.Random(SEED)
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    now = datetime.now().isoformat(timespec="seconds")

    campaign_rows = []
    daily_rows: list[dict[str, Any]] = []
    nm_rows: list[dict[str, Any]] = []

    for spec in CAMPAIGNS:
        campaign_rows.append({
            "advert_id": spec["advert_id"],
            "name": spec["name"],
            "type": spec["type"],
            "type_name": {8: "Автоматическая", 9: "Аукцион"}.get(spec["type"], "—"),
            "status": spec["status"],
            "status_name": {9: "Идут показы", 11: "На паузе"}.get(spec["status"], "—"),
            "daily_budget": spec["daily_budget"],
            "create_time": (start - timedelta(days=30)).isoformat(),
            "change_time": now,
            "start_time": (start - timedelta(days=30)).isoformat(),
            "end_time": None,
            "updated_at": now,
        })

        for i in range(days):
            day = start + timedelta(days=i)
            base = spec["base"]
            mod: Callable[[int, int], dict[str, float]] = spec["story"]
            m = mod(i, days)

            views = base["views"] * m.get("views", 1.0) * _weekday_factor(day) * _jitter(rng)
            ctr = base["ctr"] * m.get("ctr", 1.0) * _jitter(rng, 0.9, 1.1)
            cpc = base["cpc"] * m.get("cpc", 1.0) * _jitter(rng, 0.93, 1.07)
            cr_cart = base["cr_cart"] * m.get("cr_cart", 1.0) * _jitter(rng, 0.85, 1.15)
            cr_order = base["cr_order"] * m.get("cr_order", 1.0) * _jitter(rng, 0.8, 1.2)

            views_i = max(0, int(round(views)))
            clicks = int(round(views_i * ctr))
            atbs = int(round(clicks * cr_cart))
            orders = int(round(atbs * cr_order))
            shks = int(round(orders * rng.uniform(1.0, 1.25)))
            spend = round(clicks * cpc, 2)

            # Дневной бюджет — жёсткий потолок, как в кабинете
            if spec.get("cap_budget") and spend > spec["daily_budget"]:
                ratio = spec["daily_budget"] / spend
                spend = float(spec["daily_budget"])
                clicks = int(round(clicks * ratio))
                atbs = int(round(atbs * ratio))
                orders = int(round(orders * ratio))
                shks = int(round(shks * ratio))

            revenue = round(orders * base["price"] * _jitter(rng, 0.95, 1.08), 2)

            daily_rows.append({
                "advert_id": spec["advert_id"], "date": day.isoformat(),
                "views": views_i, "clicks": clicks, "atbs": atbs, "orders": orders,
                "shks": shks, "spend": spend, "revenue": revenue,
                "collected_at": now,
            })

            # Разносим день по артикулам. Доли по воронке намеренно разные:
            # у одного артикула лучше кликабельность, у другого дешевле клик,
            # у третьего хуже выкуп — иначе совет «отключите худший артикул»
            # не на чем показать.
            for idx, (nm_id, nm_name, share) in enumerate(spec["articles"]):
                lead = idx == 0
                click_k = 1.12 if lead else 0.84      # разный CTR
                bid_k = 1.06 if lead else 0.92        # разный CPC
                buy_k = 1.0 if lead else rng.uniform(0.62, 0.9)  # разный выкуп
                nm_rows.append({
                    "advert_id": spec["advert_id"], "date": day.isoformat(),
                    "nm_id": nm_id, "name": nm_name,
                    "views": int(views_i * share),
                    "clicks": int(clicks * share * click_k),
                    "atbs": int(atbs * share * click_k),
                    "orders": int(orders * share * buy_k),
                    "shks": int(shks * share * buy_k),
                    "spend": round(spend * share * bid_k, 2),
                    "revenue": round(revenue * share * buy_k, 2),
                })

    log_id = db.start_collect(conn, "demo", now, start.isoformat(), end.isoformat())
    db.upsert_campaigns(conn, campaign_rows)
    db.upsert_daily(conn, daily_rows)
    db.upsert_nm_daily(conn, nm_rows)
    db.save_balance(conn, now, balance=18400.0, bonus=2300.0, net=20700.0)
    db.finish_collect(conn, log_id, datetime.now().isoformat(timespec="seconds"),
                      len(campaign_rows), len(daily_rows))
    conn.commit()

    return {
        "campaigns": len(campaign_rows),
        "days": days,
        "rows": len(daily_rows),
        "nm_rows": len(nm_rows),
    }
