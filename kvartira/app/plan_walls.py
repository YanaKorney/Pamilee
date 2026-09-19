"""Чтение стен и проёмов прямо с векторного чертежа.

Раньше стены выводились из контуров комнат. Это давало стены, которые
не могут разойтись с комнатами, но не знало о чертеже ничего: толщина
бралась из общих соображений, а двери и окна — со слов AI, который
разглядывал картинку.

Здесь стены читаются оттуда, где они нарисованы. На плане стена — это
две параллельные линии, а проём — разрыв в них: там, где дверь или
окно, линии стены просто прерываются. Значит, и толщину, и положение
каждой двери можно взять с точностью самого чертежа.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mupdf
from .errors import get_logger

log = get_logger()

# Линии ближе этого по перпендикуляру считаются одной прямой.
SAME_LINE_MM = 30

# Разрыв вдоль прямой короче этого — след от того, что линию рисовали
# кусками, а не проём.
STITCH_MM = 40

# Грань стены короче этого не бывает.
MIN_FACE_MM = 300

# Толщина стены в жилом доме.
MIN_THICKNESS_MM = 60
MAX_THICKNESS_MM = 450

# Две грани считаются одной стеной, если идут рядом хотя бы столько.
MIN_SHARED_MM = 400

# Разрыв в стене короче этого — не проём: так рисуют стыки и штриховку.
MIN_OPENING_MM = 550

# Проём шире этого — уже не дверь и не окно, а отсутствие стены.
MAX_OPENING_MM = 4000


@dataclass
class DrawnWall:
    """Стена, прочитанная с чертежа."""
    horizontal: bool          # идёт вдоль оси X
    centre_mm: float          # положение осевой линии поперёк стены
    thickness_mm: int
    start_mm: float           # начало вдоль стены
    end_mm: float
    openings: list[tuple[float, float]] = field(default_factory=list)

    @property
    def length_mm(self) -> float:
        return self.end_mm - self.start_mm

    def ends(self) -> tuple[int, int, int, int]:
        if self.horizontal:
            return (round(self.start_mm), round(self.centre_mm),
                    round(self.end_mm), round(self.centre_mm))
        return (round(self.centre_mm), round(self.start_mm),
                round(self.centre_mm), round(self.end_mm))


def _segments(page, scale: float) -> list[tuple[float, float, float, float]]:
    """Все отрезки чертежа, переведённые в миллиметры."""
    found: list[tuple[float, float, float, float]] = []
    for item in page.get_drawings():
        for piece in item["items"]:
            if piece[0] == "l":
                a, b = piece[1], piece[2]
                found.append((a.x * scale, a.y * scale, b.x * scale, b.y * scale))
            elif piece[0] == "re":
                box = piece[1]
                x1, y1 = box.x0 * scale, box.y0 * scale
                x2, y2 = box.x1 * scale, box.y1 * scale
                found += [(x1, y1, x2, y1), (x2, y1, x2, y2),
                          (x2, y2, x1, y2), (x1, y2, x1, y1)]
    return found


@dataclass
class Face:
    """Грань стены: прямая и куски, на которых она нарисована."""
    offset: float
    pieces: list[tuple[float, float]]

    def covers(self, a: float, b: float) -> bool:
        return any(start <= a and b <= end for start, end in self.pieces)

    @property
    def start(self) -> float:
        return self.pieces[0][0]

    @property
    def end(self) -> float:
        return self.pieces[-1][1]


def _faces(segments, horizontal: bool, inside) -> list[Face]:
    """Собирает отрезки в грани: одна прямая — один ряд кусков."""
    rows: dict[int, list[tuple[float, float, float]]] = {}
    for x1, y1, x2, y2 in segments:
        if horizontal:
            if abs(y2 - y1) > 8 or abs(x2 - x1) < 20:
                continue
            across, a, b = (y1 + y2) / 2, min(x1, x2), max(x1, x2)
        else:
            if abs(x2 - x1) > 8 or abs(y2 - y1) < 20:
                continue
            across, a, b = (x1 + x2) / 2, min(y1, y2), max(y1, y2)
        if not inside(across, a, b, horizontal):
            continue
        rows.setdefault(round(across / SAME_LINE_MM), []).append((a, b, across))

    faces: list[Face] = []
    for spans in rows.values():
        spans.sort()
        pieces: list[tuple[float, float]] = []
        offsets: list[float] = []
        current_a, current_b, offset = spans[0]
        offsets.append(offset)
        for a, b, across in spans[1:]:
            if a <= current_b + STITCH_MM:
                current_b = max(current_b, b)
                offsets.append(across)
                continue
            pieces.append((current_a, current_b))
            current_a, current_b = a, b
            offsets.append(across)
        pieces.append((current_a, current_b))

        pieces = [(a, b) for a, b in pieces if b - a >= MIN_FACE_MM]
        if pieces:
            faces.append(Face(sum(offsets) / len(offsets), pieces))

    faces.sort(key=lambda face: face.offset)
    return faces


def _shared(first: Face, second: Face) -> tuple[float, float] | None:
    """Кусок, вдоль которого обе грани идут рядом."""
    low = max(first.start, second.start)
    high = min(first.end, second.end)
    return (low, high) if high - low >= MIN_SHARED_MM else None


def _openings(first: Face, second: Face, low: float, high: float):
    """Разрывы, где нет ни одной из двух граней, — это проёмы."""
    edges = sorted({low, high}
                   | {value for a, b in first.pieces for value in (a, b)}
                   | {value for a, b in second.pieces for value in (a, b)})
    edges = [value for value in edges if low <= value <= high]

    holes: list[tuple[float, float]] = []
    for a, b in zip(edges, edges[1:]):
        if b - a < 1:
            continue
        middle = (a, b)
        if first.covers(*middle) or second.covers(*middle):
            continue
        if holes and a - holes[-1][1] < 1:
            holes[-1] = (holes[-1][0], b)
        else:
            holes.append((a, b))

    return [(a, b) for a, b in holes
            if MIN_OPENING_MM <= b - a <= MAX_OPENING_MM]


def _walls_along(faces: list[Face], horizontal: bool) -> list[DrawnWall]:
    """Соединяет соседние грани в стены.

    Грань объединяется только с ближайшей следующей: иначе три
    параллельные линии дали бы три стены вместо двух.
    """
    walls: list[DrawnWall] = []
    taken: set[int] = set()

    for index, face in enumerate(faces):
        if index in taken:
            continue
        for other in range(index + 1, len(faces)):
            if other in taken:
                continue
            thickness = faces[other].offset - face.offset
            if thickness < MIN_THICKNESS_MM:
                continue
            if thickness > MAX_THICKNESS_MM:
                break
            span = _shared(face, faces[other])
            if span is None:
                continue
            walls.append(DrawnWall(
                horizontal=horizontal,
                centre_mm=(face.offset + faces[other].offset) / 2,
                thickness_mm=int(round(thickness)),
                start_mm=span[0], end_mm=span[1],
                openings=_openings(face, faces[other], *span),
            ))
            taken.add(other)
            break
    return walls


def read_walls(
    pdf_path: Path,
    page_number: int,
    scale_mm_per_unit: float,
    area: dict[str, int] | None = None,
) -> list[DrawnWall]:
    """Читает стены и проёмы со страницы чертежа.

    area — прямоугольник квартиры в миллиметрах. Он нужен, чтобы рамка
    листа, штамп и экспликация не попали в стены.
    """
    if not scale_mm_per_unit:
        return []

    document = mupdf.open_file(pdf_path)
    try:
        page = document[page_number - 1]
        segments = _segments(page, scale_mm_per_unit)
    finally:
        document.close()

    margin = 600

    def inside(across: float, a: float, b: float, horizontal: bool) -> bool:
        if not area:
            return True
        if horizontal:
            low_x, high_x, low_y, high_y = (area["min_x"], area["max_x"],
                                            area["min_y"], area["max_y"])
            return (low_y - margin <= across <= high_y + margin
                    and b >= low_x - margin and a <= high_x + margin)
        low_x, high_x, low_y, high_y = (area["min_x"], area["max_x"],
                                        area["min_y"], area["max_y"])
        return (low_x - margin <= across <= high_x + margin
                and b >= low_y - margin and a <= high_y + margin)

    walls = (_walls_along(_faces(segments, True, inside), True)
             + _walls_along(_faces(segments, False, inside), False))
    log.info("С чертежа прочитано стен: %s, проёмов: %s",
             len(walls), sum(len(w.openings) for w in walls))
    return walls


# ── Сверка построенных стен с чертежом ────────────────────────────────

# Насколько далеко от оси стены ищем её линии на чертеже.
SEARCH_AROUND_MM = 160

# Стена, от которой на чертеже нарисовано меньше этой доли, — выдумка.
REAL_WALL_SHARE = 0.75

# У настоящей стены хотя бы одна грань нарисована почти во всю длину,
# а вторая — заметной частью. Случайные линии рядом (край шкафа,
# штриховка) такой пары не образуют.
FACE_MAIN_SHARE = 0.6
FACE_PAIR_SHARE = 0.3


@dataclass
class Checked:
    """Что чертёж говорит про одну построенную стену."""
    covered_share: float                  # какая часть стены нарисована
    thickness_mm: int | None              # толщина по чертежу
    gaps: list[tuple[float, float]]       # разрывы вдоль стены — проёмы

    @property
    def exists(self) -> bool:
        return self.covered_share >= REAL_WALL_SHARE


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + STITCH_MM:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def check_wall(
    segments,
    horizontal: bool,
    centre_mm: float,
    start_mm: float,
    end_mm: float,
    thickness_mm: int,
) -> Checked:
    """Сверяет одну построенную стену с тем, что нарисовано на чертеже.

    Стена на плане — это ДВЕ параллельные линии. Одной мало: рядом со
    стеной всегда что-нибудь нарисовано — придвинутый шкаф, штриховка,
    край плитки. Поэтому ищем именно пару граней на правдоподобном
    расстоянии друг от друга, и обе должны идти вдоль стены, а не
    попадаться кусками.

    Нет такой пары — стены на чертеже нет: AI принял за границу комнаты
    открытый проход. Есть, но с разрывом, — там дверь или окно.
    """
    reach = thickness_mm / 2 + SEARCH_AROUND_MM
    length = end_mm - start_mm
    if length <= 0:
        return Checked(0.0, None, [])

    # Раскладываем подходящие линии по прямым.
    lines: dict[int, dict[str, Any]] = {}
    for x1, y1, x2, y2 in segments:
        if horizontal:
            if abs(y2 - y1) > 8 or abs(x2 - x1) < MIN_FACE_MM:
                continue
            across, a, b = (y1 + y2) / 2, min(x1, x2), max(x1, x2)
        else:
            if abs(x2 - x1) > 8 or abs(y2 - y1) < MIN_FACE_MM:
                continue
            across, a, b = (x1 + x2) / 2, min(y1, y2), max(y1, y2)

        if abs(across - centre_mm) > reach:
            continue
        low, high = max(a, start_mm), min(b, end_mm)
        if high - low <= 0:
            continue
        row = lines.setdefault(round(across / SAME_LINE_MM),
                               {"spans": [], "offsets": []})
        row["spans"].append((low, high))
        row["offsets"].append(across)

    for row in lines.values():
        row["spans"] = _union(row["spans"])
        row["offset"] = sum(row["offsets"]) / len(row["offsets"])
        row["drawn"] = sum(b - a for a, b in row["spans"])

    # Ищем лучшую пару граней: обе должны быть нарисованы как следует.
    best = None
    keys = sorted(lines, key=lambda key: lines[key]["offset"])
    for first in range(len(keys)):
        one = lines[keys[first]]
        for second in range(first + 1, len(keys)):
            two = lines[keys[second]]
            strong = max(one["drawn"], two["drawn"])
            weak = min(one["drawn"], two["drawn"])
            if strong < length * FACE_MAIN_SHARE:
                continue
            if weak < length * FACE_PAIR_SHARE:
                continue
            spread = two["offset"] - one["offset"]
            if spread < MIN_THICKNESS_MM:
                continue
            if spread > MAX_THICKNESS_MM:
                break
            together = _union(one["spans"] + two["spans"])
            covered = sum(b - a for a, b in together) / length
            if best is None or covered > best[0]:
                best = (covered, spread, together)

    if best is None:
        return Checked(0.0, None, [])

    covered_share, spread, together = best
    gaps: list[tuple[float, float]] = []
    edge = start_mm
    for a, b in together:
        if a - edge >= MIN_OPENING_MM:
            gaps.append((edge, a))
        edge = max(edge, b)
    if end_mm - edge >= MIN_OPENING_MM:
        gaps.append((edge, end_mm))

    return Checked(
        covered_share, int(round(spread)),
        [(a, b) for a, b in gaps if b - a <= MAX_OPENING_MM],
    )


def drawing_segments(pdf_path: Path, page_number: int, scale_mm_per_unit: float):
    """Все отрезки чертежа в миллиметрах — для сверки со стенами."""
    if not scale_mm_per_unit:
        return []
    document = mupdf.open_file(pdf_path)
    try:
        return _segments(document[page_number - 1], scale_mm_per_unit)
    finally:
        document.close()
