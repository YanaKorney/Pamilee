"""HTTP-слой: read-only эндпоинты для дашборда + отдача статики.

Сервер на стандартной библиотеке — чтобы запускалось без установки пакетов.
Обработчики отделены от транспорта (словарь ROUTES), поэтому при переезде
на FastAPI переносится только транспорт, а логика остаётся как есть.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import analytics, db
from .config import Config, Thresholds
from .metrics import parse_date

WEB_DIR = Path(__file__).parent / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _period(conn: sqlite3.Connection, query: dict[str, list[str]]) -> tuple[str, str]:
    """Период из параметров запроса; по умолчанию — последние 7 дней с данными."""
    date_from = (query.get("from") or [""])[0]
    date_to = (query.get("to") or [""])[0]
    if date_from and date_to:
        try:
            lo, hi = parse_date(date_from), parse_date(date_to)
            if lo > hi:
                lo, hi = hi, lo
            return lo.isoformat(), hi.isoformat()
        except ValueError:
            pass
    days = int((query.get("days") or ["7"])[0] or 7)
    return analytics.default_period(conn, max(1, min(days, 365)))


# ── обработчики ───────────────────────────────────────────────────────────

def handle_report(conn: sqlite3.Connection, cfg: Config,
                  query: dict[str, list[str]]) -> dict[str, Any]:
    date_from, date_to = _period(conn, query)
    thresholds = analytics.load_thresholds(conn, cfg.thresholds)
    return analytics.build_report(conn, date_from, date_to, thresholds,
                                  price_field=cfg.order_price_field)


def handle_campaign(conn: sqlite3.Connection, cfg: Config,
                    query: dict[str, list[str]]) -> dict[str, Any]:
    advert_id = int((query.get("id") or ["0"])[0] or 0)
    date_from, date_to = _period(conn, query)
    thresholds = analytics.load_thresholds(conn, cfg.thresholds)
    detail = analytics.campaign_detail(conn, advert_id, date_from, date_to, thresholds,
                                       price_field=cfg.order_price_field)
    if detail is None:
        return {"error": f"Кампания {advert_id} не найдена"}
    return detail


def handle_meta(conn: sqlite3.Connection, cfg: Config,
                query: dict[str, list[str]]) -> dict[str, Any]:
    lo, hi = db.data_range(conn)
    thresholds = analytics.load_thresholds(conn, cfg.thresholds)
    last = db.last_collect(conn)
    orders_lo, orders_hi = db.orders_range(conn)
    return {
        "has_token": cfg.has_token,
        "has_orders": db.has_orders(conn),
        "orders_from": orders_lo,
        "orders_to": orders_hi,
        "has_data": bool(lo),
        "data_from": lo,
        "data_to": hi,
        "thresholds": thresholds.to_dict(),
        "last_collect": dict(last) if last else None,
        "balance": dict(db.latest_balance(conn) or {}) or None,
        "campaigns": len(db.list_campaigns(conn)),
    }


def handle_settings(conn: sqlite3.Connection, cfg: Config,
                    query: dict[str, list[str]], body: dict[str, Any] | None = None
                    ) -> dict[str, Any]:
    """Смена порогов из дашборда — сохраняется в базу и переживает перезапуск."""
    payload = body or {}
    allowed = Thresholds().to_dict()
    saved = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        try:
            saved[key] = float(value)
        except (TypeError, ValueError):
            continue
        db.set_setting(conn, key, str(saved[key]))
    conn.commit()
    return {"saved": saved, "thresholds": analytics.load_thresholds(conn, cfg.thresholds).to_dict()}


def handle_export(conn: sqlite3.Connection, cfg: Config,
                  query: dict[str, list[str]]) -> tuple[str, str]:
    """Выгрузка сводки по кампаниям в CSV — открывается в Excel."""
    date_from, date_to = _period(conn, query)
    thresholds = analytics.load_thresholds(conn, cfg.thresholds)
    report = analytics.build_report(conn, date_from, date_to, thresholds, with_nm=False,
                                    price_field=cfg.order_price_field)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    def dec(value: float, digits: int = 2) -> str:
        """Excel в русской локали ждёт запятую как разделитель дробной части."""
        return f"{value:.{digits}f}".replace(".", ",")

    writer.writerow([
        "Кампания", "ID", "Тип", "Статус", "Вердикт", "Здоровье",
        # воронка целиком: счётчики и конверсия на каждом шаге
        "Показы", "CTR %", "Клики", "В корзине", "CR в корзину %",
        "Заказы", "CR в заказ %", "CR клик-заказ %",
        "CPC руб", "CPO руб", "Средний чек руб",
        "Расход руб", "Выручка с рекламы руб", "Рекламный ДРР %",
        "Весь оборот руб", "Общий ДРР %", "ROAS",
        "Проблемы",
    ])
    for c in report["campaigns"]:
        m = c["metrics"]
        writer.writerow([
            c["name"], c["advert_id"], c["type_name"], c["status_name"],
            c["verdict_label"], c["health"],
            round(m["views"]), dec(m["ctr"]), round(m["clicks"]),
            round(m["atbs"]), dec(m["cr_cart"], 1),
            round(m["orders"]), dec(m["cr_order"], 1), dec(m["cr_click_order"]),
            dec(m["cpc"]), dec(m["cpo"]), dec(m["aov"]),
            dec(m["spend"]), dec(m["revenue"]), dec(m["drr"], 1),
            dec(c.get("total_revenue") or 0), dec(c.get("total_drr") or 0, 1),
            dec(m["roas"]),
            " | ".join(f["title"] for f in c["findings"]),
        ])
    filename = f"wb-reklama-{date_from}_{date_to}.csv"
    return buffer.getvalue(), filename


ROUTES: dict[str, Callable] = {
    "/api/report": handle_report,
    "/api/campaign": handle_campaign,
    "/api/meta": handle_meta,
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "wbads/1.0"
    config: Config

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в консоли
        return

    # ── ответы ────────────────────────────────────────────────────────────

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "Файл не найден"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── маршрутизация ─────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 (имя задано базовым классом)
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)

        try:
            if route == "/api/export.csv":
                with db.session(self.config.db_path) as conn:
                    content, filename = handle_export(conn, self.config, query)
                body = content.encode("utf-8-sig")  # BOM — чтобы Excel не ломал кириллицу
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            handler = ROUTES.get(route)
            if handler:
                with db.session(self.config.db_path) as conn:
                    self._send_json(handler(conn, self.config, query))
                return

            if route in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html")
                return

            # статика: только из папки web, без выхода наружу
            candidate = (WEB_DIR / route.lstrip("/")).resolve()
            if WEB_DIR.resolve() in candidate.parents:
                self._send_file(candidate)
                return
            self._send_json({"error": "Не найдено"}, 404)

        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "Некорректный JSON"}, 400)
            return

        if parsed.path == "/api/settings":
            try:
                with db.session(self.config.db_path) as conn:
                    self._send_json(handle_settings(conn, self.config,
                                                    parse_qs(parsed.query), body))
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return
        self._send_json({"error": "Не найдено"}, 404)


# Запасные порты. На Windows 8000 нередко занят или зарезервирован системой
# (диапазоны Hyper-V, WSL, службы), и попытка его занять падает с WinError 10013 —
# «доступ запрещён», хотя порт вроде бы свободен. Перебираем варианты,
# последний — 0: система сама выдаст любой свободный.
FALLBACK_PORTS = (8000, 8080, 8123, 8765, 5000, 3000, 0)


def _bind_server(config: Config, handler: type) -> tuple[ThreadingHTTPServer, int]:
    """Занимает первый доступный порт и возвращает сервер вместе с ним."""
    tried: list[int] = []
    candidates = [config.port] + [p for p in FALLBACK_PORTS if p != config.port]

    for port in candidates:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except (PermissionError, OSError) as exc:
            tried.append(port)
            last = exc
            continue
        return server, server.server_address[1]

    raise OSError(
        "Не удалось занять ни один порт: "
        + ", ".join(str(p) for p in tried if p)
        + f". Последняя ошибка: {last}"
    )


def serve(config: Config, open_browser: bool = True) -> None:
    """Поднимает локальный дашборд на http://127.0.0.1:<порт>."""
    handler = type("BoundHandler", (DashboardHandler,), {"config": config})
    try:
        server, port = _bind_server(config, handler)
    except OSError as exc:
        print()
        print("  Не удалось открыть дашборд: все проверенные порты заняты.")
        print()
        print("  Что делать:")
        print("   1. Закройте другое окно программы, если оно ещё открыто.")
        print("   2. Перезагрузите компьютер — это освобождает занятые порты.")
        print("   3. Запустите с другим портом, например:")
        print("      python run.py serve --port 9010")
        print()
        print(f"  Подробности: {exc}")
        print()
        return

    url = f"http://127.0.0.1:{port}"
    if port != config.port:
        print(f"\n  Порт {config.port} занят системой — открываю на {port}.")

    print(f"\n  Дашборд запущен: {url}")
    print("  Ссылку можно скопировать и открыть в браузере вручную.")
    print("  Остановить — закройте это окно или нажмите Ctrl+C\n")

    if open_browser:
        def _open() -> None:
            import webbrowser
            webbrowser.open(url)
        threading.Timer(1.0, _open).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Дашборд остановлен.")
    finally:
        server.server_close()
