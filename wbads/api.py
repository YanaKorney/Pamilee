"""HTTP-слой: read-only эндпоинты для дашборда + отдача статики.

Сервер на стандартной библиотеке — чтобы запускалось без установки пакетов.
Обработчики отделены от транспорта (словарь ROUTES), поэтому при переезде
на FastAPI переносится только транспорт, а логика остаётся как есть.
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import socket
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import analytics, changes, db
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


def handle_changes(conn: sqlite3.Connection, cfg: Config,
                   query: dict[str, list[str]]) -> dict[str, Any]:
    """Журнал изменений вместе с замером эффекта по каждой записи."""
    window = int((query.get("window") or [str(changes.DEFAULT_WINDOW)])[0] or 7)
    window = max(3, min(window, 30))
    nm_id = (query.get("nm_id") or [""])[0]
    advert_id = (query.get("advert_id") or [""])[0]
    limit = int((query.get("limit") or ["200"])[0] or 200)

    rows = changes.listing(
        conn,
        date_from=(query.get("from") or [""])[0],
        date_to=(query.get("to") or [""])[0],
        nm_id=int(nm_id) if nm_id.isdigit() else None,
        advert_id=int(advert_id) if advert_id.isdigit() else None,
        limit=max(1, min(limit, 1000)),
    )
    _, data_to = db.data_range(conn)
    items = changes.with_effects(conn, rows, window=window, today=data_to or "")
    names = _article_names(conn)
    campaign_names = _campaign_names(conn)
    for item in items:
        item["nm_name"] = names.get(item.get("nm_id"), "")
        item["campaign_name"] = campaign_names.get(item.get("advert_id"), "")
        item["crowding"] = changes.crowding(conn, item["date"], window)
    return {"window": window, "changes": items, "total": len(items)}


def handle_articles(conn: sqlite3.Connection, cfg: Config,
                    query: dict[str, list[str]]) -> dict[str, Any]:
    """Артикулы и кампании для формы записи — чтобы не набивать номера руками."""
    articles = [{"nm_id": nm_id, "name": name}
                for nm_id, name in sorted(_article_names(conn).items(),
                                          key=lambda kv: kv[1] or str(kv[0]))]
    campaigns = [{"advert_id": advert_id, "name": name}
                 for advert_id, name in sorted(_campaign_names(conn).items(),
                                               key=lambda kv: kv[1] or str(kv[0]))]
    return {"articles": articles, "campaigns": campaigns}


def _article_names(conn: sqlite3.Connection) -> dict[int, str]:
    """Названия товаров по артикулу — из статистики и из заказов.

    В статистике рекламы название есть не всегда, а в заказах лежит
    предмет и артикул продавца: вместе получается понятнее, чем номер.
    """
    names: dict[int, str] = {}
    for row in conn.execute(
            "SELECT nm_id, subject, supplier_article FROM orders_raw"
            " WHERE nm_id IS NOT NULL GROUP BY nm_id"):
        parts = [p for p in (row["subject"], row["supplier_article"]) if p]
        if parts:
            names[int(row["nm_id"])] = " · ".join(str(p) for p in parts)
    for row in conn.execute(
            "SELECT nm_id, name FROM campaign_nm_daily"
            " WHERE name != '' GROUP BY nm_id"):
        names.setdefault(int(row["nm_id"]), str(row["name"]))
    for row in conn.execute("SELECT DISTINCT nm_id FROM campaign_nm_daily"):
        names.setdefault(int(row["nm_id"]), "")
    return names


def _campaign_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {int(row["advert_id"]): str(row["name"] or "")
            for row in conn.execute("SELECT advert_id, name FROM campaigns")}


ROUTES: dict[str, Callable] = {
    "/api/report": handle_report,
    "/api/campaign": handle_campaign,
    "/api/meta": handle_meta,
    "/api/changes": handle_changes,
    "/api/articles": handle_articles,
}


def handle_change_add(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    """Записывает изменение из формы дашборда."""
    nm_id = body.get("nm_id")
    advert_id = body.get("advert_id")
    change_id = changes.add(
        conn,
        day=str(body.get("date") or ""),
        text=str(body.get("text") or ""),
        nm_id=int(nm_id) if str(nm_id or "").strip().isdigit() else None,
        advert_id=int(advert_id) if str(advert_id or "").strip().isdigit() else None,
    )
    return {"ok": True, "id": change_id}


def handle_change_delete(conn: sqlite3.Connection,
                         body: dict[str, Any]) -> dict[str, Any]:
    change_id = int(body.get("id") or 0)
    return {"ok": changes.delete(conn, change_id)}


def handle_change_edit(conn: sqlite3.Connection,
                       body: dict[str, Any]) -> dict[str, Any]:
    """Правка уже сделанной записи.

    Товар и кампанию форма правки не трогает: менять их вместе с текстом
    значит незаметно переносить правку на другой объект, а вместе с ней —
    и границы окон у соседних записей.
    """
    change_id = int(body.get("id") or 0)
    ok = changes.update(conn, change_id,
                        day=str(body.get("date") or ""),
                        text=str(body.get("text") or ""),
                        keep_subject=True)
    return {"ok": ok}


def lan_addresses() -> list[str]:
    """Адреса этого компьютера в локальной сети.

    Спрашиваем у системы, каким адресом она пошла бы наружу: перебирать
    сетевые карты руками значит выдать человеку список из шести строк,
    где пять не работают.
    """
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Соединения не происходит: UDP-сокет просто выбирает маршрут.
            probe.connect(("192.168.255.255", 9))
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            address = info[4][0]
            if address.startswith("127.") or address in found:
                continue
            found.append(address)
    except OSError:
        pass
    return found


def _print_network_hint(port: int, access_code: str) -> None:
    """Говорит, что набрать на другом компьютере, и чем это чревато."""
    addresses = lan_addresses()
    print()
    if addresses:
        print("  С другого компьютера в этой же сети наберите в браузере:")
        for address in addresses:
            line = f"http://{address}:{port}"
            print(f"      {line}")
    else:
        print("  Адрес в локальной сети определить не вышло. Посмотрите его")
        print("  в настройках сети — это адрес вида 192.168.х.х.")
    if access_code:
        print()
        print(f"  Код доступа: {access_code}")
        print("  Его спросят один раз на каждом компьютере.")
    print()
    print("  Важно: дашборд сейчас виден всем в этой сети. Код — это щеколда,")
    print("  а не замок: он от случайных глаз, не от злого умысла.")
    print("  В чужой сети (кафе, коворкинг) так запускать не стоит.")
    if sys.platform == "win32":
        print()
        print("  Windows спросит разрешение на доступ к сети — разрешите")
        print("  для ЧАСТНЫХ сетей. Без этого другой компьютер не достучится.")


def _disposition(filename: str) -> str:
    """Заголовок с именем файла, который не ломается о кириллицу.

    Заголовки HTTP отправляются в latin-1, и «журнал-изменений.csv» валит
    ответ на полуслове: браузер получает пустой файл без единой ошибки.
    Поэтому русское имя кодируется по RFC 5987, а рядом остаётся простое
    латинское — для тех, кто этой кодировки не понимает.
    """
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
    stem = ascii_name.rsplit(".", 1)[0]
    # От русского имени в latin-1 остаются одни дефисы и точки — такое
    # запасное имя хуже честного «export.csv».
    if not any(char.isalnum() for char in stem):
        suffix = filename.rsplit(".", 1)[-1] if "." in filename else "csv"
        ascii_name = f"export.{suffix}"
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename)}")


def handle_changes_csv(conn: sqlite3.Connection, cfg: Config,
                       query: dict[str, list[str]]) -> tuple[str, str]:
    """Журнал целиком — файлом, который открывается в Excel.

    И читается обратно: выгрузка, которую нельзя загрузить, это не
    резервная копия, а распечатка.
    """
    names = _article_names(conn)
    campaign_names = _campaign_names(conn)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Дата", "Артикул", "Товар", "Кампания",
                     "Название кампании", "Что сделали", "Источник"])
    for row in changes.export_rows(conn):
        writer.writerow([
            row["date"],
            row["nm_id"] or "",
            names.get(row["nm_id"], ""),
            row["advert_id"] or "",
            campaign_names.get(row["advert_id"], ""),
            row["text"],
            row["source"],
        ])
    return buffer.getvalue(), "журнал-изменений.csv"


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

    def _send_file(self, path: Path,
                   query: dict[str, list[str]] | None = None) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "Файл не найден"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        if query is not None:
            # Верный код запоминаем здесь: иначе его пришлось бы вводить
            # при каждом открытии страницы.
            self._remember_code(query)
        self.end_headers()
        self.wfile.write(body)

    # ── маршрутизация ─────────────────────────────────────────────────────

    # ── код доступа ───────────────────────────────────────────────────────
    #
    # Пустой код означает «сервер слушает только этот компьютер»: спрашивать
    # у себя же пароль незачем. Код появляется вместе с выходом в сеть.

    access_code = ""

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        if not self.access_code:
            return True
        # С самого компьютера код не спрашиваем: тот, кто сидит за ним,
        # и так видит его в окне программы. Иначе в сетевом режиме
        # дашборд требовал бы пароль у собственного хозяина.
        if self._from_this_computer():
            return True
        given = (query.get("код") or query.get("code") or [""])[0]
        if hmac.compare_digest(given, self.access_code):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "wbads_code" and hmac.compare_digest(value,
                                                            self.access_code):
                return True
        return False

    def _from_this_computer(self) -> bool:
        """Запрос пришёл с самой машины, а не из сети."""
        address = (self.client_address or ("",))[0]
        return address in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _ask_for_code(self, route: str) -> None:
        """Показывает поле для кода вместо запрошенной страницы."""
        if route.startswith("/api/"):
            self._send_json({"error": "Нужен код доступа"}, 401)
            return
        body = CODE_PAGE.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _remember_code(self, query: dict[str, list[str]]) -> None:
        """Запоминает верный код в браузере, чтобы не спрашивать снова."""
        if not self.access_code:
            return
        given = (query.get("код") or query.get("code") or [""])[0]
        if given and hmac.compare_digest(given, self.access_code):
            self.send_header("Set-Cookie",
                             f"wbads_code={self.access_code}; Path=/; "
                             "Max-Age=2592000; SameSite=Lax")

    def do_GET(self) -> None:  # noqa: N802 (имя задано базовым классом)
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)

        if not self._authorized(query):
            self._ask_for_code(route)
            return

        try:
            if route in ("/api/export.csv", "/api/changes.csv"):
                maker = (handle_changes_csv if route == "/api/changes.csv"
                         else handle_export)
                with db.session(self.config.db_path) as conn:
                    content, filename = maker(conn, self.config, query)
                body = content.encode("utf-8-sig")  # BOM — чтобы Excel не ломал кириллицу
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 _disposition(filename))
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
                self._send_file(WEB_DIR / "index.html", query)
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
        if not self._authorized(parse_qs(parsed.query)):
            self._send_json({"error": "Нужен код доступа"}, 401)
            return
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

        if parsed.path in ("/api/changes", "/api/changes/delete",
                           "/api/changes/edit"):
            try:
                with db.session(self.config.db_path) as conn:
                    if parsed.path.endswith("/delete"):
                        self._send_json(handle_change_delete(conn, body))
                    elif parsed.path.endswith("/edit"):
                        self._send_json(handle_change_edit(conn, body))
                    else:
                        self._send_json(handle_change_add(conn, body))
            except ValueError as exc:
                # Понятная ошибка ввода — не повод показывать «сбой сервера».
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        self._send_json({"error": "Не найдено"}, 404)


# Запасные порты. На Windows 8000 нередко занят или зарезервирован системой
# (диапазоны Hyper-V, WSL, службы), и попытка его занять падает с WinError 10013 —
# «доступ запрещён», хотя порт вроде бы свободен. Перебираем варианты,
# последний — 0: система сама выдаст любой свободный.
FALLBACK_PORTS = (8000, 8080, 8123, 8765, 5000, 3000, 0)

# Страница ввода кода. Отдельным файлом её делать незачем: она должна
# работать даже тогда, когда всё остальное закрыто кодом.
CODE_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Аналитика рекламы — код доступа</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0;
         background: #f4f3f0; color: #0b0b0b; padding: 16px; }
  @media (prefers-color-scheme: dark) {
    body { background: #131312; color: #fff; }
    .card { background: #1a1a19; border-color: #2e2e2b; }
    input { background: #232321; color: #fff; border-color: #43433f; }
  }
  .card { background: #fff; border: 1px solid #e2e0da; border-radius: 12px;
          padding: 28px; max-width: 360px; width: 100%; }
  h1 { font-size: 19px; margin: 0 0 6px; }
  p { margin: 0 0 18px; color: #6b6a64; font-size: 14px; }
  input { width: 100%; box-sizing: border-box; font: inherit; font-size: 19px;
          letter-spacing: .18em; text-align: center; padding: 11px;
          border: 1px solid #cbc8c0; border-radius: 8px; }
  button { width: 100%; margin-top: 12px; font: inherit; font-weight: 600;
           padding: 11px; border: 0; border-radius: 8px;
           background: #2a78d6; color: #fff; cursor: pointer; }
</style></head>
<body><form class="card" method="get" action="/">
  <h1>Аналитика рекламы Wildberries</h1>
  <p>Введите код, который показан в окне программы на том компьютере,
     где она запущена.</p>
  <input name="code" inputmode="numeric" autocomplete="off" autofocus
         aria-label="Код доступа" placeholder="0000">
  <button type="submit">Открыть</button>
</form></body></html>
"""


def _bind_server(config: Config, handler: type,
                 host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, int]:
    """Занимает первый доступный порт и возвращает сервер вместе с ним."""
    tried: list[int] = []
    candidates = [config.port] + [p for p in FALLBACK_PORTS if p != config.port]

    for port in candidates:
        try:
            server = ThreadingHTTPServer((host, port), handler)
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


def serve(config: Config, open_browser: bool = True, network: bool = False,
          access_code: str = "") -> None:
    """Поднимает дашборд: только для этого компьютера или для всей сети.

    По умолчанию сервер слушает 127.0.0.1 — адрес, доступный лишь самому
    компьютеру. Это не перестраховка: в дашборде видны обороты, расходы и
    названия кампаний, а пароля у него нет. Выход в сеть включается
    отдельно и осознанно, и тогда же появляется код доступа.
    """
    handler = type("BoundHandler", (DashboardHandler,),
                   {"config": config, "access_code": access_code})
    host = "0.0.0.0" if network else "127.0.0.1"
    try:
        server, port = _bind_server(config, handler, host)
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
    if network:
        _print_network_hint(port, access_code)
    else:
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
