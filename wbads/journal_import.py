"""Перенос журнала изменений из таблицы Excel в базу сервиса.

Журнал без истории бесполезен: эффект правки виден только рядом с тем, что
было до неё. У человека эта история уже есть — месяцы записей в таблице, где
строки это артикулы, колонки это даты, а в ячейках своими словами написано,
что в тот день сделали. Начинать с пустого листа значило бы выбросить её.

Форма таблицы, под которую сделан разбор:

    | название | артикул   | … | 12.02 | 17.02 | 18.02 | …
    | паразит  | 160427990 | … | текст | текст |       | …

Колонки с датами идут после служебных, слева направо по возрастанию. На это
свойство опирается починка дат: в живой таблице шапка набита руками, и там
встречается «18.авг.» между 17 и 19 февраля — Excel подставил не тот месяц.
Выбросить такую колонку — потерять записи, поверить ей — сдвинуть их на
полгода. Поэтому день берётся из текста, а месяц и год — у соседей, и
каждая такая починка попадает в отчёт, чтобы её можно было проверить.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from .xlsx_read import XlsxError, as_date, column_letter, read_sheet

# Названия месяцев во всех видах, в которых их пишут руками.
MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

# Артикул WB — длинное число; пороги отсекают номера строк и проценты,
# случайно попавшие в колонку.
MIN_NM_ID = 1000
MAX_NM_ID = 10 ** 12

HEADER_HINTS = {"артикул", "nmid", "nm id", "номенклатура", "sku"}


@dataclass
class ImportReport:
    """Что получилось при переносе. Нужен, чтобы не переносить вслепую."""

    records: list[dict[str, Any]] = field(default_factory=list)
    articles: int = 0
    date_columns: int = 0
    repaired_dates: list[str] = field(default_factory=list)
    skipped_columns: list[str] = field(default_factory=list)
    skipped_rows: list[str] = field(default_factory=list)
    written: int = 0
    duplicates: int = 0

    def summary(self) -> list[str]:
        lines = [
            f"Записей найдено: {len(self.records)}",
            f"Артикулов: {self.articles}",
            f"Колонок с датами: {self.date_columns}",
        ]
        if self.repaired_dates:
            lines.append(f"Дат исправлено по соседним колонкам: "
                         f"{len(self.repaired_dates)}")
            for note in self.repaired_dates[:10]:
                lines.append(f"    {note}")
            if len(self.repaired_dates) > 10:
                lines.append(f"    …и ещё {len(self.repaired_dates) - 10}")
        if self.skipped_columns:
            lines.append(f"Колонок пропущено (не удалось понять дату): "
                         f"{len(self.skipped_columns)}")
            for note in self.skipped_columns[:5]:
                lines.append(f"    {note}")
        if self.skipped_rows:
            lines.append(f"Строк пропущено (нет артикула): {len(self.skipped_rows)}")
        return lines


def parse_header_date(value: Any) -> tuple[int | None, int | None, int | None]:
    """Из заголовка колонки — день, месяц, год. Неизвестное — None.

    Заголовки бывают датами Excel, бывают текстом («18.авг.», «2.апр.»,
    «02.03.»), а бывают текстом с припиской («29.05(сокращение ставок)»).
    Возвращаем то, что удалось прочесть, — остальное дополнится по соседям.
    """
    real = as_date(value)
    if real:
        return real.day, real.month, real.year

    text = str(value or "").strip().lower()
    if not text:
        return None, None, None

    match = re.match(r"(\d{1,2})\s*[.\-/\s]\s*(\d{1,2}|[а-яё]+)"
                     r"(?:\s*[.\-/\s]\s*(\d{2,4}))?", text)
    if not match:
        return None, None, None

    day = int(match.group(1))
    if not 1 <= day <= 31:
        return None, None, None

    raw_month = match.group(2)
    if raw_month.isdigit():
        month = int(raw_month)
        if not 1 <= month <= 12:
            month = None
    else:
        month = MONTHS.get(raw_month[:3])
        if month is None:
            month = MONTHS.get(raw_month)

    year = None
    if match.group(3):
        year = int(match.group(3))
        if year < 100:
            year += 2000
    return day, month, year


def _resolve_dates(headers: Sequence[tuple[int, Any]], report: ImportReport
                   ) -> dict[int, date]:
    """Колонка → дата, с починкой по соседям с обеих сторон.

    Колонки идут по возрастанию даты — это и есть источник истины там, где
    шапку набивали руками. Настоящим датам Excel верим как есть; текстовым
    заголовкам верим только в той части, которая не ломает порядок.

    Смотреть только на левого соседа недостаточно. «18.авг.» между 17 и
    19 февраля — прыжок ВПЕРЁД, порядок он не нарушает, и слева выглядит
    законно. Видно его лишь по правому соседу, который вдруг оказывается
    на полгода раньше. Поэтому дата текстового заголовка обязана попадать
    в промежуток между ближайшими надёжными соседями.
    """
    trusted: dict[int, date] = {}
    parsed: dict[int, tuple[int, int | None, int | None]] = {}
    order: list[int] = []

    for col, raw in headers:
        day, month, year = parse_header_date(raw)
        if day is None:
            report.skipped_columns.append(f"{column_letter(col)}: «{raw}»")
            continue
        order.append(col)
        real = as_date(raw)
        if real:
            trusted[col] = real
        else:
            parsed[col] = (day, month, year)

    resolved: dict[int, date] = dict(trusted)

    for position, col in enumerate(order):
        if col in resolved:
            continue
        day, month, year = parsed[col]
        left = _nearest(order, position, resolved, step=-1)
        right = _nearest(order, position, resolved, step=1)

        guess = _build(day, month, year, left or right)
        fixed = _fit_between(day, guess, left, right)
        if fixed is None:
            report.skipped_columns.append(
                f"{column_letter(col)}: не удалось согласовать дату с соседними")
            continue
        if guess is None or fixed != guess:
            report.repaired_dates.append(
                f"{column_letter(col)}: «{dict(headers)[col]}» → "
                f"{fixed.isoformat()} (по соседним колонкам)")
        resolved[col] = fixed
    return resolved


def _nearest(order: Sequence[int], position: int, known: dict[int, date],
             step: int) -> date | None:
    """Ближайшая уже известная дата слева (step=-1) или справа (step=1)."""
    index = position + step
    while 0 <= index < len(order):
        col = order[index]
        if col in known:
            return known[col]
        index += step
    return None


def _fit_between(day: int, guess: date | None, left: date | None,
                 right: date | None) -> date | None:
    """Дата с этим днём, которая укладывается между соседями.

    Если предположение уже укладывается — оставляем его. Иначе перебираем
    месяцы соседей: день в тексте почти всегда верен, ошибаются в месяце.
    """
    def fits(candidate: date | None) -> bool:
        if candidate is None:
            return False
        if left and candidate < left:
            return False
        if right and candidate > right:
            return False
        return True

    if fits(guess):
        return guess

    anchors = [a for a in (left, right) if a]
    for anchor in anchors:
        for shift in (0, 1, -1):
            month = anchor.month + shift
            year = anchor.year + (month - 1) // 12 if month > 0 else anchor.year - 1
            month = (month - 1) % 12 + 1 if month > 0 else 12
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if fits(candidate):
                return candidate
    # Соседей нет вовсе — принимаем как есть: спорить не с чем.
    if not anchors:
        return guess
    return None


def _build(day: int, month: int | None, year: int | None,
           last: date | None) -> date | None:
    """Собирает дату, дополняя неизвестное по левому соседу."""
    if month is None:
        month = last.month if last else None
    if month is None:
        return None
    if year is None:
        year = last.year if last else date.today().year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_after(day: int, last: date) -> date | None:
    """Ближайшая дата с этим днём, идущая не раньше предыдущей колонки."""
    for month_shift in range(0, 13):
        month = last.month + month_shift
        year = last.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= last:
            return candidate
    return None


def _find_article_column(rows: Sequence[Sequence[Any]]) -> int | None:
    """Колонка с артикулом: по названию в шапке, иначе по виду значений."""
    header = rows[0] if rows else []
    for index, value in enumerate(header):
        text = str(value or "").strip().lower()
        if text in HEADER_HINTS:
            return index

    # Шапку могли назвать иначе — тогда ищем колонку, где почти везде
    # стоят длинные числа. Это надёжнее, чем верить названию.
    best, best_score = None, 0
    for index in range(min(6, len(header))):
        score = 0
        for row in rows[1:]:
            if index >= len(row):
                continue
            value = row[index]
            if isinstance(value, int) and MIN_NM_ID < value < MAX_NM_ID:
                score += 1
        if score > best_score:
            best, best_score = index, score
    return best if best_score >= 3 else None


def read_journal(path: str, sheet: str | int = 0) -> ImportReport:
    """Разбирает таблицу в записи журнала. В базу ничего не пишет."""
    rows = read_sheet(path, sheet)
    report = ImportReport()
    if len(rows) < 2:
        raise XlsxError("В таблице нет данных: нужна шапка с датами "
                        "и хотя бы одна строка с артикулом")

    article_col = _find_article_column(rows)
    if article_col is None:
        raise XlsxError(
            "Не нашлась колонка с артикулами. Назовите её «артикул» "
            "в первой строке — и повторите."
        )

    header = rows[0]
    # Колонки с датами идут после служебных: первой считаем ту, что правее
    # артикула и разбирается как дата.
    candidates = [(index + 1, header[index])
                  for index in range(article_col + 1, len(header))
                  if header[index] is not None]
    dates = _resolve_dates(candidates, report)
    report.date_columns = len(dates)
    if not dates:
        raise XlsxError("В шапке не нашлось ни одной даты. Ожидается, что "
                        "колонки правее артикула названы датами.")

    name_col = 0 if article_col != 0 else None
    articles = set()

    for row_index, row in enumerate(rows[1:], start=2):
        nm_raw = row[article_col] if article_col < len(row) else None
        nm_id = _as_nm_id(nm_raw)
        if nm_id is None:
            if any(cell is not None for cell in row):
                title = str(row[name_col] if name_col is not None
                            and name_col < len(row) else "").strip()
                report.skipped_rows.append(f"строка {row_index}: {title or '—'}")
            continue
        articles.add(nm_id)

        for col, day in dates.items():
            value = row[col - 1] if col - 1 < len(row) else None
            text = _as_text(value)
            if not text:
                continue
            report.records.append({
                "date": day.isoformat(),
                "nm_id": nm_id,
                "advert_id": None,
                "text": text,
                "source": "импорт из таблицы",
            })

    report.articles = len(articles)
    return report


def _as_nm_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if MIN_NM_ID < number < MAX_NM_ID else None
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    number = int(digits)
    return number if MIN_NM_ID < number < MAX_NM_ID else None


def _as_text(value: Any) -> str:
    """Текст записи. Числа и даты в ячейке — не описание правки."""
    if value is None or isinstance(value, (bool, int, float, date)):
        return ""
    text = str(value).strip()
    # Ячейки с ошибками Excel (#DIV/0!) содержательного смысла не несут.
    if text.startswith("#") or not text:
        return ""
    return text


def import_into(conn: Any, path: str, sheet: str | int = 0) -> ImportReport:
    """Разбирает таблицу и переносит записи в базу."""
    from . import changes

    report = read_journal(path, sheet)
    before = len(report.records)
    report.written = changes.add_many(conn, report.records,
                                      source="импорт из таблицы")
    report.duplicates = before - report.written
    return report
