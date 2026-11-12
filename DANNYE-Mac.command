#!/bin/bash
# Где лежат ваши данные: база, журнал, токен. Ищет базы прежних
# установок и предлагает перенести из них журнал.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py data
else
    echo
    echo "  Python не найден. Скачайте: https://www.python.org/downloads/"
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
