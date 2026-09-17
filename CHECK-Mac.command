#!/bin/bash
# Только проверка доступа к WB — чтобы быстро перепроверить соединение
# после изменений: дата и время, антивирус, другая сеть.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py check
else
    echo
    echo "  Python не найден. Скачайте: https://www.python.org/downloads/"
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
