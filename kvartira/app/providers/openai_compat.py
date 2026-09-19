"""Подключение к сервису с привычным форматом запросов (OpenAI-совместимым).

Так устроено большинство агрегаторов — AITunnel, ProxyAPI и другие:
один адрес, один ключ, а модель называется прямо в запросе. Благодаря
этому одна и та же программа работает с Claude, FLUX и чем угодно ещё.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from ..errors import UserError, get_logger
from .base import ModelInfo, TextAnswer, guess_kind

log = get_logger()

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 300.0


class OpenAiCompatProvider:
    """Обращения к сервису-агрегатору."""

    name = "openai-compat"

    def __init__(self, base_url: str, api_key: str, title: str = "AI-сервис") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.title = title

    # ── Служебное ─────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        # Ключ уходит в заголовок запроса, а туда можно только латиницу.
        # Если что-то прилипло при копировании — объясняем это словами,
        # а не роняем программу непонятной ошибкой кодировки.
        if not self.api_key.isascii():
            raise UserError(
                "В ключе доступа есть посторонние символы.",
                "Откройте «Настройки» и вставьте ключ заново — скопируйте "
                "только сам ключ, без пробелов по краям.",
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            follow_redirects=True,
        )

    def _explain(self, response: httpx.Response) -> UserError:
        """Превращает ответ сервиса в понятное человеку сообщение."""
        code = response.status_code
        detail = ""
        try:
            body = response.json()
            detail = str(
                body.get("error", {}).get("message")
                if isinstance(body.get("error"), dict)
                else body.get("error") or body.get("message") or ""
            )
        except Exception:
            detail = response.text[:300]
        log.warning("Сервис %s ответил %s: %s", self.title, code, detail)

        if code in (401, 403):
            return UserError(
                "Сервис не принял ключ доступа.",
                "Проверьте, что ключ в файле .env скопирован целиком, без пробелов "
                "по краям, и что он не был удалён в личном кабинете.",
            )
        if code == 402 or "insufficient" in detail.lower() or "баланс" in detail.lower():
            return UserError(
                "На счету закончились деньги.",
                "Пополните баланс в личном кабинете сервиса и попробуйте снова.",
            )
        if code == 404:
            return UserError(
                "Такой модели у сервиса нет.",
                "Откройте «Настройки» и выберите модель из списка.",
            )
        if code == 429:
            return UserError(
                "Слишком много запросов подряд.",
                "Подождите минуту и попробуйте ещё раз.",
            )
        if code >= 500:
            return UserError(
                "Сервис сейчас недоступен.",
                "Это на их стороне. Попробуйте через несколько минут.",
            )
        return UserError(
            "Сервис не смог выполнить запрос.",
            "Подробности записаны в logs/app.log. Попробуйте ещё раз.",
        )

    def _network_error(self, exc: Exception) -> UserError:
        log.warning("Не удалось соединиться с %s: %s", self.base_url, exc)
        from ..netcheck import diagnose

        found = diagnose(self.base_url, exc)
        log.warning("Разбор связи: %s | %s", found.message, found.technical)
        return UserError(found.message, found.hint, technical=found.technical)

    # ── Каталог моделей ───────────────────────────────────────────────

    def list_models(self) -> list[ModelInfo]:
        try:
            with self._client() as client:
                response = client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc

        if response.status_code != 200:
            raise self._explain(response)

        try:
            payload = response.json()
        except Exception as exc:
            raise UserError(
                "Сервис прислал непонятный ответ.",
                "Проверьте адрес сервиса в настройках.",
            ) from exc

        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise UserError(
                "Сервис прислал пустой список моделей.",
                "Проверьте адрес сервиса и ключ доступа.",
            )

        models: list[ModelInfo] = []
        for row in rows:
            if isinstance(row, str):
                row = {"id": row}
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("id") or row.get("name") or "").strip()
            if not model_id:
                continue
            models.append(ModelInfo(
                id=model_id,
                title=str(row.get("name") or row.get("display_name") or model_id),
                vendor=str(row.get("owned_by") or row.get("vendor") or ""),
                kind=guess_kind(model_id, row),
                raw=row,
            ))
        models.sort(key=lambda m: m.id)
        return models

    # ── Вопрос текстовой модели ───────────────────────────────────────

    def ask(
        self,
        model: str,
        prompt: str,
        images: list[bytes] | None = None,
        pdf: bytes | None = None,
        max_tokens: int = 8000,
    ) -> TextAnswer:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

        for raw_image in images or []:
            encoded = base64.b64encode(raw_image).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
            })

        if pdf is not None:
            encoded = base64.b64encode(pdf).decode("ascii")
            content.append({
                "type": "file",
                "file": {
                    "filename": "plan.pdf",
                    "file_data": f"data:application/pdf;base64,{encoded}",
                },
            })

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }

        try:
            with self._client() as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc

        if response.status_code != 200:
            raise self._explain(response)

        payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"]
            if isinstance(text, list):  # некоторые сервисы отдают список блоков
                text = "".join(
                    part.get("text", "") for part in text if isinstance(part, dict)
                )
        except (KeyError, IndexError, TypeError) as exc:
            raise UserError(
                "Сервис прислал ответ, который не удалось прочитать.",
                "Попробуйте ещё раз или выберите другую модель в настройках.",
            ) from exc

        usage = payload.get("usage") or {}
        finish = ""
        try:
            finish = str(payload["choices"][0].get("finish_reason") or "")
        except (KeyError, IndexError, TypeError):
            pass
        return TextAnswer(
            text=text or "",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(payload.get("model") or model),
            finish_reason=finish,
        )
