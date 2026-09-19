"""Хранение файлов на диске.

Всё лежит рядом с программой, в папке data/. Никаких облаков:
ваши планы и фотографии не покидают компьютер, кроме момента,
когда вы сами нажимаете «Разобрать план» или «Создать визуализацию».

    data/projects/<id>/uploads/   загруженные планы и референсы
    data/projects/<id>/previews/  превью страниц PDF
    data/projects/<id>/renders/   кадры из 3D и готовые визуализации
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

from .config import PROJECTS_DIR

SUBDIRS = ("uploads", "previews", "renders")

# Что разрешаем загружать. DWG/DXF — в планах на будущее.
ALLOWED_MIME = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
ALLOWED_SUFFIX = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}


def project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def ensure_project_dirs(project_id: int) -> Path:
    base = project_dir(project_id)
    for name in SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def remove_project_dir(project_id: int) -> None:
    shutil.rmtree(project_dir(project_id), ignore_errors=True)


def safe_name(name: str) -> str:
    """Превращает любое имя файла в безопасное.

    Кириллица переводится в латиницу: русские имена файлов ломались
    при распаковке архивов в Windows — этот урок уже пройден.
    """
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    translit = _translit(stem)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", translit).strip("-._")
    return (cleaned or "file")[:80] + suffix


_RU_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(text: str) -> str:
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", text):
        lower = ch.lower()
        if lower in _RU_MAP:
            mapped = _RU_MAP[lower]
            out.append(mapped.upper() if ch.isupper() else mapped)
        else:
            out.append(ch)
    return "".join(out)


def unique_path(folder: Path, filename: str) -> Path:
    """Подбирает свободное имя: file.jpg, file-2.jpg, file-3.jpg…"""
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for n in range(2, 1000):
        candidate = folder / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Не удалось подобрать имя файла")


def disk_usage_mb(project_id: int) -> float:
    base = project_dir(project_id)
    if not base.exists():
        return 0.0
    total = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
    return round(total / (1024 * 1024), 1)
