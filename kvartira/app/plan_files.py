"""Приём загруженных файлов: PDF, JPG, PNG, WEBP.

Что здесь происходит:
  * файл сохраняется как есть — оригинал не портим;
  * из каждой страницы PDF делается картинка для показа на экране;
  * определяется, векторный чертёж или просто картинка.

Разница между векторным и растровым принципиальна. В векторном PDF
хранятся настоящие линии стен и подписи размеров — их можно прочитать
точно. В картинке есть только пиксели, и размеры придётся распознавать
с неизбежной погрешностью.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db, storage
from .errors import UserError, get_logger, unsupported_file

log = get_logger()

MAX_FILE_MB = 50
MAX_PDF_PAGES = 60
PREVIEW_DPI = 110          # для A3 даёт примерно 1800 px по длинной стороне
PREVIEW_MAX_PX = 2000      # картинки больше этого ужимаем для показа

# Если в PDF нарисовано хотя бы столько линий — это чертёж, а не скан.
VECTOR_DRAWING_THRESHOLD = 40


@dataclass
class Ingested:
    """Результат загрузки одного документа."""
    stored_name: str
    original_name: str
    pages: int
    is_vector: bool
    file_ids: list[int]


def _suffix_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in storage.ALLOWED_SUFFIX:
        raise unsupported_file(name)
    return ".jpg" if suffix == ".jpeg" else suffix


def _check_size(data: bytes, name: str) -> None:
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise UserError(
            f"Файл «{name}» слишком большой ({len(data) // (1024 * 1024)} МБ).",
            f"Максимум {MAX_FILE_MB} МБ. Попробуйте сохранить его с меньшим разрешением.",
        )
    if not data:
        raise UserError(f"Файл «{name}» пустой.", "Выберите другой файл.")


def ingest(project_id: int, original_name: str, data: bytes,
           kind: str = "plan", room_id: int | None = None) -> Ingested:
    """Сохраняет файл и готовит его к показу. Возвращает описание результата."""
    _check_size(data, original_name)
    suffix = _suffix_of(original_name)

    base = storage.ensure_project_dirs(project_id)
    target = storage.unique_path(base / "uploads", storage.safe_name(original_name))
    target.write_bytes(data)

    try:
        if suffix == ".pdf":
            return _ingest_pdf(project_id, original_name, target, kind, room_id)
        return _ingest_image(project_id, original_name, target, kind, room_id)
    except UserError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        log.exception("Не удалось разобрать файл %s: %s", original_name, exc)
        raise UserError(
            f"Не удалось прочитать файл «{original_name}».",
            "Возможно, он повреждён. Попробуйте пересохранить его и загрузить снова.",
        ) from exc


def _ingest_pdf(project_id: int, original_name: str, path: Path,
                kind: str, room_id: int | None = None) -> Ingested:

    previews_dir = storage.project_dir(project_id) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)

    file_ids: list[int] = []
    document_is_vector = False

    from .mupdf import open_file

    with open_file(path) as document:
        if document.page_count == 0:
            raise UserError(
                f"В файле «{original_name}» нет ни одной страницы.",
                "Проверьте файл и попробуйте снова.",
            )
        if document.page_count > MAX_PDF_PAGES:
            raise UserError(
                f"В файле «{original_name}» слишком много страниц "
                f"({document.page_count}).",
                f"Максимум {MAX_PDF_PAGES}. Разделите его на части и загрузите по очереди.",
            )

        for index, page in enumerate(document, start=1):
            drawings = len(page.get_drawings())
            page_is_vector = drawings >= VECTOR_DRAWING_THRESHOLD
            document_is_vector = document_is_vector or page_is_vector

            pixmap = page.get_pixmap(dpi=PREVIEW_DPI)
            preview_name = f"{path.stem}-p{index}.png"
            pixmap.save(previews_dir / preview_name)

            file_ids.append(db.add_file(
                project_id=project_id,
                kind=kind,
                original_name=original_name,
                stored_name=path.name,
                mime="application/pdf",
                page_no=index,
                preview_name=preview_name,
                width_px=pixmap.width,
                height_px=pixmap.height,
                is_vector=page_is_vector,
                room_id=room_id,
            ))

        pages = document.page_count

    log.info(
        "Загружен PDF %s: %s стр., %s",
        original_name, pages, "векторный" if document_is_vector else "картинка",
    )
    return Ingested(path.name, original_name, pages, document_is_vector, file_ids)


def _make_image_preview(path: Path, target: Path, original_name: str) -> tuple[int, int]:
    """Делает превью изображения и возвращает его настоящий размер в пикселях.

    JPG и PNG читает та же библиотека, что и PDF, — отдельная для этого
    не нужна. WEBP она не умеет, поэтому для него нужна pillow; если её
    нет, честно об этом говорим вместо непонятной ошибки.
    """
    suffix = path.suffix.lower()

    if suffix in (".jpg", ".jpeg", ".png"):
        from .mupdf import library, open_file
        pymupdf = library()
        try:
            with open_file(path) as document:
                page = document[0]
                width = int(page.rect.width)
                height = int(page.rect.height)
                scale = min(1.0, PREVIEW_MAX_PX / max(width, height, 1))
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
                target.write_bytes(pixmap.tobytes("jpg"))
            return width, height
        except Exception:
            pass  # попробуем запасной путь ниже

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise UserError(
            f"Файл «{original_name}» в этом формате открыть не получилось.",
            "Сохраните его как JPG или PNG и загрузите снова.",
        ) from exc

    try:
        with Image.open(io.BytesIO(path.read_bytes())) as image:
            image.load()
            width, height = image.size
            preview = image.convert("RGB")
            preview.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX), Image.LANCZOS)
            preview.save(target, "JPEG", quality=88)
        return width, height
    except UnidentifiedImageError as exc:
        raise UserError(
            f"Файл «{original_name}» не похож на изображение.",
            "Подойдут JPG, PNG или WEBP.",
        ) from exc


def _ingest_image(project_id: int, original_name: str, path: Path,
                  kind: str, room_id: int | None = None) -> Ingested:
    previews_dir = storage.project_dir(project_id) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)

    preview_name = f"{path.stem}-p1.jpg"
    width, height = _make_image_preview(path, previews_dir / preview_name, original_name)

    mime = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/jpeg")

    file_id = db.add_file(
        project_id=project_id,
        kind=kind,
        original_name=original_name,
        stored_name=path.name,
        mime=mime,
        page_no=1,
        preview_name=preview_name,
        width_px=width,
        height_px=height,
        is_vector=False,
        room_id=room_id,
    )
    log.info("Загружено изображение %s: %s×%s", original_name, width, height)
    return Ingested(path.name, original_name, 1, False, [file_id])


def group_documents(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Собирает строки-страницы обратно в документы — так удобнее показывать."""
    documents: dict[str, dict[str, Any]] = {}
    for row in files:
        key = row["stored_name"]
        doc = documents.setdefault(key, {
            "stored_name": key,
            "original_name": row["original_name"],
            "mime": row["mime"],
            "created_at": row["created_at"],
            "is_vector": False,
            "pages": [],
        })
        doc["is_vector"] = doc["is_vector"] or bool(row["is_vector"])
        doc["pages"].append({
            "id": row["id"],
            "page_no": row["page_no"],
            "label": row["label"],
            "width_px": row["width_px"],
            "height_px": row["height_px"],
            "is_vector": bool(row["is_vector"]),
        })
    for doc in documents.values():
        doc["pages"].sort(key=lambda p: (p["page_no"] or 0, p["id"]))
        doc["page_count"] = len(doc["pages"])
    return sorted(documents.values(), key=lambda d: d["created_at"])


def delete_document(project_id: int, stored_name: str) -> int:
    """Удаляет документ целиком: строки в базе, оригинал и превью."""
    rows = db.file_group(project_id, stored_name)
    if not rows:
        raise UserError("Файл не найден.", "Возможно, он уже удалён. Обновите страницу.", status=404)

    base = storage.project_dir(project_id)
    (base / "uploads" / stored_name).unlink(missing_ok=True)
    for row in rows:
        if row["preview_name"]:
            (base / "previews" / row["preview_name"]).unlink(missing_ok=True)
    return db.delete_file_group(project_id, stored_name)
