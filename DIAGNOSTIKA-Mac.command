#!/bin/bash
# Полная диагностика: проверяет все методы WB за один запуск и пишет
# отчёт в файл диагностика.txt. Не останавливается на первой ошибке.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py diagnose
else
    echo
    echo "  Python не найден. Скачайте: https://www.python.org/downloads/"
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
