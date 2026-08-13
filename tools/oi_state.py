#!/usr/bin/env python3
"""Классификатор состояния «открытый интерес × цена» и сценариев боковика.

Матрица из девяти состояний — модель, а не измерение. Она отвечает на вопрос
только вместе с тремя параметрами, которые обязан задать вызывающий:

* окно расчёта (за какой период считаем изменение);
* порог боковика (при каком изменении считаем, что величина не изменилась);
* фаза движения (начало или конец) для состояний 1/2 и 4.

Ни один из них не имеет значения по умолчанию, выведенного из рынка. Порог
боковика и пороги фазы — настраиваемые допущения; при их смене одно и то же
окно данных даёт другое состояние. Поэтому классификатор всегда возвращает
использованные пороги вместе с вердиктом.

Неизвестное не равно нулю. Если фаза не задана там, где она решает
направление сделки, состояние определяется, но сделка не формируется:
``tradable`` = False с причиной «фаза не задана».
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

UP: str = "рост"
DOWN: str = "падение"
FLAT: str = "боковик"

EARLY: str = "начало"
LATE: str = "конец"

# Пороги фазы по положению цены внутри диапазона окна. Это допущение, а не
# рыночная константа: они задают, что считать «началом» и «концом» движения,
# и оба конца шкалы намеренно оставляют неопределённую середину.
DEFAULT_EARLY_MAX_PCT: float = 33.0
DEFAULT_LATE_MIN_PCT: float = 67.0


class StateError(RuntimeError):
    pass


@dataclass
class Verdict:
    state: int
    oi: str
    price: str
    phase: str | None
    label: str
    action: str
    tradable: bool
    direction: str | None
    reason: str
    cautions: list[str] = field(default_factory=list)
    assumptions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "oi": self.oi,
            "price": self.price,
            "phase": self.phase,
            "label": self.label,
            "action": self.action,
            "tradable": self.tradable,
            "direction": self.direction,
            "reason": self.reason,
            "cautions": list(self.cautions),
            "assumptions": dict(self.assumptions),
        }


def direction_of(change_pct: float | None, flat_threshold_pct: float) -> str | None:
    """Направление величины с явной зоной боковика.

    ``None`` на входе — нет данных; возвращается ``None``, а не «боковик».
    Отсутствие замера и нулевое изменение — разные состояния.
    """
    if change_pct is None:
        return None
    if flat_threshold_pct < 0:
        raise StateError("порог боковика не может быть отрицательным")
    if abs(change_pct) <= flat_threshold_pct:
        return FLAT
    return UP if change_pct > 0 else DOWN


def phase_from_range_position(
    position_pct: float | None,
    *,
    early_max_pct: float = DEFAULT_EARLY_MAX_PCT,
    late_min_pct: float = DEFAULT_LATE_MIN_PCT,
) -> str | None:
    """Фаза движения по положению цены внутри диапазона окна, 0..100.

    Прокси, а не факт. Нужен потому, что «в начале движения» и «в конце
    движения» из исходной матрицы в момент входа не наблюдаются — постфактум
    любой исход подтверждает матрицу. Середина шкалы намеренно остаётся
    неопределённой: там фазы нет, и сделки по фазозависимым состояниям тоже.
    """
    if position_pct is None:
        return None
    if early_max_pct >= late_min_pct:
        raise StateError("early_max_pct должен быть строго меньше late_min_pct")
    if position_pct <= early_max_pct:
        return EARLY
    if position_pct >= late_min_pct:
        return LATE
    return None


_PHASE_DEPENDENT: frozenset[tuple[str, str]] = frozenset({(UP, UP), (DOWN, DOWN)})


def classify(
    oi_change_pct: float | None,
    price_change_pct: float | None,
    *,
    flat_threshold_pct: float,
    phase: str | None = None,
    window: str | None = None,
) -> Verdict:
    oi = direction_of(oi_change_pct, flat_threshold_pct)
    price = direction_of(price_change_pct, flat_threshold_pct)

    assumptions = {
        "window": window,
        "flat_threshold_pct": flat_threshold_pct,
        "oi_change_pct": oi_change_pct,
        "price_change_pct": price_change_pct,
    }

    if oi is None or price is None:
        absent = [n for n, v in (("ОИ", oi), ("цена", price)) if v is None]
        return Verdict(
            state=0,
            oi=oi or "нет данных",
            price=price or "нет данных",
            phase=phase,
            label="состояние не определено",
            action="не торговать",
            tradable=False,
            direction=None,
            reason=f"нет данных: {', '.join(absent)}",
            assumptions=assumptions,
        )

    if phase is not None and phase not in (EARLY, LATE):
        raise StateError(f"фаза {phase!r} не распознана; ожидается {EARLY!r} или {LATE!r}")

    if (oi, price) in _PHASE_DEPENDENT and phase is None:
        state = 1 if oi == UP else 4
        return Verdict(
            state=state,
            oi=oi,
            price=price,
            phase=None,
            label="ОИ и цена в одну сторону, фаза не определена",
            action="не торговать",
            tradable=False,
            direction=None,
            reason=(
                "фаза не задана: одно и то же сочетание в начале и в конце движения "
                "даёт противоположные решения"
            ),
            assumptions=assumptions,
        )

    return _resolve(oi, price, phase, assumptions)


def _resolve(oi: str, price: str, phase: str | None, assumptions: dict[str, Any]) -> Verdict:
    if oi == UP and price == UP:
        if phase == EARLY:
            return Verdict(
                1, oi, price, phase,
                "начало бычьего тренда",
                "открывать лонг",
                True, "long",
                "новые деньги входят вместе с ростом цены",
                assumptions=assumptions,
            )
        return Verdict(
            2, oi, price, phase,
            "конец бычьего тренда, ФОМО-свечи",
            "закрывать лонги",
            False, None,
            "рост на поздней фазе: вероятен разворот, но короткая позиция матрицей не выдаётся",
            cautions=["новый вход в лонг здесь запрещён стратегией"],
            assumptions=assumptions,
        )

    if oi == UP and price == DOWN:
        return Verdict(
            3, oi, price, phase,
            "агрессивный набор в падение",
            "не торговать",
            False, None,
            "стратегия не торгует этот сетап ни в лонг, ни в шорт",
            assumptions=assumptions,
        )

    if oi == DOWN and price == DOWN:
        if phase == EARLY:
            return Verdict(
                4, oi, price, phase,
                "подтверждение нисходящего тренда",
                "открывать шорт",
                True, "short",
                "слабость: цена падает, новые деньги не заходят",
                assumptions=assumptions,
            )
        return Verdict(
            4, oi, price, phase,
            "затяжное падение, вероятен отскок",
            "искать отскок",
            True, "long",
            "отскок, а не разворот: манипуляция для выбивания шортистов",
            cautions=[
                "контртрендовая сделка",
                "отскок не равен развороту — цель фиксируется, тренд остаётся нисходящим",
            ],
            assumptions=assumptions,
        )

    if oi == DOWN and price == UP:
        return Verdict(
            5, oi, price, phase,
            "слабый бычий тренд",
            "ждать разворота, готовить шорт",
            True, "short",
            "рост не подтверждён ростом ОИ",
            assumptions=assumptions,
        )

    if oi == FLAT and price == UP:
        return Verdict(
            6, oi, price, phase,
            "распределение",
            "ждать разворота, продавать",
            True, "short",
            "рост без подкрепления новыми деньгами, идёт перекладывание контрактов",
            cautions=["на хаях после заметного роста — вероятна коррекция"],
            assumptions=assumptions,
        )

    if oi == FLAT and price == DOWN:
        return Verdict(
            7, oi, price, phase,
            "накопление",
            "ждать отскока, покупать",
            True, "long",
            "падение иссякает, идёт перекладывание контрактов",
            cautions=["на низах после заметного падения — вероятен отскок"],
            assumptions=assumptions,
        )

    if oi == UP and price == FLAT:
        return Verdict(
            8, oi, price, phase,
            "накопление позиций в боковике",
            "смотреть контекст",
            False, None,
            "в боковик заходят и лонги, и шорты; направление выноса решает контекст",
            cautions=["нужен фандинг или соотношение лонг/шорт — см. range_scenario"],
            assumptions=assumptions,
        )

    return Verdict(
        9, oi, price, phase,
        "выход денег из инструмента",
        "не торговать",
        False, None,
        "позиции закрываются с обеих сторон, объёмы падают",
        assumptions=assumptions,
    )


# --------------------------------------------------------------------------
# сценарии боковика
# --------------------------------------------------------------------------


@dataclass
class ScenarioSignal:
    name: str
    expectation: str | None
    reason: str
    inputs: dict[str, str | None]

    @property
    def resolved(self) -> bool:
        return self.expectation is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expectation": self.expectation,
            "reason": self.reason,
            "inputs": dict(self.inputs),
        }


def _unresolved(name: str, inputs: dict[str, str | None]) -> ScenarioSignal | None:
    absent = [k for k, v in inputs.items() if v is None]
    if not absent:
        return None
    return ScenarioSignal(name, None, f"нет данных: {', '.join(absent)}", inputs)


def oi_delta_divergence(oi: str | None, futures_cvd: str | None) -> ScenarioSignal:
    """Расхождение ОИ и фьючерсной дельты в боковике."""
    inputs = {"oi": oi, "futures_cvd": futures_cvd}
    pending = _unresolved("дивергенция ОИ и дельты", inputs)
    if pending is not None:
        return pending
    if oi == UP and futures_cvd == DOWN:
        return ScenarioSignal(
            "дивергенция ОИ и дельты", DOWN,
            "позиции набираются под давлением продавца — пробой вниз после накопления",
            inputs,
        )
    if oi == DOWN and futures_cvd == UP:
        return ScenarioSignal(
            "дивергенция ОИ и дельты", UP,
            "позиции закрываются на растущей дельте — возможен сквиз вверх",
            inputs,
        )
    return ScenarioSignal(
        "дивергенция ОИ и дельты", None,
        "расхождения нет: ОИ и дельта сонаправлены или в боковике",
        inputs,
    )


def cvd_divergence(spot_cvd: str | None, futures_cvd: str | None) -> ScenarioSignal:
    """Расхождение спотовой и фьючерсной дельты."""
    inputs = {"spot_cvd": spot_cvd, "futures_cvd": futures_cvd}
    pending = _unresolved("расхождение дельт спот/фьючерс", inputs)
    if pending is not None:
        return pending
    if futures_cvd == UP and spot_cvd == DOWN:
        return ScenarioSignal(
            "расхождение дельт спот/фьючерс", DOWN,
            "покупка только на деривативах при слабом споте — ложный пробой вверх",
            inputs,
        )
    if futures_cvd == DOWN and spot_cvd == UP:
        return ScenarioSignal(
            "расхождение дельт спот/фьючерс", UP,
            "реальный спрос на споте против фьючерсной манипуляции — ложный пробой вниз",
            inputs,
        )
    return ScenarioSignal(
        "расхождение дельт спот/фьючерс", None,
        "дельты сонаправлены: расхождения нет",
        inputs,
    )


def funding_positioning(funding: str | None, long_short_accounts: str | None) -> ScenarioSignal:
    """Фандинг против соотношения лонг/шорт-аккаунтов."""
    inputs = {"funding": funding, "long_short_accounts": long_short_accounts}
    pending = _unresolved("фандинг и лонг/шорт", inputs)
    if pending is not None:
        return pending
    if funding == UP and long_short_accounts == DOWN:
        return ScenarioSignal(
            "фандинг и лонг/шорт", DOWN,
            "перекупленность: лонги закрываются при растущем фандинге",
            inputs,
        )
    if funding == DOWN and long_short_accounts == UP:
        return ScenarioSignal(
            "фандинг и лонг/шорт", UP,
            "перепроданность: вероятен шорт-сквиз",
            inputs,
        )
    return ScenarioSignal(
        "фандинг и лонг/шорт", None,
        "сочетание не описано стратегией",
        inputs,
    )


def confluence(signals: Sequence[ScenarioSignal]) -> dict[str, Any]:
    """Свести сценарии в один вывод, не пряча несогласие.

    Стратегия не задаёт приоритет между блоками, поэтому противоречие здесь не
    разрешается арифметикой голосов — оно называется.
    """
    resolved = [s for s in signals if s.resolved]
    pending = [s for s in signals if not s.resolved]
    directions = {s.expectation for s in resolved}
    if not resolved:
        verdict = None
        note = "ни один сценарий не разрешён"
    elif len(directions) == 1:
        verdict = directions.pop()
        note = f"согласие {len(resolved)} из {len(signals)}"
    else:
        verdict = None
        note = "сценарии противоречат друг другу; приоритет стратегией не задан"
    return {
        "expectation": verdict,
        "note": note,
        "resolved": [s.to_dict() for s in resolved],
        "pending": [s.to_dict() for s in pending],
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _optional_dir(raw: str | None) -> str | None:
    if raw is None:
        return None
    mapping = {"up": UP, "down": DOWN, "flat": FLAT}
    if raw not in mapping:
        raise StateError(f"направление {raw!r} не распознано; ожидается up, down или flat")
    return mapping[raw]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oi_state",
        description="классификация состояния ОИ/цена и сценариев боковика",
    )
    parser.add_argument("--oi-change", type=float, help="изменение ОИ за окно, %%")
    parser.add_argument("--price-change", type=float, help="изменение цены за окно, %%")
    parser.add_argument(
        "--flat-threshold",
        type=float,
        required=True,
        help="порог боковика в %%; обязателен — значения по умолчанию у него нет",
    )
    parser.add_argument("--window", help="окно расчёта, например 4h; попадает в вывод")
    parser.add_argument(
        "--range-position",
        type=float,
        help="положение цены в диапазоне окна, 0..100 — из него выводится фаза",
    )
    parser.add_argument("--phase", choices=[EARLY, LATE], help="фаза движения напрямую")
    parser.add_argument("--spot-cvd", choices=["up", "down", "flat"])
    parser.add_argument("--futures-cvd", choices=["up", "down", "flat"])
    parser.add_argument("--funding", choices=["up", "down", "flat"])
    parser.add_argument("--long-short", choices=["up", "down", "flat"])
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    return parser


def _render(verdict: Verdict, scenarios: dict[str, Any]) -> str:
    out = [
        f"Состояние: {verdict.state} — {verdict.label}",
        f"ОИ: {verdict.oi} · Цена: {verdict.price} · Фаза: {verdict.phase or 'не задана'}",
        f"Действие: {verdict.action}",
        f"Торгуемо: {'да' if verdict.tradable else 'нет'}"
        + (f" · Направление: {verdict.direction}" if verdict.direction else ""),
        f"Основание: {verdict.reason}",
    ]
    for caution in verdict.cautions:
        out.append(f"Осторожно: {caution}")
    out.append(
        "Допущения: окно "
        f"{verdict.assumptions.get('window') or 'не задано'}, "
        f"порог боковика {verdict.assumptions.get('flat_threshold_pct')}%"
    )
    if scenarios["resolved"] or scenarios["pending"]:
        out.append("")
        out.append(f"Сценарии боковика: {scenarios['note']}")
        for s in scenarios["resolved"]:
            out.append(f"  {s['name']}: ожидание {s['expectation']} — {s['reason']}")
        for s in scenarios["pending"]:
            out.append(f"  {s['name']}: {s['reason']}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        phase = args.phase or phase_from_range_position(args.range_position)
        verdict = classify(
            args.oi_change,
            args.price_change,
            flat_threshold_pct=args.flat_threshold,
            phase=phase,
            window=args.window,
        )
        oi_dir = direction_of(args.oi_change, args.flat_threshold)
        scenarios = confluence(
            [
                oi_delta_divergence(oi_dir, _optional_dir(args.futures_cvd)),
                cvd_divergence(_optional_dir(args.spot_cvd), _optional_dir(args.futures_cvd)),
                funding_positioning(_optional_dir(args.funding), _optional_dir(args.long_short)),
            ]
        )
    except StateError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"verdict": verdict.to_dict(), "scenarios": scenarios},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render(verdict, scenarios))
    return 0 if verdict.tradable else 1


if __name__ == "__main__":
    raise SystemExit(main())
