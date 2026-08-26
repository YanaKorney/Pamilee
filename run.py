#!/usr/bin/env python3
"""Единая точка входа сервиса аналитики рекламы Wildberries.

    python3 run.py demo       — заполнить базу демо-данными (без токена)
    python3 run.py collect    — забрать данные из вашего кабинета WB
    python3 run.py serve      — открыть дашборд в браузере
    python3 run.py report     — краткий отчёт прямо в консоли
    python3 run.py check      — проверить, что токен работает
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from wbads import analytics, collector, db, demo
from wbads.api import serve
from wbads.config import load_config
from wbads.rules import money, pct, signed_pct
from wbads.wb_client import WBAdvertClient, WBError


def _print(message: str) -> None:
    print(f"  {message}", flush=True)


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
    _print("Дальше: python3 run.py serve")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.has_token:
        _print("Токен не найден. Впишите WB_API_TOKEN в файл .env.")
        return 1
    try:
        client = WBAdvertClient(cfg.token)
        balance = client.balance()
        ids = client.campaign_ids()
    except WBError as exc:
        _print(f"Токен не работает: {exc}")
        return 1
    _print("Токен работает.")
    _print(f"Баланс кабинета: {money(balance['net'])} "
           f"(счёт {money(balance['balance'])}, бонусы {money(balance['bonus'])})")
    _print(f"Кампаний в кабинете: {len(ids)}")
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
        print(f"     расход {money(c['metrics']['spend'])} · выручка "
              f"{money(c['metrics']['revenue'])} · ДРР {drr} · "
              f"заказов {c['metrics']['orders']:.0f}")
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
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
