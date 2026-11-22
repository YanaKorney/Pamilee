"""Один раз говорит Max, куда присылать сообщения пользователей бота.

Запуск:  python setup_max_webhook.py https://ваш-адрес.ru
"""
import os
import sys

import requests

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
