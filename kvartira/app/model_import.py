"""Загрузка выверенной модели квартиры вместо распознавания.

Всё, что программа делала до сих пор, сводилось к попытке угадать
геометрию по картинке: AI обводил комнаты, программа правила контуры
и сверяла стены с чертежом. Угадывание неизбежно ошибается.

Здесь угадывать не надо. На вход принимается готовая модель с точными
координатами — DXF с опорными габаритами или файл параметров из архива
трёхмерной заготовки. Комнаты встают ровно туда, где им положено.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import UserError, get_logger

log = get_logger()

# Слои выверенной модели.
ROOM_LAYER = "ROOM_REF"
TEXT_LAYER = "ROOM_TEXT"

# Подпись вида «2 12.23 m2»: номер помещения и его площадь.
LABEL = re.compile(r"(\d+)\s+([\d.,]+)\s*m2", re.IGNORECASE)

# По названию из файла параметров понятно и назначение комнаты.
KINDS = {
    "balcony": ("Лоджия", "balcony"),
    "kitchen": ("Кухня", "kitchen"),
    "bathroom": ("Санузел", "bathroom"),
    "living": ("Гостиная", "living"),
    "bedroom": ("Спальня", "bedroom"),
    "hall": ("Прихожая", "hallway"),
    "corridor": ("Коридор", "corridor"),
    "study": ("Кабинет", "study"),
    "kids": ("Детская", "kids"),
}


@dataclass
class ImportedRoom:
    name: str
    kind: str
    polygon: list[tuple[int, int]]
    declared_area_m2: float | None = None

    @property
    def area_m2(self) -> float:
        xs = [x for x, _ in self.polygon]
        ys = [y for _, y in self.polygon]
        return round((max(xs) - min(xs)) * (max(ys) - min(ys)) / 1_000_000, 2)


@dataclass
class ImportedModel:
    rooms: list[ImportedRoom] = field(default_factory=list)
    ceiling_height_mm: int | None = None
    wall_thickness_mm: int | None = None
    notes: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def total_area_m2(self) -> float:
        return round(sum(r.area_m2 for r in self.rooms if r.kind != "balcony"), 2)


# ── DXF ───────────────────────────────────────────────────────────────

def _dxf_pairs(text: str):
    """DXF устроен просто: строка с кодом, следом строка со значением.

    Просто, но не всегда ровно: между записями попадаются пустые строки,
    и если считать пары подряд, счёт сбивается — половина комнат
    слипается в одну. Поэтому кодом считается только строка с числом,
    а всё прочее пропускается и счёт начинается заново.
    """
    rows = text.splitlines()
    index = 0
    while index < len(rows) - 1:
        code = rows[index].strip()
        if code.lstrip("-").isdigit():
            yield int(code), rows[index + 1].strip()
            index += 2
        else:
            index += 1


def read_dxf(data: bytes) -> ImportedModel:
    """Читает опорные габариты и подписи из DXF."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception as trouble:
        raise _unreadable("DXF") from trouble

    lines: list[tuple[str, float, float, float, float]] = []
    labels: list[tuple[float, float, str]] = []

    entity = ""
    fields: dict[int, str] = {}

    def finish() -> None:
        if entity == "LINE" and fields.get(8) == ROOM_LAYER:
            try:
                lines.append((
                    fields.get(8, ""),
                    float(fields[10]), float(fields[20]),
                    float(fields[11]), float(fields[21]),
                ))
            except (KeyError, ValueError):
                pass
        elif entity == "TEXT" and fields.get(8) == TEXT_LAYER:
            try:
                labels.append((float(fields[10]), float(fields[20]),
                               fields.get(1, "")))
            except (KeyError, ValueError):
                pass

    for code, value in _dxf_pairs(text):
        if code == 0:
            finish()
            entity, fields = value, {}
        else:
            fields.setdefault(code, value)
    finish()

    if not lines:
        raise UserError(
            "В файле не нашлось опорных габаритов комнат.",
            f"Нужен DXF, где границы комнат лежат на слое «{ROOM_LAYER}». "
            "Проверьте, что отправлен именно файл выверенной модели.",
        )

    model = ImportedModel(source="DXF")
    for box in _boxes(lines):
        model.rooms.append(_room_from(box, labels))
    log.info("Из DXF прочитано комнат: %s", len(model.rooms))
    return model


def _boxes(lines) -> list[tuple[float, float, float, float]]:
    """Собирает отрезки в прямоугольники по четыре штуки подряд."""
    boxes: list[tuple[float, float, float, float]] = []
    for start in range(0, len(lines) - 3, 4):
        piece = lines[start:start + 4]
        xs = [value for _, x1, _, x2, _ in piece for value in (x1, x2)]
        ys = [value for _, _, y1, _, y2 in piece for value in (y1, y2)]
        low_x, high_x = min(xs), max(xs)
        low_y, high_y = min(ys), max(ys)
        if high_x - low_x < 500 or high_y - low_y < 500:
            continue
        boxes.append((low_x, low_y, high_x, high_y))
    return boxes


def _room_from(box, labels) -> ImportedRoom:
    low_x, low_y, high_x, high_y = box
    middle = ((low_x + high_x) / 2, (low_y + high_y) / 2)

    number, area = "", None
    best = None
    for x, y, text in labels:
        if not (low_x <= x <= high_x and low_y <= y <= high_y):
            continue
        distance = abs(x - middle[0]) + abs(y - middle[1])
        if best is None or distance < best:
            best, found = distance, LABEL.search(text)
            if found:
                number = found.group(1)
                area = round(float(found.group(2).replace(",", ".")), 2)

    return ImportedRoom(
        name=f"Помещение {number}" if number else "Помещение",
        kind="other",
        polygon=[(round(low_x), round(low_y)), (round(high_x), round(low_y)),
                 (round(high_x), round(high_y)), (round(low_x), round(high_y))],
        declared_area_m2=area,
    )


# ── Файл параметров и архив ───────────────────────────────────────────

def read_parameters(data: bytes) -> ImportedModel:
    """Читает модель из файла параметров: там есть и назначения комнат."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as trouble:
        raise _unreadable("файл параметров") from trouble

    rooms = payload.get("rooms")
    if not isinstance(rooms, dict) or not rooms:
        raise UserError(
            "В файле параметров нет списка комнат.",
            "Проверьте, что отправлен файл model_parameters.json "
            "из архива с моделью.",
        )

    model = ImportedModel(
        ceiling_height_mm=_whole(payload.get("ceiling_height_mm")),
        wall_thickness_mm=_whole(payload.get("visualization_wall_thickness_mm")),
        notes=[str(note) for note in payload.get("notes") or []],
        source="файл параметров",
    )

    for key, box in rooms.items():
        if not isinstance(box, (list, tuple)) or len(box) < 4:
            continue
        low_x, low_y, high_x, high_y = (float(value) for value in box[:4])
        name, kind = _name_of(key)
        model.rooms.append(ImportedRoom(
            name=name, kind=kind,
            polygon=[(round(low_x), round(low_y)), (round(high_x), round(low_y)),
                     (round(high_x), round(high_y)), (round(low_x), round(high_y))],
        ))

    log.info("Из файла параметров прочитано комнат: %s", len(model.rooms))
    return model


def _name_of(key: str) -> tuple[str, str]:
    """«4_bedroom» → «Спальня 4», тип bedroom."""
    number, _, word = key.partition("_")
    word = word or key
    for hint, (name, kind) in KINDS.items():
        if hint in word.lower():
            return (f"{name} {number}" if number.isdigit() else name), kind
    return (f"Помещение {number}" if number.isdigit() else key), "other"


def read_archive(data: bytes) -> ImportedModel:
    """Достаёт модель из архива с трёхмерной заготовкой."""
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.lower().endswith("model_parameters.json"):
                    return read_parameters(archive.read(name))
            for name in archive.namelist():
                if name.lower().endswith(".dxf"):
                    return read_dxf(archive.read(name))
    except zipfile.BadZipFile as trouble:
        raise _unreadable("архив") from trouble

    raise UserError(
        "В архиве не нашлось ни файла параметров, ни DXF.",
        "Нужен архив с моделью — в нём должен лежать model_parameters.json.",
    )


def read_any(filename: str, data: bytes) -> ImportedModel:
    """Понимает, что за файл прислали, и читает его."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".dxf":
        return read_dxf(data)
    if suffix == ".json":
        return read_parameters(data)
    if suffix == ".zip":
        return read_archive(data)
    raise UserError(
        f"Файл «{filename}» программе непонятен.",
        "Подойдёт DXF с выверенными габаритами, model_parameters.json "
        "или архив с моделью целиком.",
    )


def _whole(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _unreadable(what: str) -> UserError:
    return UserError(
        f"Не удалось прочитать {what}.",
        "Возможно, файл повреждён или сохранён в непривычном виде. "
        "Попробуйте выгрузить его заново.",
    )
