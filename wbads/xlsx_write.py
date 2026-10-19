"""Запись xlsx стандартной библиотекой.

Файл Excel — это zip с XML внутри, и собрать его можно без сторонних
библиотек. Как и с чтением, это не упражнение: вся программа рассчитана
на человека, который не ставит пакеты. «Сначала установите openpyxl»
возвращает нас туда, откуда мы уходили.

CSV для Excel — не то же самое. Он приезжает текстом: числа становятся
строками, проценты — числами без смысла, разделитель зависит от
настроек Windows, и половина выгрузок открывается одной колонкой. Здесь
числа лежат числами, у них есть формат, а у листа — шапка и ширина
колонок, то есть файл можно сразу считать, а не чинить.

Пишется только то, что нужно отчётам: несколько листов, строки ячеек,
шесть форматов. Формул, картинок и сводных таблиц нет и не будет.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

# Номера форматов в styles.xml ниже. Имена, а не цифры: «s=4» в коде
# через месяц не расшифрует никто.
STYLES = {
    "": 0,          # обычный текст
    "title": 1,     # заголовок отчёта
    "head": 2,      # шапка таблицы
    "name": 3,      # первая колонка — название показателя
    "int": 4,       # количества
    "money": 5,     # рубли
    "pct": 6,       # проценты (значение уже в процентах, не доля)
    "num": 7,       # дробное без единиц
    "meta": 8,      # пояснение мелким серым
}

# Excel не принимает эти символы в имени листа и молча ломает файл.
BAD_SHEET_CHARS = set(r"[]:*?/\\")
MAX_SHEET_NAME = 31


def book(sheets: Sequence[dict[str, Any]]) -> bytes:
    """Собирает книгу Excel. Лист — {name, rows, widths, freeze}.

    Ячейка строки — число, строка, None или {"v": …, "style": "money"}.
    """
    if not sheets:
        raise ValueError("В книге должен быть хотя бы один лист")

    names = _unique_names([s.get("name") or "Лист" for s in sheets])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types(len(sheets)))
        zf.writestr("_rels/.rels", _root_rels())
        zf.writestr("xl/workbook.xml", _workbook(names))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        zf.writestr("xl/styles.xml", _styles())
        for index, sheet in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", _sheet(sheet))
    return buffer.getvalue()


def _unique_names(names: Iterable[str]) -> list[str]:
    """Имена листов: обрезанные, без запрещённых символов, без повторов."""
    out: list[str] = []
    for raw in names:
        name = "".join(" " if ch in BAD_SHEET_CHARS else ch
                       for ch in str(raw)).strip() or "Лист"
        name = name[:MAX_SHEET_NAME]
        base, counter = name, 2
        while name in out:
            tail = f" {counter}"
            name = base[:MAX_SHEET_NAME - len(tail)] + tail
            counter += 1
        out.append(name)
    return out


def _content_types(count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument'
        '.spreadsheetml.worksheet+xml"/>' for i in range(1, count + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats'
        '-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd'
        '.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd'
        '.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + sheets + '</Types>')


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006'
        '/relationships"><Relationship Id="rId1" Type="http://schemas'
        '.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        ' Target="xl/workbook.xml"/></Relationships>')


def _workbook(names: Sequence[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(names, start=1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006'
        '/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006'
        f'/relationships"><sheets>{sheets}</sheets></workbook>')


def _workbook_rels(count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org'
        '/officeDocument/2006/relationships/worksheet"'
        f' Target="worksheets/sheet{i}.xml"/>' for i in range(1, count + 1))
    styles = (f'<Relationship Id="rId{count + 1}" Type="http://schemas'
              '.openxmlformats.org/officeDocument/2006/relationships/styles"'
              ' Target="styles.xml"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006'
        f'/relationships">{rels}{styles}</Relationships>')


def _styles() -> str:
    """Шрифты, форматы чисел и их сочетания.

    Проценты держим обычным числом с припиской «%»: в таблице уже лежит
    25.2 как «25,2 %», а настоящий процентный формат Excel умножил бы
    это на сто и показал 2520 %.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml'
        '/2006/main">'
        '<numFmts count="4">'
        '<numFmt numFmtId="164" formatCode="#,##0"/>'
        '<numFmt numFmtId="165" formatCode="#,##0.00&quot; ₽&quot;"/>'
        '<numFmt numFmtId="166" formatCode="0.00&quot;%&quot;"/>'
        '<numFmt numFmtId="167" formatCode="#,##0.00"/>'
        '</numFmts>'
        '<fonts count="4">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="13"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '<font><sz val="10"/><color rgb="FF777777"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left/><right/><top/>'
        '<bottom style="thin"><color rgb="FFBBBBBB"/></bottom>'
        '<diagonal/></border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0"'
        ' borderId="0"/></cellStyleXfs>'
        '<cellXfs count="9">'
        # 0 обычный
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        # 1 заголовок отчёта
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"'
        ' applyFont="1"/>'
        # 2 шапка таблицы
        '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0"'
        ' applyFont="1" applyFill="1" applyBorder="1"/>'
        # 3 название показателя
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        # 4 количества
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0"'
        ' applyNumberFormat="1"/>'
        # 5 рубли
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0"'
        ' applyNumberFormat="1"/>'
        # 6 проценты
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0"'
        ' applyNumberFormat="1"/>'
        # 7 дробное
        '<xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0"'
        ' applyNumberFormat="1"/>'
        # 8 пояснение
        '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0"'
        ' applyFont="1"/>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0"'
        ' builtinId="0"/></cellStyles>'
        '<dxfs count="0"/>'
        '</styleSheet>')


def _sheet(sheet: dict[str, Any]) -> str:
    rows: Sequence[Sequence[Any]] = sheet.get("rows") or []
    widths: Sequence[float] = sheet.get("widths") or []
    freeze = sheet.get("freeze") or ""

    cols = ""
    if widths:
        cols = "<cols>" + "".join(
            f'<col min="{i}" max="{i}" width="{w:g}" customWidth="1"/>'
            for i, w in enumerate(widths, start=1) if w) + "</cols>"

    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell(column_letter(c) + str(r), value)
                        for c, value in enumerate(row, start=1)
                        if value is not None and value != "")
        body.append(f'<row r="{r}">{cells}</row>')

    width = max((len(row) for row in rows), default=1)
    dimension = f'A1:{column_letter(max(1, width))}{max(1, len(rows))}'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml'
        '/2006/main">'
        f'<dimension ref="{dimension}"/>'
        f'{_views(freeze)}'
        f'{cols}'
        '<sheetData>' + "".join(body) + '</sheetData>'
        '</worksheet>')


def _views(freeze: str) -> str:
    """Закреплённая область: шапка и первая колонка остаются на экране."""
    if not freeze:
        return ('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
    col, row = _split_ref(freeze)
    return (
        '<sheetViews><sheetView workbookViewId="0">'
        f'<pane xSplit="{col - 1}" ySplit="{row - 1}" topLeftCell="{freeze}"'
        ' activePane="bottomRight" state="frozen"/>'
        '</sheetView></sheetViews>')


def _cell(ref: str, value: Any) -> str:
    style = 0
    if isinstance(value, dict):
        style = STYLES.get(str(value.get("style") or ""), 0)
        value = value.get("v")
    if value is None or value == "":
        return ""

    if isinstance(value, bool):
        value = str(value)
    if isinstance(value, (int, float)):
        # Бесконечности и NaN в xlsx нет: Excel откажется открыть файл.
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        number = f"{value:.6f}".rstrip("0").rstrip(".") if isinstance(
            value, float) else str(value)
        return f'<c r="{ref}" s="{style}"><v>{number or "0"}</v></c>'

    text = _clean(str(value))
    return (f'<c r="{ref}" s="{style}" t="inlineStr"><is>'
            f'<t xml:space="preserve">{escape(text)}</t></is></c>')


def _clean(text: str) -> str:
    """Выкидывает символы, которых не бывает в XML: Excel с ними не откроется."""
    return "".join(ch for ch in text
                   if ch in "\t\n\r" or ord(ch) >= 32)


def column_letter(index: int) -> str:
    """1 → A, 27 → AA."""
    letters = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters or "A"


def _split_ref(ref: str) -> tuple[int, int]:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    digits = "".join(ch for ch in ref if ch.isdigit())
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return max(1, col), max(1, int(digits or 1))
