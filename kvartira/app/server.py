"""Веб-сервер приложения «Моя квартира».

Отдаёт страницы из папки web/ и простой JSON-интерфейс для них.
Всё работает на вашем компьютере, наружу ничего не открывается.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import dataclasses

from . import __version__, ai, db, plan_analysis, plan_files, plan_vector, storage
from .config import ENV_PATH, WEB_DIR, clean_secret, settings
from .errors import (
    UserError,
    get_logger,
    key_has_strange_characters,
    project_not_found,
)

log = get_logger()

app = FastAPI(title="Моя квартира", version=__version__, docs_url=None, redoc_url=None)


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
    _require_project(project_id)
    return {
        "rooms": db.list_rooms(project_id),
        "items": db.list_items(project_id),
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
    db.init_db()
    return app
