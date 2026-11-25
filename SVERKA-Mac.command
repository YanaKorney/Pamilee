#!/bin/bash
# Сверка заказов за один день: что отдаёт WB, что лежит у нас и какой
# способ счёта совпадает с воронкой продаж в кабинете.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 run.py sverka
else
    echo
    echo "  Python не найден. Скачайте: https://www.python.org/downloads/"
fi

echo
read -n 1 -s -r -p "  Нажмите любую клавишу, чтобы закрыть окно."
echo
