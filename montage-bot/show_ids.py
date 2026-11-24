"""Показывает номера этапов воронки и сотрудников amoCRM (они нужны для настройки).

Запуск:  python show_ids.py
Берёт AMO_SUBDOMAIN и AMO_TOKEN из файла .env, который лежит рядом со скриптом.
"""
import os

import requests


def load_env_file():
    """Читает файл .env рядом со скриптом (если он есть)."""
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

base = f"https://{os.environ['AMO_SUBDOMAIN']}.amocrm.ru/api/v4"
headers = {"Authorization": f"Bearer {os.environ['AMO_TOKEN']}"}


def get(path):
    r = requests.get(base + path, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


print("=== ЭТАПЫ ВОРОНОК (нужен номер этапа «Монтаж») ===")
for p in get("/leads/pipelines")["_embedded"]["pipelines"]:
    print(f"Воронка «{p['name']}»")
    for s in p["_embedded"]["statuses"]:
        print(f"    этап «{s['name']}»: номер {s['id']}")

print("\n=== СОТРУДНИКИ (нужен номер каждого монтажника) ===")
for u in get("/users")["_embedded"]["users"]:
    print(f"{u['name']}: номер {u['id']}")
