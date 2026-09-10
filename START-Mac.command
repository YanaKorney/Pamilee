#!/bin/bash
# Запуск аналитики рекламы Wildberries.
# Двойной клик по этому файлу. Если Mac скажет «неопознанный разработчик» —
# правый клик по файлу → «Открыть».

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py
else
    echo
    echo "  Python не найден."
    echo
    echo "  Скачайте его: https://www.python.org/downloads/"
    echo "  и запустите этот файл ещё раз."
    echo
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
