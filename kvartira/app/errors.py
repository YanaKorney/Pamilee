"""Ошибки человеческим языком.

Правило проекта: на экран пользователю попадает понятное сообщение,
а технические подробности уходят в logs/app.log.
"""

from __future__ import annotations

import logging
import logging.handlers
from typing import Any

from .config import LOGS_DIR

_LOG_READY = False


def get_logger(name: str = "kvartira") -> logging.Logger:
    """Логгер, который пишет в logs/app.log и в консоль."""
    global _LOG_READY
    logger = logging.getLogger(name)
    if not _LOG_READY:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger("kvartira")
        root.setLevel(logging.INFO)
        file_handler = logging.handlers.RotatingFileHandler(
            LOGS_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
        )
        root.addHandler(file_handler)
        _LOG_READY = True
    return logger


class UserError(Exception):
    """Ошибка, которую не стыдно показать человеку.

    message — что случилось, простыми словами.
    hint    — что с этим делать.
    """

    def __init__(
        self,
        message: str,
        hint: str = "",
        status: int = 400,
        technical: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status
        # Короткая строка для разработчика. Человеку показывается мелким
        # шрифтом под подсказкой — чтобы было что переслать, если не помогло.
        self.technical = technical

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "hint": self.hint, "technical": self.technical}


# ── Готовые сообщения для типовых ситуаций ────────────────────────────────

def project_not_found() -> UserError:
    return UserError(
        "Проект не найден.",
        "Возможно, он был удалён. Вернитесь на главную и выберите другой.",
        status=404,
    )


def unsupported_file(name: str) -> UserError:
    return UserError(
        f"Файл «{name}» не поддерживается.",
        "Подойдут PDF, JPG, PNG или WEBP.",
    )


def plan_not_recognised() -> UserError:
    return UserError(
        "Не удалось определить планировку.",
        "Попробуйте загрузить файл лучшего качества или укажите комнаты вручную.",
    )


def scale_unknown() -> UserError:
    return UserError(
        "Не удалось определить масштаб помещения.",
        "Укажите размер одной стены вручную — остальное пересчитается само.",
    )


def ai_not_configured(what: str) -> UserError:
    return UserError(
        f"Сервис «{what}» не настроен.",
        "Откройте файл .env рядом с программой и впишите ключ доступа. "
        "Как это сделать — написано в README.md.",
    )


def render_failed() -> UserError:
    return UserError(
        "Не удалось создать визуализацию.",
        "Попробовать ещё раз?",
    )
