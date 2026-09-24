"""Чтение xlsx стандартной библиотекой.

Файл Excel — это zip с XML внутри, и прочитать его можно без сторонних
библиотек. Это не упражнение в аккуратности: вся программа рассчитана на
человека, который не устанавливает пакеты и не работает с командной
строкой. Одна строчка «сначала поставьте openpyxl» возвращает нас туда,
откуда мы уходили.

Читается только то, что нужно журналу: значения ячеек листа как текст и
как даты. Формулы, оформление и картинки не нужны — и не читаются.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}

# Excel считает дни от 1899-12-30: 1 января 1900 у него день 1, плюс
# известная ошибка с 29 февраля 1900, из-за которой отсчёт сдвинут.
EXCEL_EPOCH = date(1899, 12, 30)

# Форматы, которые Excel считает датами. Нужны, чтобы отличить дату от
# обычного числа: в файле и то и другое лежит как число.
BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
DATE_HINT = re.compile(r"[dmyhs]", re.IGNORECASE)


class XlsxError(RuntimeError):
    """Файл не похож на таблицу Excel или испорчен."""


def read_sheet(path: str, sheet: str | int = 0) -> list[list[Any]]:
    """Возвращает лист как таблицу: список строк, в строке — значения.

    Пустые ячейки — None. Даты — объекты date. Всё остальное — строки
    или числа, как в файле.
    """
    try:
        with zipfile.ZipFile(path) as book:
            shared = _shared_strings(book)
            date_styles = _date_styles(book)
            target = _sheet_path(book, sheet)
            with book.open(target) as stream:
                return _rows(stream, shared, date_styles)
    except zipfile.BadZipFile as exc:
        raise XlsxError(
            "Это не файл Excel (.xlsx). Если таблица в старом формате .xls "
            "или в Google Sheets — сохраните её как .xlsx и повторите."
        ) from exc
    except KeyError as exc:
        raise XlsxError(f"В файле не нашлось нужной части: {exc}") from exc


def sheet_names(path: str) -> list[str]:
    try:
        with zipfile.ZipFile(path) as book:
            with book.open("xl/workbook.xml") as stream:
                tree = ElementTree.parse(stream)
            return [node.get("name") or ""
                    for node in tree.findall(".//x:sheets/x:sheet", NS)]
    except (zipfile.BadZipFile, KeyError) as exc:
        raise XlsxError("Не удалось прочитать список листов") from exc


def _sheet_path(book: zipfile.ZipFile, sheet: str | int) -> str:
    """Находит XML нужного листа.

    Порядок листов в workbook.xml и имена файлов внутри архива не совпадают,
    поэтому идём через связи (rels), а не угадываем sheet1.xml.
    """
    with book.open("xl/workbook.xml") as stream:
        workbook = ElementTree.parse(stream)
    sheets = workbook.findall(".//x:sheets/x:sheet", NS)
    if not sheets:
        raise XlsxError("В файле нет ни одного листа")

    if isinstance(sheet, int):
        if sheet >= len(sheets):
            raise XlsxError(f"В файле только {len(sheets)} лист(ов)")
        node = sheets[sheet]
    else:
        found = [n for n in sheets if (n.get("name") or "") == sheet]
        if not found:
            names = ", ".join(n.get("name") or "" for n in sheets)
            raise XlsxError(f"Лист «{sheet}» не найден. Есть: {names}")
        node = found[0]

    rid = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/"
                   "relationships}id")
    try:
        with book.open("xl/_rels/workbook.xml.rels") as stream:
            rels = ElementTree.parse(stream)
        for rel in rels.findall("r:Relationship", REL_NS):
            if rel.get("Id") == rid:
                target = rel.get("Target") or ""
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                return target
    except KeyError:
        pass
    index = sheets.index(node) + 1
    return f"xl/worksheets/sheet{index}.xml"


def _shared_strings(book: zipfile.ZipFile) -> list[str]:
    """Общая таблица строк: в ячейках лежат ссылки на неё, а не текст."""
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    with book.open("xl/sharedStrings.xml") as stream:
        tree = ElementTree.parse(stream)
    values = []
    for item in tree.findall("x:si", NS):
        # Текст может быть разбит на куски с разным оформлением —
        # собираем все, иначе половина строки пропадёт.
        parts = [node.text or "" for node in item.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
        values.append("".join(parts))
    return values


def _date_styles(book: zipfile.ZipFile) -> set[int]:
    """Номера стилей, которые означают дату.

    Без этого дата не отличается от числа: в файле «12.02.2026» хранится
    как 46066, и разницу задаёт только формат.
    """
    if "xl/styles.xml" not in book.namelist():
        return set()
    with book.open("xl/styles.xml") as stream:
        tree = ElementTree.parse(stream)

    custom_date_formats = set()
    for node in tree.findall(".//x:numFmts/x:numFmt", NS):
        code = node.get("formatCode") or ""
        # Убираем то, что не влияет на смысл: цвета, кавычки, литералы.
        bare = re.sub(r'\[[^\]]*\]|"[^"]*"', "", code)
        if DATE_HINT.search(bare):
            custom_date_formats.add(int(node.get("numFmtId") or -1))

    styles = set()
    for index, node in enumerate(tree.findall(".//x:cellXfs/x:xf", NS)):
        fmt_id = int(node.get("numFmtId") or 0)
        if fmt_id in BUILTIN_DATE_FORMATS or fmt_id in custom_date_formats:
            styles.add(index)
    return styles


def _rows(stream: Any, shared: list[str], date_styles: set[int]) -> list[list[Any]]:
    """Разбирает лист в таблицу, сохраняя позиции ячеек.

    Excel не пишет пустые ячейки, поэтому ориентироваться на порядок
    нельзя: адрес ячейки (A1, DZ12) — единственный надёжный источник.
    """
    table: dict[int, dict[int, Any]] = {}
    max_col = 0
    for _, element in ElementTree.iterparse(stream, events=("end",)):
        if not element.tag.endswith("}c"):
            continue
        ref = element.get("r") or ""
        row_idx, col_idx = _split_ref(ref)
        if row_idx is None:
            element.clear()
            continue
        value = _cell_value(element, shared, date_styles)
        if value is not None:
            table.setdefault(row_idx, {})[col_idx] = value
            max_col = max(max_col, col_idx)
        element.clear()

    if not table:
        return []
    height = max(table)
    return [[table.get(r, {}).get(c) for c in range(1, max_col + 1)]
            for r in range(1, height + 1)]


def _cell_value(element: Any, shared: list[str], date_styles: set[int]) -> Any:
    kind = element.get("t")
    style = element.get("s")

    if kind == "inlineStr":
        parts = [node.text or "" for node in element.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
        return "".join(parts).strip() or None

    node = element.find("x:v", NS)
    if node is None or node.text is None:
        return None
    raw = node.text

    if kind == "s":                      # ссылка на общую таблицу строк
        try:
            return shared[int(raw)].strip() or None
        except (ValueError, IndexError):
            return None
    if kind == "str":                    # результат формулы
        return raw.strip() or None
    if kind == "e":                      # ошибка в ячейке, например #DIV/0!
        return raw.strip() or None
    if kind == "b":
        return raw == "1"

    try:
        number = float(raw)
    except ValueError:
        return raw.strip() or None

    if style is not None and int(style) in date_styles:
        return _as_date(number)
    return int(number) if number.is_integer() else number


def _as_date(serial: float) -> Any:
    """Число Excel → дата. Вне разумных границ возвращаем как есть."""
    try:
        if serial < 1 or serial > 60000:
            return serial
        return EXCEL_EPOCH + timedelta(days=int(serial))
    except (OverflowError, ValueError):
        return serial


def _split_ref(ref: str) -> tuple[int | None, int]:
    """«DZ12» → (12, 130). Адрес ячейки в номера строки и колонки."""
    letters = ""
    digits = ""
    for char in ref:
        if char.isalpha():
            letters += char
        elif char.isdigit():
            digits += char
    if not letters or not digits:
        return None, 0
    col = 0
    for char in letters.upper():
        col = col * 26 + (ord(char) - 64)
    return int(digits), col


def column_letter(index: int) -> str:
    """1 → «A», 130 → «DZ». Для сообщений об ошибках разбора."""
    letters = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def as_date(value: Any) -> date | None:
    """Приводит значение ячейки к дате, если это возможно."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None
