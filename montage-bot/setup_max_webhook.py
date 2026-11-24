"""Один раз говорит Max, куда присылать сообщения пользователей бота.

Запуск:  python setup_max_webhook.py https://ваш-адрес.ru
"""
import os
import sys

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

public_url = sys.argv[1].rstrip("/")
api = os.environ.get("MAX_API_URL", "https://platform-api.max.ru")
r = requests.post(
    f"{api}/subscriptions",
    headers={"Authorization": os.environ["MAX_TOKEN"]},
    json={
        "url": f"{public_url}/max-webhook?secret={os.environ['WEBHOOK_SECRET']}",
        "update_types": ["bot_started", "message_created"],
    },
    timeout=10,
)
print(r.status_code, r.text)
