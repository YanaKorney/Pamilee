#!/bin/bash
# Запуск приложения «Моя квартира» из корня репозитория.
# Рядом лежит START-Mac.command — это ДРУГАЯ программа,
# аналитика рекламы Wildberries. Не перепутайте.

cd "$(dirname "$0")/kvartira" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py
else
    echo
    echo "На этом компьютере не найден Python."
    echo "Скачайте его с сайта python.org (раздел Downloads), установите"
    echo "и запустите этот файл ещё раз."
    echo
    read -r -p "Нажмите Enter, чтобы закрыть окно… "
fi
