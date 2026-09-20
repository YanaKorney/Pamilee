"""Веб-сервер приложения «Моя квартира».

Отдаёт страницы из папки web/ и простой JSON-интерфейс для них.
Всё работает на вашем компьютере, наружу ничего не открывается.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import dataclasses
import json

from contextlib import asynccontextmanager

from . import __version__, ai, db, design, geometry, plan_analysis, plan_files, plan_vector, storage
from .config import ENV_PATH, WEB_DIR, clean_secret, settings
from .errors import (
    UserError,
    get_logger,
    key_has_strange_characters,
    project_not_found,
)

log = get_logger()


def repair_old_projects() -> None:
    """Досчитывает то, чего не умели прошлые версии программы.

    Разбор плана стоит денег, поэтому заново его не запрашиваем:
    всё, что можно улучшить по уже сохранённому, программа делает сама
    и молча — человеку ничего нажимать не надо.
    """
    if db.get_setting(plan_analysis.ALIGNED_MARK) == "да":
        return
    for project in db.list_projects():
        try:
            plan_analysis.realign_saved_rooms(int(project["id"]))
        except Exception as trouble:        # одна кривая запись не должна
            log.warning(                    # мешать программе запуститься
                "Не удалось подтянуть проект %s: %s", project.get("id"), trouble
            )
    db.set_setting(plan_analysis.ALIGNED_MARK, "да")



@asynccontextmanager
async def on_start(_: FastAPI):
    """Что программа делает сама при каждом запуске."""
    db.init_db()
    repair_old_projects()
    yield


app = FastAPI(
    title="Моя квартира", version=__version__,
    docs_url=None, redoc_url=None, lifespan=on_start,
)


# ── Обработка ошибок ──────────────────────────────────────────────────────

@app.exception_handler(UserError)
async def user_error_handler(_: Request, exc: UserError) -> JSONResponse:
    log.warning("Показано пользователю: %s | %s", exc.message, exc.hint)
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Техническая подробность — в лог, человеку — понятная фраза.
    log.exception("Непредвиденная ошибка: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Что-то пошло не так.",
            "hint": "Подробности записаны в файл logs/app.log. Попробуйте ещё раз.",
        },
    )


# ── Данные, которые приходят с формы ──────────────────────────────────────

class ProjectIn(BaseModel):
    name: str = Field(default="Моя квартира", max_length=120)
    ceiling_height_mm: int = Field(default=0, ge=0, le=6000)
    declared_area_m2: float | None = Field(default=None, ge=0, le=10000)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    ceiling_height_mm: int | None = Field(default=None, ge=1500, le=6000)
    declared_area_m2: float | None = Field(default=None, ge=0, le=10000)
    note: str | None = None


# ── Служебные адреса ──────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@app.get("/api/status")
def status() -> dict[str, Any]:
    """Что настроено, а что ещё нет. Используется на странице «Настройки»."""
    return {
        "version": __version__,
        "ceiling_height_mm": settings.ceiling_height_mm,
        "daily_limit_rub": settings.daily_limit_rub,
        "spent_today_rub": ai.spent_today_rub(),
        "services": {
            "plan": {
                "title": "Чтение чертежа",
                "ready": settings.plan_ready,
                "model": ai.plan_model(),
            },
            "image": {
                "title": "Создание визуализаций",
                "ready": settings.image_ready,
                "model": ai.image_model(),
            },
        },
    }


# ── Проекты ───────────────────────────────────────────────────────────────

@app.get("/api/projects")
def api_list_projects() -> list[dict[str, Any]]:
    return db.list_projects()


@app.post("/api/projects", status_code=201)
def api_create_project(data: ProjectIn) -> dict[str, Any]:
    name = data.name.strip() or "Моя квартира"
    height = data.ceiling_height_mm or settings.ceiling_height_mm
    project_id = db.create_project(name, height, data.declared_area_m2)
    storage.ensure_project_dirs(project_id)
    log.info("Создан проект %s: %s", project_id, name)
    project = db.get_project(project_id)
    assert project is not None
    return project


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: int) -> dict[str, Any]:
    project = db.get_project(project_id)
    if project is None:
        raise project_not_found()
    project.update(db.project_stats(project_id))
    project["disk_mb"] = storage.disk_usage_mb(project_id)
    return project


@app.patch("/api/projects/{project_id}")
def api_update_project(project_id: int, data: ProjectPatch) -> dict[str, Any]:
    if db.get_project(project_id) is None:
        raise project_not_found()
    fields = {k: v for k, v in data.model_dump().items() if v is not None}
    if fields:
        db.update_project(project_id, **fields)
    project = db.get_project(project_id)
    assert project is not None
    return project


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: int) -> dict[str, Any]:
    if not db.delete_project(project_id):
        raise project_not_found()
    storage.remove_project_dir(project_id)
    log.info("Удалён проект %s", project_id)
    return {"ok": True}


# ── Файлы проекта ─────────────────────────────────────────────────────────

def _require_project(project_id: int) -> dict[str, Any]:
    project = db.get_project(project_id)
    if project is None:
        raise project_not_found()
    return project


def _safe_stored_name(name: str) -> str:
    """Защита от попытки выйти за пределы папки проекта."""
    cleaned = Path(name).name
    if not cleaned or cleaned != name:
        raise UserError("Файл не найден.", "Обновите страницу.", status=404)
    return cleaned


@app.post("/api/projects/{project_id}/files", status_code=201)
async def api_upload_files(
    project_id: int,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Приём загруженных планов. Можно выбрать сразу несколько файлов."""
    _require_project(project_id)
    if not files:
        raise UserError("Вы не выбрали ни одного файла.", "Нажмите «Выбрать файлы».")

    added: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for upload in files:
        name = upload.filename or "файл"
        try:
            result = plan_files.ingest(project_id, name, await upload.read())
            added.append({
                "original_name": result.original_name,
                "stored_name": result.stored_name,
                "pages": result.pages,
                "is_vector": result.is_vector,
            })
        except UserError as exc:
            # Один плохой файл не должен ломать загрузку остальных.
            problems.append({"name": name, "error": exc.message, "hint": exc.hint})

    if not added and problems:
        first = problems[0]
        raise UserError(first["error"], first["hint"])

    return {"added": added, "problems": problems}


@app.get("/api/projects/{project_id}/documents")
def api_list_documents(project_id: int) -> list[dict[str, Any]]:
    _require_project(project_id)
    return plan_files.group_documents(db.list_files(project_id, kind="plan"))


@app.delete("/api/projects/{project_id}/documents/{stored_name}")
def api_delete_document(project_id: int, stored_name: str) -> dict[str, Any]:
    _require_project(project_id)
    removed = plan_files.delete_document(project_id, _safe_stored_name(stored_name))
    return {"ok": True, "removed_pages": removed}


@app.patch("/api/files/{file_id}/label")
def api_set_label(file_id: int, body: dict[str, str]) -> dict[str, Any]:
    if db.get_file(file_id) is None:
        raise UserError("Файл не найден.", "Обновите страницу.", status=404)
    db.set_file_label(file_id, (body.get("label") or "").strip()[:120])
    return {"ok": True}


def _file_response(file_id: int, preview: bool) -> FileResponse:
    row = db.get_file(file_id)
    if row is None:
        raise UserError("Файл не найден.", "Обновите страницу.", status=404)
    base = storage.project_dir(row["project_id"])
    if preview and row["preview_name"]:
        path = base / "previews" / row["preview_name"]
        media = "image/png" if path.suffix == ".png" else "image/jpeg"
    else:
        path = base / "uploads" / row["stored_name"]
        media = row["mime"]
    if not path.exists():
        raise UserError(
            "Файл на диске не найден.",
            "Возможно, папку data перемещали. Загрузите файл заново.",
            status=404,
        )
    return FileResponse(path, media_type=media, filename=row["original_name"])


@app.get("/api/files/{file_id}/preview")
def api_file_preview(file_id: int) -> FileResponse:
    return _file_response(file_id, preview=True)


@app.get("/api/files/{file_id}/raw")
def api_file_raw(file_id: int) -> FileResponse:
    return _file_response(file_id, preview=False)


@app.get("/api/files/{file_id}/geometry")
def api_file_geometry(file_id: int) -> dict[str, Any]:
    """Что удалось прочитать из чертежа без участия AI.

    Для векторного PDF это точный масштаб и площади помещений.
    Для картинки честно возвращается «геометрии нет».
    """
    row = db.get_file(file_id)
    if row is None:
        raise UserError("Файл не найден.", "Обновите страницу.", status=404)

    if row["mime"] != "application/pdf":
        return {"is_vector": False, "reason": "Это изображение, а не чертёж."}

    project = db.get_project(row["project_id"]) or {}
    path = storage.project_dir(row["project_id"]) / "uploads" / row["stored_name"]
    if not path.exists():
        raise UserError(
            "Файл на диске не найден.",
            "Загрузите его заново.",
            status=404,
        )

    plan = plan_vector.read_plan(
        path,
        page_number=row["page_no"] or 1,
        expected_total_m2=project.get("declared_area_m2"),
    )
    data = dataclasses.asdict(plan)
    data["scale_is_reliable"] = plan.scale_is_reliable
    data["rooms_sum_m2"] = plan.rooms_sum_m2
    data["declared_area_m2"] = project.get("declared_area_m2")
    # Размерные подписи наружу отдаём только числом: список длинный,
    # а на экране нужен сам факт, что их нашли.
    data["dimensions_found"] = len(plan.dimensions)
    data["dimensions_matched"] = sum(1 for d in plan.dimensions if d.span_units)
    data.pop("dimensions", None)
    data.pop("areas", None)
    return data


@app.post("/api/projects/{project_id}/files/{file_id}/analyse")
def api_analyse_page(project_id: int, file_id: int) -> dict[str, Any]:
    """«Разобрать план»: AI находит комнаты, проёмы и мебель на листе."""
    project = _require_project(project_id)
    row = db.get_file(file_id)
    if row is None or row["project_id"] != project_id:
        raise UserError("Файл не найден.", "Обновите страницу.", status=404)

    ai.check_daily_limit()

    path = storage.project_dir(project_id) / "uploads" / row["stored_name"]
    if not path.exists():
        raise UserError(
            "Файл на диске не найден.", "Загрузите его заново.", status=404
        )

    result = plan_analysis.analyse_page(
        project_id=project_id,
        path=path,
        page_number=row["page_no"] or 1,
        is_pdf=row["mime"] == "application/pdf",
        expected_total_m2=project.get("declared_area_m2"),
    )
    plan_analysis.store(project_id, result)

    cost = ai.text_cost_rub(result.input_tokens, result.output_tokens)
    ai.record_spend("plan", cost, f"разбор листа {row['original_name']}")

    data = dataclasses.asdict(result)
    data["cost_rub"] = cost
    data["spent_today_rub"] = ai.spent_today_rub()
    return data


@app.get("/api/projects/{project_id}/rooms")
def api_list_rooms(project_id: int) -> dict[str, Any]:
    """Что программа знает о комнатах — чтобы показать это при открытии.

    Разбор плана стоит денег и делается один раз, поэтому его результат
    должен быть виден всегда, а не только в ту минуту, когда он пришёл.
    """
    project = _require_project(project_id)
    rooms = db.list_rooms(project_id)
    items = db.list_items(project_id)

    for room in rooms:
        polygon = geometry.parse_polygon(room.get("polygon", "[]"))
        room["polygon"] = polygon
        room["area_m2"] = round(geometry.polygon_area_m2(polygon), 2)
        declared = room.get("declared_area_m2")
        room["deviation_percent"] = (
            round((room["area_m2"] - declared) / declared * 100, 1)
            if declared else None
        )

    inside = [r for r in rooms if r["kind"] not in plan_analysis.OUTSIDE_KINDS]
    outside = [r for r in rooms if r["kind"] in plan_analysis.OUTSIDE_KINDS]
    return {
        "rooms": rooms,
        "items": items,
        "total_area_m2": round(sum(r["area_m2"] for r in inside), 2),
        "outside_area_m2": round(sum(r["area_m2"] for r in outside), 2),
        "declared_total_m2": project.get("declared_area_m2"),
        "unsure_items": sum(1 for i in items if (i.get("confidence") or 1) < 0.7),
        "rooms_without_doors": geometry.rooms_without_doors(
            rooms, db.list_walls(project_id)
        ),
    }


@app.get("/api/projects/{project_id}/export")
def api_export(project_id: int) -> Response:
    """Отдаёт разобранную планировку одним файлом.

    Нужен, когда в 3D что-то выглядит неправильно: по картинке причину
    видно не всегда, а по этим числам — сразу. Ключей и личных данных
    в файле нет, только геометрия квартиры.
    """
    project = _require_project(project_id)
    rooms = db.list_rooms(project_id)
    walls = db.list_walls(project_id)
    for room in rooms:
        room["polygon"] = geometry.parse_polygon(room.get("polygon", "[]"))
        room["area_m2"] = round(geometry.polygon_area_m2(room["polygon"]), 2)

    payload = {
        "версия программы": __version__,
        "квартира": {
            "название": project.get("name"),
            "площадь по документам": project.get("declared_area_m2"),
            "высота потолка": project.get("ceiling_height_mm"),
        },
        "комнаты": rooms,
        "стены": walls,
        "предметы": db.list_items(project_id),
        "комнаты без двери": geometry.rooms_without_doors(rooms, walls),
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition":
                'attachment; filename="planirovka.json"',
        },
    )


# ── Референсы: картинки, которые нравятся ────────────────────────────────

@app.post("/api/projects/{project_id}/references", status_code=201)
async def api_upload_references(
    project_id: int,
    files: list[UploadFile] = File(...),
    room_id: int | None = Form(default=None),
) -> dict[str, Any]:
    """Приём картинок «вот так мне нравится».

    Их можно привязать к комнате, а можно оставить общими на квартиру —
    тогда они годятся для любой.
    """
    _require_project(project_id)
    if not files:
        raise UserError("Вы не выбрали ни одной картинки.",
                        "Нажмите «Выбрать файлы».")

    added: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for upload in files:
        name = upload.filename or "картинка"
        try:
            result = plan_files.ingest(
                project_id, name, await upload.read(),
                kind="reference", room_id=room_id,
            )
            added.append({"original_name": result.original_name,
                          "ids": result.file_ids})
        except UserError as exc:
            problems.append({"name": name, "error": exc.message, "hint": exc.hint})

    if not added and problems:
        first = problems[0]
        raise UserError(first["error"], first["hint"])
    return {"added": added, "problems": problems}


@app.get("/api/projects/{project_id}/references")
def api_list_references(project_id: int) -> dict[str, Any]:
    _require_project(project_id)
    rooms = {int(r["id"]): r["name"] for r in db.list_rooms(project_id)}
    pictures = []
    for row in db.list_files(project_id, kind="reference"):
        pictures.append({
            "id": int(row["id"]),
            "name": row["original_name"],
            "label": row["label"],
            "room_id": row["room_id"],
            "room": rooms.get(row["room_id"]) if row["room_id"] else None,
            "image": f"/api/files/{row['id']}/preview",
        })
    return {"references": pictures}


@app.delete("/api/references/{file_id}")
def api_delete_reference(file_id: int) -> dict[str, bool]:
    row = db.get_file(file_id)
    if row is None or row["kind"] != "reference":
        raise UserError("Картинка не найдена.", "Обновите страницу.", status=404)
    db.delete_file_group(row["project_id"], row["stored_name"])
    return {"deleted": True}


# ── Дизайн: как комната будет выглядеть ──────────────────────────────────

class DesignWish(BaseModel):
    room_id: int
    style: str = Field(default="scandi", max_length=40)
    wishes: str = Field(default="", max_length=600)
    shape: str = Field(default="wide", max_length=20)
    reference_ids: list[int] = Field(default_factory=list)


@app.get("/api/projects/{project_id}/design")
def api_design_room_list(project_id: int) -> dict[str, Any]:
    """Комнаты, стили и уже нарисованное."""
    _require_project(project_id)
    rooms = []
    for room in db.list_rooms(project_id):
        facts = design.room_facts(project_id, int(room["id"]))
        if facts is None:
            continue
        rooms.append({
            "id": int(room["id"]),
            "name": facts.name,
            "kind": facts.kind,
            "area_m2": facts.area_m2,
            "facts": facts.как_текст(),
        })

    pictures = []
    for row in db.list_renders(project_id):
        pictures.append({
            "id": int(row["id"]),
            "room": row["room_name"],
            "style": design.STYLES.get(row["strictness"], row["strictness"]),
            "image": f"/api/renders/{row['id']}/image",
            "cost_rub": row["cost_usd"],
            "references": [name for name in
                           (row["base_image"] or "").split(", ") if name],
        })

    return {
        "rooms": rooms,
        "styles": [{"id": key, "title": title}
                   for key, title in design.STYLES.items()],
        "pictures": pictures,
        "price_rub": ai.image_price_rub(),
        "spent_today_rub": ai.spent_today_rub(),
        "ready": settings.image_ready,
    }


@app.post("/api/projects/{project_id}/design")
def api_design_make(project_id: int, wish: DesignWish) -> dict[str, Any]:
    """«Показать, как будет выглядеть»."""
    _require_project(project_id)
    return design.visualise(
        project_id, wish.room_id, wish.style, wish.wishes, wish.shape,
        reference_ids=wish.reference_ids,
    )


@app.get("/api/renders/{render_id}/image")
def api_render_image(render_id: int) -> FileResponse:
    row = db.get_render(render_id)
    if row is None:
        raise UserError("Картинка не найдена.", "Обновите страницу.", status=404)
    path = (storage.project_dir(_render_project(render_id))
            / "renders" / row["result_image"])
    if not path.exists():
        raise UserError(
            "Файл картинки пропал с диска.",
            "Нарисуйте заново.", status=404,
        )
    return FileResponse(path, media_type="image/png")


@app.delete("/api/renders/{render_id}")
def api_render_delete(render_id: int) -> dict[str, bool]:
    return {"deleted": db.delete_render(render_id)}


def _render_project(render_id: int) -> int:
    row = db.get_render(render_id)
    if row is None:
        raise UserError("Картинка не найдена.", "Обновите страницу.", status=404)
    with db.connect() as conn:
        found = conn.execute(
            "SELECT project_id FROM rooms WHERE id = ?", (row["room_id"],)
        ).fetchone()
    if found is None:
        raise UserError("Картинка не найдена.", "Обновите страницу.", status=404)
    return int(found["project_id"])


@app.get("/api/projects/{project_id}/scene")
def api_scene(project_id: int) -> dict[str, Any]:
    """Всё, что нужно для построения 3D-модели, в миллиметрах."""
    project = _require_project(project_id)
    rooms = db.list_rooms(project_id)
    walls = db.list_walls(project_id)
    items = db.list_items(project_id)

    default_height = int(project.get("ceiling_height_mm") or 2900)
    for room in rooms:
        room["polygon"] = geometry.parse_polygon(room.get("polygon", "[]"))
        room["area_m2"] = geometry.polygon_area_m2(room["polygon"])
        room["height_mm"] = int(room.get("ceiling_height_mm") or default_height)
    for wall in walls:
        wall["height_mm"] = int(wall.get("height_mm") or default_height)

    return {
        "name": project.get("name"),
        "ceiling_height_mm": default_height,
        "bounds": geometry.bounds(
            [{"polygon": r["polygon"]} for r in rooms]
        ),
        "rooms": rooms,
        "walls": walls,
        "items": items,
    }


# ── AI-сервис: каталог моделей, выбор, проверка доступа ───────────────────

class AiKeys(BaseModel):
    plan_key: str | None = Field(default=None, max_length=400)
    image_key: str | None = Field(default=None, max_length=400)
    same_for_both: bool = False


class AiSettingsPatch(BaseModel):
    plan_model: str | None = Field(default=None, max_length=200)
    image_model: str | None = Field(default=None, max_length=200)


@app.get("/api/ai/settings")
def api_ai_settings() -> dict[str, Any]:
    return {
        "plan": {
            "model": ai.plan_model(),
            "ready": settings.plan_ready,
            "base_url": settings.plan_base_url,
            "key_hint": settings.mask(settings.plan_api_key),
        },
        "image": {
            "model": ai.image_model(),
            "ready": settings.image_ready,
            "base_url": settings.image_base_url,
            "key_hint": settings.mask(settings.image_api_key),
        },
        "settings_file": str(ENV_PATH),
    }


@app.patch("/api/ai/settings")
def api_set_ai_settings(data: AiSettingsPatch) -> dict[str, Any]:
    if data.plan_model is not None:
        db.set_setting(ai.PLAN_MODEL_KEY, data.plan_model.strip())
    if data.image_model is not None:
        db.set_setting(ai.IMAGE_MODEL_KEY, data.image_model.strip())
    return api_ai_settings()


@app.put("/api/ai/keys")
def api_set_keys(data: AiKeys) -> dict[str, Any]:
    """Сохраняет ключ доступа, введённый прямо в программе.

    Так человеку не приходится искать скрытый файл .env и открывать
    его Блокнотом — самое хрупкое место во всей настройке.
    """
    plan_key = clean_secret(data.plan_key or "")
    image_key = clean_secret(data.image_key or "")
    if data.same_for_both and plan_key:
        image_key = plan_key

    if not plan_key and not image_key:
        raise UserError(
            "Вы не вписали ключ.",
            "Скопируйте его в личном кабинете сервиса и вставьте в поле.",
        )

    for candidate in (plan_key, image_key):
        if candidate and not candidate.isascii():
            raise key_has_strange_characters()

    settings.set_keys(plan_key or None, image_key or None)
    log.info("Ключи доступа обновлены из интерфейса")
    return api_ai_settings()


@app.get("/api/ai/models")
def api_ai_models() -> dict[str, Any]:
    """Каталог моделей сервиса — для выпадающих списков в настройках."""
    return ai.catalogue()


@app.post("/api/ai/check")
def api_ai_check() -> dict[str, Any]:
    """Кнопка «Проверить доступ»."""
    return ai.check_access()


# ── Страницы ──────────────────────────────────────────────────────────────

def _page(name: str) -> FileResponse:
    return FileResponse(WEB_DIR / name, headers={"Cache-Control": "no-cache"})


@app.get("/")
def page_index() -> FileResponse:
    return _page("index.html")


@app.get("/project/{project_id}")
def page_project(project_id: int) -> FileResponse:
    return _page("project.html")


@app.get("/project/{project_id}/plan")
def page_plan(project_id: int) -> FileResponse:
    return _page("plan.html")


@app.get("/project/{project_id}/design")
def page_design(project_id: int) -> FileResponse:
    return _page("design.html")


@app.get("/project/{project_id}/viewer")
def page_viewer(project_id: int) -> FileResponse:
    return _page("viewer.html")


@app.get("/settings")
def page_settings() -> FileResponse:
    return _page("settings.html")


class FreshFiles(StaticFiles):
    """Отдаёт файлы интерфейса без кеширования.

    Иначе после обновления программы браузер показывает старый экран,
    и человек видит вчерашнюю версию, не понимая почему.
    """

    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", FreshFiles(directory=WEB_DIR), name="static")


def create_app() -> FastAPI:
    return app
