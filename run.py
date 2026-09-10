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

import argparse
import getpass
import sys
from datetime import date, datetime, timedelta

from wbads import analytics, collector, db, demo
from wbads.api import serve
from wbads.config import (
    clear_template_token,
    load_config,
    token_from_template,
    token_in_template,
    write_token,
)
from wbads.rules import money, pct, signed_pct
from wbads.wb_client import WBAdvertClient, WBError, WBStatisticsClient


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
    """Спрашивает токен. Ввод скрыт, поэтому предупреждаем заранее."""
    print()
    _print("Откройте кабинет WB → Настройки → Доступ к API → Создать новый токен.")
    _print("Отметьте категории «Продвижение» и «Статистика», скопируйте токен.")
    print()
    _print("Сейчас вставьте его сюда. Символы на экране НЕ появятся —")
    _print("так и должно быть. Вставьте и нажмите Enter.")
    print()
    try:
        return getpass.getpass("  Токен: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
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

    if cfg.has_token:
        _print("Токен уже настроен.")
        if not _ask("Заменить его на другой?", default="н"):
            print()
            return cmd_check(args, quiet_tail)

    token = _ask_token()
    if not token:
        print()
        _print("Токен не введён. Ничего не изменила.")
        _print("Посмотреть сервис без токена можно так: python3 run.py demo")
        return 1

    path = write_token(token)
    clear_template_token()
    print()
    _print(f"Токен сохранён в {path.name}. Этот файл никуда не отправляется.")
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
            _print("Не вышло. Покажу демо-кабинет, чтобы вы не ждали.")
            return _start_demo(cfg)
        cfg = load_config()
    else:
        _print("Шаг 1 из 3. Токен на месте.")
    print()

    # ── шаг 2: данные ────────────────────────────────────────────────────
    _print("Шаг 2 из 3. Данные.")
    with db.session(cfg.db_path) as conn:
        last, _ = db.data_range(conn)
        fresh = _data_is_fresh(conn)

    if not last:
        _print("Данных ещё нет — заберём их из кабинета.")
        _print("Первый сбор идёт долго: WB отдаёт статистику раз в минуту.")
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
                                       on_progress=_print)
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

    client = WBAdvertClient(cfg.token)
    _print("Проверяем доступ к API продвижения (advert-api.wildberries.ru)…")
    print()

    failures: list[str] = []
    ids: list[int] = []

    def probe(label: str, method: str, call) -> object:
        nonlocal failures
        try:
            result = call()
        except WBError as exc:
            print(f"  ✗ {label:<26} {method}")
            print(f"    {exc}")
            failures.append(label)
            return None
        print(f"  ✓ {label:<26} {method}")
        return result

    balance = probe("Баланс кабинета", "GET  /adv/v1/balance", client.balance)
    found = probe("Список кампаний", "GET  /adv/v1/promotion/count", client.campaign_ids)
    if found:
        ids = found
        probe("Карточки кампаний", "POST /adv/v1/promotion/adverts",
              lambda: client.campaign_details(ids[:1]))
        probe("Статистика по дням", "POST /adv/v2/fullstats",
              lambda: client.fullstats(ids[:1], date.today().isoformat(),
                                       date.today().isoformat()))

    # Категория «Статистика» отдельная: без неё реклама считается,
    # а общий ДРР — нет. Поэтому её отказ не валит проверку целиком.
    print()
    _print("Проверяем доступ к заказам (нужен для общего ДРР)…")
    print()
    stats_client = WBStatisticsClient(cfg.token)
    orders_ok = probe("Заказы кабинета", "GET  /api/v1/supplier/orders",
                      lambda: stats_client.orders(date.today().isoformat(), max_pages=1))
    if orders_ok is None:
        failures.remove("Заказы кабинета")
    print()

    if failures:
        _print(f"Закрыто методов рекламы: {len(failures)}.")
        _print("Проверьте в кабинете, что у токена отмечена категория «Продвижение».")
        _print("Если она есть, а доступа нет — выпустите токен без галочки")
        _print("«Только на чтение»: статистика запрашивается методом POST.")
        return 1

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
    if not args.command:
        # Без аргументов человеку нужен не список команд, а результат
        return cmd_start(argparse.Namespace(days=30))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
