"""Построение стен по контурам комнат.

AI обводит помещения, а стены программа выводит сама: граница комнаты
и есть стена. Там, где две комнаты граничат друг с другом, стена одна
на двоих — перегородка. Там, где у грани соседа нет, это наружная стена.

Такой подход даёт главное: стены не могут разойтись с комнатами,
потому что они и есть комнаты.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

# Точность склейки: две грани считаются одной, если концы ближе этого.
SNAP_MM = 120

OUTER_THICKNESS_MM = 250
INNER_THICKNESS_MM = 120


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


def _snap(value: float) -> int:
    return int(round(value / SNAP_MM) * SNAP_MM)


def _edge_key(p1: tuple[int, int], p2: tuple[int, int]) -> tuple:
    a = (_snap(p1[0]), _snap(p1[1]))
    b = (_snap(p2[0]), _snap(p2[1]))
    return (a, b) if a <= b else (b, a)


def parse_polygon(raw: str | list) -> list[tuple[int, int]]:
    points = json.loads(raw) if isinstance(raw, str) else (raw or [])
    result: list[tuple[int, int]] = []
    for point in points:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            result.append((int(point[0]), int(point[1])))
    return result


def _line_key(start: tuple[int, int], end: tuple[int, int]) -> tuple:
    """Опознаёт прямую, на которой лежит грань.

    Две грани разных комнат лежат на одной прямой, если совпадает
    направление и расстояние до начала координат.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return (0.0, 0.0)
    ux, uy = dx / length, dy / length
    # Направление приводим к одному виду, чтобы грань туда и обратно
    # считалась одной и той же прямой.
    if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
        ux, uy = -ux, -uy
    # Расстояние от начала координат до прямой со знаком.
    offset = -uy * start[0] + ux * start[1]
    return (round(ux, 3), round(uy, 3), _snap(offset))


def _along(point: tuple[int, int], direction: tuple[float, float]) -> float:
    return point[0] * direction[0] + point[1] * direction[1]


def walls_from_rooms(rooms: Iterable[dict[str, Any]]) -> list[Wall]:
    """Собирает стены из границ комнат.

    Соседние комнаты часто граничат не всей стеной, а её частью:
    у комнаты одна длинная стена, а с той стороны к ней примыкают две
    разные. Поэтому грани сначала режутся на общие куски, и только
    потом совпавшие склеиваются. Иначе на границе выросли бы две стены
    вместо одной, и дверь упёрлась бы в лишнюю.
    """
    # 1. Собираем грани, разложенные по прямым
    lines: dict[tuple, dict[str, Any]] = {}
    for room in rooms:
        points = parse_polygon(room.get("polygon", "[]"))
        if len(points) < 3:
            continue
        room_id = int(room["id"]) if room.get("id") else None
        for index in range(len(points)):
            start = points[index]
            end = points[(index + 1) % len(points)]
            if math.hypot(end[0] - start[0], end[1] - start[1]) < SNAP_MM:
                continue
            key = _line_key(start, end)
            line = lines.setdefault(key, {"direction": None, "edges": [], "cuts": set()})
            if line["direction"] is None:
                length = math.hypot(end[0] - start[0], end[1] - start[1])
                ux, uy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
                if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
                    ux, uy = -ux, -uy
                line["direction"] = (ux, uy)
            direction = line["direction"]
            t1, t2 = _along(start, direction), _along(end, direction)
            if t1 > t2:
                t1, t2 = t2, t1
            line["edges"].append((t1, t2, room_id, start, end))
            line["cuts"].update((round(t1, 1), round(t2, 1)))

    # 2. Режем грани по общим точкам и считаем, сколько комнат у каждого куска
    pieces: dict[tuple, dict[str, Any]] = {}
    for key, line in lines.items():
        direction = line["direction"]
        cuts = sorted(line["cuts"])
        for t1, t2, room_id, start, end in line["edges"]:
            inner_cuts = [c for c in cuts if t1 + 1 < c < t2 - 1]
            marks = [t1, *inner_cuts, t2]
            for a, b in zip(marks, marks[1:]):
                if b - a < SNAP_MM:
                    continue
                piece_key = (key, round(a, 1), round(b, 1))
                piece = pieces.get(piece_key)
                if piece is None:
                    origin_t = _along(start, direction)
                    pieces[piece_key] = {
                        "x1": round(start[0] + direction[0] * (a - origin_t)),
                        "y1": round(start[1] + direction[1] * (a - origin_t)),
                        "x2": round(start[0] + direction[0] * (b - origin_t)),
                        "y2": round(start[1] + direction[1] * (b - origin_t)),
                        "rooms": [room_id] if room_id else [],
                    }
                elif room_id and room_id not in piece["rooms"]:
                    piece["rooms"].append(room_id)

    # 3. Кусок, который принадлежит двум комнатам, — перегородка
    walls: list[Wall] = []
    for piece in pieces.values():
        shared = len(piece["rooms"]) >= 2
        walls.append(Wall(
            x1=piece["x1"], y1=piece["y1"], x2=piece["x2"], y2=piece["y2"],
            thickness_mm=INNER_THICKNESS_MM if shared else OUTER_THICKNESS_MM,
            kind="inner" if shared else "outer",
            rooms=piece["rooms"],
        ))
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
