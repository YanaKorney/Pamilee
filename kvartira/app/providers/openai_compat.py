"""Подключение к сервису с привычным форматом запросов (OpenAI-совместимым).

Так устроено большинство агрегаторов — AITunnel, ProxyAPI и другие:
один адрес, один ключ, а модель называется прямо в запросе. Благодаря
этому одна и та же программа работает с Claude, FLUX и чем угодно ещё.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from ..errors import UserError, get_logger
from .base import ModelInfo, TextAnswer, guess_kind

log = get_logger()

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 300.0


class StreamNotSupported(Exception):
    """Сервис не умеет отдавать ответ по частям — попробуем обычным запросом."""


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
        # Человеку — объяснение словами, а точный ответ сервиса кладём
        # в «Подробности»: без него причину потом не найти.
        note = f"{code}: {detail}".strip()[:200]

        if code in (401, 403):
            return UserError(
                "Сервис не принял ключ доступа.",
                "Откройте «Настройки» и вставьте ключ заново — целиком и без "
                "пробелов по краям. Заодно проверьте в личном кабинете сервиса, "
                "что ключ не удалён.",
                technical=note,
            )
        if code == 402 or "insufficient" in detail.lower() or "баланс" in detail.lower():
            return UserError(
                "На счету закончились деньги.",
                "Пополните баланс в личном кабинете сервиса и попробуйте снова.",
                technical=note,
            )
        if code == 404:
            return UserError(
                "Такой модели у сервиса нет.",
                "Откройте «Настройки» и выберите модель из списка.",
                technical=note,
            )
        if code == 429:
            return UserError(
                "Слишком много запросов подряд.",
                "Подождите минуту и попробуйте ещё раз.",
                technical=note,
            )
        if code >= 500:
            return UserError(
                "Сервис сейчас недоступен.",
                "Это на их стороне. Попробуйте через несколько минут.",
                technical=note,
            )
        return UserError(
            "Сервис не смог выполнить запрос.",
            "Попробуйте ещё раз или выберите другую модель в «Настройках».",
            technical=note,
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

    # ── Вопрос текстовой модели ──────────────────────────────────────

    def _build_body(
        self, model: str, prompt: str,
        images: list[bytes] | None, pdf: bytes | None,
        max_tokens: int, stream: bool, with_usage: bool = True,
    ) -> dict[str, Any]:
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

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if stream:
            body["stream"] = True
            if with_usage:
                # Просим прислать счётчик расхода в конце потока —
                # иначе стоимость запроса посчитать не из чего.
                body["stream_options"] = {"include_usage": True}
        return body

    def ask(
        self,
        model: str,
        prompt: str,
        images: list[bytes] | None = None,
        pdf: bytes | None = None,
        max_tokens: int = 8000,
        stream: bool = True,
    ) -> TextAnswer:
        """Задаёт вопрос модели.

        По умолчанию ответ забирается потоком. Это не про скорость:
        разбор чертежа занимает минуты, а защита сервисов рвёт
        соединение, по которому долго ничего не идёт. При потоке данные
        идут постоянно, и обрыва не происходит.

        Не каждый сервис умеет поток и не каждый понимает просьбу
        прислать счётчик расхода. Поэтому способы перебираются от
        лучшего к самому простому, пока какой-нибудь не сработает.
        """
        ways: list[tuple[bool, bool]] = (
            [(True, True), (True, False), (False, False)] if stream else [(False, False)]
        )

        for number, (as_stream, with_usage) in enumerate(ways):
            body = self._build_body(
                model, prompt, images, pdf, max_tokens, as_stream, with_usage
            )
            try:
                if as_stream:
                    return self._ask_streaming(model, body)
                return self._ask_at_once(model, body)
            except StreamNotSupported as refusal:
                if number == len(ways) - 1:
                    raise UserError(
                        "Сервис не смог выполнить запрос.",
                        "Попробуйте ещё раз или выберите другую модель "
                        "в «Настройках».",
                        technical=str(refusal)[:200],
                    ) from refusal
                log.info("Пробую другой способ запроса: %s", refusal)

        raise AssertionError("недостижимо")  # pragma: no cover

    def _ask_at_once(self, model: str, body: dict[str, Any]) -> TextAnswer:
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
        return self._read_payload(payload, model)

    def _read_payload(self, payload: dict[str, Any], model: str) -> TextAnswer:
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

    # Коды, которыми сервис отвечает на непонятную ему просьбу.
    # Это не поломка, а повод попробовать запрос попроще.
    RETRY_CODES = (400, 422, 501)

    def _ask_streaming(self, model: str, body: dict[str, Any]) -> TextAnswer:
        """Забирает ответ по частям, как их присылает сервис."""
        pieces: list[str] = []
        finish = ""
        usage: dict[str, Any] = {}
        served_model = model

        try:
            with self._client() as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        if response.status_code in self.RETRY_CODES:
                            raise StreamNotSupported(
                                f"ответ {response.status_code} на запрос потоком"
                            )
                        raise self._explain(response)

                    for line in response.iter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            parsed = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        served_model = str(parsed.get("model") or served_model)
                        if parsed.get("usage"):
                            usage = parsed["usage"]
                        for choice in parsed.get("choices") or []:
                            if not isinstance(choice, dict):
                                continue
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if isinstance(piece, str):
                                pieces.append(piece)
                            elif isinstance(piece, list):
                                pieces.extend(
                                    part.get("text", "") for part in piece
                                    if isinstance(part, dict)
                                )
                            if choice.get("finish_reason"):
                                finish = str(choice["finish_reason"])
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc

        text = "".join(pieces)
        if not text:
            # Пустой поток — тоже повод попробовать обычным запросом.
            raise StreamNotSupported("поток закончился, а ответа в нём не было")

        return TextAnswer(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=served_model,
            finish_reason=finish,
        )
