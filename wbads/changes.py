"""Журнал изменений в рекламе и замер того, что из них вышло.

Аналитика в динамике отвечает «стало хуже» и «вот где именно». Она не
отвечает на главный вопрос менеджера: «после чего стало хуже». Ответ на
него знает только человек, который эти изменения вносил, — и держит его в
своей таблице. Здесь эта запись живёт рядом с цифрами, и по каждой видно,
что изменилось в метриках.

Три решения, от которых зависит, можно ли доверять числу «эффект»:

1. **Среднее за день, а не сумма.** Окна «до» и «после» почти никогда не
   равны по длине: слева упирается в предыдущее изменение, справа — в
   сегодняшний день. Складывать суммы за 7 и за 3 дня и объявлять разницу
   эффектом — значит выдавать за результат длину окна.

2. **Окна обрезаются соседними изменениями.** Если через два дня после
   правки ставки вы почистили запросы, то дни после второй правки меряют
   уже её, а не первую. Без обрезки одно изменение приписывает себе
   результат другого.

3. **День изменения исключается.** Правку вносят посреди дня, и этот день
   наполовину старый, наполовину новый. Относить его к любой из сторон —
   значит подмешивать шум ровно в ту точку, где считается эффект.

И честность: совпадение во времени — не доказательство. Сервис показывает,
сколько изменений было рядом и насколько окно короткое, и не называет
причиной то, что всего лишь случилось раньше.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from .metrics import RAW_KEYS, derive, parse_date, safe_div

# Сколько дней смотрим до и после изменения. Неделя — чтобы сгладить
# разницу между буднями и выходными, которая на WB заметна.
DEFAULT_WINDOW = 7
# Меньше трёх дней после изменения — судить рано, и об этом надо сказать
# прямо, а не показывать бодрые проценты по одному дню.
MIN_DAYS_TO_JUDGE = 3
# Ниже этого сдвига вывод словами не делается: в таблице числа видны, а
# фраза «ДРР снизился» про десятые доли процента обещает больше, чем есть.
NOTABLE_CHANGE_PCT = 5.0

# Показатели, по которым считается эффект. Порядок — как в воронке, чтобы
# читалось сверху вниз: сначала откуда пришли, потом во что превратилось.
EFFECT_METRICS = [
    ("views", "Показы", "шт", "up"),
    ("clicks", "Клики", "шт", "up"),
    ("ctr", "CTR", "%", "up"),
    ("cpc", "Цена клика", "₽", "down"),
    ("atbs", "В корзину", "шт", "up"),
    ("cr_cart", "Клик → корзина", "%", "up"),
    ("orders", "Заказы", "шт", "up"),
    ("cr_order", "Корзина → заказ", "%", "up"),
    ("spend", "Расход", "₽", "flat"),
    ("revenue", "Выручка с рекламы", "₽", "up"),
    ("cpo", "Цена заказа", "₽", "down"),
    ("drr", "ДРР", "%", "down"),
]

# Показатели, которые складываются по дням; остальные считаются из них.
SUMMED = set(RAW_KEYS)


def add(conn: sqlite3.Connection, day: str, text: str,
        nm_id: int | None = None, advert_id: int | None = None,
        source: str = "ручная запись") -> int:
    """Записывает изменение. Возвращает его номер."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Не записано, что именно изменили")
    day = _as_day(day)
    if not day:
        raise ValueError("Не разобрана дата изменения")
    if nm_id is None and advert_id is None:
        raise ValueError("Укажите артикул или кампанию — иначе эффект "
                         "не с чем сопоставить")
    cur = conn.execute(
        "INSERT INTO changes (date, nm_id, advert_id, text, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (day, nm_id, advert_id, text, source,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_many(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]],
             source: str = "импорт") -> int:
    """Пакетная запись — для переноса истории из таблицы.

    Повторный импорт того же файла не должен плодить дубли: запись
    узнаётся по дате, артикулу и тексту.
    """
    written = 0
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        day = _as_day(row.get("date"))
        text = (row.get("text") or "").strip()
        if not day or not text:
            continue
        nm_id = row.get("nm_id")
        advert_id = row.get("advert_id")
        exists = conn.execute(
            "SELECT 1 FROM changes WHERE date = ? AND text = ?"
            " AND IFNULL(nm_id, -1) = ? AND IFNULL(advert_id, -1) = ?",
            (day, text, nm_id if nm_id is not None else -1,
             advert_id if advert_id is not None else -1),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO changes (date, nm_id, advert_id, text, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (day, nm_id, advert_id, text, row.get("source") or source, now),
        )
        written += 1
    conn.commit()
    return written


def delete(conn: sqlite3.Connection, change_id: int) -> bool:
    cur = conn.execute("DELETE FROM changes WHERE id = ?", (change_id,))
    conn.commit()
    return cur.rowcount > 0


def listing(conn: sqlite3.Connection, date_from: str = "", date_to: str = "",
            nm_id: int | None = None, advert_id: int | None = None,
            limit: int = 500) -> list[dict[str, Any]]:
    """Записи журнала, свежие сверху."""
    where, params = [], []
    if date_from:
        where.append("date >= ?")
        params.append(_as_day(date_from))
    if date_to:
        where.append("date <= ?")
        params.append(_as_day(date_to))
    if nm_id is not None:
        where.append("nm_id = ?")
        params.append(nm_id)
    if advert_id is not None:
        where.append("advert_id = ?")
        params.append(advert_id)
    sql = "SELECT * FROM changes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


# ── замер эффекта ─────────────────────────────────────────────────────────

def _as_day(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return parse_date(text).isoformat()
    except ValueError:
        return ""


def _totals(conn: sqlite3.Connection, day_from: str, day_to: str,
            nm_id: int | None, advert_id: int | None) -> dict[str, float]:
    """Суммы метрик за окно по артикулу и/или кампании.

    Артикул точнее кампании: правку делают в кампании, но смотрят на товар,
    а один товар может крутиться сразу в нескольких кампаниях.
    """
    if nm_id is not None:
        sql = ("SELECT " + ", ".join(f"SUM({k}) AS {k}" for k in RAW_KEYS) +
               " FROM campaign_nm_daily WHERE nm_id = ? AND date BETWEEN ? AND ?")
        params: list[Any] = [nm_id, day_from, day_to]
        if advert_id is not None:
            sql += " AND advert_id = ?"
            params.append(advert_id)
    else:
        sql = ("SELECT " + ", ".join(f"SUM({k}) AS {k}" for k in RAW_KEYS) +
               " FROM campaign_daily WHERE advert_id = ? AND date BETWEEN ? AND ?")
        params = [advert_id, day_from, day_to]
    row = conn.execute(sql, params).fetchone()
    return {key: float((row[key] if row else 0) or 0) for key in RAW_KEYS}


def _days_with_data(conn: sqlite3.Connection, day_from: str, day_to: str,
                    nm_id: int | None, advert_id: int | None) -> int:
    """Сколько дней в окне вообще есть в базе.

    Делить на длину окна нельзя: если сбор захватил не весь период, часть
    дней отсутствует, и среднее за день окажется заниженным ровно на долю
    несобранного.
    """
    if nm_id is not None:
        sql = ("SELECT COUNT(DISTINCT date) FROM campaign_nm_daily"
               " WHERE nm_id = ? AND date BETWEEN ? AND ?")
        params: list[Any] = [nm_id, day_from, day_to]
        if advert_id is not None:
            sql += " AND advert_id = ?"
            params.append(advert_id)
    else:
        sql = ("SELECT COUNT(DISTINCT date) FROM campaign_daily"
               " WHERE advert_id = ? AND date BETWEEN ? AND ?")
        params = [advert_id, day_from, day_to]
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _neighbours(conn: sqlite3.Connection,
                change: dict[str, Any]) -> tuple[str, str]:
    """Даты ближайших изменений по тому же объекту: до и после.

    Ими обрезаются окна. Без этого одно изменение приписывает себе
    результат соседнего — самая частая ошибка ручного разбора.
    """
    if change.get("nm_id") is not None:
        clause, params = "nm_id = ?", [change["nm_id"]]
    elif change.get("advert_id") is not None:
        clause, params = "advert_id = ?", [change["advert_id"]]
    else:
        return "", ""

    before = conn.execute(
        f"SELECT MAX(date) FROM changes WHERE {clause} AND date < ?",
        (*params, change["date"]),
    ).fetchone()[0]
    after = conn.execute(
        f"SELECT MIN(date) FROM changes WHERE {clause} AND date > ?",
        (*params, change["date"]),
    ).fetchone()[0]
    return before or "", after or ""


def effect(conn: sqlite3.Connection, change: dict[str, Any],
           window: int = DEFAULT_WINDOW, today: str = "") -> dict[str, Any]:
    """Что изменилось в метриках вокруг одной записи журнала."""
    day = parse_date(change["date"])
    last_day = parse_date(today) if today else date.today()

    prev_change, next_change = _neighbours(conn, change)

    # Окно «до»: заканчивается накануне правки, начинается не раньше
    # предыдущего изменения — иначе меряем чужой результат.
    before_to = (day - timedelta(days=1)).isoformat()
    before_from = (day - timedelta(days=window)).isoformat()
    if prev_change and prev_change >= before_from:
        before_from = (parse_date(prev_change) + timedelta(days=1)).isoformat()

    # Окно «после»: начинается назавтра, заканчивается либо через window
    # дней, либо накануне следующего изменения, либо сегодня.
    after_from = (day + timedelta(days=1)).isoformat()
    after_to = (day + timedelta(days=window)).isoformat()
    if next_change and next_change <= after_to:
        after_to = (parse_date(next_change) - timedelta(days=1)).isoformat()
    if after_to > last_day.isoformat():
        after_to = last_day.isoformat()

    nm_id = change.get("nm_id")
    advert_id = change.get("advert_id")

    result: dict[str, Any] = {
        "change_id": change.get("id"),
        "window": window,
        "before_period": [before_from, before_to],
        "after_period": [after_from, after_to],
        "cut_by_previous": bool(prev_change and prev_change >= (
            day - timedelta(days=window)).isoformat()),
        "cut_by_next": bool(next_change and next_change <= (
            day + timedelta(days=window)).isoformat()),
        "next_change": next_change,
        "metrics": [],
        "verdict": "",
        "tone": "early",
        "ready": False,
    }

    if after_from > after_to:
        result["verdict"] = "Изменение сделано только что — эффект ещё не виден."
        result["tone"] = "early"
        return result

    before_days = _days_with_data(conn, before_from, before_to, nm_id, advert_id)
    after_days = _days_with_data(conn, after_from, after_to, nm_id, advert_id)
    result["before_days"] = before_days
    result["after_days"] = after_days

    if not before_days or not after_days:
        result["verdict"] = ("Не с чем сравнивать: за одно из окон данных "
                             "в базе нет. Соберите статистику за больший период.")
        result["tone"] = "early"
        return result

    before = _per_day(_totals(conn, before_from, before_to, nm_id, advert_id),
                      before_days)
    after = _per_day(_totals(conn, after_from, after_to, nm_id, advert_id),
                     after_days)

    result["metrics"] = _compare(before, after)
    result["ready"] = after_days >= MIN_DAYS_TO_JUDGE
    result["verdict"] = _verdict(result, after_days)
    # Окраска вердикта считается здесь же, из того же порога: иначе
    # страница красит зелёным фразу «заметных сдвигов нет».
    result["tone"] = _tone(result)
    return result


def _tone(result: dict[str, Any]) -> str:
    """Каким цветом показать вывод: он обязан совпадать со словами."""
    if not result.get("ready"):
        return "early"
    drr = next((m for m in result["metrics"] if m["key"] == "drr"), None)
    if not drr or abs(drr.get("delta_pct") or 0) < NOTABLE_CHANGE_PCT:
        return "none"
    return "good" if drr["direction"] == "better" else "bad"


def _per_day(totals: dict[str, float], days: int) -> dict[str, float]:
    """Среднее за день и производные от него.

    Складывать окна разной длины и сравнивать суммы — значит мерить длину
    окна, а не результат правки.
    """
    if days <= 0:
        return derive({key: 0.0 for key in RAW_KEYS})
    return derive({key: value / days for key, value in totals.items()})


def _compare(before: dict[str, float], after: dict[str, float]) -> list[dict[str, Any]]:
    """Построчное «было → стало» с направлением, которое считается хорошим."""
    rows = []
    for key, title, unit, good in EFFECT_METRICS:
        was = float(before.get(key) or 0)
        now = float(after.get(key) or 0)
        change_pct = safe_div(now - was, abs(was)) * 100 if was else None
        rows.append({
            "key": key,
            "title": title,
            "unit": unit,
            "before": was,
            "after": now,
            "delta": now - was,
            "delta_pct": change_pct,
            "direction": _direction(was, now, good),
        })
    return rows


def _direction(was: float, now: float, good: str) -> str:
    """Стало лучше, хуже или всё равно — по смыслу показателя.

    Рост расхода сам по себе не плохо и не хорошо: важно, что он принёс.
    Поэтому у таких показателей направление не оценивается.
    """
    if good == "flat":
        return "neutral"
    # Меньше процента разницы — это шум, а не результат правки.
    if not was or abs(safe_div(now - was, abs(was))) < 0.01:
        return "same"
    better = now > was if good == "up" else now < was
    return "better" if better else "worse"


def _verdict(result: dict[str, Any], after_days: int) -> str:
    """Короткий вывод словами — и оговорка там, где она честна."""
    if after_days < MIN_DAYS_TO_JUDGE:
        return (f"После изменения прошло дней с данными: {after_days}. "
                "Для вывода мало — вернитесь к этой записи через несколько дней.")

    by_key = {row["key"]: row for row in result["metrics"]}
    drr = by_key.get("drr", {})
    orders = by_key.get("orders", {})

    def as_pct(value: float) -> str:
        return f"{value:.1f}".replace(".", ",") + "%"

    # Порог для вывода словами выше, чем для стрелки в таблице. «ДРР
    # снизился с 24,9% до 24,6%» звучит как результат, хотя это колебание
    # в пределах обычного дневного разброса. В таблице точные числа видны,
    # а фраза обязана говорить только о том, что заметно.
    def loud(metric: dict[str, Any]) -> bool:
        return abs(metric.get("delta_pct") or 0) >= NOTABLE_CHANGE_PCT

    parts = []
    if drr.get("direction") == "better" and loud(drr):
        parts.append(f"ДРР снизился с {as_pct(drr['before'])} "
                     f"до {as_pct(drr['after'])}")
    elif drr.get("direction") == "worse" and loud(drr):
        parts.append(f"ДРР вырос с {as_pct(drr['before'])} "
                     f"до {as_pct(drr['after'])}")
    if orders.get("direction") == "better" and loud(orders):
        parts.append("заказов в день стало больше")
    elif orders.get("direction") == "worse" and loud(orders):
        parts.append("заказов в день стало меньше")

    if not parts:
        return "Заметных сдвигов в метриках нет."
    text = ", ".join(parts) + "."
    if result.get("cut_by_next"):
        text += (" Окно обрезано следующим изменением — дальше меряется уже оно.")
    return text


def with_effects(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]],
                 window: int = DEFAULT_WINDOW,
                 today: str = "") -> list[dict[str, Any]]:
    """Записи журнала вместе с замером по каждой."""
    out = []
    for row in rows:
        item = dict(row)
        item["effect"] = effect(conn, row, window=window, today=today)
        out.append(item)
    return out


def crowding(conn: sqlite3.Connection, day: str, window: int = DEFAULT_WINDOW
             ) -> int:
    """Сколько ещё изменений пришлось на те же дни — по всему кабинету.

    Когда в один день правят десять кампаний, приписывать сдвиг общих
    показателей одной из них нельзя. Число рядом с записью честнее любых
    оговорок мелким шрифтом.
    """
    start = (parse_date(day) - timedelta(days=1)).isoformat()
    end = (parse_date(day) + timedelta(days=1)).isoformat()
    return int(conn.execute(
        "SELECT COUNT(*) FROM changes WHERE date BETWEEN ? AND ?",
        (start, end)).fetchone()[0] or 0)
