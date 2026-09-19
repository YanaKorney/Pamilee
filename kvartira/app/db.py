"""База данных: SQLite, один файл data/app.db.

Соглашение по всему проекту: все размеры хранятся в ЦЕЛЫХ МИЛЛИМЕТРАХ.
Так не накапливается ошибка округления и невозможно перепутать метры
с сантиметрами. В метры переводим только при показе на экране.

Система координат плана: X вправо, Y вниз (как на чертеже), начало — в
левом верхнем углу габаритного прямоугольника квартиры.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH, ensure_dirs
from .errors import database_broken

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Проект = одна квартира.
CREATE TABLE IF NOT EXISTS projects (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL,
    ceiling_height_mm  INTEGER NOT NULL DEFAULT 2900,
    declared_area_m2   REAL,              -- общая площадь по документам
    scale_mm_per_unit  REAL,              -- масштаб чертежа, когда определён
    note               TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

-- Загруженные файлы: планы, референсы, фото материалов, готовые картинки.
CREATE TABLE IF NOT EXISTS files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    room_id        INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    kind           TEXT    NOT NULL,      -- plan | reference | material | render
    original_name  TEXT    NOT NULL,
    stored_name    TEXT    NOT NULL,
    mime           TEXT    NOT NULL,
    page_no        INTEGER,               -- номер страницы для PDF
    preview_name   TEXT,                  -- PNG-превью страницы
    label          TEXT    NOT NULL DEFAULT '',
    width_px       INTEGER,
    height_px      INTEGER,
    is_vector      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);

-- Помещения.
CREATE TABLE IF NOT EXISTS rooms (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name               TEXT    NOT NULL,
    kind               TEXT    NOT NULL DEFAULT 'room',
    polygon            TEXT    NOT NULL DEFAULT '[]',   -- JSON [[x,y],...] в мм
    declared_area_m2   REAL,                            -- площадь из экспликации
    ceiling_height_mm  INTEGER,                         -- если отличается от квартиры
    floor_material     TEXT    NOT NULL DEFAULT '',
    wall_material      TEXT    NOT NULL DEFAULT '',
    ceiling_material   TEXT    NOT NULL DEFAULT '',
    want               TEXT    NOT NULL DEFAULT '',     -- «что я хочу получить»
    avoid              TEXT    NOT NULL DEFAULT '',     -- «чего я не хочу»
    sort_order         INTEGER NOT NULL DEFAULT 0
);

-- Стены: отрезок с толщиной и высотой.
CREATE TABLE IF NOT EXISTS walls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    x1            INTEGER NOT NULL,
    y1            INTEGER NOT NULL,
    x2            INTEGER NOT NULL,
    y2            INTEGER NOT NULL,
    thickness_mm  INTEGER NOT NULL DEFAULT 120,
    height_mm     INTEGER,
    kind          TEXT    NOT NULL DEFAULT 'inner'   -- outer | inner | glass
);

-- Проёмы в стенах: двери и окна.
CREATE TABLE IF NOT EXISTS openings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wall_id    INTEGER NOT NULL REFERENCES walls(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,          -- door | window | arch
    offset_mm  INTEGER NOT NULL,          -- от начала стены
    width_mm   INTEGER NOT NULL,
    height_mm  INTEGER NOT NULL,
    sill_mm    INTEGER NOT NULL DEFAULT 0,
    swing      TEXT    NOT NULL DEFAULT ''
);

-- Предметы: мебель, сантехника, техника, свет.
CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    room_id        INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    category       TEXT    NOT NULL,      -- furniture|plumbing|appliance|light|decor
    subtype        TEXT    NOT NULL,      -- bed|sofa|table|sink|fridge|...
    label          TEXT    NOT NULL DEFAULT '',
    x              INTEGER NOT NULL,      -- центр предмета, мм
    y              INTEGER NOT NULL,
    z_mm           INTEGER NOT NULL DEFAULT 0,   -- высота установки над полом
    rotation_deg   REAL    NOT NULL DEFAULT 0,
    width_mm       INTEGER NOT NULL,
    depth_mm       INTEGER NOT NULL,
    height_mm      INTEGER NOT NULL,
    color          TEXT    NOT NULL DEFAULT '',
    material       TEXT    NOT NULL DEFAULT '',
    photo_file_id  INTEGER REFERENCES files(id) ON DELETE SET NULL,
    confidence     REAL    NOT NULL DEFAULT 1.0,  -- ниже 0.7 подсвечиваем жёлтым
    confirmed      INTEGER NOT NULL DEFAULT 0
);

-- Ракурсы съёмки для визуализации.
CREATE TABLE IF NOT EXISTS viewpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    z           INTEGER NOT NULL DEFAULT 1600,
    yaw_deg     REAL    NOT NULL DEFAULT 0,
    pitch_deg   REAL    NOT NULL DEFAULT 0,
    fov_deg     REAL    NOT NULL DEFAULT 60,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- Визуализации и их версии.
CREATE TABLE IF NOT EXISTS renders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id       INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    viewpoint_id  INTEGER REFERENCES viewpoints(id) ON DELETE SET NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL DEFAULT 'pending',    -- pending|done|failed
    strictness    TEXT    NOT NULL DEFAULT 'balanced',   -- exact|balanced|beautiful
    prompt        TEXT    NOT NULL DEFAULT '',
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    base_image    TEXT,      -- кадр из 3D
    depth_image   TEXT,      -- карта глубины
    result_image  TEXT,      -- итоговая картинка
    cost_usd      REAL    NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);

-- Счётчик расходов на AI.
CREATE TABLE IF NOT EXISTS spend (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL,          -- ГГГГ-ММ-ДД
    kind        TEXT    NOT NULL,          -- plan | image
    amount_usd  REAL    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

-- Настройки программы, которые меняются из интерфейса
-- (в отличие от .env, который правится руками).
CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_project    ON files(project_id);
CREATE INDEX IF NOT EXISTS idx_files_room       ON files(room_id);
CREATE INDEX IF NOT EXISTS idx_rooms_project    ON rooms(project_id);
CREATE INDEX IF NOT EXISTS idx_walls_project    ON walls(project_id);
CREATE INDEX IF NOT EXISTS idx_openings_wall    ON openings(wall_id);
CREATE INDEX IF NOT EXISTS idx_items_project    ON items(project_id);
CREATE INDEX IF NOT EXISTS idx_items_room       ON items(room_id);
CREATE INDEX IF NOT EXISTS idx_viewpoints_room  ON viewpoints(room_id);
CREATE INDEX IF NOT EXISTS idx_renders_room     ON renders(room_id);
CREATE INDEX IF NOT EXISTS idx_spend_day        ON spend(day);
"""


def now() -> str:
    """Текущее время в виде строки, одинаково во всей программе."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Открывает базу, отдаёт соединение, сам закрывает и сохраняет."""
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт таблицы при первом запуске. Повторный вызов безопасен."""
    try:
        with connect() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
    except sqlite3.DatabaseError as exc:
        # Повреждённый файл базы не должен показывать человеку
        # техническую ошибку — только понятную инструкцию.
        raise database_broken(DB_PATH) from exc


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ── Проекты ───────────────────────────────────────────────────────────────

def list_projects() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM files f
                     WHERE f.project_id = p.id AND f.kind = 'plan') AS plan_files,
                   (SELECT COUNT(*) FROM rooms r WHERE r.project_id = p.id) AS room_count
              FROM projects p
             ORDER BY p.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row_to_dict(row)


def create_project(
    name: str,
    ceiling_height_mm: int,
    declared_area_m2: float | None = None,
) -> int:
    stamp = now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (name, ceiling_height_mm, declared_area_m2,
                                  created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, ceiling_height_mm, declared_area_m2, stamp, stamp),
        )
        return int(cur.lastrowid)


def update_project(project_id: int, **fields: Any) -> bool:
    allowed = {"name", "ceiling_height_mm", "declared_area_m2", "scale_mm_per_unit", "note"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return False
    changes["updated_at"] = now()
    assignments = ", ".join(f"{k} = ?" for k in changes)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE projects SET {assignments} WHERE id = ?",
            (*changes.values(), project_id),
        )
        return cur.rowcount > 0


def delete_project(project_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0


def touch_project(project_id: int) -> None:
    """Отмечает, что проект менялся — чтобы он поднялся в списке."""
    with connect() as conn:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), project_id))


# ── Файлы ─────────────────────────────────────────────────────────────────

def add_file(
    project_id: int,
    kind: str,
    original_name: str,
    stored_name: str,
    mime: str,
    page_no: int | None = None,
    preview_name: str | None = None,
    label: str = "",
    width_px: int | None = None,
    height_px: int | None = None,
    is_vector: bool = False,
    room_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO files (project_id, room_id, kind, original_name, stored_name,
                               mime, page_no, preview_name, label, width_px, height_px,
                               is_vector, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, room_id, kind, original_name, stored_name, mime, page_no,
             preview_name, label, width_px, height_px, int(is_vector), now()),
        )
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), project_id))
        return int(cur.lastrowid)


def list_files(project_id: int, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM files WHERE project_id = ?"
    params: list[Any] = [project_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY created_at, page_no, id"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_file(file_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        )


def file_group(project_id: int, stored_name: str) -> list[dict[str, Any]]:
    """Все строки одного загруженного документа (у PDF это его страницы)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM files WHERE project_id = ? AND stored_name = ? ORDER BY page_no",
            (project_id, stored_name),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_file_group(project_id: int, stored_name: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM files WHERE project_id = ? AND stored_name = ?",
            (project_id, stored_name),
        )
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), project_id))
        return cur.rowcount


def set_file_label(file_id: int, label: str) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE files SET label = ? WHERE id = ?", (label, file_id))
        return cur.rowcount > 0


def project_stats(project_id: int) -> dict[str, int]:
    """Сколько в проекте документов, страниц и комнат."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(DISTINCT stored_name) FROM files
                WHERE project_id = ? AND kind = 'plan')  AS plan_files,
              (SELECT COUNT(*) FROM files
                WHERE project_id = ? AND kind = 'plan')  AS plan_pages,
              (SELECT COUNT(*) FROM files
                WHERE project_id = ? AND kind = 'reference') AS reference_files,
              (SELECT COUNT(*) FROM rooms WHERE project_id = ?) AS room_count
            """,
            (project_id, project_id, project_id, project_id),
        ).fetchone()
    return {k: int(row[k]) for k in row.keys()}


# ── Настройки, меняемые из интерфейса ─────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── Комнаты, проёмы и предметы ────────────────────────────────────────────

def clear_recognised(project_id: int) -> None:
    """Убирает прошлый разбор плана перед новым.

    Правки пользователя пока не защищаем: разбор — черновик, который
    он подтверждает на следующем шаге. Когда появится экран проверки,
    здесь будет сохранение подтверждённого.
    """
    with connect() as conn:
        conn.execute("DELETE FROM items WHERE project_id = ?", (project_id,))
        conn.execute(
            "DELETE FROM openings WHERE wall_id IN "
            "(SELECT id FROM walls WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute("DELETE FROM walls WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM rooms WHERE project_id = ?", (project_id,))


def add_room(
    project_id: int,
    name: str,
    kind: str,
    polygon: str,
    declared_area_m2: float | None = None,
    ceiling_height_mm: int | None = None,
    sort_order: int = 0,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO rooms (project_id, name, kind, polygon, declared_area_m2,
                               ceiling_height_mm, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, name, kind, polygon, declared_area_m2,
             ceiling_height_mm, sort_order),
        )
        return int(cur.lastrowid)


def list_rooms(project_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rooms WHERE project_id = ? ORDER BY sort_order, id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_item(
    project_id: int,
    category: str,
    subtype: str,
    label: str,
    x: int,
    y: int,
    width_mm: int,
    depth_mm: int,
    height_mm: int,
    rotation_deg: float = 0.0,
    room_id: int | None = None,
    confidence: float = 1.0,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO items (project_id, room_id, category, subtype, label,
                               x, y, rotation_deg, width_mm, depth_mm, height_mm,
                               confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, room_id, category, subtype, label, x, y, rotation_deg,
             width_mm, depth_mm, height_mm, confidence),
        )
        return int(cur.lastrowid)


def list_items(project_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE project_id = ? ORDER BY id", (project_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_wall(
    project_id: int,
    x1: int, y1: int, x2: int, y2: int,
    thickness_mm: int = 120,
    kind: str = "inner",
    height_mm: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO walls (project_id, x1, y1, x2, y2, thickness_mm, height_mm, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, x1, y1, x2, y2, thickness_mm, height_mm, kind),
        )
        return int(cur.lastrowid)


def add_opening(
    wall_id: int,
    kind: str,
    offset_mm: int,
    width_mm: int,
    height_mm: int,
    sill_mm: int = 0,
    swing: str = "",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO openings (wall_id, kind, offset_mm, width_mm, height_mm,
                                  sill_mm, swing)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (wall_id, kind, offset_mm, width_mm, height_mm, sill_mm, swing),
        )
        return int(cur.lastrowid)


def list_walls(project_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        walls = [dict(r) for r in conn.execute(
            "SELECT * FROM walls WHERE project_id = ? ORDER BY id", (project_id,)
        ).fetchall()]
        for wall in walls:
            wall["openings"] = [dict(r) for r in conn.execute(
                "SELECT * FROM openings WHERE wall_id = ? ORDER BY offset_mm",
                (wall["id"],),
            ).fetchall()]
    return walls
