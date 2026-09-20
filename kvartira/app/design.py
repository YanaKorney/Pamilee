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

# Задание художнику — один абзац. Больше просить незачем, а сервис
# держит на счету сумму, посчитанную именно по этому числу.
MAX_BRIEF_TOKENS = 600

SIZES = {
    "wide": "1536x1024",
    "square": "1024x1024",
}


def brief(facts: RoomFacts, style: str, wishes: str,
          references: int = 0, colours: list[dict[str, Any]] | None = None) -> str:
    """Что мы просим сочинить — по-русски, чтобы было видно человеку.

    Когда приложены референсы, главный тут они, а не название стиля.
    Иначе выходит наоборот: «уютный современный» рисуется по своему
    разумению, а картинки, ради которых всё затевалось, остаются
    припиской.
    """
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

    if references:
        lines += [
            "",
            "ГЛАВНОЕ — КАРТИНКИ «ВОТ ТАК МНЕ НРАВИТСЯ».",
            f"Их приложено: {references}. Они показывают желаемый вид точнее",
            "любых слов, поэтому именно по ним и надо рисовать.",
            "",
            "Разгляди их и перенеси в задание:",
            "— какого цвета фасады, стены, пол, столешница;",
            "— из чего всё сделано: дерево и какое, камень, крашеный МДФ,",
            "  стекло, металл и какой — латунь, чёрный, сталь;",
            "— матовое или глянцевое, гладкое или с фактурой;",
            "— каким должен быть свет: тёплый или холодный, откуда идёт,",
            "  есть ли подсветка под шкафами, какие светильники;",
            "— общее настроение: светлое и воздушное или тёмное и плотное.",
        ]
        if colours:
            lines += [
                "",
                "Главные цвета с этих картинок посчитаны точно:",
            ]
            for colour in colours:
                lines.append(
                    f"— {colour['hex']} ({colour['english']}, "
                    f"{colour['по-русски']}) — {colour['доля']} % картинки"
                )
            lines.append(
                "Держись этих цветов. Если на картинках темно — рисуй тёмное,"
            )
            lines.append(
                "если светло — светлое. Не подменяй их «уютными» по умолчанию."
            )
        lines += [
            "",
            f"Название направления — «{style_words}» — тут лишь подсказка.",
            "Если оно расходится с картинками, слушай картинки.",
            "Планировку с картинок НЕ бери: комната своя, её размеры выше.",
        ]
    else:
        lines += [
            "",
            f"ЖЕЛАЕМЫЙ ДИЗАЙН: {style_words}",
        ]

    if wishes.strip():
        lines += ["", f"ПОЖЕЛАНИЯ ХОЗЯЙКИ (важнее всего): {wishes.strip()}"]

    lines += [
        "",
        "Правила:",
        "1. Ответь ОДНИМ абзацем на английском языке — это и есть задание",
        "   художнику. Никаких пояснений, заголовков и кавычек.",
        "2. Обязательно назови пропорции комнаты, высоту потолка и окна:",
        "   картинка должна быть про ЭТУ комнату, а не про похожую.",
        "3. Называй цвета и материалы конкретно, словами и оттенками,",
        "   а не общими словами вроде «уютный» и «стильный».",
        "4. Проси фотореалистичный вид интерьера с уровня глаз, дневной свет.",
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
    colours = main_colours(references or [])
    answer = service.ask(
        ai.design_model(),
        brief(facts, style, wishes, len(references or []), colours),
        images=references or None,
        # Просим ровно столько, сколько нужно на один абзац. Сервис
        # резервирует деньги по этому числу, а не по факту: оставишь
        # его большим — и на счету «зависнет» лишнее.
        max_tokens=MAX_BRIEF_TOKENS,
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
    # Проверяем ДО того, как платить за описание: если рисовать нечем,
    # незачем тратиться на задание художнику.
    if image_provider is None:
        ai.check_draws(ai.image_model())

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
        references=", ".join(reference_names),
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


# ── Палитра референса ────────────────────────────────────────────────

# Цвета, которые человек различает и называет словами. Модели, которая
# рисует, понятнее «warm walnut brown», чем шестнадцатеричный код.
COLOUR_WORDS = (
    ((255, 255, 255), "белый", "white"),
    ((245, 240, 230), "тёплый белый", "warm white"),
    ((235, 228, 214), "кремовый", "cream"),
    ((214, 199, 176), "песочный", "sand beige"),
    ((190, 165, 130), "светлое дерево", "light oak"),
    ((150, 115, 75), "медовое дерево", "honey wood"),
    ((110, 78, 48), "орех", "walnut brown"),
    ((70, 48, 30), "тёмное дерево", "dark wood"),
    ((45, 35, 28), "почти чёрный тёплый", "near-black brown"),
    ((25, 25, 25), "чёрный", "black"),
    ((200, 200, 198), "светло-серый", "light grey"),
    ((140, 140, 138), "серый", "mid grey"),
    ((85, 85, 85), "тёмно-серый", "charcoal grey"),
    ((180, 190, 175), "шалфейный", "sage green"),
    ((110, 130, 110), "приглушённый зелёный", "muted green"),
    ((55, 75, 60), "тёмно-зелёный", "deep green"),
    ((165, 185, 200), "голубовато-серый", "dusty blue"),
    ((70, 95, 120), "синий", "slate blue"),
    ((195, 150, 140), "терракотовый светлый", "blush terracotta"),
    ((160, 95, 70), "терракотовый", "terracotta"),
    ((120, 60, 55), "бордовый", "deep rust"),
    ((205, 175, 120), "латунь", "brass"),
)


def _closest_word(colour: tuple[int, int, int]) -> tuple[str, str]:
    red, green, blue = colour
    best = min(
        COLOUR_WORDS,
        key=lambda row: (row[0][0] - red) ** 2
        + (row[0][1] - green) ** 2
        + (row[0][2] - blue) ** 2,
    )
    return best[1], best[2]


def palette(picture: bytes, count: int = 5) -> list[dict[str, Any]]:
    """Достаёт из картинки главные цвета.

    Пересказ словами теряет именно цвет: «тёплый и уютный» можно
    нарисовать и светлым дубом, и тёмным орехом. Поэтому цвета
    считаем сами и передаём числами.
    """
    import pymupdf

    document = pymupdf.open(stream=picture, filetype="png")
    try:
        page = document[0]
        side = max(page.rect.width, page.rect.height) or 1
        zoom = min(1.0, 96 / side)
        small = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        pixels = small.samples
    finally:
        document.close()

    # Складываем близкие оттенки в корзины, иначе каждый пиксель свой.
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for start in range(0, len(pixels) - 2, 3):
        red, green, blue = pixels[start], pixels[start + 1], pixels[start + 2]
        key = (red // 32, green // 32, blue // 32)
        row = buckets.setdefault(key, [0, 0, 0, 0])
        row[0] += red
        row[1] += green
        row[2] += blue
        row[3] += 1

    total = sum(row[3] for row in buckets.values()) or 1
    order = sorted(buckets.values(), key=lambda row: -row[3])[:count]

    found: list[dict[str, Any]] = []
    for row in order:
        colour = (row[0] // row[3], row[1] // row[3], row[2] // row[3])
        russian, english = _closest_word(colour)
        found.append({
            "hex": "#%02x%02x%02x" % colour,
            "по-русски": russian,
            "english": english,
            "доля": round(row[3] / total * 100),
        })
    return found


def main_colours(pictures: list[bytes], count: int = 5) -> list[dict[str, Any]]:
    """Главные цвета всех референсов вместе."""
    if not pictures:
        return []
    together: dict[str, dict[str, Any]] = {}
    for picture in pictures:
        try:
            found = palette(picture, count)
        except Exception as trouble:
            log.warning("Палитру референса прочитать не удалось: %s", trouble)
            continue
        for colour in found:
            row = together.setdefault(colour["hex"], dict(colour, доля=0))
            row["доля"] += colour["доля"]

    rows = sorted(together.values(), key=lambda row: -row["доля"])[:count]
    total = sum(row["доля"] for row in rows) or 1
    for row in rows:
        row["доля"] = round(row["доля"] / total * 100)
    return rows
