#!/bin/bash
# Открывает дашборд для других компьютеров этой же сети. В окне покажет
# адрес и код доступа. Сбор данных по-прежнему идёт на этом компьютере.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py serve --network
else
    echo
    echo "  Python не найден. Скачайте: https://www.python.org/downloads/"
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
