"""Чтение геометрии прямо из векторного PDF — без AI и без денег.

В векторном чертеже хранятся настоящие линии и настоящие подписи.
Значит масштаб можно не угадывать, а вычислить: у каждой размерной
линии есть подпись в миллиметрах и длина в единицах чертежа. Одна пара
даёт оценку, три десятка пар — проверенный ответ.

Здесь берётся только то, что читается надёжно:
    * масштаб чертежа с оценкой точности;
    * все размерные подписи с их местом на листе;
    * площади помещений из экспликации;
    * габариты чертежа.

Формы комнат отсюда НЕ берутся: заливка помещений нарисована
треугольниками и протекает через дверные проёмы. Комнаты распознаёт
AI, а посчитанные здесь числа служат ему опорой и проверкой.
"""

from __future__ import annotations

import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .errors import get_logger

log = get_logger()

# Подписи размеров и площадей набраны разным кеглем — это их и различает.
SMALL_TEXT_MAX = 26.0        # размерные подписи, мм
LARGE_TEXT_MIN = 26.0        # площади и экспликация, м²

MIN_SEGMENT = 5.0            # короче этого отрезки не считаем линиями
MAX_GAP = 70.0               # разрыв размерной линии под подпись
LABEL_OFFSET = 26.0          # насколько подпись может отстоять от линии


@dataclass
class Dimension:
    """Размерная подпись: сколько миллиметров и где стоит."""
    value_mm: int
    x: float
    y: float
    vertical: bool
    span_units: float = 0.0


@dataclass
class AreaLabel:
    """Подпись площади помещения, м²."""
    value_m2: float
    x: float
    y: float


@dataclass
class VectorPlan:
    """Всё, что удалось прочитать из чертежа без участия AI."""
    is_vector: bool = False
    scale_mm_per_unit: float | None = None
    scale_worst_error_mm: float | None = None
    scale_samples: int = 0
    dimensions: list[Dimension] = field(default_factory=list)
    areas: list[AreaLabel] = field(default_factory=list)
    summary: list[float] = field(default_factory=list)
    total_area_m2: float | None = None
    room_areas: list[float] = field(default_factory=list)
    extra_areas: list[float] = field(default_factory=list)
    # Габариты всего чертежа на листе, вместе с размерными линиями
    # и штампом, — это НЕ размер квартиры.
    drawing_width_mm: int | None = None
    drawing_height_mm: int | None = None
    page_width: float = 0.0
    page_height: float = 0.0
    bbox: tuple[float, float, float, float] | None = None

    @property
    def scale_is_reliable(self) -> bool:
        return (
            self.scale_mm_per_unit is not None
            and self.scale_samples >= 4
            and (self.scale_worst_error_mm or 999) <= 20
        )

    @property
    def rooms_sum_m2(self) -> float:
        return round(sum(self.room_areas), 2)


def _load(pdf_path: Path):
    from .mupdf import library, open_file
    return library(), open_file(pdf_path)


# ── Текст ─────────────────────────────────────────────────────────────────

def _merged_spans(page) -> list[tuple[str, tuple[float, float, float, float], float, bool]]:
    """Склеивает соседние фрагменты текста: «43,6» + «3» = «43,63»."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            direction = line.get("dir", (1.0, 0.0))
            vertical = abs(direction[1]) > abs(direction[0])
            merged: list[list] = []
            for span in line["spans"]:
                box = list(span["bbox"])
                if merged:
                    prev_text, prev_box, prev_size = merged[-1]
                    touching = (
                        abs(box[0] - prev_box[2]) < 2.5
                        and abs(box[1] - prev_box[1]) < 2.5
                        and abs(span["size"] - prev_size) < 0.5
                    )
                    if touching:
                        merged[-1][0] = prev_text + span["text"]
                        merged[-1][1] = [
                            min(prev_box[0], box[0]), min(prev_box[1], box[1]),
                            max(prev_box[2], box[2]), max(prev_box[3], box[3]),
                        ]
                        continue
                merged.append([span["text"], box, span["size"]])
            for text, box, size in merged:
                out.append((text.strip(), tuple(box), size, vertical))
    return out


def _read_labels(page) -> tuple[list[Dimension], list[AreaLabel], list[float]]:
    dimensions: list[Dimension] = []
    areas: list[AreaLabel] = []

    for text, box, size, vertical in _merged_spans(page):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2

        if size < SMALL_TEXT_MAX and text.isdigit() and 3 <= len(text) <= 5:
            dimensions.append(Dimension(int(text), cx, cy, vertical))
            continue

        if size >= LARGE_TEXT_MIN and "," in text:
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                continue
            if 0.5 <= value <= 500:
                areas.append(AreaLabel(round(value, 2), cx, cy))

    # Экспликация — несколько чисел столбиком в одном месте листа.
    columns: dict[int, list[AreaLabel]] = defaultdict(list)
    for area in areas:
        columns[round(area.x / 12)].append(area)
    summary: list[float] = []
    for group in columns.values():
        if len(group) >= 3:
            summary = [a.value_m2 for a in sorted(group, key=lambda a: a.y)]
            for a in group:
                areas.remove(a)
            break

    return dimensions, areas, summary


# ── Масштаб ───────────────────────────────────────────────────────────────

def _dimension_spans(page, dimensions: list[Dimension]) -> list[tuple[int, float]]:
    """Находит для подписи её размерную линию и меряет длину в единицах листа.

    Размерная линия разорвана посередине — подпись стоит в разрыве.
    Значит ищем две половинки на одной прямой с подходящим промежутком.
    """
    horizontal: dict[float, list[tuple[float, float]]] = defaultdict(list)
    vertical: dict[float, list[tuple[float, float]]] = defaultdict(list)

    for item in page.get_drawings():
        for element in item["items"]:
            if element[0] != "l":
                continue
            x1, y1, x2, y2 = element[1].x, element[1].y, element[2].x, element[2].y
            if abs(y2 - y1) < 0.6 and abs(x2 - x1) > MIN_SEGMENT:
                horizontal[round(y1, 1)].append((min(x1, x2), max(x1, x2)))
            elif abs(x2 - x1) < 0.6 and abs(y2 - y1) > MIN_SEGMENT:
                vertical[round(x1, 1)].append((min(y1, y2), max(y1, y2)))

    found: list[tuple[int, float]] = []
    used: set[int] = set()

    def scan(groups, want_vertical: bool) -> None:
        for coord, spans in groups.items():
            ordered = sorted(spans)
            for first, second in zip(ordered, ordered[1:]):
                gap_lo, gap_hi = first[1], second[0]
                if not (2 < gap_hi - gap_lo < MAX_GAP):
                    continue
                span = second[1] - first[0]
                if span < 25:
                    continue
                for index, dim in enumerate(dimensions):
                    if index in used or dim.vertical != want_vertical:
                        continue
                    along, across = (dim.y, dim.x) if want_vertical else (dim.x, dim.y)
                    if not (gap_lo - 5 <= along <= gap_hi + 5):
                        continue
                    if abs(across - coord) > LABEL_OFFSET:
                        continue
                    dim.span_units = span
                    found.append((dim.value_mm, span))
                    used.add(index)
                    break

    scan(horizontal, False)
    scan(vertical, True)
    return found


def _fit_scale(pairs: list[tuple[int, float]]) -> tuple[float | None, float | None, int]:
    """Подбирает масштаб по всем найденным размерам.

    Линия рисуется чуть длиннее самого размера — на вынос стрелок.
    Поэтому подгоняем «длина = размер / масштаб + запас», а не просто
    делим одно на другое: иначе короткие размеры врут сильнее длинных.
    Выбросы отсеиваем и считаем заново.
    """
    if len(pairs) < 3:
        return None, None, len(pairs)

    working = list(pairs)
    for _ in range(3):
        n = len(working)
        sum_v = sum(v for v, _ in working)
        sum_s = sum(s for _, s in working)
        sum_vv = sum(v * v for v, _ in working)
        sum_vs = sum(v * s for v, s in working)
        denominator = n * sum_vv - sum_v * sum_v
        if denominator == 0:
            return None, None, n
        slope = (n * sum_vs - sum_v * sum_s) / denominator
        offset = (sum_s - slope * sum_v) / n
        if slope <= 0:
            return None, None, n
        scale = 1 / slope

        errors = [abs((s - offset) * scale - v) for v, s in working]
        worst = max(errors)
        if worst <= 15 or n <= 4:
            return round(scale, 4), round(worst, 1), n
        limit = sorted(errors)[int(len(errors) * 0.85)]
        kept = [pair for pair, error in zip(working, errors) if error <= max(limit, 15)]
        if len(kept) == n:
            return round(scale, 4), round(worst, 1), n
        working = kept

    return None, None, len(working)


# ── Площади ───────────────────────────────────────────────────────────────

def _reconcile(values: list[float], total: float) -> tuple[list[float], list[float]] | None:
    """Ищет, какие подписи дают в сумме заданный итог.

    Площади бывают почти одинаковые — санузел 4,48 и лоджия 4,49, —
    поэтому недостаточно взять первое сочетание, которое сошлось:
    берём то, что сходится ТОЧНЕЕ всех, и отбрасываем поменьше подписей.
    """
    if abs(sum(values) - total) < 0.005:
        return list(values), []

    for drop_count in range(1, min(4, len(values)) + 1):
        best: tuple[float, list[float], list[float]] | None = None
        for dropped in combinations(range(len(values)), drop_count):
            kept = [v for i, v in enumerate(values) if i not in dropped]
            if not kept:
                continue
            deviation = abs(sum(kept) - total)
            if deviation < 0.02 and (best is None or deviation < best[0]):
                best = (deviation, kept, [values[i] for i in dropped])
        if best is not None:
            return best[1], best[2]
    return None


def _split_areas(
    areas: list[AreaLabel],
    summary: list[float],
    expected_total: float | None = None,
) -> tuple[float | None, list[float], list[float]]:
    """Делит подписи площадей на комнаты квартиры и всё остальное.

    Опора — экспликация: комнаты обязаны дать в сумме общую площадь.
    В экспликации обычно несколько итогов подряд — жилая площадь, общая,
    общая с коэффициентом балкона, общая с балконом целиком. Нам нужна
    общая площадь самой квартиры: наименьшая из сошедшихся, которая при
    этом покрывает большую часть подписей. Так балкон остаётся за
    скобками, а жилая площадь не принимается за общую.
    """
    values = [a.value_m2 for a in areas]
    if not values:
        return None, values, []

    candidates = list(summary)
    if expected_total is not None:
        candidates.insert(0, expected_total)
    if not candidates:
        return None, values, []

    everything = sum(values)
    reconciled: list[tuple[float, list[float], list[float]]] = []
    for total in candidates:
        result = _reconcile(values, total)
        if result is not None:
            reconciled.append((total, result[0], result[1]))

    if not reconciled:
        return None, values, []

    if expected_total is not None:
        for row in reconciled:
            if abs(row[0] - expected_total) < 0.02:
                return row

    meaningful = [row for row in reconciled if row[0] >= everything * 0.6]
    return min(meaningful or reconciled, key=lambda row: row[0])


# ── Главная функция ───────────────────────────────────────────────────────

def read_plan(
    pdf_path: Path,
    page_number: int = 1,
    expected_total_m2: float | None = None,
) -> VectorPlan:
    """Читает страницу чертежа и возвращает всё, что удалось понять."""
    plan = VectorPlan()
    pymupdf, document = _load(pdf_path)
    try:
        page = document[page_number - 1]
        plan.page_width = page.rect.width
        plan.page_height = page.rect.height

        drawings = page.get_drawings()
        plan.is_vector = len(drawings) >= 40
        if not plan.is_vector:
            return plan

        box = None
        for item in drawings:
            box = item["rect"] if box is None else (box | item["rect"])
        if box is not None:
            plan.bbox = (box.x0, box.y0, box.x1, box.y1)

        plan.dimensions, plan.areas, plan.summary = _read_labels(page)
        pairs = _dimension_spans(page, plan.dimensions)
        scale, worst, samples = _fit_scale(pairs)
        plan.scale_mm_per_unit = scale
        plan.scale_worst_error_mm = worst
        plan.scale_samples = samples

        total, rooms, extra = _split_areas(plan.areas, plan.summary, expected_total_m2)
        plan.total_area_m2 = total
        plan.room_areas = rooms
        plan.extra_areas = extra

        if scale and plan.bbox:
            plan.drawing_width_mm = round((plan.bbox[2] - plan.bbox[0]) * scale)
            plan.drawing_height_mm = round((plan.bbox[3] - plan.bbox[1]) * scale)
    finally:
        document.close()

    log.info(
        "Чертёж прочитан: масштаб %s мм/ед (ошибка %s мм по %s размерам), "
        "комнат %s, общая %s м²",
        plan.scale_mm_per_unit, plan.scale_worst_error_mm, plan.scale_samples,
        len(plan.room_areas), plan.total_area_m2,
    )
    return plan
