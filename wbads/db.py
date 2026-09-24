"""Хранилище: SQLite + схема + миграции.

Wildberries не хранит историю рекламной статистики сколько-нибудь долго,
поэтому каждый сбор мы складываем к себе с меткой даты — так копится
динамика, по которой и видно, что кампания «поехала».
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

SCHEMA = """
-- Справочник кампаний (обновляется на каждом сборе)
CREATE TABLE IF NOT EXISTS campaigns (
    advert_id     INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL DEFAULT '',
    type          INTEGER,
    type_name     TEXT,
    status        INTEGER,
    status_name   TEXT,
    daily_budget  REAL    DEFAULT 0,
    create_time   TEXT,
    change_time   TEXT,
    start_time    TEXT,
    end_time      TEXT,
    updated_at    TEXT    NOT NULL
);

-- Статистика кампании по дням — основа всей динамики
CREATE TABLE IF NOT EXISTS campaign_daily (
    advert_id  INTEGER NOT NULL,
    date       TEXT    NOT NULL,           -- YYYY-MM-DD
    views      INTEGER NOT NULL DEFAULT 0, -- показы
    clicks     INTEGER NOT NULL DEFAULT 0, -- клики
    atbs       INTEGER NOT NULL DEFAULT 0, -- добавлений в корзину
    orders     INTEGER NOT NULL DEFAULT 0, -- заказов
    shks       INTEGER NOT NULL DEFAULT 0, -- заказано штук
    spend      REAL    NOT NULL DEFAULT 0, -- расход, руб.
    revenue    REAL    NOT NULL DEFAULT 0, -- выручка с рекламы, руб.
    collected_at TEXT  NOT NULL,
    PRIMARY KEY (advert_id, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON campaign_daily(date);

-- Та же статистика в разрезе артикулов: показывает, какой товар
-- внутри кампании тянет её вниз
CREATE TABLE IF NOT EXISTS campaign_nm_daily (
    advert_id INTEGER NOT NULL,
    date      TEXT    NOT NULL,
    nm_id     INTEGER NOT NULL,
    name      TEXT    DEFAULT '',
    views     INTEGER NOT NULL DEFAULT 0,
    clicks    INTEGER NOT NULL DEFAULT 0,
    atbs      INTEGER NOT NULL DEFAULT 0,
    orders    INTEGER NOT NULL DEFAULT 0,
    shks      INTEGER NOT NULL DEFAULT 0,
    spend     REAL    NOT NULL DEFAULT 0,
    revenue   REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (advert_id, date, nm_id)
);
CREATE INDEX IF NOT EXISTS idx_nm_date ON campaign_nm_daily(date);

-- Снимки баланса рекламного кабинета
CREATE TABLE IF NOT EXISTS balance_snapshots (
    taken_at TEXT PRIMARY KEY,
    balance  REAL DEFAULT 0,   -- счёт
    bonus    REAL DEFAULT 0,   -- бонусы
    net      REAL DEFAULT 0    -- баланс
);

-- Журнал изменений: что менеджер сделал в рекламе и когда.
-- Без него динамика отвечает «стало хуже», но не отвечает «после чего».
-- Привязка к артикулу, к кампании или к обоим сразу: правку ставки делают
-- в кампании, а следят за ней по товару, и запрещать любую из связок
-- значило бы навязывать чужой порядок работы.
CREATE TABLE IF NOT EXISTS changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT    NOT NULL,          -- YYYY-MM-DD, день изменения
    nm_id      INTEGER,                   -- артикул WB, если правка про товар
    advert_id  INTEGER,                   -- кампания, если правка про неё
    text       TEXT    NOT NULL,          -- что именно сделали, своими словами
    source     TEXT    NOT NULL DEFAULT 'ручная запись',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_date ON changes(date);
CREATE INDEX IF NOT EXISTS idx_changes_nm ON changes(nm_id, date);
CREATE INDEX IF NOT EXISTS idx_changes_advert ON changes(advert_id, date);

-- Журнал сборов: видно, когда данные обновлялись и не упало ли что-то
CREATE TABLE IF NOT EXISTS collect_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT NOT NULL,      -- 'wb-api' | 'demo'
    date_from   TEXT,
    date_to     TEXT,
    campaigns   INTEGER DEFAULT 0,
    rows        INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'running',
    error       TEXT
);

-- Заказы кабинета — построчно, а не агрегатами.
-- Так отмена, приехавшая на следующий день, исправляет уже собранный день:
-- строка обновляется по своему srid. С агрегатами это было бы невозможно.
CREATE TABLE IF NOT EXISTS orders_raw (
    srid             TEXT    PRIMARY KEY,   -- уникальный идентификатор заказа в WB
    date             TEXT    NOT NULL,      -- дата заказа, YYYY-MM-DD
    last_change      TEXT,
    nm_id            INTEGER NOT NULL,
    supplier_article TEXT,
    brand            TEXT,
    subject          TEXT,
    warehouse        TEXT,
    region           TEXT,
    total_price      REAL    DEFAULT 0,     -- цена до скидки продавца
    discount_percent REAL    DEFAULT 0,
    price_with_disc  REAL    DEFAULT 0,     -- сумма заказа после скидки продавца
    finished_price   REAL    DEFAULT 0,     -- что фактически заплатил покупатель (с СПП)
    is_cancel        INTEGER DEFAULT 0,
    cancel_date      TEXT,
    collected_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders_raw(date);
CREATE INDEX IF NOT EXISTS idx_orders_nm ON orders_raw(date, nm_id);

-- Пороги и настройки, изменённые из дашборда
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = init_db(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── запись ────────────────────────────────────────────────────────────────

def upsert_campaigns(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    sql = """
        INSERT INTO campaigns (advert_id, name, type, type_name, status, status_name,
                               daily_budget, create_time, change_time, start_time,
                               end_time, updated_at)
        VALUES (:advert_id, :name, :type, :type_name, :status, :status_name,
                :daily_budget, :create_time, :change_time, :start_time,
                :end_time, :updated_at)
        ON CONFLICT(advert_id) DO UPDATE SET
            name=excluded.name, type=excluded.type, type_name=excluded.type_name,
            status=excluded.status, status_name=excluded.status_name,
            daily_budget=excluded.daily_budget, change_time=excluded.change_time,
            start_time=excluded.start_time, end_time=excluded.end_time,
            updated_at=excluded.updated_at
    """
    rows = list(rows)
    conn.executemany(sql, rows)
    return len(rows)


def upsert_daily(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    sql = """
        INSERT INTO campaign_daily (advert_id, date, views, clicks, atbs, orders,
                                    shks, spend, revenue, collected_at)
        VALUES (:advert_id, :date, :views, :clicks, :atbs, :orders,
                :shks, :spend, :revenue, :collected_at)
        ON CONFLICT(advert_id, date) DO UPDATE SET
            views=excluded.views, clicks=excluded.clicks, atbs=excluded.atbs,
            orders=excluded.orders, shks=excluded.shks, spend=excluded.spend,
            revenue=excluded.revenue, collected_at=excluded.collected_at
    """
    rows = list(rows)
    conn.executemany(sql, rows)
    return len(rows)


def upsert_nm_daily(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    sql = """
        INSERT INTO campaign_nm_daily (advert_id, date, nm_id, name, views, clicks,
                                       atbs, orders, shks, spend, revenue)
        VALUES (:advert_id, :date, :nm_id, :name, :views, :clicks,
                :atbs, :orders, :shks, :spend, :revenue)
        ON CONFLICT(advert_id, date, nm_id) DO UPDATE SET
            name=excluded.name, views=excluded.views, clicks=excluded.clicks,
            atbs=excluded.atbs, orders=excluded.orders, shks=excluded.shks,
            spend=excluded.spend, revenue=excluded.revenue
    """
    rows = list(rows)
    conn.executemany(sql, rows)
    return len(rows)


def upsert_orders(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Пишет заказы по ключу srid: повторный сбор обновляет, а не дублирует.

    Именно так до нас доезжают отмены — заказ приходит второй раз
    с is_cancel = 1 и перезаписывает прежнюю строку.
    """
    sql = """
        INSERT INTO orders_raw (srid, date, last_change, nm_id, supplier_article,
                                brand, subject, warehouse, region, total_price,
                                discount_percent, price_with_disc, finished_price,
                                is_cancel, cancel_date, collected_at)
        VALUES (:srid, :date, :last_change, :nm_id, :supplier_article,
                :brand, :subject, :warehouse, :region, :total_price,
                :discount_percent, :price_with_disc, :finished_price,
                :is_cancel, :cancel_date, :collected_at)
        ON CONFLICT(srid) DO UPDATE SET
            date=excluded.date, last_change=excluded.last_change,
            nm_id=excluded.nm_id, supplier_article=excluded.supplier_article,
            brand=excluded.brand, subject=excluded.subject,
            warehouse=excluded.warehouse, region=excluded.region,
            total_price=excluded.total_price,
            discount_percent=excluded.discount_percent,
            price_with_disc=excluded.price_with_disc,
            finished_price=excluded.finished_price,
            is_cancel=excluded.is_cancel, cancel_date=excluded.cancel_date,
            collected_at=excluded.collected_at
    """
    rows = list(rows)
    conn.executemany(sql, rows)
    return len(rows)


def save_balance(conn: sqlite3.Connection, taken_at: str, balance: float,
                 bonus: float, net: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO balance_snapshots (taken_at, balance, bonus, net)"
        " VALUES (?,?,?,?)",
        (taken_at, balance, bonus, net),
    )


def start_collect(conn: sqlite3.Connection, source: str, started_at: str,
                  date_from: str, date_to: str) -> int:
    cur = conn.execute(
        "INSERT INTO collect_log (started_at, source, date_from, date_to)"
        " VALUES (?,?,?,?)",
        (started_at, source, date_from, date_to),
    )
    return int(cur.lastrowid)


def finish_collect(conn: sqlite3.Connection, log_id: int, finished_at: str,
                   campaigns: int, rows: int, status: str = "ok",
                   error: str | None = None) -> None:
    conn.execute(
        "UPDATE collect_log SET finished_at=?, campaigns=?, rows=?, status=?, error=?"
        " WHERE id=?",
        (finished_at, campaigns, rows, status, error, log_id),
    )


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
    )


# ── чтение ────────────────────────────────────────────────────────────────

def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def list_campaigns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM campaigns ORDER BY name"))


def daily_rows(conn: sqlite3.Connection, date_from: str, date_to: str,
               advert_ids: Sequence[int] | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM campaign_daily WHERE date BETWEEN ? AND ?"
    params: list = [date_from, date_to]
    if advert_ids:
        placeholders = ",".join("?" * len(advert_ids))
        sql += f" AND advert_id IN ({placeholders})"
        params.extend(advert_ids)
    sql += " ORDER BY date"
    return list(conn.execute(sql, params))


def nm_rows(conn: sqlite3.Connection, advert_id: int, date_from: str,
            date_to: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM campaign_nm_daily WHERE advert_id=? AND date BETWEEN ? AND ?"
        " ORDER BY date",
        (advert_id, date_from, date_to),
    ))


def orders_by_nm(conn: sqlite3.Connection, date_from: str, date_to: str,
                 price_field: str = "price_with_disc") -> list[sqlite3.Row]:
    """Заказы, свёрнутые по «артикул + день». Отменённые не считаем.

    price_field выбирает, какую цену брать за сумму заказа:
    price_with_disc — после скидки продавца (по умолчанию, сопоставимо
    с рекламным отчётом), finished_price — что заплатил покупатель с учётом СПП.
    """
    if price_field not in ("price_with_disc", "finished_price", "total_price"):
        price_field = "price_with_disc"
    return list(conn.execute(f"""
        SELECT date, nm_id,
               COUNT(*)            AS orders,
               SUM({price_field})  AS revenue,
               MAX(supplier_article) AS supplier_article,
               MAX(brand)          AS brand,
               MAX(subject)        AS subject
        FROM orders_raw
        WHERE date BETWEEN ? AND ? AND is_cancel = 0
        GROUP BY date, nm_id
    """, (date_from, date_to)))


def orders_totals(conn: sqlite3.Connection, date_from: str, date_to: str,
                  price_field: str = "price_with_disc") -> dict[str, float]:
    """Итог по кабинету за период: заказы, их сумма и отмены."""
    if price_field not in ("price_with_disc", "finished_price", "total_price"):
        price_field = "price_with_disc"
    row = conn.execute(f"""
        SELECT
            SUM(CASE WHEN is_cancel = 0 THEN 1 ELSE 0 END)             AS orders,
            SUM(CASE WHEN is_cancel = 0 THEN {price_field} ELSE 0 END) AS revenue,
            SUM(CASE WHEN is_cancel = 1 THEN 1 ELSE 0 END)             AS cancels,
            SUM(CASE WHEN is_cancel = 1 THEN {price_field} ELSE 0 END) AS cancel_revenue
        FROM orders_raw WHERE date BETWEEN ? AND ?
    """, (date_from, date_to)).fetchone()
    return {
        "orders": float(row["orders"] or 0),
        "revenue": float(row["revenue"] or 0),
        "cancels": float(row["cancels"] or 0),
        "cancel_revenue": float(row["cancel_revenue"] or 0),
    }


def has_orders(conn: sqlite3.Connection) -> bool:
    """Есть ли вообще данные о заказах — от этого зависит, показывать ли общий ДРР."""
    return conn.execute("SELECT 1 FROM orders_raw LIMIT 1").fetchone() is not None


def orders_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT MIN(date) AS lo, MAX(date) AS hi FROM orders_raw").fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)


def data_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi FROM campaign_daily"
    ).fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)


def last_collect(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM collect_log ORDER BY id DESC LIMIT 1"
    ).fetchone()


def latest_balance(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM balance_snapshots ORDER BY taken_at DESC LIMIT 1"
    ).fetchone()
