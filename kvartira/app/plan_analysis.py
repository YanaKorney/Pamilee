"""Распознавание планировки: лист чертежа → комнаты, проёмы, мебель.

Разделение труда здесь принципиальное.

Точные числа — масштаб и площади — уже прочитаны из векторного PDF
без всякого AI (см. plan_vector.py). AI не просят их угадывать: они
передаются ему как данность и служат проверкой его ответа.

AI отвечает на вопрос, который программа решить не может: где какая
комната, что за предмет мебели и как он повёрнут. Его ответ — черновик,
который человек подтверждает на следующем шаге.

Координаты AI возвращает в пикселях присланной картинки. Перевод в
миллиметры делает программа — по масштабу, который знает точно.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ai, align, db, geometry
from .errors import UserError, get_logger, scale_unknown
from .plan_vector import VectorPlan, read_plan

log = get_logger()

# Картинку для модели делаем не больше этого по длинной стороне:
# крупнее она всё равно не рассматривает, а платить за это пришлось бы.
IMAGE_MAX_PX = 1540

# Габариты по умолчанию для предметов, мм: ширина, глубина, высота.
DEFAULT_SIZES: dict[str, tuple[int, int, int]] = {
    "bed": (1600, 2000, 600),
    "sofa": (2200, 900, 850),
    "armchair": (800, 850, 850),
    "table": (1400, 800, 750),
    "chair": (450, 450, 900),
    "wardrobe": (1200, 600, 2400),
    "shelf": (800, 400, 2000),
    "tv": (1200, 80, 700),
    "kitchen": (600, 600, 900),
    "fridge": (600, 650, 2000),
    "stove": (600, 600, 900),
    "oven": (600, 600, 600),
    "sink": (600, 600, 200),
    "dishwasher": (600, 600, 820),
    "washer": (600, 600, 850),
    "bath": (1700, 700, 600),
    "shower": (900, 900, 2000),
    "toilet": (380, 700, 800),
    "basin": (600, 480, 850),
    "lamp": (500, 500, 300),
    "desk": (1200, 600, 750),
    "nightstand": (450, 400, 500),
    "other": (600, 600, 700),
}

ROOM_KINDS = (
    "living", "bedroom", "kids", "kitchen", "kitchen_living", "bathroom",
    "toilet", "hall", "corridor", "wardrobe", "balcony", "other",
)

# Балкон и лоджия — не часть квартиры: в общую площадь они не входят
# и в сверке с документами не участвуют.
OUTSIDE_KINDS = ("balcony",)


@dataclass
class RoomGuess:
    name: str
    kind: str
    polygon_mm: list[tuple[int, int]]
    area_m2: float
    declared_area_m2: float | None = None
    deviation_percent: float | None = None


@dataclass
class ItemGuess:
    category: str
    subtype: str
    label: str
    x: int
    y: int
    width_mm: int
    depth_mm: int
    height_mm: int
    rotation_deg: float = 0.0
    confidence: float = 1.0


@dataclass
class OpeningGuess:
    kind: str          # door | window
    x: int
    y: int
    width_mm: int


@dataclass
class Analysis:
    """Результат разбора одного листа."""
    page_kind: str = "other"
    mm_per_px: float | None = None
    scale_source: str = ""
    rooms: list[RoomGuess] = field(default_factory=list)
    openings: list[OpeningGuess] = field(default_factory=list)
    items: list[ItemGuess] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    total_area_m2: float = 0.0
    outside_area_m2: float = 0.0
    declared_total_m2: float | None = None
    cost_note: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    alignment: dict[str, Any] | None = None   # насколько подтянули к чертежу


# ── Картинка для модели ───────────────────────────────────────────────────

def render_page(path: Path, page_number: int) -> tuple[bytes, float]:
    """Рисует страницу и говорит, во сколько раз увеличила.

    Возвращает (картинка PNG, пикселей на единицу чертежа).
    """
    from .mupdf import library, open_file

    pymupdf = library()
    with open_file(path) as document:
        page = document[page_number - 1]
        longest = max(page.rect.width, page.rect.height) or 1
        factor = min(3.0, IMAGE_MAX_PX / longest)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(factor, factor))
        return pixmap.tobytes("png"), factor


# ── Запрос к модели ───────────────────────────────────────────────────────

def build_prompt(
    plan: VectorPlan,
    width_px: int,
    height_px: int,
    mm_per_px: float | None = None,
    factor: float = 1.0,
) -> str:
    known: list[str] = []
    if plan.total_area_m2:
        known.append(
            f"Общая площадь квартиры: {plan.total_area_m2} м². "
            "Балкон и лоджия в неё НЕ входят."
        )
    if plan.room_areas:
        listed = ", ".join(f"{v} м²" for v in sorted(plan.room_areas, reverse=True))
        known.append(
            f"На чертеже подписаны площади {len(plan.room_areas)} помещений квартиры: "
            f"{listed}. Ровно столько помещений и должно получиться, не считая "
            "балкона или лоджии."
        )
    if plan.extra_areas:
        count = len(plan.extra_areas)
        listed = ", ".join(f"{v} м²" for v in plan.extra_areas)
        known.append(
            (f"Отдельно подписана площадь балкона или лоджии: {listed}."
             if count == 1 else
             f"Отдельно подписаны площади {count} балконов или лоджий: {listed}.")
            + (" Обведи его тоже и пометь kind = balcony. "
               "Такое помещение здесь ровно одно — больше не ищи."
               if count == 1 else
               f" Обведи их тоже и пометь kind = balcony. "
               f"Таких помещений здесь ровно {count} — больше не ищи.")
        )

    check_block = ""
    if mm_per_px:
        check_block = f"""
ПРОВЕРЬ СЕБЯ. Один пиксель этой картинки = {mm_per_px:.3f} мм.
Площадь в квадратных метрах = площадь многоугольника в пикселях,
умноженная на {mm_per_px:.3f}, ещё раз на {mm_per_px:.3f} и делённая на 1 000 000.
Обведя помещение, посчитай его площадь по своим же точкам и сравни
с подписанной на чертеже. Расходится больше чем на 5 % — подвинь точки
и посчитай заново. Обводи по ВНУТРЕННИМ граням стен, не по осям и не по
наружным граням: именно от внутренних граней считается площадь помещения.
"""

    marks = [d for d in plan.dimensions if d.value_mm >= 500]
    marks_block = ""
    if marks and factor:
        listed = "; ".join(
            f"{d.value_mm} мм около ({round(d.x * factor)}, {round(d.y * factor)})"
            for d in sorted(marks, key=lambda d: -d.value_mm)[:30]
        )
        marks_block = f"""
ОПОРНЫЕ РАЗМЕРЫ. На чертеже есть размерные подписи — значение и место
подписи в пикселях картинки: {listed}.
Используй их, чтобы точно ставить углы помещений.
"""

    known_block = "\n".join(f"- {line}" for line in known) or "- ничего не известно заранее"

    scale_task = "" if (plan.scale_is_reliable or mm_per_px) else """
5. МАСШТАБ. Масштаб этого листа неизвестен. Найди на чертеже подписи
   размеров (числа в миллиметрах рядом с размерными линиями) и для двух-трёх
   из них укажи концы соответствующей размерной линии в пикселях. Бери
   размеры подлиннее — по ним масштаб точнее.
"""

    return f"""Ты разбираешь лист дизайн-проекта квартиры. Отвечай ТОЛЬКО одним
объектом JSON, без пояснений и без markdown.

Ответ должен быть КОМПАКТНЫМ: без отступов и переносов строк, без
рассуждений до или после него. Все проверки делай про себя, в ответ
пиши только итог. Длинный ответ не поместится и пропадёт целиком.

Картинка: {width_px}×{height_px} пикселей. Начало координат — левый верхний
угол, X вправо, Y вниз. ВСЕ координаты в ответе — в пикселях этой картинки.

Что уже известно про эту квартиру:
{known_block}
{check_block}{marks_block}
Задачи:

1. ТИП ЛИСТА. Определи, что на нём: план с расстановкой мебели
   ("plan_with_furniture"), обмерный план с размерами ("dimensioned_plan"),
   развертка стены ("elevation") или иное ("other").

2. ПОМЕЩЕНИЯ. Обведи каждое помещение многоугольником по внутренним
   граням стен. Для прямоугольной комнаты ровно 4 точки, для Г-образной 6.
   Больше 8 точек не используй: лишняя подробность только вредит.
   Название дай по-русски и по назначению: Гостиная, Спальня, Детская, Кухня,
   Кухня-гостиная, Прихожая, Коридор, Гардеробная, Ванная, Санузел, Лоджия.
   Если рядом подписана площадь — укажи её в area_m2.
   Помещения не должны перекрываться.

3. ПРОЁМЫ. Отметь двери и окна: точку в середине проёма и ширину в пикселях.

4. МЕБЕЛЬ, САНТЕХНИКА И ТЕХНИКА. Отметь только то, что занимает место:
   мебель, сантехнику, крупную технику. Мелкий декор, ковры, растения
   и надписи на чертеже пропускай. Для каждого предмета укажи центр,
   ширину и глубину в пикселях, поворот в градусах (0 — предмет стоит
   как нарисован) и уверенность от 0 до 1. Ставь низкую уверенность, если
   не уверен: такие предметы будут подсвечены для проверки человеком.
{scale_task}
Формат ответа:

{{
  "page_kind": "plan_with_furniture",
  "scale_references": [
    {{"mm": 4200, "x1": 100, "y1": 50, "x2": 400, "y2": 50}}
  ],
  "rooms": [
    {{"name": "Спальня", "kind": "bedroom", "area_m2": 19.09,
      "polygon": [[100,100],[400,100],[400,300],[100,300]]}}
  ],
  "openings": [
    {{"kind": "door", "x": 250, "y": 300, "width_px": 40}}
  ],
  "items": [
    {{"category": "furniture", "subtype": "bed", "label": "Двуспальная кровать",
      "x": 250, "y": 180, "width_px": 120, "depth_px": 150,
      "rotation_deg": 0, "confidence": 0.95}}
  ],
  "notes": "что показалось непонятным"
}}

Допустимые kind помещений: {", ".join(ROOM_KINDS)}.
Допустимые category предметов: furniture, plumbing, appliance, light, decor.
Допустимые subtype: {", ".join(sorted(DEFAULT_SIZES))}.

Важно: не выдумывай помещения, которых нет на чертеже, и не меняй
планировку. Лучше отметить низкой уверенностью, чем угадать."""


def _repair_truncated(text: str) -> str | None:
    """Пытается починить ответ, оборвавшийся на полуслове.

    Модель пишет длинный список комнат и предметов и иногда не успевает
    дописать его до конца. Обрывок всё равно ценен: отрезаем недописанный
    кусок по последнему целому элементу и закрываем скобки.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    last_safe = -1

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack:
                stack.pop()
            if stack:
                last_safe = index
        elif char == "," and stack:
            last_safe = index - 1

    if last_safe < 0 or not stack:
        return None

    healed = text[:last_safe + 1]
    # Пересчитываем, что осталось незакрытым после обрезки
    stack = []
    in_string = False
    escaped = False
    for char in healed:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()
    return healed + "".join(reversed(stack))


def parse_answer(text: str, finish_reason: str = "") -> dict[str, Any]:
    """Достаёт JSON из ответа, даже если модель обернула его в markdown
    или не успела дописать."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    if start >= 0:
        end = cleaned.rfind("}")
        cleaned = cleaned[start:end + 1] if end > start else cleaned[start:]

    for candidate in (cleaned, _repair_truncated(cleaned)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            if candidate is not cleaned:
                log.warning("Ответ модели оборвался — восстановлена часть")
            return data

    log.warning(
        "Ответ модели не разобран (окончание: %s): %s",
        finish_reason or "неизвестно", text[:600],
    )
    snippet = text.strip()[:200].replace("\n", " ")
    if finish_reason == "length":
        raise UserError(
            "Ответ получился слишком длинным и оборвался.",
            "Попробуйте ещё раз — обычно со второго раза проходит. "
            "Если повторяется, выберите в «Настройках» другую модель "
            "для чтения чертежа.",
            technical=f"ответ оборван по пределу длины: {snippet}",
        )
    raise UserError(
        "Не удалось разобрать ответ сервиса.",
        "Попробуйте ещё раз. Если повторяется — выберите в «Настройках» "
        "другую модель для чтения чертежа: не все модели умеют отвечать "
        "строго по форме.",
        technical=f"окончание: {finish_reason or 'неизвестно'}; ответ: {snippet}",
    )


# ── Масштаб ───────────────────────────────────────────────────────────────

def scale_from_references(references: list[dict[str, Any]]) -> float | None:
    """Считает миллиметры на пиксель по размерным линиям, найденным моделью."""
    values: list[float] = []
    for reference in references or []:
        try:
            millimetres = float(reference["mm"])
            length = math.hypot(
                float(reference["x2"]) - float(reference["x1"]),
                float(reference["y2"]) - float(reference["y1"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if millimetres > 100 and length > 10:
            values.append(millimetres / length)
    if not values:
        return None
    return statistics.median(values)


# ── Разбор ────────────────────────────────────────────────────────────────

def polygon_area_m2(points: list[tuple[float, float]], mm_per_px: float) -> float:
    if len(points) < 3:
        return 0.0
    doubled = 0.0
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        doubled += x1 * y2 - x2 * y1
    area_px = abs(doubled) / 2
    return area_px * mm_per_px * mm_per_px / 1_000_000


def _match_declared(area: float, available: list[float]) -> float | None:
    """Подбирает подписанную площадь, ближайшую к посчитанной."""
    if not available:
        return None
    best = min(available, key=lambda value: abs(value - area))
    return best if abs(best - area) / max(best, 0.1) < 0.25 else None


def interpret(data: dict[str, Any], plan: VectorPlan, factor: float) -> Analysis:
    """Превращает ответ модели в понятные программе миллиметры."""
    result = Analysis()
    result.page_kind = str(data.get("page_kind") or "other")
    result.notes = str(data.get("notes") or "")[:800]

    if plan.scale_is_reliable and plan.scale_mm_per_unit:
        result.mm_per_px = plan.scale_mm_per_unit / factor
        result.scale_source = "из чертежа"
    else:
        result.mm_per_px = scale_from_references(data.get("scale_references") or [])
        result.scale_source = "по размерным линиям на картинке"
    if not result.mm_per_px:
        raise scale_unknown()

    available = list(plan.room_areas)
    for index, raw in enumerate(data.get("rooms") or []):
        points = [
            (float(p[0]), float(p[1]))
            for p in raw.get("polygon") or []
            if isinstance(p, (list, tuple)) and len(p) >= 2
        ]
        if len(points) < 3:
            continue
        area = round(polygon_area_m2(points, result.mm_per_px), 2)
        declared = raw.get("area_m2")
        try:
            declared = float(declared) if declared is not None else None
        except (TypeError, ValueError):
            declared = None
        if declared is None:
            declared = _match_declared(area, available)
        if declared in available:
            available.remove(declared)

        kind = str(raw.get("kind") or "other")
        result.rooms.append(RoomGuess(
            name=str(raw.get("name") or f"Помещение {index + 1}")[:60],
            kind=kind if kind in ROOM_KINDS else "other",
            polygon_mm=[(round(x * result.mm_per_px), round(y * result.mm_per_px))
                        for x, y in points],
            area_m2=area,
            declared_area_m2=declared,
            deviation_percent=(
                round((area - declared) / declared * 100, 1)
                if declared else None
            ),
        ))

    for raw in data.get("openings") or []:
        try:
            x = round(float(raw["x"]) * result.mm_per_px)
            y = round(float(raw["y"]) * result.mm_per_px)
        except (KeyError, TypeError, ValueError):
            continue
        width = raw.get("width_px")
        width_mm = round(float(width) * result.mm_per_px) if width else 900
        # Дверь уже метра полтора или уже полуметра — это не дверь.
        if not (500 <= width_mm <= 4000):
            width_mm = 900
        kind = "window" if str(raw.get("kind")) == "window" else "door"
        result.openings.append(OpeningGuess(kind, x, y, width_mm))

    for raw in data.get("items") or []:
        subtype = str(raw.get("subtype") or "other")
        default = DEFAULT_SIZES.get(subtype, DEFAULT_SIZES["other"])
        try:
            x = round(float(raw["x"]) * result.mm_per_px)
            y = round(float(raw["y"]) * result.mm_per_px)
        except (KeyError, TypeError, ValueError):
            continue
        width = raw.get("width_px")
        depth = raw.get("depth_px")
        width_mm = round(float(width) * result.mm_per_px) if width else default[0]
        depth_mm = round(float(depth) * result.mm_per_px) if depth else default[1]
        # Совсем крошечные или гигантские габариты — признак промаха модели.
        if not (80 <= width_mm <= 6000) or not (80 <= depth_mm <= 6000):
            width_mm, depth_mm = default[0], default[1]
        try:
            confidence = float(raw.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        result.items.append(ItemGuess(
            category=str(raw.get("category") or "furniture"),
            subtype=subtype,
            label=str(raw.get("label") or subtype)[:80],
            x=x, y=y,
            width_mm=width_mm, depth_mm=depth_mm, height_mm=default[2],
            rotation_deg=float(raw.get("rotation_deg") or 0),
            confidence=max(0.0, min(1.0, confidence)),
        ))

    align_to_drawing(result)

    inside = [r for r in result.rooms if r.kind not in OUTSIDE_KINDS]
    result.total_area_m2 = round(sum(r.area_m2 for r in inside), 2)
    result.outside_area_m2 = round(
        sum(r.area_m2 for r in result.rooms if r.kind in OUTSIDE_KINDS), 2
    )
    result.declared_total_m2 = plan.total_area_m2
    _add_warnings(result, plan)
    return result


def align_to_drawing(result: Analysis) -> None:
    """Подтягивает контуры комнат к точным площадям из чертежа.

    AI обводит комнаты по картинке и промахивается на сантиметры.
    Точные площади при этом уже прочитаны из экспликации. Здесь общие
    линии стен чуть сдвигаются, чтобы площади сошлись, — форма и
    расположение комнат не меняются.
    """
    targets = [r.declared_area_m2 for r in result.rooms]
    if not any(targets):
        return

    before = [list(r.polygon_mm) for r in result.rooms]
    after = align.align_rooms(before, targets)
    changes = align.report(before, after, targets)

    # Если подгонка ничего не улучшила — оставляем как было.
    if changes["error_after_m2"] > changes["error_before_m2"]:
        log.info("Подгонка не помогла, оставляю контуры AI: %s", changes)
        result.alignment = dict(changes, applied=False)
        return

    for room, polygon in zip(result.rooms, after):
        room.polygon_mm = polygon
        room.area_m2 = round(align.area_m2(
            [(float(x), float(y)) for x, y in polygon]
        ), 2)
        if room.declared_area_m2:
            room.deviation_percent = round(
                (room.area_m2 - room.declared_area_m2)
                / room.declared_area_m2 * 100, 1
            )
    result.alignment = dict(changes, applied=True)
    log.info("Контуры подтянуты к чертежу: %s", changes)


def _add_warnings(result: Analysis, plan: VectorPlan) -> None:
    if not result.rooms:
        result.warnings.append("Помещения на этом листе не найдены.")
        return

    inside = [r for r in result.rooms if r.kind not in OUTSIDE_KINDS]
    if plan.room_areas and len(inside) != len(plan.room_areas):
        result.warnings.append(
            f"На чертеже подписано {len(plan.room_areas)} помещений квартиры, "
            f"а распознано {len(inside)}. Балкон и лоджия не считаются."
        )

    outside = [r for r in result.rooms if r.kind in OUTSIDE_KINDS]
    if plan.extra_areas and len(outside) != len(plan.extra_areas):
        word = ("лоджия или балкон" if len(plan.extra_areas) == 1
                else f"{len(plan.extra_areas)} лоджий или балконов")
        result.warnings.append(
            f"На чертеже подписана {word}, а распознано {len(outside)}. "
            "Проверьте в 3D-модели."
        )

    if result.declared_total_m2:
        difference = result.total_area_m2 - result.declared_total_m2
        share = abs(difference) / result.declared_total_m2 * 100
        if share > 5:
            result.warnings.append(
                f"Сумма площадей получилась {result.total_area_m2} м², "
                f"а по документам {result.declared_total_m2} м² "
                f"(расхождение {share:.0f} %). Проверьте планировку."
            )

    crooked = [r for r in result.rooms
               if r.deviation_percent is not None and abs(r.deviation_percent) > 15]
    for room in crooked:
        result.warnings.append(
            f"«{room.name}»: посчитано {room.area_m2} м², "
            f"а подписано {room.declared_area_m2} м²."
        )

    unsure = [i for i in result.items if i.confidence < 0.7]
    if unsure:
        result.warnings.append(
            f"Неуверенно распознано предметов: {len(unsure)}. "
            "Они будут подсвечены для проверки."
        )


# ── Сохранение ────────────────────────────────────────────────────────────

def store(project_id: int, result: Analysis) -> None:
    """Записывает распознанное в базу, заменяя прошлый разбор.

    Стены не приходят от AI — они выводятся из контуров комнат,
    поэтому разойтись с комнатами не могут.
    """
    db.clear_recognised(project_id)
    room_ids: list[int] = []
    for order, room in enumerate(result.rooms):
        room_ids.append(db.add_room(
            project_id=project_id,
            name=room.name,
            kind=room.kind,
            polygon=json.dumps(room.polygon_mm),
            declared_area_m2=room.declared_area_m2,
            sort_order=order,
        ))

    saved_rooms = db.list_rooms(project_id)
    walls = geometry.walls_from_rooms(saved_rooms)
    geometry.attach_openings(walls, [
        {"kind": o.kind, "x": o.x, "y": o.y, "width_mm": o.width_mm}
        for o in result.openings
    ])
    for wall in walls:
        wall_id = db.add_wall(
            project_id, wall.x1, wall.y1, wall.x2, wall.y2,
            wall.thickness_mm, wall.kind,
        )
        for opening in wall.openings:
            db.add_opening(wall_id, **opening)

    for item in result.items:
        db.add_item(
            project_id=project_id,
            category=item.category,
            subtype=item.subtype,
            label=item.label,
            x=item.x, y=item.y,
            width_mm=item.width_mm, depth_mm=item.depth_mm, height_mm=item.height_mm,
            rotation_deg=item.rotation_deg,
            confidence=item.confidence,
        )
    db.touch_project(project_id)


# ── Главная функция ───────────────────────────────────────────────────────

def analyse_page(
    project_id: int,
    path: Path,
    page_number: int,
    is_pdf: bool,
    expected_total_m2: float | None = None,
    provider=None,
) -> Analysis:
    """Разбирает один лист: готовит картинку, спрашивает модель, проверяет ответ."""
    plan = read_plan(path, page_number, expected_total_m2) if is_pdf else VectorPlan()

    if is_pdf:
        image, factor = render_page(path, page_number)
    else:
        image, factor = path.read_bytes(), 1.0

    from .mupdf import library

    pixmap = library().Pixmap(image)
    width_px, height_px = pixmap.width, pixmap.height

    known_mm_per_px = (
        plan.scale_mm_per_unit / factor
        if plan.scale_is_reliable and plan.scale_mm_per_unit else None
    )
    prompt = build_prompt(plan, width_px, height_px, known_mm_per_px, factor)
    service = provider or ai.plan_provider()
    # Запас по длине ответа: список комнат с мебелью бывает объёмным,
    # а оборванный ответ теряется целиком.
    answer = service.ask(ai.plan_model(), prompt, images=[image], max_tokens=32000)

    result = interpret(parse_answer(answer.text, answer.finish_reason), plan, factor)
    result.input_tokens = answer.input_tokens
    result.output_tokens = answer.output_tokens
    log.info(
        "Лист разобран: помещений %s, предметов %s, токенов %s/%s",
        len(result.rooms), len(result.items), answer.input_tokens, answer.output_tokens,
    )
    return result


# ── Починка уже сохранённого ──────────────────────────────────────────────

# Отметка «уже починено». Номер меняется, когда починка научилась
# чему-то новому: иначе у тех, кто обновился, ничего не пересчитается.
# v2 — стены строятся в промежутках между комнатами, а не поперёк граней.
# v3 — убраны лишние квадраты на перекрестиях перегородок.
ALIGNED_MARK = "rooms_aligned_v3"


def realign_saved_rooms(project_id: int) -> dict[str, Any] | None:
    """Подтягивает к чертежу комнаты, разобранные до появления подгонки.

    Нужно, чтобы уже сделанный (и уже оплаченный) разбор стал точнее
    сам, без повторного обращения к сервису.
    """
    rooms = db.list_rooms(project_id)
    targets = [r.get("declared_area_m2") for r in rooms]
    if not rooms or not any(targets):
        return None

    before = [geometry.parse_polygon(r.get("polygon", "[]")) for r in rooms]
    after = align.align_rooms(before, targets)
    changes = align.report(before, after, targets)
    if changes["error_after_m2"] > changes["error_before_m2"]:
        log.info("Проект %s: подгонка не помогла, оставляю как есть", project_id)
        return dict(changes, applied=False)

    for room, polygon in zip(rooms, after):
        db.set_room_polygon(room["id"], json.dumps(polygon))
        room["polygon"] = polygon

    # Стены выводятся из комнат, поэтому их надо переложить заново.
    # Двери и окна хранятся смещением вдоль стены — возвращаем им
    # место на плане по старым стенам, чтобы привязать к новым.
    openings: list[dict[str, Any]] = []
    for wall in db.list_walls(project_id):
        length = math.hypot(wall["x2"] - wall["x1"], wall["y2"] - wall["y1"])
        if length <= 0:
            continue
        for hole in wall["openings"]:
            share = hole["offset_mm"] / length
            openings.append({
                "kind": hole["kind"],
                "x": wall["x1"] + (wall["x2"] - wall["x1"]) * share,
                "y": wall["y1"] + (wall["y2"] - wall["y1"]) * share,
                "width_mm": hole["width_mm"],
            })
    db.clear_walls(project_id)
    walls = geometry.walls_from_rooms(db.list_rooms(project_id))
    if openings:
        geometry.attach_openings(walls, openings)
    for wall in walls:
        wall_id = db.add_wall(
            project_id, wall.x1, wall.y1, wall.x2, wall.y2,
            wall.thickness_mm, wall.kind,
        )
        for opening in wall.openings:
            db.add_opening(wall_id, **opening)

    log.info("Проект %s: контуры подтянуты к чертежу %s", project_id, changes)
    return dict(changes, applied=True)
