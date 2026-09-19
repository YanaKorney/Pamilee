"""Подгонка контуров комнат под точные площади из чертежа.

AI обводит комнаты по картинке и неизбежно промахивается на несколько
сантиметров. А точные площади программа уже прочитала из чертежа —
из экспликации, с точностью до сотых квадратного метра.

Здесь эти два источника сводятся вместе. Контуры не перерисовываются
заново: у них лишь чуть сдвигаются общие линии стен — так, чтобы
площади сошлись с чертежом. Форма комнат и их расположение остаются
теми же, а квартира становится настоящей.

Почему это работает. Площадь многоугольника считается по формуле,
в которую каждая координата входит линейно. Значит, если двигать одну
линию стены, площади всех комнат вдоль неё меняются по прямой — и
лучшее положение этой линии находится точно, а не подбором.
"""

from __future__ import annotations

import math
from typing import Iterable

# Ближе этого две координаты считаются одной и той же линией стены.
# Меньше толщины перегородки (120 мм), чтобы не слепить соседние стены.
CLUSTER_MM = 90

# Дальше этого линию не сдвигаем: лучше оставить небольшую погрешность,
# чем увести стену туда, где её на чертеже нет.
MAX_SHIFT_MM = 350

# Соседние линии не сходятся ближе этого — только чтобы они не поменялись
# местами. Значение обязано быть меньше толщины перегородки (120 мм):
# у перегородки две грани, и они — две разные линии, а не одна.
MIN_GAP_MM = 40

# Сила «притяжения» линии к исходному месту. Подобрана так, чтобы площади
# сходились, но линии не разбегались ради последних сотых метра.
PULL = 2e-6

PASSES = 60


def signed_area_mm2(points: list[tuple[float, float]]) -> float:
    doubled = 0.0
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        doubled += x1 * y2 - x2 * y1
    return doubled / 2


def area_m2(points: list[tuple[float, float]]) -> float:
    return abs(signed_area_mm2(points)) / 1_000_000


def _cluster(values: Iterable[float], tolerance: float) -> list[list[float]]:
    """Собирает близкие координаты в группы — это линии стен.

    Просто «соседи ближе допуска» тут не годится: при разбросе группы
    слипаются в цепочку — каждый следующий угол близко к предыдущему,
    а первый и последний уже на разных стенах. Поэтому группа ещё и
    не может быть шире допуска: слишком широкую разрезаем по самому
    большому промежутку внутри неё.
    """
    ordered = sorted(values)
    if not ordered:
        return []

    groups: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])

    done: list[list[float]] = []
    queue = list(groups)
    while queue:
        group = queue.pop(0)
        if len(group) < 2 or group[-1] - group[0] <= tolerance:
            done.append(group)
            continue
        gaps = [group[n + 1] - group[n] for n in range(len(group) - 1)]
        cut = gaps.index(max(gaps)) + 1
        queue.append(group[:cut])
        queue.append(group[cut:])

    done.sort(key=lambda g: g[0])
    return done


class _Grid:
    """Линии стен по одной оси и то, какие углы комнат на них лежат."""

    def __init__(self, rooms: list[list[tuple[float, float]]], axis: int) -> None:
        self.axis = axis
        every = [point[axis] for room in rooms for point in room]
        groups = _cluster(every, CLUSTER_MM)

        self.position: list[float] = [
            round(sum(group) / len(group), 1) for group in groups
        ]
        self.original: list[float] = list(self.position)
        # Для каждого угла каждой комнаты — на какой линии он лежит.
        self.owner: list[list[int]] = []
        for room in rooms:
            here: list[int] = []
            for point in room:
                value = point[axis]
                line = min(
                    range(len(self.position)),
                    key=lambda n: abs(self.original[n] - value),
                )
                here.append(line)
            self.owner.append(here)

    def limits(self, line: int) -> tuple[float, float]:
        """Докуда линию можно двигать, не перепутав её с соседними."""
        low = self.original[line] - MAX_SHIFT_MM
        high = self.original[line] + MAX_SHIFT_MM
        if line > 0:
            low = max(low, self.position[line - 1] + MIN_GAP_MM)
        if line + 1 < len(self.position):
            high = min(high, self.position[line + 1] - MIN_GAP_MM)
        return (low, high) if low <= high else (self.original[line],) * 2


def _shape(rooms, grids, room_index: int, moved: dict | None = None):
    """Контур комнаты при текущем положении линий стен."""
    points = []
    for corner in range(len(rooms[room_index])):
        coords = []
        for axis in (0, 1):
            grid = grids[axis]
            line = grid.owner[room_index][corner]
            place = grid.position[line]
            if moved and moved["axis"] == axis and moved["line"] == line:
                place = moved["to"]
            coords.append(place)
        points.append((coords[0], coords[1]))
    return points


def align_rooms(
    polygons: list[list[tuple[int, int]]],
    targets: list[float | None],
) -> list[list[tuple[int, int]]]:
    """Сдвигает линии стен так, чтобы площади сошлись с чертежом.

    polygons — контуры комнат в миллиметрах, targets — площадь каждой
    комнаты по чертежу в квадратных метрах (None, если неизвестна).
    Возвращает исправленные контуры в том же порядке.
    """
    rooms = [[(float(x), float(y)) for x, y in poly] for poly in polygons]
    usable = [index for index, poly in enumerate(rooms) if len(poly) >= 3]
    if not usable:
        return [list(poly) for poly in polygons]

    # Ни одной точной площади — значит, подтягивать не к чему.
    # Двигать стены «на глазок» нельзя: квартира должна остаться её.
    if not any(targets[:len(rooms)]):
        return [list(poly) for poly in polygons]

    grids = (_Grid(rooms, 0), _Grid(rooms, 1))

    wanted = [
        float(targets[index]) if index < len(targets) and targets[index] else None
        for index in range(len(rooms))
    ]

    for _ in range(PASSES):
        biggest_move = 0.0
        for axis in (0, 1):
            grid = grids[axis]
            for line in range(len(grid.position)):
                low, high = grid.limits(line)
                if high - low < 1:
                    continue
                start = grid.position[line]

                # Площадь каждой комнаты меняется по прямой, поэтому
                # достаточно посмотреть её в двух положениях линии.
                slope_sum = 0.0
                cross_sum = 0.0
                for index in usable:
                    target = wanted[index]
                    if target is None or line not in grid.owner[index]:
                        continue
                    a = area_m2(_shape(rooms, grids, index))
                    b = area_m2(_shape(
                        rooms, grids, index,
                        {"axis": axis, "line": line, "to": start + 100.0},
                    ))
                    slope = (b - a) / 100.0          # м² на миллиметр
                    if abs(slope) < 1e-9:
                        continue
                    base = a - slope * start
                    slope_sum += slope * slope
                    cross_sum += slope * (target - base)

                if slope_sum <= 0:
                    continue
                # Притяжение к исходному месту не даёт линии убежать.
                best = (cross_sum + PULL * grid.original[line]) / (slope_sum + PULL)
                best = min(max(best, low), high)
                biggest_move = max(biggest_move, abs(best - start))
                grid.position[line] = best

        if biggest_move < 0.5:
            break

    result: list[list[tuple[int, int]]] = []
    for index, poly in enumerate(polygons):
        if len(rooms[index]) < 3:
            result.append(list(poly))
            continue
        result.append([
            (int(round(x)), int(round(y)))
            for x, y in _shape(rooms, grids, index)
        ])
    return result


def report(
    before: list[list[tuple[int, int]]],
    after: list[list[tuple[int, int]]],
    targets: list[float | None],
) -> dict:
    """Насколько стало точнее — в квадратных метрах и миллиметрах."""

    def off(polys) -> float:
        total = 0.0
        for poly, target in zip(polys, targets):
            if target and len(poly) >= 3:
                total += abs(area_m2([(float(x), float(y)) for x, y in poly]) - target)
        return round(total, 3)

    shifted = 0.0
    for old, new in zip(before, after):
        for (x1, y1), (x2, y2) in zip(old, new):
            shifted = max(shifted, math.hypot(x2 - x1, y2 - y1))

    return {
        "error_before_m2": off(before),
        "error_after_m2": off(after),
        "biggest_shift_mm": int(round(shifted)),
    }
