"""Перенос журнала из таблицы Excel.

Таблица набита руками, и это её главное свойство: «18.авг.» между 17 и
19 февраля — не исключение, а норма. Выбросить такую колонку значит
потерять записи, поверить ей — сдвинуть их на полгода. Здесь проверяется,
что разбор выбирает третье: чинит по соседям и говорит, что починил.
"""

import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from argparse import Namespace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wbads import db, journal_import  # noqa: E402
from wbads.xlsx_read import XlsxError, column_letter, read_sheet  # noqa: E402

SHEET_HEAD = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.'
              'openxmlformats.org/spreadsheetml/2006/main"><sheetData>')


def build_xlsx(path: Path, rows: list[list[object]]) -> None:
    """Собирает минимальный .xlsx — чтобы проверять разбор без Excel."""
    strings: list[str] = []

    def cell(ref: str, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, date):
            serial = (value - date(1899, 12, 30)).days
            return f'<c r="{ref}" s="1"><v>{serial}</v></c>'
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        if value not in strings:
            strings.append(str(value))
        return f'<c r="{ref}" t="s"><v>{strings.index(str(value))}</v></c>'

    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            cell(f"{column_letter(c)}{r}", value)
            for c, value in enumerate(row, start=1))
        body.append(f'<row r="{r}">{cells}</row>')
    sheet = SHEET_HEAD + "".join(body) + "</sheetData></worksheet>"

    shared = ('<?xml version="1.0"?><sst xmlns="http://schemas.'
              'openxmlformats.org/spreadsheetml/2006/main" '
              f'count="{len(strings)}" uniqueCount="{len(strings)}">'
              + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")

    with zipfile.ZipFile(path, "w") as book:
        book.writestr("[Content_Types].xml",
                      '<?xml version="1.0"?><Types xmlns="http://schemas.'
                      'openxmlformats.org/package/2006/content-types"/>')
        book.writestr("xl/workbook.xml",
                      '<?xml version="1.0"?><workbook xmlns="http://schemas.'
                      'openxmlformats.org/spreadsheetml/2006/main" '
                      'xmlns:r="http://schemas.openxmlformats.org/'
                      'officeDocument/2006/relationships"><sheets>'
                      '<sheet name="Лист1" sheetId="1" r:id="rId1"/>'
                      '</sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<?xml version="1.0"?><Relationships xmlns="http://'
                      'schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                      '</Relationships>')
        book.writestr("xl/styles.xml",
                      '<?xml version="1.0"?><styleSheet xmlns="http://schemas.'
                      'openxmlformats.org/spreadsheetml/2006/main"><cellXfs>'
                      '<xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
                      '</styleSheet>')
        book.writestr("xl/sharedStrings.xml", shared)
        book.writestr("xl/worksheets/sheet1.xml", sheet)



class TestReadingXlsxWithoutLibraries(unittest.TestCase):
    """Своё чтение xlsx — не упражнение: строчка «поставьте openpyxl»
    возвращает человека туда, откуда его уводили."""

    def make(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "t.xlsx"
        build_xlsx(path, rows)
        return path

    def test_values_types_and_gaps_survive(self):
        path = self.make([
            ["название", "артикул", date(2026, 2, 12), None, "18.авг."],
            ["товар", 160427990, "почистила запросы", None, "подняла ставку"],
        ])
        rows = read_sheet(str(path))
        self.assertEqual(rows[0][2], date(2026, 2, 12), "дата должна остаться датой")
        self.assertEqual(rows[1][1], 160427990)
        self.assertIsNone(rows[1][3], "пустая ячейка — это None, а не сдвиг")
        self.assertEqual(rows[1][4], "подняла ставку")

    def test_a_file_that_is_not_excel_says_so_plainly(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "fake.xlsx"
        path.write_text("это просто текст", encoding="utf-8")
        with self.assertRaises(XlsxError) as caught:
            read_sheet(str(path))
        self.assertIn(".xls", str(caught.exception),
                      "человеку надо сказать, что делать со старым форматом")


class TestHeaderDates(unittest.TestCase):
    """Шапку набивают руками — разбор обязан это пережить."""

    def test_written_forms_are_understood(self):
        cases = {
            "02.03.": (2, 3, None),
            "3.мар.": (3, 3, None),
            "4.мая": (4, 5, None),
            "19.06.2026 ЧИСТКА ЗАПРОСОВ": (19, 6, 2026),
            "29.05(сокращение ставок в полках)": (29, 5, None),
        }
        for text, expected in cases.items():
            self.assertEqual(journal_import.parse_header_date(text), expected,
                             f"не разобрано: {text}")

    def test_real_dates_are_taken_as_is(self):
        self.assertEqual(journal_import.parse_header_date(date(2026, 9, 18)),
                         (18, 9, 2026))

    def test_plain_words_are_not_dates(self):
        self.assertEqual(journal_import.parse_header_date("ДРР за месяц"),
                         (None, None, None))
        self.assertEqual(journal_import.parse_header_date(""), (None, None, None))


class TestImportingTheJournal(unittest.TestCase):

    def make(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "j.xlsx"
        build_xlsx(path, rows)
        return str(path)

    def test_records_are_read_per_article_and_day(self):
        path = self.make([
            ["название", "артикул", date(2026, 2, 12), date(2026, 2, 17)],
            ["паразит", 160427990, "почистила запросы", None],
            ["хлор", 160427790, None, "подняла ставку"],
        ])
        report = journal_import.read_journal(path)
        self.assertEqual(len(report.records), 2)
        self.assertEqual(report.articles, 2)
        first = [r for r in report.records if r["nm_id"] == 160427990][0]
        self.assertEqual(first["date"], "2026-02-12")
        self.assertEqual(first["text"], "почистила запросы")

    def test_wrong_month_between_right_ones_is_repaired(self):
        """«18.авг.» между 17 и 19 февраля — опечатка, а не август.

        Прыжок вперёд порядок не нарушает и слева выглядит законным.
        Видно его только по правому соседу.
        """
        path = self.make([
            ["название", "артикул", date(2026, 2, 17), "18.авг.",
             date(2026, 2, 19)],
            ["товар", 160427990, "а", "б", "в"],
        ])
        report = journal_import.read_journal(path)
        days = sorted(r["date"] for r in report.records)
        self.assertEqual(days, ["2026-02-17", "2026-02-18", "2026-02-19"])
        self.assertTrue(report.repaired_dates, "починку надо показать человеку")
        self.assertIn("18.авг.", report.repaired_dates[0])

    def test_missing_month_is_taken_from_the_neighbour(self):
        path = self.make([
            ["название", "артикул", date(2026, 4, 1), "2.апр.", "3.апр."],
            ["товар", 160427990, "а", "б", "в"],
        ])
        report = journal_import.read_journal(path)
        self.assertEqual(sorted(r["date"] for r in report.records),
                         ["2026-04-01", "2026-04-02", "2026-04-03"])

    def test_service_columns_are_not_mistaken_for_dates(self):
        path = self.make([
            ["название", "артикул", "ДРР за месяц", "затраты",
             date(2026, 2, 12)],
            ["товар", 160427990, 0.39, 179644, "почистила запросы"],
        ])
        report = journal_import.read_journal(path)
        self.assertEqual(len(report.records), 1)
        self.assertEqual(report.date_columns, 1)
        self.assertEqual(len(report.skipped_columns), 2)

    def test_numbers_and_errors_in_cells_are_not_records(self):
        """#DIV/0! и суммы — не описание правки."""
        path = self.make([
            ["название", "артикул", date(2026, 2, 12), date(2026, 2, 13)],
            ["товар", 160427990, "#DIV/0!", 12345],
        ])
        report = journal_import.read_journal(path)
        self.assertEqual(report.records, [])

    def test_rows_without_an_article_are_reported_not_silently_dropped(self):
        path = self.make([
            ["название", "артикул", date(2026, 2, 12)],
            ["товар", 160427990, "правка"],
            ["итого", None, "что-то"],
        ])
        report = journal_import.read_journal(path)
        self.assertEqual(len(report.records), 1)
        self.assertEqual(len(report.skipped_rows), 1)
        self.assertIn("итого", report.skipped_rows[0])

    def test_article_column_is_found_by_shape_when_unnamed(self):
        """Колонку могли назвать иначе — тогда ищем по виду значений."""
        path = self.make([
            ["товар", "код", date(2026, 2, 12)],
            ["а", 160427990, "правка"],
            ["б", 160427791, "правка"],
            ["в", 160427792, "правка"],
        ])
        report = journal_import.read_journal(path)
        self.assertEqual(report.articles, 3)

    def test_a_table_without_articles_says_what_to_do(self):
        path = self.make([
            ["что", "когда"],
            ["правка", "вчера"],
        ])
        with self.assertRaises(XlsxError) as caught:
            journal_import.read_journal(path)
        self.assertIn("артикул", str(caught.exception))

    def test_import_writes_into_the_database(self):
        path = self.make([
            ["название", "артикул", date(2026, 2, 12)],
            ["товар", 160427990, "почистила запросы"],
        ])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = db.init_db(Path(tmp.name) / "t.db")
        self.addCleanup(conn.close)

        report = journal_import.import_into(conn, path)
        self.assertEqual(report.written, 1)

        again = journal_import.import_into(conn, path)
        self.assertEqual(again.written, 0, "повтор не должен плодить дубли")
        self.assertEqual(again.duplicates, 1)


class TestNoTypingRequired(unittest.TestCase):
    """Человек, который не работает с командной строкой, до отдельной
    команды не дойдёт. Программа обязана найти таблицу сама и предложить
    перенос — иначе журнал так и останется пустым."""

    def setUp(self):
        import run as cli
        self.cli = cli
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.patcher = unittest.mock.patch.object(cli, "ROOT", self.folder)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_tables_next_to_the_program_are_found(self):
        build_xlsx(self.folder / "журнал.xlsx", [["артикул"], [160427990]])
        found = self.cli._journal_files()
        self.assertEqual([p.name for p in found], ["журнал.xlsx"])

    def test_excel_temp_files_are_ignored(self):
        """Excel держит открытый файл как ~$имя.xlsx — он пустой."""
        build_xlsx(self.folder / "журнал.xlsx", [["артикул"], [160427990]])
        (self.folder / "~$журнал.xlsx").write_bytes(b"")
        found = self.cli._journal_files()
        self.assertEqual([p.name for p in found], ["журнал.xlsx"])

    def test_no_tables_is_not_an_error_to_decipher(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.cli.cmd_import_changes(
                Namespace(file="", sheet="", dry_run=True, yes=True))
        self.assertEqual(code, 1)
        self.assertIn("Положите ваш файл", out.getvalue(),
                      "человеку надо сказать, что делать, а не «файл не найден»")

    def test_offer_is_silent_when_there_is_nothing_new(self):
        """Спрашивать про уже перенесённое — значит приучать жать «нет»."""
        asked = []
        with unittest.mock.patch.object(self.cli, "_ask",
                                        lambda q: asked.append(q) or True):
            self.cli._offer_journal_import()
        self.assertEqual(asked, [], "нет таблицы — нет и вопроса")
