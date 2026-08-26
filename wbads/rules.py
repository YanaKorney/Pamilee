"""Движок диагностики: находит проблемные кампании и говорит, что делать.

Каждое правило — небольшая функция, которая смотрит на кампанию в динамике
(текущий период против предыдущего + ряд по дням) и либо молчит, либо
возвращает находку: что происходит, с цифрами, и конкретные действия.

Добавить своё правило = написать функцию и повесить на неё @rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .config import Thresholds
from .metrics import (
    compare,
    days_with_activity,
    derive,
    trailing_zero_days,
    trend_slope,
)

CRITICAL = "critical"
WARNING = "warning"
OPPORTUNITY = "opportunity"
INFO = "info"

SEVERITY_WEIGHT = {CRITICAL: 35, WARNING: 15, OPPORTUNITY: 0, INFO: 3}
SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, OPPORTUNITY: 2, INFO: 3}

# Статусы кампаний в API WB
STATUS_ACTIVE = 9
STATUS_PAUSED = 11
STATUS_READY = 4
STATUS_FINISHED = 7

STATUS_NAMES = {
    -1: "Удаляется",
    4: "Готова к запуску",
    7: "Завершена",
    8: "Отказался",
    9: "Идут показы",
    11: "На паузе",
}

TYPE_NAMES = {
    4: "Каталог",
    5: "Карточка товара",
    6: "Поиск",
    7: "Главная страница",
    8: "Автоматическая",
    9: "Аукцион",
}


# ── форматирование чисел для человекочитаемых текстов ─────────────────────

def money(value: float) -> str:
    """1234.5 → «1 235 ₽» (разряды пробелом, как в кабинете)."""
    return f"{round(value):,.0f}".replace(",", " ") + " ₽"


def num(value: float) -> str:
    return f"{round(value):,.0f}".replace(",", " ")


def pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%".replace(".", ",")


def signed_pct(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value):.0f}%"


def rub(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",") + " ₽"


# ── структуры ─────────────────────────────────────────────────────────────

@dataclass
class Finding:
    """Одна найденная проблема или возможность."""

    code: str
    severity: str
    title: str
    why: str
    actions: list[str]
    # Порядок внутри одной степени критичности: меньше — показываем выше.
    # Первопричину («баланс кончился») нужно видеть раньше следствия («ДРР вырос»).
    priority: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "why": self.why,
            "actions": self.actions,
            "priority": self.priority,
        }


@dataclass
class Context:
    """Всё, что нужно правилу, чтобы вынести суждение."""

    campaign: Mapping[str, Any]
    series: Sequence[Mapping[str, Any]]
    current: Mapping[str, float]
    previous: Mapping[str, float]
    thresholds: Thresholds
    nm_items: Sequence[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cur = derive(self.current)
        self.prev = derive(self.previous)
        self.cmp = compare(self.current, self.previous)

    def delta_pct(self, key: str) -> float | None:
        return self.cmp.get(key, {}).get("delta_pct")

    @property
    def status(self) -> int:
        return int(self.campaign.get("status") or 0)

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def days(self) -> int:
        return len(self.series)

    @property
    def significant(self) -> bool:
        """Достаточно ли открутки, чтобы делать выводы."""
        return self.cur["spend"] >= self.thresholds.min_spend


RULES: list[Callable[[Context], Finding | None]] = []


def rule(func: Callable[[Context], Finding | None]) -> Callable:
    RULES.append(func)
    return func


# ── правила ───────────────────────────────────────────────────────────────

@rule
def spend_without_orders(ctx: Context) -> Finding | None:
    """Деньги уходят, заказов нет — самая дорогая из проблем."""
    t = ctx.thresholds
    if ctx.cur["orders"] > 0 or ctx.cur["spend"] < t.spend_no_orders:
        return None
    clicks = ctx.cur["clicks"]
    actions = [
        "Остановите кампанию сегодня — каждый следующий день это чистый минус.",
        "Проверьте остатки: если товара нет на складах покупателя, показы идут, а заказов не будет.",
        "Сравните свою цену с первой страницей выдачи по вашему запросу. Дороже на 15%+ — реклама это не вытянет.",
        "Проверьте карточку: главное фото, рейтинг, количество отзывов, сроки доставки.",
    ]
    if clicks < 30:
        actions.insert(
            1,
            f"Кликов всего {num(clicks)} — выборка крошечная. Возможно, дело не в товаре, "
            "а в том, что реклама почти не откручивается: проверьте ставку и охват.",
        )
    return Finding(
        code="spend_without_orders",
        priority=10,
        severity=CRITICAL,
        title="Расход без единого заказа",
        why=(
            f"За период кампания потратила {money(ctx.cur['spend'])} и не принесла "
            f"ни одного заказа. Показов {num(ctx.cur['views'])}, кликов {num(clicks)}, "
            f"добавлений в корзину {num(ctx.cur['atbs'])}."
        ),
        actions=actions,
    )


@rule
def drr_critical(ctx: Context) -> Finding | None:
    """ДРР сильно выше цели — кампания съедает маржу."""
    t = ctx.thresholds
    limit = t.target_drr * t.drr_critical_multiplier
    if not ctx.significant or ctx.cur["orders"] == 0 or ctx.cur["drr"] < limit:
        return None
    drr = ctx.cur["drr"]
    overspend = ctx.cur["spend"] - ctx.cur["revenue"] * t.target_drr / 100
    return Finding(
        code="drr_critical",
        priority=30,
        severity=CRITICAL,
        title=f"ДРР {pct(drr)} при цели {pct(t.target_drr, 0)}",
        why=(
            f"На каждые 100 ₽ выручки с рекламы уходит {drr:.0f} ₽ рекламного бюджета. "
            f"Расход {money(ctx.cur['spend'])}, выручка {money(ctx.cur['revenue'])}. "
            f"Переплата к цели — {money(max(0, overspend))} за период."
        ),
        actions=[
            "Снизьте ставку на 20–30% и посмотрите 2–3 дня: часто объём почти не падает, а ДРР приходит в норму.",
            "Отключите внутри кампании артикулы с худшим ДРР — обычно 20% товаров съедают половину бюджета.",
            f"Целевой CPO при вашей цели: {money(ctx.cur['aov'] * t.target_drr / 100)} "
            f"(сейчас {money(ctx.cur['cpo'])}).",
            "Если снизить ДРР не удаётся — считайте, окупает ли кампания себя вообще, и ставьте на паузу.",
        ],
    )


@rule
def drr_high(ctx: Context) -> Finding | None:
    """ДРР выше цели, но пока не критично."""
    t = ctx.thresholds
    limit = t.target_drr * t.drr_critical_multiplier
    if not ctx.significant or ctx.cur["orders"] == 0:
        return None
    if not (t.target_drr < ctx.cur["drr"] < limit):
        return None
    return Finding(
        code="drr_high",
        priority=55,
        severity=WARNING,
        title=f"ДРР {pct(ctx.cur['drr'])} — выше цели {pct(t.target_drr, 0)}",
        why=(
            f"Расход {money(ctx.cur['spend'])} против выручки {money(ctx.cur['revenue'])}. "
            f"Пока терпимо, но запас по марже уже съеден."
        ),
        actions=[
            "Снизьте ставку на 10–15% — это обычно возвращает ДРР к цели без потери заказов.",
            "Посмотрите разбивку по артикулам ниже: отключите худшие, оставьте те, что дают ДРР в норме.",
            "Проверьте, не выросла ли цена клика: если да — это аукцион, а не ваша карточка.",
        ],
    )


@rule
def drr_worsening(ctx: Context) -> Finding | None:
    """ДРР ухудшается неделя к неделе."""
    t = ctx.thresholds
    change = ctx.delta_pct("drr")
    if not ctx.significant or change is None or ctx.cur["orders"] == 0:
        return None
    if change < 20 or ctx.cur["drr"] <= t.target_drr:
        return None
    return Finding(
        code="drr_worsening",
        priority=60,
        severity=WARNING,
        title=f"ДРР растёт: {pct(ctx.prev['drr'])} → {pct(ctx.cur['drr'])}",
        why=(
            f"За период ДРР ухудшился на {signed_pct(change)}. Тренд идёт не в вашу "
            "сторону: без вмешательства через неделю будет хуже."
        ),
        actions=[
            "Найдите причину по цепочке: подорожал клик (CPC) → это аукцион; упала конверсия → это карточка или цена.",
            "Не ждите конца месяца — правьте ставку сейчас, пока переплата не накопилась.",
        ],
    )


@rule
def cpc_spike(ctx: Context) -> Finding | None:
    """Клик резко подорожал — обычно конкуренты подняли ставки."""
    t = ctx.thresholds
    change = ctx.delta_pct("cpc")
    if not ctx.significant or change is None or change < t.cpc_growth:
        return None
    if ctx.cur["clicks"] < 20:
        return None
    return Finding(
        code="cpc_spike",
        priority=40,
        severity=WARNING,
        title=f"Клик подорожал на {signed_pct(change)}",
        why=(
            f"CPC вырос с {rub(ctx.prev['cpc'])} до {rub(ctx.cur['cpc'])}. "
            "Так выглядит разогретый аукцион: конкуренты подняли ставки, "
            "и вы платите больше за тот же трафик."
        ),
        actions=[
            "Проверьте ставку в кабинете и посмотрите, какое место в выдаче она даёт сейчас.",
            "Не догоняйте аукцион вслепую: поднимать ставку имеет смысл, только если ДРР остаётся в цели.",
            "Разнесите бюджет — часть в автокампанию, часть в аукцион: цена клика там обычно разная.",
            "Если это сезонный всплеск (распродажа, праздники) — переждите 3–5 дней на сниженной ставке.",
        ],
    )


@rule
def ctr_drop(ctx: Context) -> Finding | None:
    """CTR просел — карточка перестала цеплять в выдаче."""
    t = ctx.thresholds
    change = ctx.delta_pct("ctr")
    if not ctx.significant or change is None or change > -t.ctr_drop:
        return None
    if ctx.cur["views"] < 1000:
        return None
    return Finding(
        code="ctr_drop",
        priority=40,
        severity=WARNING,
        title=f"CTR упал на {signed_pct(change)}",
        why=(
            f"Кликабельность снизилась с {pct(ctx.prev['ctr'], 2)} до {pct(ctx.cur['ctr'], 2)}. "
            "Показы идут, но по карточке перестали кликать — либо изменилось окружение "
            "в выдаче, либо что-то поменялось в самой карточке."
        ),
        actions=[
            "Откройте выдачу по своему запросу глазами покупателя: как ваше главное фото смотрится рядом с соседями?",
            "Проверьте, не менялись ли за это время фото, цена или заголовок.",
            "Проверьте рейтинг и свежие отзывы — плохой отзыв на витрине бьёт по CTR сразу.",
            "Протестируйте новое главное фото: это самый быстрый рычаг для CTR.",
        ],
    )


@rule
def ctr_low(ctx: Context) -> Finding | None:
    """CTR ниже минимально приемлемого."""
    t = ctx.thresholds
    if not ctx.significant or ctx.cur["views"] < 2000:
        return None
    if ctx.cur["ctr"] >= t.ctr_floor:
        return None
    return Finding(
        code="ctr_low",
        priority=42,
        severity=WARNING,
        title=f"Низкий CTR — {pct(ctx.cur['ctr'], 2)}",
        why=(
            f"На {num(ctx.cur['views'])} показов всего {num(ctx.cur['clicks'])} кликов. "
            f"Вы платите за показы, которые не превращаются в переходы."
        ),
        actions=[
            "Главное фото — рычаг №1: крупный товар, читаемый на маленьком экране, понятная выгода.",
            "Проверьте, по тем ли запросам вас показывают: в автокампании отминусуйте нерелевантные кластеры.",
            "Цена в выдаче видна сразу: если вы заметно дороже соседей, кликать не будут.",
        ],
    )


@rule
def cr_cart_low(ctx: Context) -> Finding | None:
    """Кликают, но не кладут в корзину — вопрос к карточке."""
    t = ctx.thresholds
    if ctx.cur["clicks"] < 50 or ctx.cur["cr_cart"] >= t.cr_cart_floor:
        return None
    return Finding(
        code="cr_cart_low",
        priority=45,
        severity=WARNING,
        title=f"Клики не доходят до корзины — CR {pct(ctx.cur['cr_cart'])}",
        why=(
            f"Из {num(ctx.cur['clicks'])} переходов в корзину положили только "
            f"{num(ctx.cur['atbs'])}. Реклама свою работу сделала — привела людей. "
            "Дальше их теряет карточка."
        ),
        actions=[
            "Это не проблема ставки — поднимать бюджет здесь бессмысленно, дороже станет каждый заказ.",
            "Проверьте вторые-третьи фото и инфографику: закрывают ли они главные возражения?",
            "Опишите размеры, состав, комплектацию — то, из-за чего люди уходят «подумать».",
            "Посмотрите отзывы и вопросы: если один и тот же минус повторяется — вынесите ответ на него в фото.",
        ],
    )


@rule
def cr_order_low(ctx: Context) -> Finding | None:
    """В корзину кладут, а заказывать не идут — вопрос к цене и доверию."""
    t = ctx.thresholds
    if ctx.cur["atbs"] < 20 or ctx.cur["cr_order"] >= t.cr_order_floor:
        return None
    return Finding(
        code="cr_order_low",
        priority=45,
        severity=WARNING,
        title=f"Корзина не превращается в заказ — CR {pct(ctx.cur['cr_order'])}",
        why=(
            f"В корзину положили {num(ctx.cur['atbs'])} раз, заказов — {num(ctx.cur['orders'])}. "
            "Товар нравится, но в момент оформления что-то останавливает."
        ),
        actions=[
            "Цена: покупатели откладывают в корзину и ждут скидку. Проверьте, как вы смотритесь против соседей по выдаче.",
            "Срок доставки: если товара нет на ближнем складе, срок растёт и корзину бросают. Проверьте распределение остатков по складам.",
            "Рейтинг и отзывы — второй по силе стоп-фактор на этом шаге.",
            "Проверьте наличие нужных размеров/цветов: часто в корзине лежит то, чего уже нет.",
        ],
    )


@rule
def views_collapse(ctx: Context) -> Finding | None:
    """Показы обвалились — кампания перестала выкупать трафик."""
    t = ctx.thresholds
    change = ctx.delta_pct("views")
    if change is None or change > -t.views_drop:
        return None
    if ctx.prev["views"] < 1000:
        return None
    return Finding(
        code="views_collapse",
        priority=20,
        severity=WARNING,
        title=f"Показы упали на {signed_pct(change)}",
        why=(
            f"Было {num(ctx.prev['views'])} показов, стало {num(ctx.cur['views'])}. "
            "Кампания проигрывает аукцион или упирается в ограничение — оборот вы теряете молча."
        ),
        actions=[
            "Проверьте баланс кабинета и дневной бюджет кампании — самая частая причина.",
            "Проверьте ставку: если конкуренты подняли свои, ваша перестала проходить в показ.",
            "Проверьте, не закончился ли товар и не ушла ли карточка в архив — без остатка показы отключаются.",
        ],
    )


@rule
def no_impressions(ctx: Context) -> Finding | None:
    """Кампания активна, но показов нет несколько дней подряд."""
    t = ctx.thresholds
    if not ctx.is_active:
        return None
    silent = trailing_zero_days(ctx.series, "views")
    if silent < t.silent_days:
        return None
    return Finding(
        code="no_impressions",
        priority=15,
        severity=CRITICAL if silent >= 4 else WARNING,
        title=f"Активна, но {num(silent)} дн. без показов",
        why=(
            f"Статус кампании — «идут показы», однако последние {num(silent)} дней "
            "показов нет вообще. Кампания числится рабочей, а трафика не даёт."
        ),
        actions=[
            "Проверьте баланс рекламного кабинета — при нуле показы останавливаются, а статус остаётся прежним.",
            "Проверьте дневной бюджет: если он исчерпан, кампания молчит до следующих суток.",
            "Проверьте остатки товара: карточка без остатка из рекламы выпадает.",
            "Проверьте ставку — на слишком низкой кампания просто не проходит в аукцион.",
        ],
    )


@rule
def budget_capped(ctx: Context) -> Finding | None:
    """Кампания каждый день упирается в дневной бюджет."""
    t = ctx.thresholds
    budget = float(ctx.campaign.get("daily_budget") or 0)
    if budget <= 0 or not ctx.is_active:
        return None
    capped = [p for p in ctx.series if float(p["spend"]) >= budget * t.budget_hit_ratio]
    if len(capped) < max(3, ctx.days // 2):
        return None
    if ctx.cur["drr"] > t.target_drr or ctx.cur["orders"] == 0:
        return None  # упирается в лимит, но и так дорого — расширять нечего
    return Finding(
        code="budget_capped",
        priority=30,
        severity=OPPORTUNITY,
        title=f"Упирается в дневной бюджет {num(len(capped))} дн. из {num(ctx.days)}",
        why=(
            f"Дневной лимит {money(budget)} выбирается почти полностью, при этом ДРР "
            f"{pct(ctx.cur['drr'])} — в пределах цели. Кампания зарабатывает, но её держит потолок."
        ),
        actions=[
            f"Поднимите дневной бюджет на 30–50% (до {money(budget * 1.4)}) и следите за ДРР 3 дня.",
            "Если после расширения ДРР останется в цели — повторите шаг ещё раз.",
            "Заранее пополните баланс кабинета, иначе расширение упрётся уже в него.",
        ],
    )


@rule
def spend_up_orders_flat(ctx: Context) -> Finding | None:
    """Расход вырос, заказы — нет."""
    t = ctx.thresholds
    spend_change = ctx.delta_pct("spend")
    orders_change = ctx.delta_pct("orders")
    if not ctx.significant or spend_change is None or spend_change < t.spend_growth:
        return None
    if orders_change is not None and orders_change > 5:
        return None
    return Finding(
        code="spend_up_orders_flat",
        priority=50,
        severity=WARNING,
        title=f"Расход {signed_pct(spend_change)}, заказы {signed_pct(orders_change)}",
        why=(
            f"Бюджет вырос с {money(ctx.prev['spend'])} до {money(ctx.cur['spend'])}, "
            f"а заказов как было {num(ctx.prev['orders'])}, так и осталось {num(ctx.cur['orders'])}. "
            "Дополнительные деньги ушли в никуда."
        ),
        actions=[
            "Верните ставку и бюджет к прежним значениям — прошлая неделя работала лучше.",
            "Если расход рос сам (аукцион разогрелся) — ставьте ограничение дневного бюджета.",
            "Проверьте, не начали ли показываться по нерелевантным запросам: в автокампании — минусуйте кластеры.",
        ],
    )


@rule
def cpo_growth(ctx: Context) -> Finding | None:
    """Заказ дорожает."""
    change = ctx.delta_pct("cpo")
    if not ctx.significant or change is None or change < 30:
        return None
    if ctx.cur["orders"] < 3 or ctx.prev["orders"] < 3:
        return None
    return Finding(
        code="cpo_growth",
        priority=65,
        severity=WARNING,
        title=f"Заказ подорожал на {signed_pct(change)}",
        why=(
            f"Цена заказа выросла с {money(ctx.prev['cpo'])} до {money(ctx.cur['cpo'])}. "
            f"При среднем чеке {money(ctx.cur['aov'])} это {pct(ctx.cur['drr'])} ДРР."
        ),
        actions=[
            "Сравните: подорожал клик или упала конверсия? Первое лечится ставкой, второе — карточкой.",
            "Отключите артикулы, у которых CPO выше среднего по кампании.",
        ],
    )


@rule
def zombie_campaign(ctx: Context) -> Finding | None:
    """Активна, но почти не тратит — висит и создаёт иллюзию работы."""
    if not ctx.is_active or ctx.cur["spend"] >= 50 or ctx.cur["views"] == 0:
        return None
    return Finding(
        code="zombie_campaign",
        priority=70,
        severity=INFO,
        title="Активна, но почти не откручивается",
        why=(
            f"За период потрачено всего {money(ctx.cur['spend'])} при {num(ctx.cur['views'])} показах. "
            "Кампания числится рабочей, но объёма не даёт."
        ),
        actions=[
            "Поднимите ставку до конкурентного уровня — или закройте кампанию, чтобы не мешала в списке.",
            "Проверьте, не сузили ли вы кампанию до одного-двух артикулов без остатков.",
        ],
    )


@rule
def scale_up(ctx: Context) -> Finding | None:
    """Кампания работает лучше цели — есть куда расти."""
    t = ctx.thresholds
    if not ctx.significant or ctx.cur["orders"] < 3:
        return None
    if ctx.cur["drr"] > t.target_drr * t.scale_up_ratio or ctx.cur["drr"] <= 0:
        return None
    spend_trend = trend_slope([float(p["spend"]) for p in ctx.series])
    return Finding(
        code="scale_up",
        priority=25,
        severity=OPPORTUNITY,
        title=f"Работает лучше цели — ДРР {pct(ctx.cur['drr'])}",
        why=(
            f"При цели {pct(t.target_drr, 0)} кампания держит {pct(ctx.cur['drr'])}: "
            f"{money(ctx.cur['spend'])} расхода принесли {money(ctx.cur['revenue'])} "
            f"({ctx.cur['roas']:.1f} ₽ на каждый вложенный рубль)."
            + ("" if spend_trend > 0 else " При этом расход не растёт — потенциал не выбран.")
        ),
        actions=[
            "Поднимите ставку и дневной бюджет на 30%, следите за ДРР три дня.",
            f"Запас до цели есть: можно дойти до {money(ctx.cur['revenue'] * t.target_drr / 100)} "
            "расхода на текущей выручке.",
            "Перед расширением проверьте остатки — иначе разгоните спрос и уйдёте в out-of-stock.",
        ],
    )


@rule
def not_enough_data(ctx: Context) -> Finding | None:
    """Мало открутки — выводы делать рано."""
    t = ctx.thresholds
    if ctx.cur["spend"] >= t.min_spend:
        return None
    if ctx.cur["spend"] <= 0:
        return None
    return Finding(
        code="not_enough_data",
        priority=90,
        severity=INFO,
        title="Мало данных для выводов",
        why=(
            f"Расход за период — {money(ctx.cur['spend'])}, активных дней "
            f"{num(days_with_activity(ctx.series))}. Такой выборке доверять рано."
        ),
        actions=[
            "Дайте кампании накопить статистику или расширьте период анализа.",
        ],
    )


# ── связывание причины и следствия ────────────────────────────────────────

# Высокий ДРР — почти всегда следствие. Настоящая причина лежит в воронке:
# подорожал клик, упал CTR, не доходят до корзины, не выкупают. Если причина
# найдена отдельным правилом, ДРР-находка должна вести к ней, а не советовать
# «снизьте ставку» там, где ставка ни при чём.
ROOT_CAUSE_HINTS = {
    "no_impressions": (
        "Сначала причина: кампания стоит без показов. Пока она молчит, "
        "ДРР считается по остаткам старой открутки — сначала верните показы."
    ),
    "cr_cart_low": (
        "Ставку здесь трогать бессмысленно: до корзины не доходят клики, которые вы уже "
        "оплатили. Причина в карточке — с неё и начните (см. находку про корзину ниже)."
    ),
    "cr_order_low": (
        "Ставка тут ни при чём: товар кладут в корзину и не выкупают. "
        "Смотрите цену, сроки доставки и отзывы (см. находку про корзину ниже)."
    ),
    "ctr_drop": (
        "Причина — упавший CTR: вы платите за показы, по которым перестали кликать. "
        "Начните с главного фото, а не со ставки."
    ),
    "cpc_spike": (
        "Причина — подорожавший клик: аукцион разогрелся. "
        "Снижайте ставку осознанно и следите, сколько объёма теряете."
    ),
}

# Порядок важен: если причин несколько, ведём к самой ранней в воронке.
ROOT_CAUSE_ORDER = ("no_impressions", "ctr_drop", "cr_cart_low", "cr_order_low", "cpc_spike")


def link_root_cause(findings: list[Finding]) -> None:
    """Дописывает ДРР-находке подсказку о настоящей причине."""
    codes = {f.code for f in findings}
    cause = next((c for c in ROOT_CAUSE_ORDER if c in codes), None)
    if not cause:
        return
    for finding in findings:
        if finding.code in ("drr_critical", "drr_high", "drr_worsening"):
            finding.actions.insert(0, ROOT_CAUSE_HINTS[cause])


# ── сборка диагноза ───────────────────────────────────────────────────────

STATUS_LABELS = {
    CRITICAL: ("🔴", "Требует действий"),
    WARNING: ("🟡", "Под наблюдением"),
    OPPORTUNITY: ("🚀", "Можно масштабировать"),
    INFO: ("⚪️", "Мало данных"),
    "ok": ("🟢", "Норма"),
    "idle": ("⏸", "Без открутки"),
}


def health_score(findings: Sequence[Finding]) -> int:
    """0–100. Чем ниже, тем громче кампания просит внимания."""
    score = 100
    for f in findings:
        score -= SEVERITY_WEIGHT.get(f.severity, 0)
    return max(0, min(100, score))


def overall_status(findings: Sequence[Finding]) -> str:
    for severity in (CRITICAL, WARNING, OPPORTUNITY, INFO):
        if any(f.severity == severity for f in findings):
            return severity
    return "ok"


def diagnose(ctx: Context) -> dict[str, Any]:
    """Прогоняет кампанию через все правила и собирает вердикт."""
    findings: list[Finding] = []
    for check in RULES:
        try:
            found = check(ctx)
        except Exception as exc:  # одно сломанное правило не должно ронять отчёт
            found = Finding(
                code=f"rule_error:{check.__name__}",
                severity=INFO,
                title="Правило не отработало",
                why=f"{check.__name__}: {exc}",
                actions=[],
            )
        if found:
            findings.append(found)

    link_root_cause(findings)
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.priority))

    # Кампания вообще не откручивалась за период — это не «норма»,
    # но и не проблема: просто нечего анализировать.
    idle = ctx.cur["spend"] == 0 and ctx.cur["views"] == 0
    status = "idle" if idle else overall_status(findings)
    icon, label = STATUS_LABELS[status]

    return {
        "advert_id": int(ctx.campaign.get("advert_id") or 0),
        "name": ctx.campaign.get("name") or f"Кампания {ctx.campaign.get('advert_id')}",
        "type_name": ctx.campaign.get("type_name") or TYPE_NAMES.get(
            int(ctx.campaign.get("type") or 0), "—"),
        "status": ctx.status,
        "status_name": ctx.campaign.get("status_name") or STATUS_NAMES.get(ctx.status, "—"),
        "daily_budget": float(ctx.campaign.get("daily_budget") or 0),
        "health": health_score(findings),
        "verdict": status,
        "verdict_icon": icon,
        "verdict_label": label,
        "findings": [f.to_dict() for f in findings],
        "critical_count": sum(1 for f in findings if f.severity == CRITICAL),
        "warning_count": sum(1 for f in findings if f.severity == WARNING),
        "metrics": ctx.cur,
        "previous": ctx.prev,
        "compare": ctx.cmp,
        "series": list(ctx.series),
        "nm_items": list(ctx.nm_items),
    }
