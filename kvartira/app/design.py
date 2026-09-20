"""Как комната будет выглядеть в разном дизайне.

Трёхмерная модель оказалась дорогой дорогой к простой цели: увидеть
свою квартиру в разных вариантах отделки. Здесь путь короче.

О комнате программа уже знает главное и знает точно: размеры, площадь,
высоту потолка, где окно, где дверь, что в ней стоит из несъёмного.
Этого хватает, чтобы картинка получилась про ЭТУ комнату, а не про
абстрактную «спальню в скандинавском стиле».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import db, geometry
from .errors import get_logger

log = get_logger()

CEILING_DEFAULT_MM = 2900

# Готовые направления. Свои пожелания можно дописать словами.
STYLES = {
    "scandi": "Скандинавский — светлое дерево, белые стены, простые формы",
    "modern": "Современный — чистые линии, спокойные цвета, много света",
    "classic": "Классический — лепнина, тёплые тона, симметрия",
    "minimal": "Минимализм — только необходимое, ничего лишнего",
    "japandi": "Джапанди — японская сдержанность и скандинавское тепло",
    "loft": "Лофт — кирпич, бетон, металл, открытые коммуникации",
    "provence": "Прованс — пастель, состаренное дерево, цветочные мотивы",
    "cosy": "Уютный современный — мягкие ткани, тёплый свет, дерево",
}

ROOM_WORDS = {
    "living": "гостиная", "bedroom": "спальня", "kitchen": "кухня",
    "bathroom": "ванная", "toilet": "санузел", "hallway": "прихожая",
    "corridor": "коридор", "balcony": "лоджия", "kids": "детская",
    "study": "кабинет", "wardrobe": "гардеробная", "laundry": "постирочная",
}


@dataclass
class RoomFacts:
    """Всё, что известно о комнате точно."""
    name: str
    kind: str
    area_m2: float
    width_mm: int
    depth_mm: int
    height_mm: int
    windows: list[int] = field(default_factory=list)   # ширины проёмов
    doors: list[int] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)     # что не двигается

    def как_текст(self) -> str:
        """Описание по-русски — его человек видит на экране."""
        rows = [
            f"{self.name}: {_number(self.area_m2)} м², "
            f"{_metres(self.width_mm)} × {_metres(self.depth_mm)} м, "
            f"потолок {_metres(self.height_mm)} м."
        ]
        if self.windows:
            rows.append(
                f"Окон: {len(self.windows)} "
                f"(шириной {', '.join(_metres(w) for w in self.windows)} м)."
            )
        else:
            rows.append("Окон нет.")
        if self.doors:
            rows.append(f"Дверей и проходов: {len(self.doors)}.")
        if self.fixed:
            rows.append("Не двигается: " + ", ".join(self.fixed) + ".")
        return " ".join(rows)


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _metres(mm: float) -> str:
    return _number(mm / 1000)


def _near(room_polygon, wall: dict[str, Any]) -> bool:
    """Примыкает ли стена к этой комнате."""
    length = max(1.0, ((wall["x2"] - wall["x1"]) ** 2
                       + (wall["y2"] - wall["y1"]) ** 2) ** 0.5)
    ux, uy = (wall["x2"] - wall["x1"]) / length, (wall["y2"] - wall["y1"]) / length
    nx, ny = -uy, ux
    reach = wall.get("thickness_mm", 120) / 2 + 250
    for share in (0.25, 0.5, 0.75):
        base_x = wall["x1"] + (wall["x2"] - wall["x1"]) * share
        base_y = wall["y1"] + (wall["y2"] - wall["y1"]) * share
        for way in (1, -1):
            point = (base_x + nx * way * reach, base_y + ny * way * reach)
            if geometry._point_inside(point, room_polygon):
                return True
    return False


def room_facts(project_id: int, room_id: int) -> RoomFacts | None:
    """Собирает всё, что известно о комнате, в одно описание."""
    rooms = {int(r["id"]): r for r in db.list_rooms(project_id)}
    room = rooms.get(room_id)
    if room is None:
        return None

    polygon = geometry.parse_polygon(room.get("polygon", "[]"))
    if len(polygon) < 3:
        return None

    xs = [x for x, _ in polygon]
    ys = [y for _, y in polygon]
    project = db.get_project(project_id) or {}

    facts = RoomFacts(
        name=str(room.get("name") or "Помещение"),
        kind=str(room.get("kind") or "other"),
        area_m2=round(geometry.polygon_area_m2(polygon), 2),
        width_mm=max(xs) - min(xs),
        depth_mm=max(ys) - min(ys),
        height_mm=int(room.get("ceiling_height_mm")
                      or project.get("ceiling_height_mm")
                      or CEILING_DEFAULT_MM),
    )

    for wall in db.list_walls(project_id):
        if not wall.get("openings") or not _near(polygon, wall):
            continue
        for hole in wall["openings"]:
            if hole["kind"] == "window":
                facts.windows.append(int(hole["width_mm"]))
            else:
                facts.doors.append(int(hole["width_mm"]))

    # Несъёмное: сантехника, кухня, крупная техника. Их AI трогать нельзя.
    for item in db.list_items(project_id):
        point = (item["x"], item["y"])
        if not geometry._point_inside(point, polygon):
            continue
        if item["category"] in ("plumbing", "appliance"):
            facts.fixed.append(str(item.get("label") or item["subtype"]))

    return facts


# ── Задание художнику ────────────────────────────────────────────────

SIZES = {
    "wide": "1536x1024",
    "square": "1024x1024",
}


def brief(facts: RoomFacts, style: str, wishes: str,
          references: int = 0) -> str:
    """Что мы просим сочинить — по-русски, чтобы было видно человеку."""
    style_words = STYLES.get(style, style or "на ваш вкус")
    room_word = ROOM_WORDS.get(facts.kind, facts.kind)

    lines = [
        "Составь задание для модели, которая рисует интерьеры.",
        "",
        "КОМНАТА (это настоящие размеры, менять их нельзя):",
        f"— назначение: {room_word}",
        f"— площадь {_number(facts.area_m2)} м², "
        f"размеры {_metres(facts.width_mm)} × {_metres(facts.depth_mm)} м",
        f"— высота потолка {_metres(facts.height_mm)} м",
    ]
    if facts.windows:
        lines.append(
            f"— окон: {len(facts.windows)}, шириной "
            + ", ".join(f"{_metres(w)} м" for w in facts.windows)
        )
    else:
        lines.append("— окон нет")
    if facts.doors:
        lines.append(f"— дверей и проходов: {len(facts.doors)}")
    if facts.fixed:
        lines.append("— переносить нельзя: " + ", ".join(facts.fixed))

    lines += [
        "",
        f"ЖЕЛАЕМЫЙ ДИЗАЙН: {style_words}",
    ]
    if wishes.strip():
        lines.append(f"ПОЖЕЛАНИЯ ХОЗЯЙКИ: {wishes.strip()}")

    if references:
        lines += [
            "",
            f"К письму приложено картинок «вот так мне нравится»: {references}.",
            "Посмотри на них и возьми оттуда настроение: цвета, материалы,"
            " фактуры, характер мебели, каким должен быть свет.",
            "Брать оттуда планировку НЕ надо — комната своя, её размеры выше.",
        ]

    lines += [
        "",
        "Правила:",
        "1. Ответь ОДНИМ абзацем на английском языке — это и есть задание",
        "   художнику. Никаких пояснений, заголовков и кавычек.",
        "2. Обязательно назови пропорции комнаты, высоту потолка и окна:",
        "   картинка должна быть про ЭТУ комнату, а не про похожую.",
        "3. Проси фотореалистичный вид интерьера с уровня глаз, дневной свет.",
        "4. Цвет, материалы, мебель и свет — по желаемому дизайну.",
        "5. Не добавляй текст, надписи, подписи и людей на картинку.",
    ]
    return "\n".join(lines)


def compose(facts: RoomFacts, style: str, wishes: str, provider=None,
            references: list[bytes] | None = None):
    """Просит текстовую модель сочинить задание художнику.

    Если хозяйка приложила референсы, модель смотрит на них сама
    и переносит в задание то, что на них нравится: цвет, материалы,
    свет. Планировку с них брать нельзя — комната своя.
    """
    from . import ai

    service = provider or ai.plan_provider()
    answer = service.ask(
        ai.plan_model(),
        brief(facts, style, wishes, len(references or [])),
        images=references or None,
        max_tokens=700,
    )
    text = " ".join(answer.text.split()).strip().strip('"')
    if not text:
        raise_empty()
    return text, answer


def raise_empty():
    from .errors import UserError
    raise UserError(
        "Не удалось составить задание для картинки.",
        "Попробуйте ещё раз или опишите пожелания другими словами.",
    )


# ── Создание картинки ────────────────────────────────────────────────

# Сколько референсов имеет смысл показывать модели за раз. Больше —
# дороже и мутнее: она начинает усреднять вместо того, чтобы взять
# главное.
MAX_REFERENCES = 4

# До каких размеров ужимаем референс перед отправкой. Модели хватает,
# а запрос не раздувается на мегабайты.
REFERENCE_SIDE_PX = 1024


def reference_images(project_id: int, ids: list[int]) -> tuple[list[bytes], list[str]]:
    """Готовит выбранные референсы к отправке модели."""
    from . import mupdf, storage

    pictures: list[bytes] = []
    names: list[str] = []
    for file_id in ids[:MAX_REFERENCES]:
        row = db.get_file(file_id)
        if row is None or row["kind"] != "reference":
            continue
        if int(row["project_id"]) != project_id:
            continue
        path = storage.project_dir(project_id) / "uploads" / row["stored_name"]
        if not path.exists():
            continue
        try:
            pictures.append(_shrink(path, mupdf))
            names.append(str(row["original_name"]))
        except Exception as trouble:
            log.warning("Референс %s не прочитался: %s", file_id, trouble)
    return pictures, names


def _shrink(path, mupdf) -> bytes:
    """Ужимает картинку до разумного размера."""
    with mupdf.open_file(path) as document:
        page = document[0]
        side = max(page.rect.width, page.rect.height) or 1
        zoom = min(1.0, REFERENCE_SIDE_PX / side)
        import pymupdf
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pixmap.tobytes("png")


def visualise(
    project_id: int,
    room_id: int,
    style: str,
    wishes: str = "",
    shape: str = "wide",
    reference_ids: list[int] | None = None,
    text_provider=None,
    image_provider=None,
) -> dict[str, Any]:
    """Рисует, как комната будет выглядеть в выбранном дизайне."""
    from . import ai, storage
    from .errors import UserError

    facts = room_facts(project_id, room_id)
    if facts is None:
        raise UserError(
            "Такой комнаты в проекте нет.",
            "Обновите страницу и выберите комнату заново.",
            status=404,
        )

    ai.check_daily_limit()

    references, reference_names = reference_images(project_id, reference_ids or [])
    prompt, answer = compose(facts, style, wishes, text_provider, references)
    text_cost = ai.text_cost_rub(answer.input_tokens, answer.output_tokens)

    painter = image_provider or ai.image_provider()
    picture = painter.draw(ai.image_model(), prompt, SIZES.get(shape, SIZES["wide"]))

    folder = storage.ensure_project_dirs(project_id) / "renders"
    folder.mkdir(parents=True, exist_ok=True)
    made = db.count_renders(room_id) + 1
    path = storage.unique_path(
        folder, f"{storage.safe_name(facts.name)}-{style}-{made}.png"
    )
    path.write_bytes(picture)

    image_cost = ai.image_price_rub()
    ai.record_spend("design", text_cost + image_cost,
                    f"{facts.name}: {STYLES.get(style, style)}")

    render_id = db.add_render(
        room_id=room_id, style=style, prompt=prompt,
        model=ai.image_model(), result_image=path.name,
        cost_rub=round(text_cost + image_cost, 2),
    )
    log.info("Нарисована визуализация %s для комнаты %s", render_id, room_id)

    return {
        "id": render_id,
        "room": facts.name,
        "facts": facts.как_текст(),
        "style": STYLES.get(style, style),
        "wishes": wishes,
        "references": reference_names,
        "prompt": prompt,
        "image": f"/api/renders/{render_id}/image",
        "cost_rub": round(text_cost + image_cost, 2),
        "spent_today_rub": ai.spent_today_rub(),
    }
