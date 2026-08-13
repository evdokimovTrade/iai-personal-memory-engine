#!/usr/bin/env python3
"""Риск-менеджмент: размер позиции, R:R, сопровождение сделки.

Всё считается от риска, а не от желаемого объёма: объём — производная от
расстояния до стопа и допустимого процента счёта, а не от того, сколько
хочется взять.

Пороги сопровождения (перевод в безубыток, частичная фиксация, потолок
доборов) — параметры, а не константы рынка. Значения по умолчанию помечены
как предложение и подлежат подтверждению.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

LONG: str = "long"
SHORT: str = "short"

# Предложение, не измеренная величина. Подтвердить или заменить.
DEFAULT_MIN_RR: float = 2.0
DEFAULT_BREAKEVEN_AT_R: float = 1.0
DEFAULT_PARTIALS: tuple[tuple[float, float], ...] = ((1.0, 0.33), (2.0, 0.33))


class RiskError(RuntimeError):
    pass


def _validate_sides(direction: str, entry: float, stop: float) -> float:
    if direction not in (LONG, SHORT):
        raise RiskError(f"направление {direction!r} не распознано")
    risk_per_unit = entry - stop if direction == LONG else stop - entry
    if risk_per_unit <= 0:
        side = "ниже" if direction == LONG else "выше"
        raise RiskError(
            f"стоп {stop:g} должен быть {side} входа {entry:g} для {direction}; "
            f"расстояние до стопа {risk_per_unit:g}"
        )
    return risk_per_unit


def position_size(
    equity: float, risk_pct: float, entry: float, stop: float, direction: str
) -> dict[str, float]:
    """Объём позиции из допустимого риска и расстояния до стопа."""
    if equity <= 0:
        raise RiskError("эквити счёта должно быть положительным")
    if risk_pct <= 0:
        raise RiskError("риск в % счёта должен быть положительным")
    risk_per_unit = _validate_sides(direction, entry, stop)
    risk_abs = equity * risk_pct / 100.0
    size = risk_abs / risk_per_unit
    return {
        "size": size,
        "risk_abs": risk_abs,
        "risk_per_unit": risk_per_unit,
        "notional": size * entry,
    }


def rr_ratio(direction: str, entry: float, stop: float, target: float) -> float:
    """Отношение потенциала к риску в единицах R."""
    risk_per_unit = _validate_sides(direction, entry, stop)
    reward = target - entry if direction == LONG else entry - target
    if reward <= 0:
        side = "выше" if direction == LONG else "ниже"
        raise RiskError(f"цель {target:g} должна быть {side} входа {entry:g} для {direction}")
    return reward / risk_per_unit


def r_multiple(direction: str, entry: float, stop: float, current: float) -> float:
    """Текущий результат сделки в единицах R. Отрицательный — против позиции."""
    risk_per_unit = _validate_sides(direction, entry, stop)
    move = current - entry if direction == LONG else entry - current
    return move / risk_per_unit


# --------------------------------------------------------------------------
# план сделки
# --------------------------------------------------------------------------


@dataclass
class TradePlan:
    instrument: str
    direction: str
    entry: float
    stop: float
    target: float | None
    equity: float
    risk_pct: float
    min_rr: float = DEFAULT_MIN_RR
    max_add_ons: int | None = None
    max_total_risk_pct: float | None = None

    def evaluate(self) -> dict[str, Any]:
        sizing = position_size(self.equity, self.risk_pct, self.entry, self.stop, self.direction)
        blockers: list[str] = []
        notes: list[str] = []

        if self.target is None:
            rr = None
            notes.append("цель не задана — R:R не считается, сделка не оценивается по потенциалу")
        else:
            rr = rr_ratio(self.direction, self.entry, self.stop, self.target)
            if rr < self.min_rr:
                blockers.append(
                    f"R:R {rr:.2f} ниже минимума {self.min_rr:.2f}"
                )

        if self.max_add_ons is None or self.max_total_risk_pct is None:
            # Добор против позиции — единственное место, где риск растёт после
            # входа. Без потолка риск на сделку не определён, и заявленный
            # risk_pct описывает только первый вход.
            notes.append(
                "потолок доборов не задан: риск на сделку определён только для первого входа"
            )
        elif self.max_total_risk_pct < self.risk_pct:
            blockers.append(
                f"потолок суммарного риска {self.max_total_risk_pct:g}% ниже риска первого "
                f"входа {self.risk_pct:g}%"
            )

        return {
            "instrument": self.instrument,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rr": rr,
            "min_rr": self.min_rr,
            "size": sizing["size"],
            "risk_abs": sizing["risk_abs"],
            "risk_pct": self.risk_pct,
            "notional": sizing["notional"],
            "max_add_ons": self.max_add_ons,
            "max_total_risk_pct": self.max_total_risk_pct,
            "blockers": blockers,
            "notes": notes,
            "acceptable": not blockers,
        }


def add_on_budget(plan: TradePlan) -> dict[str, Any]:
    """Сколько риска остаётся на доборы и по сколько на каждый."""
    if plan.max_total_risk_pct is None or plan.max_add_ons is None:
        return {
            "resolved": False,
            "reason": "потолок доборов или суммарного риска не задан",
        }
    remaining = plan.max_total_risk_pct - plan.risk_pct
    if remaining < 0:
        raise RiskError("суммарный потолок риска ниже риска первого входа")
    per_add_on = remaining / plan.max_add_ons if plan.max_add_ons else 0.0
    return {
        "resolved": True,
        "remaining_risk_pct": remaining,
        "max_add_ons": plan.max_add_ons,
        "per_add_on_risk_pct": per_add_on,
    }


# --------------------------------------------------------------------------
# сопровождение
# --------------------------------------------------------------------------


@dataclass
class ManagementRules:
    breakeven_at_r: float | None = DEFAULT_BREAKEVEN_AT_R
    partials: tuple[tuple[float, float], ...] = DEFAULT_PARTIALS
    stop_out_at_r: float = -1.0


@dataclass
class Action:
    kind: str
    detail: str
    at_r: float

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "at_r": self.at_r}


@dataclass
class ManagementState:
    current_r: float
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_r": self.current_r,
            "actions": [a.to_dict() for a in self.actions],
            "warnings": list(self.warnings),
        }


def manage(
    plan: TradePlan,
    current: float,
    rules: ManagementRules | None = None,
    already_done: Sequence[str] = (),
) -> ManagementState:
    """Какие действия по сделке сработали на текущей цене.

    Возвращает только сработавшие и ещё не выполненные действия; ``already_done``
    несёт ключи из прошлых вызовов, чтобы частичная фиксация не предлагалась
    повторно на одном и том же уровне.
    """
    rules = rules or ManagementRules()
    current_r = r_multiple(plan.direction, plan.entry, plan.stop, current)
    state = ManagementState(current_r=current_r)
    done = set(already_done)

    if current_r <= rules.stop_out_at_r:
        state.actions.append(
            Action("стоп", f"цена прошла стоп: {current_r:.2f}R", current_r)
        )
        return state

    if rules.breakeven_at_r is not None and current_r >= rules.breakeven_at_r:
        key = "breakeven"
        if key not in done:
            state.actions.append(
                Action("перевод в безубыток",
                       f"достигнуто {current_r:.2f}R — стоп в точку входа {plan.entry:g}",
                       rules.breakeven_at_r)
            )

    for level, fraction in rules.partials:
        key = f"partial@{level:g}"
        if current_r >= level and key not in done:
            sizing = position_size(
                plan.equity, plan.risk_pct, plan.entry, plan.stop, plan.direction
            )
            state.actions.append(
                Action("частичная фиксация",
                       f"достигнуто {current_r:.2f}R — зафиксировать {fraction * 100:g}% "
                       f"({sizing['size'] * fraction:.6g} ед.)",
                       level)
            )

    if current_r < 0:
        state.warnings.append(
            f"сделка против позиции: {current_r:.2f}R — добор увеличивает риск, "
            f"а не усредняет его"
        )
    return state


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="risk", description="расчёт риска и сопровождение")
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--direction", required=True, choices=[LONG, SHORT])
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, required=True)
    parser.add_argument("--target", type=float)
    parser.add_argument("--equity", type=float, required=True)
    parser.add_argument("--risk-pct", type=float, required=True)
    parser.add_argument("--min-rr", type=float, default=DEFAULT_MIN_RR)
    parser.add_argument("--max-add-ons", type=int)
    parser.add_argument("--max-total-risk-pct", type=float)
    parser.add_argument("--current", type=float, help="текущая цена для сопровождения")
    parser.add_argument("--done", nargs="*", default=[], help="уже выполненные действия")
    parser.add_argument("--json", action="store_true")
    return parser


def _render(plan_eval: dict[str, Any], budget: dict[str, Any],
            state: ManagementState | None) -> str:
    rr = plan_eval["rr"]
    out = [
        f"Инструмент: {plan_eval['instrument']} · Направление: {plan_eval['direction']}",
        f"Вход: {plan_eval['entry']:g} · Стоп: {plan_eval['stop']:g} · "
        f"Цель: {plan_eval['target'] if plan_eval['target'] is not None else 'не задано'}",
        f"R:R: {f'{rr:.2f}' if rr is not None else 'не считается'} "
        f"(минимум {plan_eval['min_rr']:.2f})",
        f"Объём: {plan_eval['size']:.6g} ед. · Номинал: {plan_eval['notional']:.2f}",
        f"Риск: {plan_eval['risk_abs']:.2f} ({plan_eval['risk_pct']:g}% счёта)",
    ]
    if budget.get("resolved"):
        out.append(
            f"Доборы: до {budget['max_add_ons']}, по {budget['per_add_on_risk_pct']:.3g}% "
            f"на каждый, остаток риска {budget['remaining_risk_pct']:.3g}%"
        )
    else:
        out.append(f"Доборы: {budget['reason']}")

    for blocker in plan_eval["blockers"]:
        out.append(f"БЛОКИРУЕТ: {blocker}")
    for note in plan_eval["notes"]:
        out.append(f"Замечание: {note}")
    out.append(f"Сделка приемлема: {'да' if plan_eval['acceptable'] else 'нет'}")

    if state is not None:
        out.append("")
        out.append(f"Сопровождение на {state.current_r:.2f}R:")
        if not state.actions:
            out.append("  действий нет")
        for action in state.actions:
            out.append(f"  {action.kind}: {action.detail}")
        for warning in state.warnings:
            out.append(f"  внимание: {warning}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan = TradePlan(
        instrument=args.instrument,
        direction=args.direction,
        entry=args.entry,
        stop=args.stop,
        target=args.target,
        equity=args.equity,
        risk_pct=args.risk_pct,
        min_rr=args.min_rr,
        max_add_ons=args.max_add_ons,
        max_total_risk_pct=args.max_total_risk_pct,
    )
    try:
        plan_eval = plan.evaluate()
        budget = add_on_budget(plan)
        state = manage(plan, args.current, already_done=args.done) if args.current else None
    except RiskError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {"plan": plan_eval, "add_ons": budget}
        if state is not None:
            payload["management"] = state.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render(plan_eval, budget, state))
    return 0 if plan_eval["acceptable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
