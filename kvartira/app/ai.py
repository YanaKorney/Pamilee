"""Связь программы с AI-сервисом.

Здесь решается, какой сервис и какая модель сейчас используются,
и здесь же живёт проверка доступа — та самая кнопка «Проверить доступ».
"""

from __future__ import annotations

from typing import Any

from . import db
from .config import settings
from .errors import UserError, ai_not_configured, get_logger
from .providers import ModelInfo, OpenAiCompatProvider

log = get_logger()

# Ключи в таблице настроек
PLAN_MODEL_KEY = "plan_model"
IMAGE_MODEL_KEY = "image_model"


def plan_model() -> str:
    """Модель, которая читает чертёж. Выбор из интерфейса важнее .env."""
    return db.get_setting(PLAN_MODEL_KEY) or settings.plan_model


def image_model() -> str:
    """Модель, которая рисует визуализации."""
    return db.get_setting(IMAGE_MODEL_KEY) or settings.image_model


def plan_provider() -> OpenAiCompatProvider:
    if not settings.plan_ready:
        raise ai_not_configured("Чтение чертежа")
    return OpenAiCompatProvider(
        settings.plan_base_url, settings.plan_api_key, "Чтение чертежа"
    )


def image_provider() -> OpenAiCompatProvider:
    if not settings.image_ready:
        raise ai_not_configured("Создание визуализаций")
    return OpenAiCompatProvider(
        settings.image_base_url, settings.image_api_key, "Создание визуализаций"
    )


def catalogue() -> dict[str, list[dict[str, Any]]]:
    """Список моделей сервиса, разложенный по назначению."""
    provider = plan_provider()
    models = provider.list_models()

    # Если картинки берутся у того же сервиса — второй запрос не нужен.
    if (settings.image_base_url.rstrip("/") != settings.plan_base_url.rstrip("/")
            or settings.image_api_key != settings.plan_api_key):
        try:
            models += image_provider().list_models()
        except UserError as exc:
            log.warning("Каталог второго сервиса не получен: %s", exc.message)

    seen: set[str] = set()
    text: list[dict[str, Any]] = []
    image: list[dict[str, Any]] = []
    for model in models:
        if model.id in seen:
            continue
        seen.add(model.id)
        row = {"id": model.id, "title": model.title, "vendor": model.vendor}
        if model.kind == "image":
            image.append(row)
        elif model.kind == "text":
            text.append(row)
    return {"text": text, "image": image}


def _service_report(
    title: str,
    ready: bool,
    chosen: str,
    provider_factory,
    want_kind: str,
    probe: bool,
) -> dict[str, Any]:
    """Проверяет один сервис и описывает результат человеческим языком."""
    report: dict[str, Any] = {
        "title": title,
        "model": chosen,
        "ok": False,
        "message": "",
        "hint": "",
        "models_found": 0,
        "model_available": False,
        "technical": "",
    }

    if not ready:
        report["message"] = "Ключ доступа не вписан."
        report["hint"] = "Откройте файл .env рядом с программой и впишите ключ."
        return report

    try:
        provider = provider_factory()
        models = provider.list_models()
    except UserError as exc:
        report["message"] = exc.message
        report["hint"] = exc.hint
        report["technical"] = exc.technical
        return report

    report["models_found"] = len(models)
    available = {m.id for m in models}
    report["model_available"] = chosen in available

    if not report["model_available"]:
        similar = _closest(chosen, [m for m in models if m.kind == want_kind])
        report["message"] = f"Ключ работает, но модели «{chosen}» в списке нет."
        report["hint"] = (
            f"Выберите модель из списка ниже. Похоже подходит: {similar}."
            if similar else "Выберите модель из списка ниже."
        )
        return report

    if probe:
        try:
            answer = provider.ask(
                chosen,
                "Ответь одним словом: готов",
                max_tokens=16,
            )
        except UserError as exc:
            report["message"] = exc.message
            report["hint"] = exc.hint
            report["technical"] = exc.technical
            return report
        log.info("Проверка %s прошла, ответ: %r", title, answer.text[:40])

    report["ok"] = True
    report["message"] = "Работает."
    report["hint"] = f"Моделей в каталоге: {len(models)}."
    return report


def _closest(wanted: str, models: list[ModelInfo]) -> str:
    """Подсказывает похожее название, если выбранной модели нет."""
    if not models:
        return ""
    parts = [p for p in wanted.lower().replace("_", "-").split("-") if p]
    best, best_score = "", 0
    for model in models:
        lowered = model.id.lower()
        score = sum(1 for part in parts if part in lowered)
        if score > best_score:
            best, best_score = model.id, score
    return best if best_score else models[0].id


def check_access() -> dict[str, Any]:
    """Полная проверка: ключи, связь, наличие выбранных моделей."""
    same_service = (
        settings.plan_base_url.rstrip("/") == settings.image_base_url.rstrip("/")
        and settings.plan_api_key == settings.image_api_key
    )

    plan = _service_report(
        "Чтение чертежа", settings.plan_ready, plan_model(),
        plan_provider, "text", probe=True,
    )
    image = _service_report(
        "Создание визуализаций", settings.image_ready, image_model(),
        image_provider, "image", probe=False,
    )
    return {"plan": plan, "image": image, "same_service": same_service}


# ── Расходы ───────────────────────────────────────────────────────────────

def text_cost_rub(input_tokens: int, output_tokens: int) -> float:
    """Во сколько обошёлся запрос к текстовой модели, в рублях."""
    return round(
        input_tokens / 1_000_000 * settings.price_in_rub_per_million
        + output_tokens / 1_000_000 * settings.price_out_rub_per_million,
        2,
    )


def record_spend(kind: str, amount_rub: float, note: str = "") -> None:
    from datetime import date
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO spend (day, kind, amount_usd, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (date.today().isoformat(), kind, amount_rub, note, db.now()),
        )


def spent_today_rub() -> float:
    from datetime import date
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM spend WHERE day = ?",
            (date.today().isoformat(),),
        ).fetchone()
    return round(float(row["total"]), 2)


def check_daily_limit(expected_rub: float = 0.0) -> None:
    """Не даёт превысить дневной лимит трат."""
    spent = spent_today_rub()
    limit = settings.daily_limit_rub
    if spent + expected_rub > limit:
        raise UserError(
            f"Достигнут дневной лимит трат: {limit:.0f} ₽.",
            f"Сегодня уже потрачено {spent:.2f} ₽. Лимит меняется в файле .env, "
            "строка DAILY_LIMIT_RUB. Он существует, чтобы случайное нажатие "
            "не опустошило счёт.",
        )
