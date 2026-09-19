"""Построение стен по контурам комнат.

AI обводит помещения по ВНУТРЕННИМ граням стен — так его просят,
и так площадь комнаты выходит настоящей. Значит, соседние комнаты
не соприкасаются: между ними остаётся промежуток шириной ровно
в перегородку. В этом промежутке стена и стоит.

Наружная стена ставится снаружи от грани комнаты, а не поперёк неё:
иначе половина стены съедала бы площадь комнаты.

Главное свойство остаётся прежним: стены не могут разойтись
с комнатами, потому что выводятся из них. Но теперь и толщина стен
берётся с чертежа, а не из общих соображений.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

# Короче этого кусок стены не строим — это остаток от округлений.
SNAP_MM = 120

OUTER_THICKNESS_MM = 250
INNER_THICKNESS_MM = 120

# Промежуток между гранями двух комнат шире этого — уже не перегородка,
# а что-то другое: не найденная комната или ошибка распознавания.
MAX_PARTITION_MM = 450

# Грани сошлись вплотную — перегородку ставим стандартной толщины
# по осевой линии, иначе стены между комнатами не будет вовсе.
TOUCHING_MM = 50

# Насколько грань может быть перекошена и всё-таки считаться
# параллельной соседней. AI обводит комнаты от руки, и грань, которая
# на чертеже строго вертикальна, у него уходит на несколько градусов.
# Без этого допуска программа не узнаёт в двух гранях одну стену
# и строит вместо перегородки две отдельные.
ANGLE_TOLERANCE_DEG = 8


@dataclass
class Wall:
    x1: int
    y1: int
    x2: int
    y2: int
    thickness_mm: int
    kind: str                       # outer | inner
    rooms: list[int] = field(default_factory=list)
    openings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def length_mm(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


def parse_polygon(raw: str | list) -> list[tuple[int, int]]:
    points = json.loads(raw) if isinstance(raw, str) else (raw or [])
    result: list[tuple[int, int]] = []
    for point in points:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            result.append((int(point[0]), int(point[1])))
    return result


def _point_inside(point: tuple[float, float], polygon: list[tuple[int, int]]) -> bool:
    """Лежит ли точка внутри контура. Нужно, чтобы понять, с какой
    стороны от грани находится комната, — туда стену ставить нельзя."""
    x, y = point
    inside = False
    for index in range(len(polygon)):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % len(polygon)]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


def _canonical(ux: float, uy: float) -> tuple[float, float]:
    """Грань туда и обратно — одна и та же прямая."""
    if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
        return -ux, -uy
    return ux, uy


def _group_directions(edges: list[dict[str, Any]]) -> None:
    """Собирает почти параллельные грани в одно направление.

    Длинная грань задаёт направление точнее короткой, поэтому за основу
    берутся самые длинные: вокруг них и собираются остальные.
    """
    limit = math.cos(math.radians(ANGLE_TOLERANCE_DEG))
    axes: list[tuple[float, float]] = []

    for edge in sorted(edges, key=lambda e: -e["length"]):
        ux, uy = edge["raw_unit"]
        found = None
        for number, (ax, ay) in enumerate(axes):
            if abs(ux * ax + uy * ay) >= limit:
                found = number
                break
        if found is None:
            axes.append((ux, uy))
            found = len(axes) - 1
        edge["axis"] = found

    # Пересчитываем грани по направлению их общей прямой: так
    # перекошенная грань встаёт ровно и находит себе пару.
    for edge in edges:
        ux, uy = axes[edge["axis"]]
        nx, ny = -uy, ux
        start, end = edge["start"], edge["end"]
        middle = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        t1 = start[0] * ux + start[1] * uy
        t2 = end[0] * ux + end[1] * uy
        if t1 > t2:
            t1, t2 = t2, t1
        edge.update({
            "unit": (ux, uy), "normal": (nx, ny),
            "offset": middle[0] * nx + middle[1] * ny,
            "t1": t1, "t2": t2, "free": [(t1, t2)],
        })


def _edges_of(room: dict[str, Any]) -> list[dict[str, Any]]:
    """Разбирает контур комнаты на грани в удобных для стен величинах.

    Каждая грань описывается направлением прямой, расстоянием до неё
    от начала координат, отрезком вдоль прямой — и стороной, с которой
    лежит сама комната.
    """
    points = parse_polygon(room.get("polygon", "[]"))
    if len(points) < 3:
        return []
    room_id = int(room["id"]) if room.get("id") else None

    edges: list[dict[str, Any]] = []
    for index in range(len(points)):
        start_point = points[index]
        end_point = points[(index + 1) % len(points)]
        dx = end_point[0] - start_point[0]
        dy = end_point[1] - start_point[1]
        length = math.hypot(dx, dy)
        if length < SNAP_MM:
            continue

        ux, uy = _canonical(dx / length, dy / length)
        nx, ny = -uy, ux

        # С какой стороны комната: отходим от середины грани по нормали.
        middle = ((start_point[0] + end_point[0]) / 2,
                  (start_point[1] + end_point[1]) / 2)
        probe = (middle[0] + nx * SNAP_MM / 4, middle[1] + ny * SNAP_MM / 4)
        inside = 1 if _point_inside(probe, points) else -1

        edges.append({
            "start": start_point, "end": end_point, "length": length,
            "raw_unit": (ux, uy), "room": room_id, "inside": inside,
        })
    return edges


def _cut_out(spans: list[tuple[float, float]], a: float, b: float):
    """Вычёркивает занятый кусок из списка свободных."""
    left: list[tuple[float, float]] = []
    for start, end in spans:
        if end <= a or start >= b:
            left.append((start, end))
            continue
        if start < a:
            left.append((start, a))
        if end > b:
            left.append((b, end))
    return left


def _overlaps(first, second):
    """Общие куски двух списков отрезков."""
    shared = []
    for a1, a2 in first:
        for b1, b2 in second:
            low, high = max(a1, b1), min(a2, b2)
            if high - low >= SNAP_MM:
                shared.append((low, high))
    return shared


def _make_wall(edge, offset: float, t1: float, t2: float,
               thickness: int, kind: str, rooms: list[int]) -> Wall:
    """Переводит «прямая плюс отрезок на ней» обратно в две точки."""
    ux, uy = edge["unit"]
    nx, ny = edge["normal"]
    return Wall(
        x1=round(ux * t1 + nx * offset), y1=round(uy * t1 + ny * offset),
        x2=round(ux * t2 + nx * offset), y2=round(uy * t2 + ny * offset),
        thickness_mm=thickness, kind=kind,
        rooms=[r for r in rooms if r],
    )


def _join_collinear(walls: list[Wall]) -> list[Wall]:
    """Сращивает соседние куски одной стены.

    Наружная стена рвётся там, где в неё упирается перегородка:
    у комнат слева и справа от перегородки грани заканчиваются, и
    между ними остаётся щель шириной в саму перегородку. В доме такой
    дыры нет — куски надо срастить.
    """
    groups: dict[tuple, list[Wall]] = {}
    for wall in walls:
        dx, dy = wall.x2 - wall.x1, wall.y2 - wall.y1
        length = math.hypot(dx, dy) or 1
        ux, uy = dx / length, dy / length
        if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
            ux, uy = -ux, -uy
        nx, ny = -uy, ux
        offset = wall.x1 * nx + wall.y1 * ny
        key = (round(ux, 2), round(uy, 2), round(offset / 20),
               wall.thickness_mm, wall.kind)
        groups.setdefault(key, []).append(wall)

    joined: list[Wall] = []
    for group in groups.values():
        sample = group[0]
        dx, dy = sample.x2 - sample.x1, sample.y2 - sample.y1
        length = math.hypot(dx, dy) or 1
        ux, uy = dx / length, dy / length
        if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
            ux, uy = -ux, -uy
        nx, ny = -uy, ux

        spans = []
        for wall in group:
            a = wall.x1 * ux + wall.y1 * uy
            b = wall.x2 * ux + wall.y2 * uy
            spans.append((min(a, b), max(a, b), wall))
        spans.sort()

        current_a, current_b, first = spans[0]
        rooms = list(first.rooms)
        offset = first.x1 * nx + first.y1 * ny
        for a, b, wall in spans[1:]:
            if a - current_b <= MAX_PARTITION_MM:
                current_b = max(current_b, b)
                rooms.extend(r for r in wall.rooms if r not in rooms)
                continue
            joined.append(_make_wall(
                {"unit": (ux, uy), "normal": (nx, ny)},
                offset, current_a, current_b,
                first.thickness_mm, first.kind, rooms,
            ))
            current_a, current_b, first = a, b, wall
            rooms = list(wall.rooms)
            offset = wall.x1 * nx + wall.y1 * ny
        joined.append(_make_wall(
            {"unit": (ux, uy), "normal": (nx, ny)},
            offset, current_a, current_b,
            first.thickness_mm, first.kind, rooms,
        ))
    return joined


def walls_from_rooms(rooms: Iterable[dict[str, Any]]) -> list[Wall]:
    """Собирает стены из границ комнат.

    Порядок такой. Сначала ищем пары граней, которые смотрят друг на
    друга через небольшой промежуток, — это перегородки, и стена
    занимает промежуток целиком, какой он есть на чертеже. Всё, что
    не нашло пары, остаётся наружной стеной и ставится снаружи от
    комнаты. Напоследок куски одной стены сращиваются.
    """
    edges: list[dict[str, Any]] = []
    for room in rooms:
        edges.extend(_edges_of(room))
    if not edges:
        return []
    _group_directions(edges)

    by_direction: dict[int, list[dict[str, Any]]] = {}
    for edge in edges:
        by_direction.setdefault(edge["axis"], []).append(edge)

    walls: list[Wall] = []
    for group in by_direction.values():
        # Пары перебираем от самых близких: если грань смотрит сразу
        # на две, ближняя и есть её перегородка.
        pairs = []
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                if first["room"] and first["room"] == second["room"]:
                    continue
                low, high = (first, second) if first["offset"] <= second["offset"] \
                    else (second, first)
                gap = high["offset"] - low["offset"]
                if gap > MAX_PARTITION_MM:
                    continue
                # Комнаты обязаны быть по разные стороны промежутка,
                # иначе это две грани одной и той же стороны.
                # Когда грани сошлись вплотную, «ниже» и «выше» теряют
                # смысл — тогда достаточно, что комнаты смотрят в разные
                # стороны от общей границы.
                if gap <= TOUCHING_MM:
                    if low["inside"] == high["inside"]:
                        continue
                elif not (low["inside"] < 0 < high["inside"]):
                    continue
                pairs.append((gap, i, low, high))
        pairs.sort(key=lambda row: (row[0], row[1]))

        for gap, _, low, high in pairs:
            for a, b in _overlaps(low["free"], high["free"]):
                if gap <= TOUCHING_MM:
                    thickness = INNER_THICKNESS_MM
                    middle = (low["offset"] + high["offset"]) / 2
                else:
                    thickness = int(round(gap))
                    middle = (low["offset"] + high["offset"]) / 2
                walls.append(_make_wall(
                    low, middle, a, b, thickness, "inner",
                    [low["room"], high["room"]],
                ))
                low["free"] = _cut_out(low["free"], a, b)
                high["free"] = _cut_out(high["free"], a, b)

        # Что не нашло пары — наружная стена, снаружи от комнаты.
        for edge in group:
            for a, b in edge["free"]:
                if b - a < SNAP_MM:
                    continue
                offset = edge["offset"] - edge["inside"] * OUTER_THICKNESS_MM / 2
                walls.append(_make_wall(
                    edge, offset, a, b,
                    OUTER_THICKNESS_MM, "outer", [edge["room"]],
                ))

    return _close_corners(_join_collinear(walls))


def _close_corners(walls: list[Wall]) -> list[Wall]:
    """Достраивает наружные стены до углов дома.

    Наружная стена заканчивается там, где заканчивается грань комнаты,
    а это на пол-толщины не доходит до угла. Получается выемка.
    Перегородки удлинять не надо: они и так упираются во внутреннюю
    поверхность наружной стены.
    """
    for wall in walls:
        if wall.kind != "outer":
            continue
        length = wall.length_mm
        if length <= 0:
            continue
        step = wall.thickness_mm / 2
        ux = (wall.x2 - wall.x1) / length
        uy = (wall.y2 - wall.y1) / length
        wall.x1 = round(wall.x1 - ux * step)
        wall.y1 = round(wall.y1 - uy * step)
        wall.x2 = round(wall.x2 + ux * step)
        wall.y2 = round(wall.y2 + uy * step)
    return walls


def _distance_to_wall(wall: Wall, x: float, y: float) -> tuple[float, float]:
    """Расстояние от точки до стены и её положение вдоль стены."""
    dx, dy = wall.x2 - wall.x1, wall.y2 - wall.y1
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.hypot(x - wall.x1, y - wall.y1), 0.0
    t = ((x - wall.x1) * dx + (y - wall.y1) * dy) / length_squared
    t = max(0.0, min(1.0, t))
    nearest_x = wall.x1 + t * dx
    nearest_y = wall.y1 + t * dy
    return math.hypot(x - nearest_x, y - nearest_y), t * math.sqrt(length_squared)


def attach_openings(
    walls: list[Wall],
    openings: Iterable[dict[str, Any]],
    max_distance_mm: int = 1200,
) -> int:
    """Привязывает двери и окна к ближайшей стене.

    Проём, до которого ни одна стена не дотянулась, отбрасывается:
    дверь посреди комнаты — это ошибка распознавания, а не дверь.
    """
    attached = 0
    for opening in openings:
        try:
            x, y = float(opening["x"]), float(opening["y"])
            width = int(opening.get("width_mm") or 900)
        except (KeyError, TypeError, ValueError):
            continue

        best: tuple[float, float, Wall] | None = None
        for wall in walls:
            distance, offset = _distance_to_wall(wall, x, y)
            if best is None or distance < best[0]:
                best = (distance, offset, wall)
        if best is None or best[0] > max_distance_mm:
            continue

        distance, offset, wall = best
        width = max(500, min(width, int(wall.length_mm)))
        half = width / 2
        offset = max(half, min(offset, wall.length_mm - half))

        kind = str(opening.get("kind") or "door")
        wall.openings.append({
            "kind": "window" if kind == "window" else "door",
            "offset_mm": int(offset - half),
            "width_mm": width,
            "height_mm": int(opening.get("height_mm") or (1500 if kind == "window" else 2100)),
            "sill_mm": int(opening.get("sill_mm") or (800 if kind == "window" else 0)),
        })
        attached += 1
    return attached


def bounds(rooms: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Габаритный прямоугольник всей квартиры, мм."""
    xs: list[int] = []
    ys: list[int] = []
    for room in rooms:
        for x, y in parse_polygon(room.get("polygon", "[]")):
            xs.append(x)
            ys.append(y)
    if not xs:
        return {"min_x": 0, "min_y": 0, "max_x": 0, "max_y": 0, "width": 0, "depth": 0}
    return {
        "min_x": min(xs), "min_y": min(ys),
        "max_x": max(xs), "max_y": max(ys),
        "width": max(xs) - min(xs), "depth": max(ys) - min(ys),
    }


def polygon_area_m2(points: list[tuple[int, int]]) -> float:
    if len(points) < 3:
        return 0.0
    doubled = 0.0
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        doubled += x1 * y2 - x2 * y1
    return round(abs(doubled) / 2 / 1_000_000, 2)
