#!/usr/bin/env python3
"""Единая точка входа сервиса аналитики рекламы Wildberries.

Если не знаете, с чего начать — запустите без аргументов:

    python3 run.py            — мастер: проведёт от токена до дашборда

Отдельные шаги, если нужно вручную:

    python3 run.py setup      — настроить токен (спросит и сохранит сам)
    python3 run.py check      — проверить, что доступов хватает
    python3 run.py collect    — забрать данные из кабинета WB
    python3 run.py serve      — открыть дашборд в браузере
    python3 run.py demo       — показать демо-кабинет без токена
    python3 run.py report     — краткий отчёт прямо в окне команд
"""

from __future__ import annotations

import sys

# Проверку версии держим до остальных импортов: на старом Python модули
# сервиса не загрузятся, и человек увидит непонятную ошибку вместо совета.
if sys.version_info < (3, 9):
    print()
    print("  На компьютере установлен Python "
          f"{sys.version_info.major}.{sys.version_info.minor}, а нужен 3.9 или новее.")
    print()
    print("  Скачайте свежую версию: https://www.python.org/downloads/")
    print("  На Windows при установке отметьте «Add Python to PATH».")
    print()
    raise SystemExit(1)

import argparse
from datetime import date, datetime, timedelta

from wbads import analytics, collector, db, demo
from wbads.api import serve
from wbads.config import (
    ROOT,
    TOKEN_DROP_FILE,
    clear_template_token,
    load_config,
    token_from_file,
    token_from_template,
    token_in_template,
    write_token,
)
from wbads.rules import money, pct, signed_pct
from wbads.wb_client import (
    WBAdvertClient,
    WBError,
    WBStatisticsClient,
    certifi_bundle,
    describe_token,
    inspect_certificate,
    name_interceptor,
)


def _print(message: str) -> None:
    print(f"  {message}", flush=True)


def _warn_token_in_template() -> None:
    """Предупреждает, если токен вписан в шаблон вместо .env.

    Это не мелочь: .env.example лежит в репозитории, и токен из него
    уедет на GitHub при первом push.
    """
    if not token_in_template():
        return
    print()
    _print("⚠  Токен вписан в .env.example — это шаблон, он уходит в git.")
    _print("   Сервис читает только .env, поэтому токен сейчас не работает,")
    _print("   а при push уедет в репозиторий.")
    _print("   Как исправить:")
    _print("     cp .env.example .env      (Windows: copy .env.example .env)")
    _print("     git checkout .env.example")
    _print("   Токен окажется в .env — этот файл git игнорирует.")
    print()


def _ask(question: str, default: str = "д") -> bool:
    """Вопрос «да/нет». Пустой ответ = вариант по умолчанию."""
    hint = "Д/н" if default == "д" else "д/Н"
    try:
        answer = input(f"  {question} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default == "д"
    return answer[0] in ("д", "y", "1")


def _ask_token() -> str:
    """Спрашивает токен — видимым вводом и с запасным путём через файл.

    Скрытый ввод здесь только мешал: вставить в окно командной строки
    получается не у всех, а увидеть, вставилось ли, было нельзя вообще.
    Токен всё равно ложится в файл на этом же компьютере, так что прятать
    его от самого владельца смысла нет.
    """
    print()
    _print("Откройте кабинет WB → Настройки → Доступ к API → Создать новый токен.")
    _print("Отметьте категории «Продвижение» и «Статистика», скопируйте токен.")
    print()
    if sys.platform == "win32":
        _print("Вставить в это окно можно так:")
        _print("  • щелчок ПРАВОЙ кнопкой мыши (обычно срабатывает сразу);")
        _print("  • если правая кнопка открыла меню — выберите в нём «Вставить»;")
        _print("  • или Ctrl+V.")
    else:
        _print("Вставьте токен: ⌘+V (Mac) или Ctrl+Shift+V (Linux).")
    _print("Токен будет виден на экране — так вы сразу увидите, что он вставился.")
    print()
    _print("Не получается вставить? Тогда сделайте так:")
    _print(f"  1. Создайте рядом с программой файл token.txt")
    _print("  2. Вставьте туда токен через Блокнот и сохраните")
    _print("  3. Вернитесь сюда и просто нажмите Enter")
    _print("  Программа возьмёт токен из файла и удалит его.")
    print()

    try:
        typed = input("  Токен (или Enter, если положили в token.txt): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""

    if typed:
        return typed

    from_file = token_from_file()
    if from_file:
        print()
        _print(f"Взяла токен из файла {TOKEN_DROP_FILE.name} и удалила его.")
        return from_file
    return ""


def cmd_setup(args: argparse.Namespace, quiet_tail: bool = False) -> int:
    """Настройка токена без ручного редактирования файлов."""
    cfg = load_config()

    # Частая ошибка: токен вписан в шаблон, который уходит в репозиторий.
    # Молча переносим его в .env и чистим шаблон.
    stray = token_from_template()
    if stray:
        _print("Нашла токен в файле .env.example — это шаблон, он уходит в репозиторий.")
        write_token(stray)
        clear_template_token()
        _print("Перенесла токен в файл .env, шаблон очистила. Так безопасно.")
        print()
        return cmd_check(args, quiet_tail)

    if cfg.has_token and not getattr(args, "force", False):
        _print("Токен уже настроен.")
        if not _ask("Заменить его на другой?", default="н"):
            print()
            return cmd_check(args, quiet_tail)

    token = ""
    for attempt in (1, 2, 3):
        token = _ask_token()
        if not token:
            print()
            _print("Токен не введён.")
            if attempt < 3 and _ask("Попробовать ещё раз?"):
                continue
            _print("Ничего не изменила.")
            _print("Посмотреть сервис без токена можно так: python run.py demo")
            return 1

        info = describe_token(token)
        if not info["error"]:
            break
        print()
        _print(f"Получено {info['length']} символов — это не похоже на токен WB.")
        _print(f"({info['error']})")
        _print("Обычно токен длиной 150–250 символов и начинается на eyJ.")
        if attempt < 3 and _ask("Попробовать ещё раз?"):
            continue
        _print("Сохраню как есть, но, скорее всего, он не заработает.")
        break

    path = write_token(token)
    clear_template_token()
    info = describe_token(token)
    print()
    _print(f"Токен сохранён в {path.name}. Этот файл никуда не отправляется.")
    _print(f"Сохранено {info['length']} символов, начало {info['preview']}.")
    if info["error"]:
        _print("Похоже, вставилось не всё — проверьте и запустите настройку заново.")
    print()
    return cmd_check(args, quiet_tail)


def cmd_start(args: argparse.Namespace) -> int:
    """Мастер запуска: проводит от токена до открытого дашборда.

    Рассчитан на то, что человек не работает с командной строкой:
    каждый шаг объясняется, на каждой развилке задаётся вопрос.
    """
    cfg = load_config()
    print()
    print("  Аналитика рекламы Wildberries")
    print("  " + "─" * 52)
    print()

    # ── шаг 1: токен ─────────────────────────────────────────────────────
    if token_from_template() or not cfg.has_token:
        _print("Шаг 1 из 3. Доступ к кабинету.")
        print()
        if not cfg.has_token and not token_from_template():
            _print("Токен ещё не настроен. Без него сервис покажет демо-кабинет")
            _print("с придуманными данными — чтобы вы увидели, как всё устроено.")
            print()
            if not _ask("Настроить токен сейчас?"):
                return _start_demo(cfg)
        if cmd_setup(args, quiet_tail=True) != 0:
            print()
            if _ask("Попробовать ввести токен ещё раз?"):
                if cmd_setup(args, quiet_tail=True) != 0:
                    print()
                    _print("Покажу демо-кабинет, чтобы вы не ждали.")
                    return _start_demo(cfg)
            else:
                _print("Покажу демо-кабинет, чтобы вы не ждали.")
                return _start_demo(cfg)
        cfg = load_config()
    else:
        # Токен есть — но он мог устареть или оказаться не тем.
        # Молча идти дальше с нерабочим токеном хуже, чем проверить.
        _print("Шаг 1 из 3. Токен на месте, проверяю доступ.")
        print()
        if cmd_check(args, quiet_tail=True) != 0:
            print()
            if _ask("Ввести другой токен?"):
                if cmd_setup(argparse.Namespace(force=True), quiet_tail=True) != 0:
                    print()
                    _print("Покажу демо-кабинет, чтобы вы не ждали.")
                    return _start_demo(cfg)
                cfg = load_config()
            else:
                print()
                _print("Покажу демо-кабинет на придуманных данных.")
                return _start_demo(cfg)
    print()

    # ── шаг 2: данные ────────────────────────────────────────────────────
    _print("Шаг 2 из 3. Данные.")
    with db.session(cfg.db_path) as conn:
        last, _ = db.data_range(conn)
        fresh = _data_is_fresh(conn)

    if not last:
        _print("Данных ещё нет — заберём их из кабинета.")
        _print("Первый сбор идёт не быстро: WB отдаёт статистику "
               "по три запроса в минуту.")
        print()
        if not _ask("Начать сбор?"):
            return _start_demo(cfg)
        need_collect = True
    elif fresh:
        _print("Данные свежие, собирать заново не нужно.")
        need_collect = False
    else:
        _print("Данные устарели.")
        need_collect = _ask("Обновить их из кабинета?")

    if need_collect:
        print()
        collect_args = argparse.Namespace(days=args.days)
        if cmd_collect(collect_args) != 0:
            print()
            _print("Сбор не удался. Открою дашборд на том, что уже есть.")
    print()

    # ── шаг 3: дашборд ───────────────────────────────────────────────────
    _print("Шаг 3 из 3. Открываю дашборд в браузере.")
    return cmd_serve(argparse.Namespace(port=None, no_browser=False))


def _data_is_fresh(conn, hours: int = 20) -> bool:
    """Свежие ли данные — чтобы не гонять долгий сбор без нужды."""
    last = db.last_collect(conn)
    if not last or not last["finished_at"]:
        return False
    try:
        finished = datetime.fromisoformat(str(last["finished_at"]))
    except ValueError:
        return False
    return (datetime.now() - finished) < timedelta(hours=hours)


def _start_demo(cfg) -> int:
    """Показывает демо-кабинет, когда реальных данных нет."""
    print()
    _print("Показываю демо-кабинет с придуманными данными.")
    with db.session(cfg.db_path) as conn:
        last, _ = db.data_range(conn)
        if not last:
            demo.generate(conn, days=60)
    print()
    return cmd_serve(argparse.Namespace(port=None, no_browser=False))


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = load_config()
    with db.session(cfg.db_path) as conn:
        result = demo.generate(conn, days=args.days)
    _print(f"Демо-данные готовы: {result['campaigns']} кампаний за {result['days']} дней.")
    _print(f"База: {cfg.db_path}")
    _print("Дальше: python3 run.py serve")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    cfg = load_config()
    _warn_token_in_template()
    if not cfg.has_token:
        _print("Не задан токен WB.")
        _print("1. Скопируйте .env.example в .env")
        _print("2. Кабинет WB → Настройки → Доступ к API → создать токен")
        _print("   с категориями «Продвижение» и «Статистика»")
        _print("3. Вставьте его в строку WB_API_TOKEN= файла .env")
        _print("Пока токена нет, посмотреть сервис можно так: python3 run.py demo")
        return 1

    with db.session(cfg.db_path) as conn:
        try:
            result = collector.collect(conn, cfg.token, days=args.days,
                                       on_progress=_print,
                                       ca_bundle=cfg.ca_bundle)
        except WBError as exc:
            _print(f"Ошибка: {exc}")
            return 1
    _print(f"Сохранено: {result['rows']} дней статистики по {result['campaigns']} кампаниям.")
    if result.get("orders"):
        _print(f"Заказов кабинета: {result['orders']} — общий ДРР будет считаться.")
    else:
        _print("Заказы не собраны: общий ДРР считаться не будет. "
               "Нужна категория «Статистика» у токена.")
    _print("Дальше: python3 run.py serve")
    return 0


def _describe_token_for_human(token: str) -> None:
    """Печатает, что записано в самом токене. Помогает не гадать при отказе."""
    info = describe_token(token)

    print()
    _print("Что записано в вашем токене:")
    _print(f"  длина {info['length']} символов, начинается на {info['preview']}")

    if info["error"]:
        _print(f"  ✗ {info['error']}")
        _print("  Скорее всего токен вставился не целиком.")
        _print("  В окне Windows сочетание Ctrl+V часто не работает —")
        _print("  вставляйте щелчком ПРАВОЙ кнопки мыши.")
        return

    if info["expires_at"]:
        when = info["expires_at"].strftime("%d.%m.%Y")
        if info["expired"]:
            _print(f"  ✗ срок действия истёк {when} — нужен новый токен")
        else:
            _print(f"  ✓ действует до {when}")

    if info["sandbox"]:
        _print("  ✗ это токен ТЕСТОВОГО контура")
        _print("    При создании была отмечена галочка «Тестовый контур».")
        _print("    К настоящему кабинету такой токен не подходит — отсюда отказ")
        _print("    сразу по всем методам. Выпустите новый БЕЗ этой галочки.")
    elif info["sandbox"] is False:
        _print("  ✓ токен боевой, не тестовый")

    if info["seller_id"]:
        _print(f"  кабинет: {info['seller_id']}")


def _explain_total_denial(token: str) -> None:
    """Отказ сразу по всем методам — это обычно не про категории."""
    print()
    _print("Отказано во всех методах сразу — и в рекламе, и в заказах.")
    _print("Когда не хватает одной категории, закрывается только она,")
    _print("поэтому дело, скорее всего, в самом токене, а не в галочках.")
    _describe_token_for_human(token)
    print()
    _print("Что проверить по порядку:")
    _print("  1. Не отмечен ли «Тестовый контур» при создании токена.")
    _print("  2. Тот ли это кабинет — токен работает только в своём.")
    _print("  3. Целиком ли он вставился (см. длину выше — обычно 150–250 символов).")
    _print("  4. Не истёк ли срок.")
    print()
    _print("Проще всего выпустить токен заново: Настройки → Доступ к API,")
    _print("категории «Продвижение» и «Статистика», «Тестовый контур» — снять.")
    _print("Потом запустите программу ещё раз, она спросит новый токен.")


def _install_certifi() -> bool:
    """Ставит набор корневых сертификатов ТЕМ ЖЕ Python, что запущен сейчас.

    На Windows «python» и «py» нередко указывают на разные установки,
    и набор, поставленный вручную, оказывается не в той. sys.executable
    снимает этот вопрос: ставим ровно туда, откуда работаем.
    """
    import subprocess

    print()
    _print("Ставлю набор корневых сертификатов…")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "certifi"],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:
        _print(f"Не вышло запустить установку: {exc}")
        return False

    if result.returncode != 0:
        _print("Установка не удалась.")
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        for line in tail[-4:]:
            _print(f"  {line}")
        return False

    # Модуль мог появиться уже после старта программы
    import importlib
    import sys as _sys
    _sys.modules.pop("certifi", None)
    importlib.invalidate_caches()

    if certifi_bundle():
        _print("Готово — набор корней установлен.")
        return True
    _print("Установка прошла, но набор всё равно не виден. Странно.")
    return False


def _offer_certificate_bundle() -> None:
    """Предлагает поставить свежий набор корней и делает это сама."""
    bundle = certifi_bundle()

    if bundle:
        _print("Свежий набор корней (certifi) уже установлен:")
        _print(f"  {bundle}")
        _print("Но проверка всё равно не прошла — значит, дело глубже.")
        print()
        _print("Остаётся вылечить хранилище Windows:")
    else:
        _print("Самый быстрый способ — поставить свежий набор корней.")
        _print("Проверка при этом остаётся полной: меняется только")
        _print("список доверенных корней, ничего не отключается.")
        print()
        if _ask("Поставить его прямо сейчас?"):
            if _install_certifi():
                print()
                _print("Теперь запустите проверку ещё раз:")
                _print("  дважды щёлкните CHECK-Windows.bat")
                return
        else:
            print()
            _print("Если решите поставить вручную, команда такая")
            _print("(именно с этим путём — у вас может быть несколько Python):")
            print()
            _print(f"  {sys.executable} -m pip install certifi")
        print()
        _print("Основательный способ — вылечить хранилище Windows:")

    _print("  • установите все обновления Windows;")
    _print("  • либо поставьте корневой сертификат ISRG Root X1:")
    _print("    letsencrypt.org/certificates → раздел Root CAs →")
    _print("    ISRG Root X1 → строка «Certificate details (self-signed)»")
    _print("    → ссылка der. Затем двойной клик по файлу →")
    _print("    Установить → Локальный компьютер → «Доверенные")
    _print("    корневые центры сертификации».")
    _print("    Строку с DST Root CA X3 не берите — она помечена retired.")


def _who_breaks_the_connection() -> None:
    """Смотрит, чей сертификат показывает WB, и называет виновника по имени.

    Без этого приходится гадать между антивирусом, прокси и самим сайтом.
    По такому соединению ничего не передаётся — только чтение сертификата.
    """
    print()
    _print("Смотрю, чей сертификат отвечает вместо Wildberries…")
    _print("(ничего не передаю — только читаю, кем он выдан)")
    print()

    info = inspect_certificate("advert-api.wildberries.ru")
    if info["error"]:
        _print(f"Разглядеть не вышло: {info['error']}")
        return

    _print(f"Сертификат выдан: {info['issuer'] or 'не разобрать'}")
    if info["not_after"]:
        mark = " — СРОК ВЫШЕЛ" if info["expired"] else ""
        _print(f"Годен до: {info['not_after']}{mark}")

    culprit = name_interceptor(info["issuer"])
    print()
    if culprit:
        _print(f"Это {culprit} — он вклинивается в защищённые соединения")
        _print("и подставляет свой сертификат. Именно поэтому проверка не проходит.")
        print()
        _print("Что сделать: отключите у него проверку защищённых соединений")
        _print("(HTTPS/SSL-сканирование) — или добавьте в исключения python.exe.")
    elif info["expired"] is False:
        # Сертификат сайта в порядке, а проверка всё равно говорит
        # «просрочен» — значит, споткнулись на корне из хранилища системы.
        _print("Это настоящий сертификат Wildberries, и он НЕ просрочен.")
        _print("Значит, «просрочен» относится не к нему, а к корневому")
        _print("сертификату в хранилище вашей системы: оно отстало,")
        _print("и проверка цепочки срывается на устаревшем корне.")
        print()
        _offer_certificate_bundle()
    else:
        _print("Имя выдавшего не похоже на антивирус или корпоративный шлюз.")
        _print("Похоже, в системе не хватает корневого сертификата.")
        _print("Установите обновления Windows, а если не поможет —")
        _print("выполните в этом окне: python -m pip install certifi")


def _likely_to_have_stats(index: list[dict]) -> list[int]:
    """Кампании, у которых статистика за прошлую неделю правдоподобна.

    Проверять на первых пяти по списку — значит часто попадать на давно
    остановленные и получать честное «данных нет» как предупреждение.
    Свежесть важнее номера: сначала те, что менялись недавно.
    """
    live = [row for row in index
            if int(row.get("status") or 0) in (9, 11, 7) and row.get("advertId")]
    live.sort(key=lambda row: str(row.get("changeTime") or ""), reverse=True)
    return [row["advertId"] for row in live]


def cmd_check(args: argparse.Namespace, quiet_tail: bool = False) -> int:
    """Проверяет токен по всем методам, которые нужны сервису.

    Отвечает на вопрос «хватает ли моему токену доступа»: если какой-то
    метод закрыт, видно ровно какой, а не общее «не работает».
    """
    cfg = load_config()
    _warn_token_in_template()
    if not cfg.has_token:
        _print("Токен не найден. Впишите WB_API_TOKEN в файл .env.")
        _print("Кабинет WB → Настройки → Доступ к API → Создать новый токен,")
        _print("категории «Продвижение» и «Статистика».")
        return 1

    client = WBAdvertClient(cfg.token, ca_bundle=cfg.ca_bundle)
    _print("Проверяем доступ к API продвижения (advert-api.wildberries.ru)…")
    print()

    failures: list[str] = []      # отказано в доступе — вот это проблема
    empty: list[str] = []         # доступ есть, но данных за период нет
    problems: list[WBError] = []
    ids: list[int] = []

    def probe(label: str, method: str, call, expect_data: bool = False) -> object:
        """expect_data — метод обязан что-то вернуть; пустой ответ это ⚠.

        Без этого пустой список выглядел бы как успех: сбор проглатывает
        404 внутри себя, и проверка объявляла работающим метод, который
        на деле не отдаёт ничего.
        """
        try:
            result = call()
        except WBError as exc:
            # 404 означает «запрос дошёл, прав хватило, но отдавать нечего».
            # Считать это отказом доступа — значит путать человека:
            # у кампании просто может не быть статистики за период.
            if exc.status == 404:
                print(f"  ⚠ {label:<26} {method}")
                print("    Данных за проверяемый период нет — доступ при этом есть.")
                empty.append(label)
                return None
            print(f"  ✗ {label:<26} {method}")
            # Многострочные объяснения печатаем один раз в конце, а не
            # по три копии подряд — здесь только суть.
            print(f"    {str(exc).splitlines()[0]}")
            failures.append(label)
            problems.append(exc)
            return None

        if expect_data and isinstance(result, list) and not result:
            print(f"  ⚠ {label:<26} {method}")
            print("    Метод отвечает, но данных не отдаёт.")
            # Сбор проглатывает 404 внутри себя, а WB обычно пишет причину
            # в ответе. Показываем её здесь — иначе она теряется совсем.
            reason = getattr(client, "last_404_message", "")
            if reason:
                for line in reason.splitlines():
                    print(f"    {line}")
            empty.append(label)
            return result

        print(f"  ✓ {label:<26} {method}")
        return result

    balance = probe("Баланс кабинета", "GET  /adv/v1/balance", client.balance)
    found = probe("Список кампаний", "GET  /adv/v1/promotion/count",
                  client.campaign_index)
    if found:
        ids = [row["advertId"] for row in found]
        # Спрашиваем статистику у активных кампаний, а не у первых по списку:
        # у остановленной год назад её честно нет, и проверка зря пугала бы
        # предупреждением. Статистика бывает у статусов 9, 11 и 7.
        probe_ids = _likely_to_have_stats(found) or ids
        probe("Названия кампаний", "GET  /api/advert/v2/adverts",
              lambda: client.campaign_details(probe_ids[:5]), expect_data=True)
        # За сегодня статистики может ещё не быть — берём прошедшую неделю.
        stats_to = date.today() - timedelta(days=1)
        stats_from = stats_to - timedelta(days=6)
        probe("Статистика по дням", "GET  /adv/v3/fullstats",
              lambda: client.fullstats(probe_ids[:5], stats_from.isoformat(),
                                       stats_to.isoformat()),
              expect_data=True)

    # Категория «Статистика» отдельная: без неё реклама считается,
    # а общий ДРР — нет. Поэтому её отказ не валит проверку целиком.
    print()
    _print("Проверяем доступ к заказам (нужен для общего ДРР)…")
    print()
    stats_client = WBStatisticsClient(cfg.token, ca_bundle=cfg.ca_bundle)
    orders_ok = probe("Заказы кабинета", "GET  /api/v1/supplier/orders",
                      lambda: stats_client.orders(date.today().isoformat(), max_pages=1))
    if orders_ok is None:
        failures.remove("Заказы кабинета")
    print()

    if failures:
        # Сначала разбираемся, WB вообще отвечал или до него не дошли.
        # Сетевой сбой и просроченный сертификат не имеют отношения
        # к токену, и советовать его перевыпустить — вредно.
        blocked = next((e for e in problems if e.kind in ("tls", "network")), None)
        if blocked is not None:
            print()
            for line in str(blocked).splitlines():
                print(f"  {line}" if line else "")
            if blocked.kind == "tls":
                _who_breaks_the_connection()
            print()
            _print("Пока это не решится, можно посмотреть демо-кабинет:")
            _print("  python run.py demo   затем   python run.py serve")
            return 1

        # Отказ и в рекламе, и в заказах означает проблему с самим токеном,
        # а не с набором категорий — тогда советовать «проверьте галочки» вредно.
        if orders_ok is None and len(failures) >= 2:
            _explain_total_denial(cfg.token)
            return 1
        _print(f"Закрыто методов рекламы: {len(failures)}.")
        if balance is not None or found:
            # Часть методов «Продвижения» прошла — значит, категория на месте,
            # и советовать её проверить было бы просто неверно.
            _print("Категория «Продвижение» у токена есть: другие её методы")
            _print("отработали. Значит, дело в чём-то одном:")
            _print("  • WB закрыл доступ именно к этим методам для вашего")
            _print("    кабинета — тогда остаётся написать в поддержку;")
            _print("  • либо это временно: попробуйте через час.")
        else:
            _print("Проверьте в кабинете, что у токена отмечена категория «Продвижение».")
            _print("Если она есть, а доступа нет — выпустите токен заново")
            _print("в разделе «Настройки → Доступ к API».")
            _describe_token_for_human(cfg.token)
        return 1

    if getattr(client, "used_fallback_bundle", False):
        _print("Хранилище сертификатов системы устарело — использую набор certifi.")
        _print("Работает, но стоит установить обновления Windows.")
        print()

    if empty:
        _print("Доступ есть везде. Часть методов данных не отдала:")
        if "Названия кампаний" in empty:
            _print("  • Названия кампаний — у вашего кабинета этот метод молчит.")
            _print("    Не страшно: тип и статус берутся из списка кампаний,")
            _print("    а сами кампании будут называться по номеру.")
        if "Статистика по дням" in empty:
            _print("  • Статистика — у проверенных кампаний могло не быть")
            _print("    открутки за период. Сбор переберёт все и найдёт те,")
            _print("    у которых данные есть.")
        print()
        _print("Сбор запускайте — это рабочая ситуация.")
        print()

    if orders_ok is None:
        _print("Реклама читается, заказы — нет.")
        _print("Рекламный ДРР считаться будет, общий — нет.")
        _print("Чтобы появился общий ДРР, нужен токен с категорией «Статистика».")
    else:
        _print("Токен работает, доступа хватает — считаются оба ДРР.")
    if balance:
        _print(f"Баланс кабинета: {money(balance['net'])} "
               f"(счёт {money(balance['balance'])}, бонусы {money(balance['bonus'])})")
    _print(f"Кампаний в кабинете: {len(ids)}")
    if not quiet_tail:
        _print("Дальше: python3 run.py collect --days 30")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.port:
        cfg.port = args.port
    with db.session(cfg.db_path) as conn:
        lo, _ = db.data_range(conn)
    if not lo:
        _print("В базе пока нет данных.")
        _print("Заполните демо-данными: python3 run.py demo")
        _print("Или соберите из кабинета:  python3 run.py collect")
        return 1
    serve(cfg, open_browser=not args.no_browser)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Короткий отчёт в консоли — когда браузер открывать не хочется."""
    cfg = load_config()
    with db.session(cfg.db_path) as conn:
        lo, _ = db.data_range(conn)
        if not lo:
            _print("В базе нет данных. Запустите: python3 run.py demo")
            return 1
        date_from, date_to = analytics.default_period(conn, args.days)
        thresholds = analytics.load_thresholds(conn, cfg.thresholds)
        report = analytics.build_report(conn, date_from, date_to, thresholds,
                                        with_nm=False)

    t, s = report["totals"], report["summary"]
    cmp_ = report["compare"]
    print()
    print(f"  Реклама Wildberries · {date_from} — {date_to}")
    print("  " + "─" * 64)
    print(f"  Расход  {money(t['spend']):>14}   ({signed_pct(cmp_['spend']['delta_pct'])} к прошлому периоду)")
    print(f"  Выручка {money(t['revenue']):>14}   ({signed_pct(cmp_['revenue']['delta_pct'])})")
    print(f"  ДРР     {pct(t['drr']):>14}   при цели {pct(thresholds.target_drr, 0)}")
    print(f"  Заказы  {t['orders']:>14.0f}   ({signed_pct(cmp_['orders']['delta_pct'])})")
    print(f"  CPO     {money(t['cpo']):>14}   ROAS {t['roas']:.1f}")

    orders = report.get("orders") or {}
    if orders.get("available"):
        print()
        print(f"  Весь оборот по заказам {money(orders['revenue']):>14}"
              f"   ({orders['orders']:.0f} заказов, отменено {orders['cancels']:.0f})")
        print(f"  ДРР общий              {pct(orders['total_drr']):>14}"
              f"   реклама даёт {pct(orders['ad_share'], 0)} оборота")
    print()
    print("  Воронка")
    print(f"  {t['views']:>10,.0f} показов".replace(",", " ") +
          f"  → CTR {pct(t['ctr'], 2)} ({signed_pct(cmp_['ctr']['delta_pct'])})")
    print(f"  {t['clicks']:>10,.0f} кликов".replace(",", " ") +
          f"   → в корзину {pct(t['cr_cart'])} ({signed_pct(cmp_['cr_cart']['delta_pct'])})")
    print(f"  {t['atbs']:>10,.0f} в корзине".replace(",", " ") +
          f" → в заказ {pct(t['cr_order'])} ({signed_pct(cmp_['cr_order']['delta_pct'])})")
    print(f"  {t['orders']:>10,.0f} заказов".replace(",", " ") +
          f"   · сквозная клик → заказ {pct(t['cr_click_order'], 2)}")
    print()
    print(f"  🔴 {s['critical']} требуют действий   🟡 {s['warning']} под наблюдением   "
          f"🚀 {s['opportunity']} можно масштабировать   🟢 {s['healthy']} в норме")
    if s["money_at_risk"] > 0:
        print(f"  Мимо цели уходит примерно {money(s['money_at_risk'])} за период.")
    print()

    for c in report["campaigns"]:
        if c["verdict"] in ("ok", "idle") and not args.all:
            continue
        drr = pct(c["metrics"]["drr"]) if c["metrics"]["revenue"] else "—"
        print(f"  {c['verdict_icon']} {c['name']}")
        m = c["metrics"]
        print(f"     расход {money(m['spend'])} · выручка {money(m['revenue'])} · "
              f"ДРР {drr} · заказов {m['orders']:.0f}")
        print(f"     CTR {pct(m['ctr'], 2)} · в корзину {pct(m['cr_cart'])} · "
              f"в заказ {pct(m['cr_order'])} · клик → заказ {pct(m['cr_click_order'], 2)}")
        if c.get("orders_available") and c.get("total_revenue"):
            print(f"     весь оборот {money(c['total_revenue'])} · ДРР общий "
                  f"{pct(c['total_drr'])} · без рекламы {pct(c['organic_share'], 0)}")
        for finding in c["findings"][: (None if args.all else 2)]:
            print(f"     • {finding['title']}")
            for action in finding["actions"][:1]:
                print(f"       → {action}")
        print()

    if not args.all:
        print("  Полный разбор с графиками: python3 run.py serve")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Аналитика рекламных кампаний Wildberries в динамике",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="мастер: провести от токена до дашборда")
    p_start.add_argument("--days", type=int, default=30, help="глубина сбора в днях")
    p_start.set_defaults(func=cmd_start)

    p_setup = sub.add_parser("setup", help="настроить токен без редактирования файлов")
    p_setup.set_defaults(func=cmd_setup)

    p_demo = sub.add_parser("demo", help="заполнить базу демо-данными")
    p_demo.add_argument("--days", type=int, default=60, help="сколько дней истории (по умолчанию 60)")
    p_demo.set_defaults(func=cmd_demo)

    p_collect = sub.add_parser("collect", help="забрать данные из кабинета WB")
    p_collect.add_argument("--days", type=int, default=30, help="глубина сбора в днях (по умолчанию 30)")
    p_collect.set_defaults(func=cmd_collect)

    p_serve = sub.add_parser("serve", help="открыть дашборд")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    p_serve.set_defaults(func=cmd_serve)

    p_report = sub.add_parser("report", help="краткий отчёт в консоли")
    p_report.add_argument("--days", type=int, default=7, help="период анализа (по умолчанию 7)")
    p_report.add_argument("--all", action="store_true", help="показать все кампании и все находки")
    p_report.set_defaults(func=cmd_report)

    p_check = sub.add_parser("check", help="проверить токен WB")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    try:
        if not args.command:
            # Без аргументов человеку нужен не список команд, а результат
            return cmd_start(argparse.Namespace(days=30))
        return args.func(args)
    except KeyboardInterrupt:
        print()
        _print("Остановлено.")
        return 0
    except Exception as exc:  # noqa: BLE001 — последний рубеж перед пользователем
        return _report_crash(exc)


def _report_crash(exc: BaseException) -> int:
    """Показывает понятное сообщение вместо простыни с ошибкой.

    Подробности сохраняются в файл: они нужны, только если придётся
    разбираться, и не должны пугать в самом окне.
    """
    import traceback

    log = ROOT / "ошибка.txt"
    try:
        log.write_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
        saved = f"Подробности сохранены в файл {log.name} рядом с программой."
    except OSError:
        saved = ""

    print()
    _print("Что-то пошло не так, и программа остановилась.")
    _print(f"Причина: {exc}")
    print()
    _print("Что можно попробовать:")
    _print("  1. Запустить программу ещё раз — часть сбоев разовые.")
    _print("  2. Посмотреть демо-кабинет: python run.py demo, затем python run.py serve")
    if saved:
        print()
        _print(saved)
        _print("Пришлите этот файл — по нему будет видно, в чём дело.")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
