#!/bin/bash
# Двойной клик по этому файлу запускает сервис.
# Если Mac говорит «неопознанный разработчик» — правый клик → «Открыть».

cd "$(dirname "$0")" || exit 1

echo
echo "  Аналитика рекламы Wildberries"
echo "  ----------------------------------------"
echo

if command -v python3 >/dev/null 2>&1; then
    python3 run.py
else
    echo "  На компьютере не найден Python."
    echo
    echo "  Установите его с сайта https://www.python.org/downloads/"
    echo "  и запустите этот файл ещё раз."
fi

echo
echo "  Окно можно закрыть."
