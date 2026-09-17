"""Клиенты API Wildberries: продвижение и статистика.

Только стандартная библиотека — ставить ничего не нужно.
Токен берётся из переменной окружения WB_API_TOKEN (см. .env.example)
и никогда не пишется в код и не логируется.

Документация: https://dev.wildberries.ru/openapi

Категории токена и что они открывают:
    «Продвижение» → advert-api.wildberries.ru  — кампании, статистика рекламы, баланс
    «Статистика»  → statistics-api.wildberries.ru — заказы (нужны для общего ДРР)

Лимиты, которые здесь учтены:
    /adv/v1/promotion/count       — 300 запросов/мин
    /adv/v1/promotion/adverts     — 300 запросов/мин, до 50 кампаний в теле
    /adv/v2/fullstats             — 1 запрос/мин, до 100 кампаний в теле
    /adv/v1/balance               — 60 запросов/мин
    /api/v1/supplier/orders       — 1 запрос/мин
"""

from __future__ import annotations

import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

ADVERT_URL = "https://advert-api.wildberries.ru"
STATISTICS_URL = "https://statistics-api.wildberries.ru"

# Пауза между запросами к методам, которые WB отдаёт раз в минуту.
MINUTE_COOLDOWN = 61
MAX_IDS_PER_STATS_CALL = 100
MAX_IDS_PER_DETAIL_CALL = 50

# Сколько страниц заказов готовы забрать за один сбор. Каждая — минута ожидания,
# поэтому ограничиваем: остальное доберётся следующим запуском.
MAX_ORDER_PAGES = 12

Progress = Callable[[str], None]


class WBError(RuntimeError):
    """Ошибка обращения к API WB с человеческим объяснением.

    kind различает причины, потому что лечатся они по-разному:
        "http"    — WB ответил и отказал: дело в токене или правах
        "tls"     — не удалось проверить защищённое соединение
        "network" — до WB вообще не дошли: сеть, прокси, брандмауэр
    Без этого различия сетевой сбой выглядел как проблема с токеном,
    и человек шёл перевыпускать исправный токен.
    """

    def __init__(self, message: str, status: int | None = None,
                 kind: str = "http") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


def explain_tls_error(reason: object) -> str:
    """Объясняет отказ проверки сертификата — и сразу показывает, куда смотреть.

    Самая частая причина «сертификат просрочен» на стороне клиента —
    неверные дата и время на компьютере: при сбитой дате любой сертификат
    выглядит недействительным. Поэтому печатаем текущую дату машины:
    ошибку видно глазами.
    """
    now = datetime.now()
    text = str(reason)
    lines = [
        "Не удалось проверить защищённое соединение с Wildberries.",
        "Это НЕ про токен — до проверки доступа дело даже не дошло.",
        "",
        f"Дата и время на этом компьютере: {now:%d.%m.%Y %H:%M}.",
        "Если они неверные — исправьте и запустите программу снова.",
        "При сбитой дате любой сертификат выглядит просроченным,",
        "и это самая частая причина такой ошибки.",
        "",
        "Если дата верная, то по порядку:",
        "  1. Антивирус или корпоративный прокси, проверяющий защищённые",
        "     соединения своим сертификатом. Отключите такую проверку",
        "     для python.exe или попробуйте другую сеть — например,",
        "     телефон как точку доступа.",
    ]
    if sys.platform == "darwin":
        lines.append("  2. На Mac: откройте папку установленного Python "
                     "и запустите «Install Certificates.command».")
    elif sys.platform == "win32":
        lines.append("  2. Установите обновления Windows: с ними приезжают "
                     "свежие корневые сертификаты.")
    else:
        lines.append("  2. Обновите системные корневые сертификаты "
                     "(пакет ca-certificates).")
    lines.append("  3. Обновите Python до последней версии с python.org.")
    lines.append("")
    lines.append(f"Техническая причина: {text}")
    return "\n".join(lines)


def explain_status(status: int, scope: str = "Продвижение") -> str:
    return {
        400: "WB не принял запрос (400). Проверьте период — он не должен быть в будущем.",
        401: "Токен не принят (401). Проверьте WB_API_TOKEN в файле .env: "
             "возможно, он скопирован не полностью или у него истёк срок действия.",
        403: f"Доступ запрещён (403). Возможные причины: у токена нет категории «{scope}»; "
             "он выпущен для другого кабинета; либо он создан в режиме «Только на чтение», "
             "а WB счёл запрос изменяющим — тогда выпустите токен без этой галочки.",
        404: "Метод не найден (404). Возможно, WB изменил адрес API.",
        422: "WB не смог обработать данные запроса (422).",
        429: "Слишком часто (429). Сработал лимит запросов — сервис подождёт и повторит.",
    }.get(status, f"WB вернул ошибку {status}.")


def describe_token(token: str) -> dict[str, Any]:
    """Разбирает токен WB, не обращаясь в интернет.

    Токен WB — это JWT: три части через точку, средняя содержит открытые
    сведения о самом токене. Это не расшифровка секрета — просто чтение
    того, что в него записано при выпуске. Помогает понять причину отказа,
    не гадая: обрезан ли токен при вставке, не истёк ли, не выпущен ли
    он для тестового контура.
    """
    token = (token or "").strip()
    info: dict[str, Any] = {
        "length": len(token),
        "preview": (token[:8] + "…") if len(token) > 8 else token,
        "looks_like_jwt": False,
        "expires_at": None,
        "expired": None,
        "sandbox": None,
        "seller_id": None,
        "scopes_raw": None,
        "error": None,
    }
    parts = token.split(".")
    if len(parts) != 3:
        info["error"] = "не похоже на токен WB: у него должно быть три части через точку"
        return info
    info["looks_like_jwt"] = True

    try:
        raw = parts[1]
        raw += "=" * (-len(raw) % 4)          # base64 требует длину, кратную четырём
        payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except Exception as exc:
        info["error"] = f"не удалось прочитать содержимое токена: {exc}"
        return info

    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        moment = datetime.fromtimestamp(exp)
        info["expires_at"] = moment
        info["expired"] = moment < datetime.now()

    # t = true означает токен тестового контура: к боевому API он не подойдёт
    if "t" in payload:
        info["sandbox"] = bool(payload["t"])
    info["seller_id"] = payload.get("sid")
    if isinstance(payload.get("s"), int):
        info["scopes_raw"] = payload["s"]
    return info


class _BaseClient:
    """Общий транспорт: заголовки, повторы, разбор ответа.

    Повторяем только то, что имеет смысл повторять: 429 и 5xx — временные,
    401 и 403 — про права, их повтор ничего не изменит.
    """

    base_url = ""
    scope = "Продвижение"

    def __init__(self, token: str, base_url: str | None = None,
                 timeout: int = 90, max_retries: int = 4) -> None:
        if not token or not token.strip():
            raise WBError(
                "Не задан токен WB. Скопируйте .env.example в .env и впишите WB_API_TOKEN."
            )
        self.token = token.strip()
        if base_url:
            self.base_url = base_url
        self.base_url = self.base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def _request(self, method: str, path: str, payload: Any | None = None,
                 params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": self.token,
            "Accept": "application/json",
            "User-Agent": "wbads-analytics/1.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        delay = 2.0
        last_error: Exception | None = None
        for _ in range(self.max_retries):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8").strip()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    last_error = WBError(explain_status(exc.code, self.scope), exc.code)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise WBError(explain_status(exc.code, self.scope), exc.code) from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                # Отказ проверки сертификата повторять бессмысленно:
                # он не временный и от повтора не исправится.
                if isinstance(reason, ssl.SSLCertVerificationError) or \
                        "CERTIFICATE_VERIFY" in str(reason):
                    raise WBError(explain_tls_error(reason), kind="tls") from exc
                last_error = exc
                time.sleep(delay)
                delay *= 2
            except (TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(delay)
                delay *= 2
        raise WBError(
            "Не удалось связаться с Wildberries.\n"
            "Проверьте подключение к интернету и попробуйте снова.\n"
            f"Техническая причина: {last_error}",
            kind="network",
        )


class _MinuteLimited(_BaseClient):
    """Клиент для методов, которые WB отдаёт не чаще раза в минуту."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_call = 0.0

    def _wait_turn(self, on_progress: Progress | None = None) -> None:
        if self._last_call == 0.0:
            return
        remaining = MINUTE_COOLDOWN - (time.monotonic() - self._last_call)
        if remaining > 0:
            if on_progress:
                on_progress(f"Ждём {int(remaining)} с — WB отдаёт эти данные раз в минуту")
            time.sleep(remaining)


class WBAdvertClient(_MinuteLimited):
    """Кампании, их статистика и баланс кабинета. Категория «Продвижение»."""

    base_url = ADVERT_URL
    scope = "Продвижение"

    def balance(self) -> dict[str, float]:
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

    def fullstats(self, advert_ids: Sequence[int], date_from: str, date_to: str,
                  on_progress: Progress | None = None) -> list[dict[str, Any]]:
        """Статистика по дням. Метод медленный: один запрос в минуту."""
        result: list[dict[str, Any]] = []
        chunks = list(_chunks(list(advert_ids), MAX_IDS_PER_STATS_CALL))
        for index, chunk in enumerate(chunks):
            self._wait_turn(on_progress)
            payload = [
                {"id": advert_id, "interval": {"begin": date_from, "end": date_to}}
                for advert_id in chunk
            ]
            data = self._request("POST", "/adv/v2/fullstats", payload=payload)
            self._last_call = time.monotonic()
            if isinstance(data, list):
                result.extend(data)
            if on_progress and len(chunks) > 1:
                on_progress(f"Статистика рекламы: пачка {index + 1} из {len(chunks)}")
        return result


class WBStatisticsClient(_MinuteLimited):
    """Заказы кабинета. Категория «Статистика».

    Нужны, чтобы считать общий ДРР — от всех заказов артикула, а не только
    от тех, что WB атрибутировал рекламе.
    """

    base_url = STATISTICS_URL
    scope = "Статистика"

    def orders(self, date_from: str, on_progress: Progress | None = None,
               max_pages: int = MAX_ORDER_PAGES) -> list[dict[str, Any]]:
        """Заказы, изменённые начиная с date_from.

        WB отдаёт до ~80 тысяч строк за раз. Продолжение запрашивается
        со сдвигом по lastChangeDate последней строки — так добираются
        остальные страницы. Каждая страница стоит минуты ожидания,
        поэтому их число ограничено: недобранное приедет следующим сбором.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor = _to_rfc3339(date_from)

        for page in range(max_pages):
            self._wait_turn(on_progress)
            data = self._request("GET", "/api/v1/supplier/orders",
                                 params={"dateFrom": cursor, "flag": "0"})
            self._last_call = time.monotonic()

            rows = data if isinstance(data, list) else []
            fresh = [r for r in rows if _order_key(r) not in seen]
            for row in fresh:
                seen.add(_order_key(row))
            collected.extend(fresh)

            if on_progress:
                on_progress(f"Заказы: страница {page + 1}, всего строк {len(collected)}")

            # Страница не полная или ничего нового — данные закончились
            if len(rows) < 1000 or not fresh:
                break

            next_cursor = max((str(r.get("lastChangeDate") or "") for r in rows),
                              default="")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            if on_progress:
                on_progress("Достигнут предел страниц за один сбор — "
                            "остальное доберётся при следующем запуске.")

        return collected


def _to_rfc3339(value: str) -> str:
    """«2026-08-01» → «2026-08-01T00:00:00», как ждёт метод заказов."""
    value = str(value).strip()
    return value if "T" in value else f"{value}T00:00:00"


def _order_key(row: dict[str, Any]) -> str:
    """Устойчивый ключ заказа: srid, а если его нет — номер заказа с артикулом."""
    srid = str(row.get("srid") or "").strip()
    if srid:
        return srid
    return f"{row.get('gNumber')}:{row.get('nmId')}:{row.get('barcode')}"


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
