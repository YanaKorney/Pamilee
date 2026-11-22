"""Показывает номера этапов воронки и сотрудников amoCRM (они нужны для настройки).

Запуск:  python show_ids.py
Берёт AMO_SUBDOMAIN и AMO_TOKEN из переменных окружения (из файла .env).
"""
import os

import requests

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
