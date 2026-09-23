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
import os
import socket
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

# Дробить пачку статистики мельче этого невыгодно: каждый запрос — минута,
# а выигрыш от всё более мелких пачек быстро сходит на нет.
MIN_IDS_PER_STATS_CALL = 12
# Потолок запросов статистики за один сбор, он же потолок минут ожидания.
MAX_STATS_REQUESTS = 40

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


def inspect_certificate(host: str, port: int = 443,
                        timeout: int = 15) -> dict[str, Any]:
    """Смотрит, каким сертификатом отвечает узел, и кто его выдал.

    Нужна, чтобы не гадать при отказе проверки: подменяет ли соединение
    антивирус, корпоративный прокси, или дело в самом сертификате сайта.

    Подлинность здесь намеренно не проверяется — иначе узнать причину
    отказа было бы нельзя. Поэтому по такому соединению НИЧЕГО не
    передаётся: ни токена, ни запросов. Только рукопожатие и чтение
    сертификата, который узел показал сам.
    """
    result: dict[str, Any] = {
        "host": host, "issuer": None, "subject": None,
        "not_after": None, "expired": None, "error": None,
    }

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except Exception as exc:
        result["error"] = f"не удалось соединиться: {exc}"
        return result

    if not der:
        result["error"] = "узел не показал сертификат"
        return result

    # Разобрать DER средствами стандартной библиотеки можно только через
    # временный файл; если не выйдет — молча обходимся без подробностей.
    try:
        import tempfile

        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False,
                                         encoding="ascii") as handle:
            handle.write(pem)
            path = handle.name
        try:
            parsed = ssl._ssl._test_decode_cert(path)  # noqa: SLF001
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception as exc:
        result["error"] = f"сертификат получен, но разобрать не вышло: {exc}"
        return result

    def flatten(field: object) -> str:
        parts = []
        for item in field or ():
            for key, value in item:
                if key in ("organizationName", "commonName"):
                    parts.append(str(value))
        return " · ".join(dict.fromkeys(parts))

    result["issuer"] = flatten(parsed.get("issuer"))
    result["subject"] = flatten(parsed.get("subject"))
    not_after = parsed.get("notAfter")
    if not_after:
        result["not_after"] = not_after
        try:
            moment = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
            result["expired"] = moment < datetime.now()
        except ValueError:
            pass
    return result


# Кого мы узнаём по имени выдавшего сертификат — чтобы сразу назвать виновника
KNOWN_INTERCEPTORS = {
    "dr.web": "антивирус Dr.Web",
    "drweb": "антивирус Dr.Web",
    "kaspersky": "антивирус Kaspersky",
    "avast": "антивирус Avast",
    "eset": "антивирус ESET",
    "avg": "антивирус AVG",
    "bitdefender": "антивирус Bitdefender",
    "nod32": "антивирус ESET NOD32",
    "fortinet": "корпоративный шлюз Fortinet",
    "zscaler": "корпоративный шлюз Zscaler",
    "sophos": "антивирус Sophos",
    "mcafee": "антивирус McAfee",
}


def name_interceptor(issuer: str | None) -> str | None:
    """Узнаёт по выдавшему сертификат, кто вклинился в соединение."""
    if not issuer:
        return None
    low = issuer.lower()
    for mark, name in KNOWN_INTERCEPTORS.items():
        if mark in low:
            return name
    return None


def clock_looks_plausible(token: str) -> bool | None:
    """Похожи ли дата и время компьютера на правильные — по самому токену.

    Токены WB выпускаются на срок порядка полугода. Если до конца срока
    осталось разумное время, значит часы примерно совпадают с моментом
    выпуска токена, и сбитая дата не при чём. None — если судить не по чему.
    """
    info = describe_token(token)
    expires = info.get("expires_at")
    if not expires:
        return None
    left = (expires - datetime.now()).days
    # Токен не мог быть выпущен на срок больше года; отрицательное
    # означает, что срок уже вышел — часы либо сбиты, либо токен старый.
    return 0 < left <= 400


def explain_tls_error(reason: object, token: str = "") -> str:
    """Объясняет отказ проверки сертификата и упорядочивает причины.

    Порядок советов зависит от того, похожи ли часы компьютера на верные:
    при сбитой дате любой сертификат выглядит просроченным, и это самая
    частая причина — но если дату удалось признать правдоподобной,
    гонять человека в настройки времени незачем.
    """
    now = datetime.now()
    text = str(reason)
    plausible = clock_looks_plausible(token)

    lines = [
        "Не удалось проверить защищённое соединение с Wildberries.",
        "Это НЕ про токен — до проверки доступа дело даже не дошло.",
        "",
        f"Дата и время на этом компьютере: {now:%d.%m.%Y %H:%M}.",
    ]
    if plausible:
        lines += [
            "Судя по сроку действия вашего токена, дата похожа на правильную —",
            "значит, причина, скорее всего, не в часах. Смотрите пункт 1.",
        ]
    elif plausible is False:
        lines += [
            "Эта дата НЕ сходится со сроком действия вашего токена —",
            "очень похоже, что часы на компьютере сбиты. Начните с них:",
            "правый клик по часам в углу экрана → «Настройка даты и времени»",
            "→ включить «Установить время автоматически». Потом перепроверьте.",
        ]
    else:
        lines += [
            "Если они неверные — исправьте и перепроверьте.",
            "При сбитой дате любой сертификат выглядит просроченным.",
        ]

    lines += [
        "",
        "Причины по порядку:",
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
    lines.append("Поменяли что-то — перепроверьте, не запуская всё заново:")
    lines.append("  дважды щёлкните CHECK-Windows.bat (на Mac CHECK-Mac.command)")
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


def certifi_bundle() -> str | None:
    """Путь к набору корневых сертификатов certifi, если он установлен.

    Хранилище Windows обновляется вместе с системой, и на отставшей машине
    в нём попадается просроченный корень: проверка цепочки срывается на нём,
    хотя сертификат сайта в полном порядке. certifi — тот же набор корней,
    что используют браузеры, и он поддерживается отдельно от системы.
    """
    try:
        import certifi
    except ImportError:
        return None
    path = certifi.where()
    return path if os.path.exists(path) else None


def build_ssl_context(ca_bundle: str | None = None) -> ssl.SSLContext:
    """Готовит проверку соединения. Проверка всегда полная.

    ca_bundle подменяет только набор доверенных корней — сама проверка
    подлинности и имени узла остаётся включённой.
    """
    if ca_bundle and os.path.exists(ca_bundle):
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


class _BaseClient:
    """Общий транспорт: заголовки, повторы, разбор ответа.

    Повторяем только то, что имеет смысл повторять: 429 и 5xx — временные,
    401 и 403 — про права, их повтор ничего не изменит.
    """

    base_url = ""
    scope = "Продвижение"

    def __init__(self, token: str, base_url: str | None = None,
                 timeout: int = 90, max_retries: int = 4,
                 ca_bundle: str = "") -> None:
        if not token or not token.strip():
            raise WBError(
                "Не задан токен WB. Скопируйте .env.example в .env и впишите WB_API_TOKEN."
            )
        self.token = token.strip()
        self.ca_bundle = ca_bundle
        self.used_fallback_bundle = False
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
        context = build_ssl_context(self.ca_bundle)
        tried_fallback = bool(self.ca_bundle)

        for _ in range(self.max_retries):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=context) as resp:
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
                is_cert = (isinstance(reason, ssl.SSLCertVerificationError)
                           or "CERTIFICATE_VERIFY" in str(reason))

                # Проверка сорвалась на корне из хранилища системы — пробуем
                # тот же запрос с набором корней certifi. Проверка при этом
                # остаётся полной, меняется только список доверенных корней.
                if is_cert and not tried_fallback:
                    bundle = certifi_bundle()
                    if bundle:
                        tried_fallback = True
                        context = build_ssl_context(bundle)
                        self.used_fallback_bundle = True
                        continue

                # Повторять отказ по сертификату бессмысленно: он не временный.
                if is_cert:
                    raise WBError(explain_tls_error(reason, self.token), kind="tls") from exc
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

    def campaign_details(self, advert_ids: Sequence[int],
                         on_progress: Progress | None = None) -> list[dict[str, Any]]:
        """Карточки кампаний: название, тип, статус, дневной бюджет.

        WB отвечает 404 на всю пачку, если отдавать нечего хотя бы по части
        кампаний в ней. Молча пропускать пачку нельзя — так теряются все
        нормальные кампании внутри неё. Поэтому пачка делится пополам,
        пока не выделятся те, по которым данные есть. Лимит у метода
        щедрый (300 запросов в минуту), так что дробить можно до одной.
        """
        result: list[dict[str, Any]] = []
        skipped = 0
        queue: list[list[int]] = list(_chunks(list(advert_ids), MAX_IDS_PER_DETAIL_CALL))

        while queue:
            chunk = queue.pop(0)
            try:
                data = self._request("POST", "/adv/v1/promotion/adverts",
                                     payload=list(chunk))
            except WBError as exc:
                if exc.status != 404:
                    raise
                if len(chunk) > 1:
                    middle = len(chunk) // 2
                    queue.insert(0, chunk[middle:])
                    queue.insert(0, chunk[:middle])
                else:
                    skipped += 1
                continue
            if isinstance(data, list):
                result.extend(data)

        if skipped and on_progress:
            on_progress(f"Карточек не нашлось у {skipped} кампаний — вероятно, удалены.")
        return result

    def fullstats(self, advert_ids: Sequence[int], date_from: str, date_to: str,
                  on_progress: Progress | None = None,
                  max_requests: int = MAX_STATS_REQUESTS) -> list[dict[str, Any]]:
        """Статистика по дням.

        WB отвечает 404 на всю пачку, если статистики нет хотя бы по части
        кампаний в ней. Пропускать пачку целиком — значит терять работающие
        кампании, которые в ней были. Поэтому пачка делится пополам.

        Метод отдаётся раз в минуту, и дробить до бесконечности нельзя:
        каждый запрос — это минута ожидания. Поэтому есть и нижняя граница
        размера пачки, и общий потолок числа запросов за сбор. Недобранное
        приедет следующим запуском.
        """
        result: list[dict[str, Any]] = []
        queue: list[list[int]] = list(_chunks(list(advert_ids), MAX_IDS_PER_STATS_CALL))
        done = 0
        spent = 0
        without_stats = 0

        while queue:
            if spent >= max_requests:
                if on_progress:
                    left = sum(len(c) for c in queue)
                    on_progress(f"Достигнут предел запросов за один сбор; "
                                f"{left} кампаний доберём в следующий раз.")
                break

            chunk = queue.pop(0)
            self._wait_turn(on_progress)
            payload = [
                {"id": advert_id, "interval": {"begin": date_from, "end": date_to}}
                for advert_id in chunk
            ]
            try:
                data = self._request("POST", "/adv/v2/fullstats", payload=payload)
            except WBError as exc:
                self._last_call = time.monotonic()
                spent += 1
                if exc.status != 404:
                    raise
                if len(chunk) > MIN_IDS_PER_STATS_CALL:
                    # Половины отправляем в КОНЕЦ очереди, а не в начало:
                    # иначе одна пустая пачка, дробясь вглубь, съедает весь
                    # бюджет запросов, и до остальных дело не доходит.
                    middle = len(chunk) // 2
                    queue.append(chunk[:middle])
                    queue.append(chunk[middle:])
                    if on_progress:
                        on_progress(f"По {len(chunk)} кампаниям разом данных нет — "
                                    f"делю пополам и пробую снова")
                else:
                    without_stats += len(chunk)
                    if on_progress:
                        on_progress(f"У {len(chunk)} кампаний нет открутки за период")
                continue

            self._last_call = time.monotonic()
            spent += 1
            if isinstance(data, list):
                result.extend(data)
                done += len(chunk)
            if on_progress:
                on_progress(f"Статистика собрана по {done} кампаниям, "
                            f"в очереди ещё {len(queue)} пачек")

        if without_stats and on_progress:
            on_progress(f"Без статистики за период: {without_stats} кампаний.")
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
