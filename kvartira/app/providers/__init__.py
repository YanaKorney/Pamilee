"""Подключения к AI-сервисам.

Здесь всё, что связано с обращениями наружу. Отделено от остальной
программы намеренно: сменить сервис должно быть можно, не трогая
ни базу, ни 3D, ни интерфейс.
"""

from .base import AiProvider, ModelInfo  # noqa: F401
from .openai_compat import OpenAiCompatProvider  # noqa: F401
