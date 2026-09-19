"""Работа с библиотекой чтения PDF в одном месте.

Две вещи, которые иначе пришлось бы помнить в каждом файле.

Первое: библиотека при загрузке сыплет устаревшими предупреждениями
своих внутренних модулей — человеку это видеть незачем.

Второе и важное: файлы открываются по СОДЕРЖИМОМУ, а не по имени.
Путь к данным теперь содержит русские буквы («Документы/Моя квартира»),
а передавать такие пути в библиотеку, написанную на C, — известный
источник неприятностей на Windows. Python читает файл сам и отдаёт
библиотеке готовые байты, поэтому путь может быть любым.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

FILETYPES = {
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".bmp": "bmp",
    ".tif": "tiff",
    ".tiff": "tiff",
}


def library() -> Any:
    """Сама библиотека, без её предупреждений в консоли."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import pymupdf
    return pymupdf


def open_file(path: Path) -> Any:
    """Открывает файл по содержимому. Путь может быть с русскими буквами."""
    pymupdf = library()
    filetype = FILETYPES.get(Path(path).suffix.lower(), "pdf")
    return pymupdf.open(stream=Path(path).read_bytes(), filetype=filetype)


def open_bytes(data: bytes, filetype: str = "pdf") -> Any:
    pymupdf = library()
    return pymupdf.open(stream=data, filetype=filetype)
