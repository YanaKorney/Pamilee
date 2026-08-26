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
