"""Общий вид любого AI-сервиса.

Программа обращается только к этим методам. Каким сервисом они
выполнены — AITunnel, ProxyAPI или напрямую Anthropic и Google —
остальной программе знать не нужно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ModelInfo:
    """Одна модель в каталоге сервиса."""
    id: str
    title: str = ""
    vendor: str = ""
    kind: str = "text"          # text | image | other
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TextAnswer:
    """Ответ текстовой модели."""
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class AiProvider(Protocol):
    """То, что программа ожидает от любого сервиса."""

    name: str

    def list_models(self) -> list[ModelInfo]:
        """Каталог доступных моделей."""

    def ask(
        self,
        model: str,
        prompt: str,
        images: list[bytes] | None = None,
        pdf: bytes | None = None,
        max_tokens: int = 8000,
    ) -> TextAnswer:
        """Задать вопрос модели, приложив картинки или PDF."""


# Как понять, что модель умеет рисовать. Каталоги у разных сервисов
# устроены по-разному, поэтому смотрим и на признаки, и на название.
IMAGE_HINTS = (
    "flux", "image", "dall-e", "dalle", "midjourney", "recraft",
    "stable-diffusion", "sd3", "sdxl", "imagen", "banana", "grok-image",
)

TEXT_ONLY_HINTS = ("embed", "whisper", "tts", "rerank", "moderation")


def guess_kind(model_id: str, raw: dict[str, Any] | None = None) -> str:
    """Определяет по названию и описанию, рисует модель или пишет текст."""
    raw = raw or {}

    modality = str(
        raw.get("output_modalities")
        or raw.get("architecture", {}).get("output_modalities", "")
        or raw.get("type", "")
    ).lower()
    if "image" in modality:
        return "image"

    lowered = model_id.lower()
    if any(hint in lowered for hint in TEXT_ONLY_HINTS):
        return "other"
    if any(hint in lowered for hint in IMAGE_HINTS):
        return "image"
    return "text"
