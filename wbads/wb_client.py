"""Клиент API продвижения Wildberries.

Только стандартная библиотека — ставить ничего не нужно.
Токен берётся из переменной окружения WB_API_TOKEN (см. .env.example)
и никогда не пишется в код и не логируется.

Документация методов: https://dev.wildberries.ru/openapi/promotion
Лимиты, которые здесь учтены:
    /adv/v1/promotion/count    — 300 запросов/мин
    /adv/v1/promotion/adverts  — 300 запросов/мин, до 50 кампаний в теле
    /adv/v2/fullstats          — 1 запрос/мин, до 100 кампаний в теле
    /adv/v1/balance            — 60 запросов/мин
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Sequence

BASE_URL = "https://advert-api.wildberries.ru"

# Пауза между запросами к fullstats: метод разрешает один запрос в минуту.
FULLSTATS_COOLDOWN = 61
MAX_IDS_PER_STATS_CALL = 100
MAX_IDS_PER_DETAIL_CALL = 50


class WBError(RuntimeError):
    """Ошибка обращения к API WB с человеческим объяснением."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def explain_status(status: int) -> str:
    return {
        400: "WB не принял запрос (400). Проверьте период — он не должен быть в будущем.",
        401: "Токен не принят (401). Проверьте WB_API_TOKEN в файле .env: "
             "он должен быть создан в кабинете WB с категорией «Продвижение».",
        403: "Доступ запрещён (403). У токена нет категории «Продвижение» "
             "либо он выпущен для другого кабинета.",
        404: "Метод не найден (404). Возможно, WB изменил адрес API.",
        422: "WB не смог обработать данные запроса (422).",
        429: "Слишком часто (429). Сработал лимит запросов — сервис подождёт и повторит.",
    }.get(status, f"WB вернул ошибку {status}.")


class WBAdvertClient:
    def __init__(self, token: str, base_url: str = BASE_URL,
                 timeout: int = 60, max_retries: int = 4) -> None:
        if not token or not token.strip():
            raise WBError(
                "Не задан токен WB. Скопируйте .env.example в .env и впишите WB_API_TOKEN."
            )
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_stats_call = 0.0

    # ── транспорт ─────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, payload: Any | None = None,
                 params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{query}"

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": self.token,
            "Accept": "application/json",
            "User-Agent": "wbads-analytics/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        delay = 2.0
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8").strip()
                    if not raw:
                        return None
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                # 429 и 5xx — временные, их имеет смысл повторить
                if exc.code == 429 or exc.code >= 500:
                    last_error = WBError(explain_status(exc.code), exc.code)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise WBError(explain_status(exc.code), exc.code) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(delay)
                delay *= 2
        raise WBError(f"Не удалось получить данные от WB: {last_error}")

    # ── методы API ────────────────────────────────────────────────────────

    def balance(self) -> dict[str, float]:
        """Баланс рекламного кабинета: счёт, бонусы, баланс."""
        data = self._request("GET", "/adv/v1/balance") or {}
        return {
            "balance": float(data.get("balance") or 0),
            "bonus": float(data.get("bonus") or 0),
            "net": float(data.get("net") or 0),
        }

    def campaign_ids(self) -> list[int]:
        """Все id кампаний кабинета (метод отдаёт их сгруппированными по типам)."""
        data = self._request("GET", "/adv/v1/promotion/count") or {}
        ids: list[int] = []
        for group in data.get("adverts") or []:
            for item in group.get("advert_list") or []:
                advert_id = item.get("advertId")
                if advert_id:
                    ids.append(int(advert_id))
        return sorted(set(ids))

    def campaign_details(self, advert_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Карточки кампаний: название, тип, статус, дневной бюджет."""
        result: list[dict[str, Any]] = []
        for chunk in _chunks(list(advert_ids), MAX_IDS_PER_DETAIL_CALL):
            data = self._request("POST", "/adv/v1/promotion/adverts", payload=list(chunk))
            if isinstance(data, list):
                result.extend(data)
        return result

    def fullstats(self, advert_ids: Sequence[int], date_from: str,
                  date_to: str, on_progress=None) -> list[dict[str, Any]]:
        """Статистика по дням. Метод медленный: один запрос в минуту.

        Поэтому кампании идут пачками до 100 штук, а между пачками —
        обязательная пауза, иначе WB ответит 429.
        """
        result: list[dict[str, Any]] = []
        chunks = list(_chunks(list(advert_ids), MAX_IDS_PER_STATS_CALL))
        for index, chunk in enumerate(chunks):
            self._respect_stats_cooldown(first=index == 0, on_progress=on_progress)
            payload = [
                {"id": advert_id, "interval": {"begin": date_from, "end": date_to}}
                for advert_id in chunk
            ]
            data = self._request("POST", "/adv/v2/fullstats", payload=payload)
            self._last_stats_call = time.monotonic()
            if isinstance(data, list):
                result.extend(data)
            if on_progress:
                on_progress(f"Статистика: пачка {index + 1} из {len(chunks)}")
        return result

    def _respect_stats_cooldown(self, first: bool, on_progress=None) -> None:
        if first and self._last_stats_call == 0.0:
            return
        waited = time.monotonic() - self._last_stats_call
        remaining = FULLSTATS_COOLDOWN - waited
        if remaining > 0:
            if on_progress:
                on_progress(
                    f"Ждём {int(remaining)} с — WB разрешает статистику раз в минуту"
                )
            time.sleep(remaining)


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
