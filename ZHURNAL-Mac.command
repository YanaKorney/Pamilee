#!/bin/bash
# Переносит журнал изменений из таблицы Excel, которая лежит рядом
# с программой. Сначала покажет, что прочиталось, и спросит.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py import-changes
else
    echo
    echo "  Python не найден. Скачайте: https://www.python.org/downloads/"
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
