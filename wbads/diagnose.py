"""Полная диагностика связи с WB одним запуском.

Зачем отдельно от команды `check`. Проверка отвечает на вопрос «можно ли
начинать сбор» и останавливается, как только ответ ясен. Диагностика
отвечает на другой вопрос — «что именно WB отдаёт и в каком виде» — и не
останавливается никогда: каждая проба выполняется независимо, отказ одной
не отменяет остальные. В конце пишется файл, который целиком отправляют
тому, кто будет разбираться.

Так один запуск заменяет цепочку «запустил → упало → отправил → исправили →
скачал заново». Без этого каждая ошибка вскрывается по одной, а платит за
это временем человек, у которого единственный доступ к живому кабинету.

Главное, что собирается по каждому методу: код ответа, текст отказа
словами WB, сколько записей пришло и КАКИЕ В НИХ ПОЛЯ. Последнее важнее
всего: когда WB меняет метод, ломается обычно не доступ, а форма ответа —
и по списку полей это видно сразу, а по нулям в отчёте не видно никак.
"""

from __future__ import annotations

import json
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .rules import plural
from .wb_client import (
    ADVERT_URL,
    MAX_STATS_DAYS,
    STATISTICS_URL,
    build_ssl_context,
    certifi_bundle,
    clock_looks_plausible,
    describe_token,
    inspect_certificate,
    name_interceptor,
)

Progress = Callable[[str], None]

# Все методы, на которых держится сервис. Проверяются все, независимо друг
# от друга: раньше отказ первого прятал состояние остальных.
PROBES = [
    ("Баланс кабинета", ADVERT_URL, "GET", "/adv/v1/balance", None, False),
    ("Список кампаний", ADVERT_URL, "GET", "/adv/v1/promotion/count", None, True),
    ("Карточки кампаний", ADVERT_URL, "GET", "/api/advert/v2/adverts", "ids", True),
    ("Статистика по дням", ADVERT_URL, "GET", "/adv/v3/fullstats", "stats", True),
    ("Заказы кабинета", STATISTICS_URL, "GET", "/api/v1/supplier/orders", "orders", True),
]

# Адреса, которые WB отключил. Проверяем их намеренно: если старый молчит,
# а новый отвечает — переход завершён, и это видно, а не предполагается.
RETIRED = [
    ("Карточки кампаний (старый адрес)", ADVERT_URL, "POST",
     "/adv/v1/promotion/adverts"),
    ("Статистика (старый адрес)", ADVERT_URL, "POST", "/adv/v2/fullstats"),
]


@dataclass
class Finding:
    """Одна проба: что спрашивали и что получили."""

    label: str
    method: str
    url: str
    status: int | None = None
    seconds: float = 0.0
    error: str = ""
    wb_said: str = ""
    shape: str = ""
    records: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    level: str = "ok"          # ok | warn | fail
    note: str = ""
    # Разобранный ответ нужен самой диагностике: из списка кампаний
    # берутся номера для следующих проб. В отчёт он не печатается.
    parsed: Any = None

    @property
    def mark(self) -> str:
        return {"ok": "✓", "warn": "⚠", "fail": "✗"}[self.level]


def describe_shape(value: Any, depth: int = 0) -> str:
    """Словами описывает форму ответа: тип, поля, вложенность.

    Это ядро диагностики. Ответ «200, но ноль записей» не говорит ничего:
    данных может не быть, а может быть другая форма ответа, которую
    программа не читает. Список полей отвечает на это сразу.
    """
    pad = "  " * depth
    if value is None:
        return "пустой ответ (null или тело без содержимого)"
    if isinstance(value, list):
        if not value:
            return "пустой список"
        inner = describe_shape(value[0], depth + 1)
        word = plural(len(value), "записи", "записей", "записей")
        return f"список из {len(value)} {word}; первая — {inner}"
    if isinstance(value, dict):
        keys = list(value.keys())
        head = f"объект с полями: {', '.join(str(k) for k in keys[:12])}"
        if len(keys) > 12:
            head += f" и ещё {len(keys) - 12}"
        if depth >= 2:
            return head
        # Раскрываем вложенные списки: именно там лежат дни и артикулы.
        nested = []
        for key in keys:
            if isinstance(value[key], list) and value[key]:
                nested.append(f"{pad}  {key} → {describe_shape(value[key], depth + 1)}")
        if nested:
            return head + "\n" + "\n".join(nested)
        return head
    if isinstance(value, str):
        short = value if len(value) <= 40 else value[:40] + "…"
        return f"строка «{short}»"
    return f"{type(value).__name__} = {value}"


def count_records(value: Any) -> int | None:
    """Сколько содержательных записей в ответе."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("adverts", "orders", "data", "cards"):
            if isinstance(value.get(key), list):
                return len(value[key])
        return 1
    return None


def raw_request(url: str, token: str, method: str = "GET",
                ca_bundle: str = "", timeout: int = 60) -> Finding:
    """Один запрос без повторов — так видно настоящий ответ WB.

    Повторы здесь только мешали бы: диагностике нужен первый ответ, с его
    кодом и заголовками, а не итог четырёх попыток.
    """
    finding = Finding(label="", method=method, url=url)
    headers = {
        "Authorization": token,
        "Accept": "application/json",
        "User-Agent": "wbads-analytics/1.1",
    }
    context = build_ssl_context(ca_bundle)
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context))
        with opener.open(req, timeout=timeout) as resp:
            finding.status = resp.status
            finding.headers = _rate_headers(resp.headers)
            raw = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        finding.status = exc.code
        finding.headers = _rate_headers(getattr(exc, "headers", None))
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        finding.wb_said = _first_line(body)
        finding.seconds = time.monotonic() - started
        return finding
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        finding.error = str(reason)
        finding.seconds = time.monotonic() - started
        return finding
    except (TimeoutError, socket.timeout):
        finding.error = "ответа не было дольше минуты"
        finding.seconds = time.monotonic() - started
        return finding
    except Exception as exc:                                  # noqa: BLE001
        # Диагностика не имеет права падать: она нужна именно тогда,
        # когда что-то сломано непредвиденным образом.
        finding.error = f"{type(exc).__name__}: {exc}"
        finding.seconds = time.monotonic() - started
        return finding

    finding.seconds = time.monotonic() - started
    if not raw:
        finding.shape = "пустой ответ (тело без содержимого)"
        finding.records = 0
        return finding
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        finding.shape = f"не JSON: {_first_line(raw)}"
        return finding
    finding.parsed = parsed
    finding.shape = describe_shape(parsed)
    finding.records = count_records(parsed)
    return finding


def _rate_headers(headers: Any) -> dict[str, str]:
    """Оставляем только заголовки про лимиты — по ним видно, что WB считает."""
    if not headers:
        return {}
    keep = {}
    for name, value in headers.items():
        low = name.lower()
        if "ratelimit" in low or low == "retry-after":
            keep[name] = str(value).strip()
    return keep


def _first_line(text: str, limit: int = 300) -> str:
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text.replace("\n", " ")[:limit]
    if isinstance(data, dict):
        for key in ("error", "errorText", "detail", "message", "title",
                    "description"):
            if data.get(key):
                return str(data[key])[:limit]
    return text.replace("\n", " ")[:limit]


def _grade(finding: Finding, expect_data: bool) -> None:
    """Расставляет ✓ / ⚠ / ✗ по одним правилам для всех проб."""
    if finding.error:
        finding.level = "fail"
        return
    if finding.status in (401, 403):
        finding.level = "fail"
        finding.note = "нет доступа: дело в токене или его категориях"
        return
    if finding.status == 404:
        finding.level = "fail"
        finding.note = ("метод не найден — WB перенёс адрес; "
                        "сверьте список методов в начале wbads/wb_client.py")
        return
    if finding.status == 429:
        finding.level = "warn"
        finding.note = "лимит частоты запросов — это не отказ, надо подождать"
        return
    if finding.status and finding.status >= 400:
        finding.level = "fail"
        return
    if expect_data and not finding.records:
        finding.level = "warn"
        finding.note = ("метод ответил, но записей не отдал — "
                        "либо данных за период нет, либо форма ответа другая")
        return
    finding.level = "ok"


def run_diagnostics(token: str, ca_bundle: str = "",
                    on_progress: Progress | None = None) -> dict[str, Any]:
    """Прогоняет все пробы и возвращает собранную картину.

    Ни одна проба не прерывает остальные — в этом весь смысл. Даже если
    токен просрочен, сертификаты сломаны, а половина методов отвечает
    отказом, отчёт будет полным.
    """
    def say(text: str) -> None:
        if on_progress:
            on_progress(text)

    report: dict[str, Any] = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "environment": _environment(),
        "token": _token_section(token),
        "tls": _tls_section(ca_bundle),
        "findings": [],
        "retired": [],
    }

    # Список кампаний нужен, чтобы спрашивать карточки и статистику у
    # настоящих активных кампаний, а не у выдуманных номеров.
    live_ids: list[int] = []

    for label, base, method, path, kind, expect_data in PROBES:
        say(f"Проверяю: {label}…")
        url = base + path + _query_for(kind, live_ids)
        finding = raw_request(url, token, method, ca_bundle)
        finding.label = label
        _grade(finding, expect_data)
        report["findings"].append(finding)

        # Номера для следующих проб берём из ответа списка кампаний:
        # спрашивать статистику у выдуманных ID бессмысленно.
        if path == "/adv/v1/promotion/count" and finding.parsed:
            live_ids = _live_ids(finding.parsed)
            report["campaigns_total"] = len(live_ids)
            say(f"Кампаний, у которых может быть статистика: {len(live_ids)}")

    say("Проверяю отключённые адреса — чтобы убедиться, что переход сделан…")
    for label, base, method, path in RETIRED:
        finding = raw_request(base + path, token, method, ca_bundle)
        finding.label = label
        # Здесь 404 — правильный ответ: адрес и должен быть мёртв.
        if finding.status == 404:
            finding.level = "ok"
            finding.note = "отключён, как и ожидалось"
        elif finding.status is None:
            # До адреса не дошли вовсе — про переход это ничего не говорит,
            # и пугать этим в отчёте незачем.
            finding.level = "warn"
            finding.note = f"проверить не удалось: {finding.error or 'нет ответа'}"
        else:
            finding.level = "warn"
            finding.note = (f"отвечает кодом {finding.status} — возможно, WB "
                            "вернул метод; стоит перепроверить")
        report["retired"].append(finding)

    report["problems"] = [f for f in report["findings"] if f.level == "fail"]
    report["warnings"] = [f for f in report["findings"] if f.level == "warn"]
    return report


def _query_for(kind: str | None, live_ids: list[int]) -> str:
    """Собирает параметры пробы: настоящие ID и период, который WB примет."""
    if kind is None:
        return ""
    sample = ",".join(str(i) for i in live_ids[:5])
    if kind == "ids":
        return f"?ids={sample}" if sample else ""
    if kind == "stats":
        if not sample:
            return ""
        end = date.today() - timedelta(days=1)
        begin = end - timedelta(days=min(6, MAX_STATS_DAYS - 1))
        return (f"?ids={sample}&beginDate={begin.isoformat()}"
                f"&endDate={end.isoformat()}")
    if kind == "orders":
        begin = (date.today() - timedelta(days=1)).isoformat()
        return f"?dateFrom={begin}&flag=0"
    return ""


def _live_ids(data: Any) -> list[int]:
    """Номера кампаний, у которых статистика правдоподобна.

    Спрашивать статистику у первых по списку — значит часто попадать на
    остановленные год назад и принимать честное «данных нет» за поломку.
    Поэтому берём активные, на паузе и завершённые, свежие — первыми.
    """
    if not isinstance(data, dict):
        return []
    rows: list[tuple[str, int]] = []
    for group in (data.get("adverts") or []):
        if int(group.get("status") or 0) not in (9, 11, 7):
            continue
        for item in (group.get("advert_list") or []):
            if item.get("advertId"):
                rows.append((str(item.get("changeTime") or ""),
                             int(item["advertId"])))
    rows.sort(reverse=True)
    return [advert_id for _, advert_id in rows]


def _environment() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "system": f"{platform.system()} {platform.release()}",
        "now": datetime.now().isoformat(timespec="seconds"),
        "openssl": ssl.OPENSSL_VERSION,
        "certifi": certifi_bundle() or "не установлен",
    }


def _token_section(token: str) -> dict[str, Any]:
    """Что записано в самом токене. Без обращения в интернет и без секрета."""
    info = describe_token(token)
    expires = info.get("expires_at")
    return {
        "length": info.get("length"),
        "looks_like_jwt": info.get("looks_like_jwt"),
        "expires_at": expires.strftime("%d.%m.%Y") if expires else None,
        "expired": info.get("expired"),
        "sandbox": info.get("sandbox"),
        "seller_id": info.get("seller_id"),
        "clock_plausible": clock_looks_plausible(token),
        "error": info.get("error"),
    }


def _tls_section(ca_bundle: str) -> dict[str, Any]:
    """Каким сертификатом отвечает WB и не вклинился ли кто в соединение."""
    host = urllib.parse.urlsplit(ADVERT_URL).hostname or ""
    cert = inspect_certificate(host)
    return {
        "host": host,
        "issuer": cert.get("issuer"),
        "not_after": cert.get("not_after"),
        "expired": cert.get("expired"),
        "interceptor": name_interceptor(cert.get("issuer")),
        "own_bundle": ca_bundle or "",
        "error": cert.get("error"),
    }


# ── человекочитаемый отчёт ────────────────────────────────────────────────

def render(report: dict[str, Any]) -> str:
    """Собирает отчёт, который можно отправить целиком, ничего не поясняя."""
    lines: list[str] = []
    add = lines.append

    add("ДИАГНОСТИКА СВЯЗИ С WILDBERRIES")
    add(f"Снято: {report['when']}")
    add("")

    env = report["environment"]
    add("Компьютер")
    add(f"  Python {env['python']} · {env['system']}")
    add(f"  Время на компьютере: {env['now']}")
    add(f"  OpenSSL: {env['openssl']}")
    add(f"  Набор сертификатов certifi: {env['certifi']}")
    add("")

    tok = report["token"]
    add("Токен (прочитан локально, в интернет не отправлялся)")
    add(f"  Длина: {tok['length']} символов")
    if tok.get("error"):
        add(f"  Проблема: {tok['error']}")
    if tok.get("expires_at"):
        add(f"  Действует до: {tok['expires_at']}"
            + ("  — СРОК ВЫШЕЛ" if tok.get("expired") else ""))
    if tok.get("sandbox"):
        add("  Выпущен для тестового контура (песочницы) — реальных данных не даст")
    if tok.get("seller_id"):
        add(f"  Кабинет: {tok['seller_id']}")
    if tok.get("clock_plausible") is False:
        add("  Часы компьютера похожи на сбитые — от этого «портятся» сертификаты")
    add("")

    tls = report["tls"]
    add("Защищённое соединение")
    if tls.get("error"):
        add(f"  Не удалось посмотреть сертификат: {tls['error']}")
    else:
        add(f"  {tls['host']} отвечает сертификатом от: {tls['issuer']}")
        add(f"  Сертификат действует до: {tls['not_after']}")
    if tls.get("interceptor"):
        add(f"  В соединение вклинивается: {tls['interceptor']}")
        add("  Это и ломает проверку сертификата")
    if tls.get("own_bundle"):
        add(f"  Используется свой набор корней: {tls['own_bundle']}")
    add("")

    add("Методы WB")
    add("")
    for finding in report["findings"]:
        add(f"  {finding.mark} {finding.label}")
        add(f"      {finding.method} {_short_url(finding.url)}")
        if finding.status is not None:
            add(f"      Код ответа: {finding.status} · {finding.seconds:.1f} с")
        if finding.error:
            add(f"      Не дошли: {finding.error}")
        if finding.wb_said:
            add(f"      Ответ WB: {finding.wb_said}")
        if finding.records is not None:
            add(f"      Записей: {finding.records}")
        if finding.shape:
            for i, chunk in enumerate(finding.shape.split("\n")):
                add(f"      {'Форма ответа: ' if i == 0 else '  '}{chunk.strip()}")
        if finding.headers:
            pairs = ", ".join(f"{k}: {v}" for k, v in finding.headers.items())
            add(f"      Лимиты: {pairs}")
        if finding.note:
            add(f"      → {finding.note}")
        add("")

    add("Отключённые адреса (здесь 404 — правильный ответ)")
    for finding in report["retired"]:
        add(f"  {finding.mark} {finding.label}: {finding.note}")
    add("")

    problems = report["problems"]
    warnings = report["warnings"]
    add("ИТОГ")
    if not problems and not warnings:
        add("  Всё отвечает и отдаёт данные. Можно собирать.")
    if problems:
        add(f"  Не работает методов: {len(problems)}")
        for finding in problems:
            add(f"    • {finding.label}: {finding.note or finding.error or finding.status}")
    if warnings:
        add(f"  Отвечает, но без данных: {len(warnings)}")
        for finding in warnings:
            add(f"    • {finding.label}: {finding.note}")
    return "\n".join(lines)


def _short_url(url: str) -> str:
    """Адрес без параметров: в них номера кампаний, они только мешают читать."""
    return url.split("?")[0]


def save(report: dict[str, Any], folder: Path) -> Path:
    """Пишет отчёт рядом с программой — файл отправляют как есть."""
    path = folder / "диагностика.txt"
    path.write_text(render(report), encoding="utf-8")
    return path
