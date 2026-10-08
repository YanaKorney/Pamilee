#!/bin/bash
# Только сбор данных: забирает статистику из кабинета WB за 30 дней
# и дополняет базу. Дашборд при этом не открывается.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py collect --days 30
else
    echo
    echo "  Python не найден. Скачайте: https://www.python.org/downloads/"
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
