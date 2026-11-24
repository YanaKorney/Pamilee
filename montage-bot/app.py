"""Бот-уведомлялка: сделка в amoCRM перешла на этап «Монтаж» -> монтажнику приходит сообщение в Max.

Как это работает:
1. amoCRM присылает сюда сигнал (вебхук) с номером сделки: /amo-webhook
2. Мы запрашиваем у amoCRM данные сделки и клиента.
3. Находим монтажника (ответственного в сделке) в employees.json.
4. Отправляем ему сообщение через бота Max.

Дополнительно бот отвечает сотруднику его Max-ID, когда тот пишет боту /start. Этот ID
нужно один раз внести в employees.json.
"""
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, abort, request


def load_env_file():
    """Читает файл .env рядом с программой (если он есть)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("montage-bot")

app = Flask(__name__)

MSK = timezone(timedelta(hours=3))
DEDUPE_SECONDS = 600
_recent = {}
_recent_lock = threading.Lock()


def env(name, default=""):
    return os.environ.get(name, default).strip()


# ---------- amoCRM ----------

def amo_get(path, params=None):
    url = f"https://{env('AMO_SUBDOMAIN')}.amocrm.ru/api/v4{path}"
    headers = {"Authorization": f"Bearer {env('AMO_TOKEN')}"}
    r = requests.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json() if r.content else {}


def field_value(entity, name=None, code=None):
    """Значение дополнительного поля по названию (name) или коду (code)."""
    for f in entity.get("custom_fields_values") or []:
        if (name and f.get("field_name") == name) or (code and f.get("field_code") == code):
            values = f.get("values") or []
            return values[0].get("value") if values else None
    return None


def format_date(value):
    """amoCRM отдаёт поля-даты числом (секунды). Превращаем в «12.10.2026 14:00»."""
    if value in (None, ""):
        return "не указана"
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return str(value)
    dt = datetime.fromtimestamp(ts, MSK)
    if dt.hour == 0 and dt.minute == 0:
        return dt.strftime("%d.%m.%Y")
    return dt.strftime("%d.%m.%Y %H:%M")


def collect_deal_info(lead_id):
    lead = amo_get(f"/leads/{lead_id}", {"with": "contacts"})
    contacts = (lead.get("_embedded") or {}).get("contacts") or []
    main = next((c for c in contacts if c.get("is_main")), contacts[0] if contacts else None)
    client = amo_get(f"/contacts/{main['id']}") if main else {}
    return {
        "deal": lead.get("name") or f"Сделка {lead_id}",
        "address": field_value(lead, name=env("AMO_ADDRESS_FIELD", "Адрес")) or "не указан",
        "date": format_date(field_value(lead, name=env("AMO_DATE_FIELD", "Дата монтажа"))),
        "comment": field_value(lead, name=env("AMO_COMMENT_FIELD", "Комментарий")) or "",
        "client_name": client.get("name") or "не указан",
        "client_phone": field_value(client, code="PHONE") or "не указан",
        "responsible_user_id": lead.get("responsible_user_id"),
    }


# ---------- Max ----------

def max_send(user_id, text):
    url = f"{env('MAX_API_URL', 'https://platform-api.max.ru')}/messages"
    r = requests.post(
        url,
        params={"user_id": user_id},
        headers={"Authorization": env("MAX_TOKEN")},  # токен без слова Bearer
        json={"text": text},
        timeout=10,
    )
    if r.status_code >= 400:
        log.error("Max не принял сообщение для %s: %s %s", user_id, r.status_code, r.text)
    r.raise_for_status()


# ---------- сотрудники ----------

def load_employees():
    path = env("EMPLOYEES_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "employees.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_employee(amo_user_id):
    for e in load_employees():
        if str(e.get("amo_user_id")) == str(amo_user_id):
            return e
    return None


# ---------- основная логика ----------

def build_message(info):
    lines = [
        "Назначен монтаж",
        f"Заказ: {info['deal']}",
        f"Дата и время: {info['date']}",
        f"Адрес: {info['address']}",
        f"Клиент: {info['client_name']}, {info['client_phone']}",
    ]
    if info["comment"]:
        lines.append(f"Комментарий: {info['comment']}")
    return "\n".join(lines)


def notify_installer(lead_id):
    info = collect_deal_info(lead_id)
    text = build_message(info)
    employee = find_employee(info["responsible_user_id"])
    manager = env("MANAGER_MAX_USER_ID")
    if employee and employee.get("max_user_id"):
        max_send(employee["max_user_id"], text)
        log.info("Сделка %s: уведомление отправлено (%s)", lead_id, employee.get("name"))
    elif manager:
        max_send(manager, "Не нашёл монтажника для уведомления.\n\n" + text)
        log.warning("Сделка %s: монтажник не найден, написали руководителю", lead_id)
    else:
        log.warning("Сделка %s: монтажник не найден в employees.json", lead_id)


def _safe_notify(lead_id):
    try:
        notify_installer(lead_id)
    except Exception:
        log.exception("Сделка %s: не удалось отправить уведомление", lead_id)


def _is_duplicate(key):
    now = time.time()
    with _recent_lock:
        for k in [k for k, t in _recent.items() if now - t > DEDUPE_SECONDS]:
            del _recent[k]
        if key in _recent:
            return True
        _recent[key] = now
        return False


def _check_secret():
    expected = env("WEBHOOK_SECRET")
    given = request.args.get("secret", "")
    if not expected or not hmac.compare_digest(expected, given):
        abort(403)


# ---------- адреса, на которые стучатся amoCRM и Max ----------

@app.get("/health")
def health():
    return "ok"


@app.post("/amo-webhook")
def amo_webhook():
    _check_secret()
    wanted_status = env("AMO_MONTAGE_STATUS_ID")
    # amoCRM присылает поля вида leads[status][0][id]
    for key, lead_id in request.form.items():
        m = re.fullmatch(r"leads\[status\]\[(\d+)\]\[id\]", key)
        if not m:
            continue
        status_id = request.form.get(f"leads[status][{m.group(1)}][status_id]", "")
        if wanted_status and status_id != wanted_status:
            continue
        if _is_duplicate((lead_id, status_id)):
            log.info("Сделка %s: повторный сигнал, пропускаем", lead_id)
            continue
        threading.Thread(target=_safe_notify, args=(lead_id,), daemon=True).start()
    return "ok"  # отвечаем сразу, чтобы amoCRM не ждала


@app.post("/max-webhook")
def max_webhook():
    _check_secret()
    update = request.get_json(silent=True) or {}
    kind = update.get("update_type")
    user_id = None
    if kind == "bot_started":
        user_id = (update.get("user") or {}).get("user_id")
    elif kind == "message_created":
        user_id = ((update.get("message") or {}).get("sender") or {}).get("user_id")
    if user_id:
        try:
            max_send(user_id, f"Здравствуйте! Ваш ID в Max: {user_id}\nПередайте его руководителю, "
                              "чтобы вас подключили к уведомлениям о монтаже.")
        except Exception:
            log.exception("Не удалось ответить пользователю %s", user_id)
    return "ok"
