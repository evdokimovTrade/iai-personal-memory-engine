#!/usr/bin/env python3
"""Соответствие торговли риск-протоколу челленджа (ETS-1, $100 000).

Источник цифр — риск-протокол, присланный пользователем 2026-08-14. Часть
правил однозначна и проверяется по журналу `tools/diary.py` напрямую. Часть
опирается на термины, для которых сам протокол не называет базу расчёта:

* «Дневной риск» — не сказано, это сумма риска, взятого в открытых за день
  сделках, или реализованный убыток дня. Обе трактовки в `daily_risk()`,
  выбор явный через `basis`, тихого умолчания нет.
* «Абсолютный риск аккаунта» — трактуется здесь как просадка **от начального
  баланса** (не от пикового эквити), потому что слово «абсолютный»
  противопоставлено скользящей просадке, а не потому что это где-то сказано
  прямо. Это допущение, а не факт из протокола, и оно возвращается в выводе
  вместе с результатом.

Всё меряется от **стартового баланса челленджа**, а не от текущего эквити
сделки. Это разные знаменатели: `tools/diary.py` пишет `account_equity` на
момент открытия каждой сделки, а протокол требует делить на одно и то же
число всю дистанцию. Поэтому `starting_balance` — обязательный параметр
каждой проверки, а не читается из записей сделок.

Низколиквидные активы (капитализация < $100M, суточный объём $500K–$5M) не
определяются из журнала: в нём нет ни капитализации, ни объёма инструмента.
Классификация принимается только явно, инструмент за инструментом; догадка
по цене или тикеру не делается.

Семь процедурных запретов протокола (запрос демо-средств, ручная правка
баланса, работа с API-ключами, спот внутри фьючерсного челленджа, EUR/USD,
опционы и USDC-пары, одновременный спот+маржа) не проверяются кодом: это
действия с аккаунтом, которых в append-only журнале сделок не видно.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diary import LedgerError, Trade, default_ledger_path, fold_trades, read_records  # noqa: E402

# --------------------------------------------------------------------------
# протокол: именованные числа
# --------------------------------------------------------------------------

TARGET_PROFIT_PCT: float = 10.0            # цель, не лимит; закрытыми позициями
MIN_TRADING_DAYS: int = 5
MIN_DAY_NOTIONAL_PCT: float = 5.0          # объём сделки в день дисциплины, % от старта
MIN_DAY_PNL_ABS_PCT: float = 1.0           # |PnL закрытия| в день дисциплины, % от старта
MAX_DAILY_RISK_PCT: float = 4.0            # база расчёта не названа — см. daily_risk()
MAX_ACCOUNT_RISK_PCT: float = 6.0          # трактовка «от старта» — см. docstring модуля
MAX_INACTIVITY_DAYS: int = 30
FUND_MAX_RISK_PER_TRADE_PCT: float = 3.0
USER_MAX_RISK_PER_TRADE_PCT: float = 0.5   # внутренний регламент, в 6 раз строже фонда
MAX_SINGLE_DAY_PROFIT_SHARE_PCT: float = 40.0
LOW_LIQ_MAX_AGGREGATE_PCT: float = 5.0
LOW_LIQ_MCAP_MAX_USD: float = 100_000_000.0
LOW_LIQ_VOLUME_MIN_USD: float = 500_000.0
LOW_LIQ_VOLUME_MAX_USD: float = 5_000_000.0


class ProtocolError(RuntimeError):
    pass


@dataclass
class Check:
    name: str
    resolved: bool
    ok: bool | None
    detail: str
    basis_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _unresolved(name: str, detail: str) -> Check:
    return Check(name, resolved=False, ok=None, detail=detail)


def _utc_day(iso_ts: str) -> date:
    return datetime.fromisoformat(iso_ts).astimezone(timezone.utc).date()


def _validate_starting_balance(starting_balance: float) -> None:
    if starting_balance <= 0:
        raise ProtocolError(
            f"стартовый баланс должен быть положительным, получено {starting_balance!r}"
        )


# --------------------------------------------------------------------------
# риск на сделку
# --------------------------------------------------------------------------


def per_trade_risk(trade: Trade, starting_balance: float) -> Check:
    """Риск сделки от начального баланса челленджа, не от equity на момент входа.

    Риск пересчитывается из entry/stop/size, а не берётся из поля risk_pct
    сделки — то поле может быть посчитано от другого знаменателя или введено
    вручную с ошибкой; протокол сверяется с фактом, не с заявлением.
    """
    _validate_starting_balance(starting_balance)
    if trade.entry is None or trade.stop is None or trade.size is None:
        return _unresolved(
            f"{trade.trade_id}: риск на сделку",
            "нет данных: entry, stop или size не заданы — риск не пересчитывается",
        )
    risk_abs = abs(trade.entry - trade.stop) * trade.size
    risk_pct = risk_abs / starting_balance * 100.0
    if risk_pct > FUND_MAX_RISK_PER_TRADE_PCT:
        return Check(
            f"{trade.trade_id}: риск на сделку", True, False,
            f"{risk_pct:.3f}% от старта — превышен лимит фонда {FUND_MAX_RISK_PER_TRADE_PCT}%",
        )
    if risk_pct > USER_MAX_RISK_PER_TRADE_PCT:
        return Check(
            f"{trade.trade_id}: риск на сделку", True, True,
            f"{risk_pct:.3f}% от старта — в пределах фонда, но выше внутреннего "
            f"регламента {USER_MAX_RISK_PER_TRADE_PCT}%",
        )
    return Check(
        f"{trade.trade_id}: риск на сделку", True, True,
        f"{risk_pct:.3f}% от старта — в пределах внутреннего регламента",
    )


# --------------------------------------------------------------------------
# дневной риск
# --------------------------------------------------------------------------


def daily_risk(
    trades: dict[str, Trade], starting_balance: float, basis: str = "opened"
) -> dict[date, Check]:
    """Дневной риск по UTC-дням, по одной из двух баз.

    ``basis="opened"`` — сумма риска (|entry-stop|*size) сделок, ОТКРЫТЫХ в
    этот день. Отвечает на вопрос «сколько риска взято сегодня».

    ``basis="realized"`` — сумма отрицательного P&L сделок, ЗАКРЫТЫХ в этот
    день. Отвечает на вопрос «сколько потеряно сегодня по факту».

    Протокол не говорит, какая из двух имелась в виду под «дневным риском» —
    обе дают разный вердикт на одном журнале, поэтому вызывающий обязан
    выбрать явно.
    """
    _validate_starting_balance(starting_balance)
    if basis not in ("opened", "realized"):
        raise ProtocolError(f"basis={basis!r} не распознан; ожидается 'opened' или 'realized'")

    by_day: dict[date, float] = {}
    unresolved_days: set[date] = set()

    if basis == "opened":
        for t in trades.values():
            if t.opened_at is None:
                continue
            day = _utc_day(t.opened_at)
            if t.entry is None or t.stop is None or t.size is None:
                unresolved_days.add(day)
                continue
            by_day[day] = by_day.get(day, 0.0) + abs(t.entry - t.stop) * t.size
    else:
        for t in trades.values():
            if t.is_open or t.closed_at is None:
                continue
            day = _utc_day(t.closed_at)
            if t.pnl is None:
                unresolved_days.add(day)
                continue
            if t.pnl < 0:
                by_day[day] = by_day.get(day, 0.0) + (-t.pnl)
            else:
                by_day.setdefault(day, 0.0)

    basis_note = (
        "риск взятый в открытых за день сделках" if basis == "opened"
        else "реализованный убыток дня (только отрицательный P&L)"
    )
    result: dict[date, Check] = {}
    for day in sorted(set(by_day) | unresolved_days):
        if day in unresolved_days and day not in by_day:
            result[day] = Check(
                f"{day}: дневной риск ({basis})", False, None,
                "нет данных: не у всех сделок дня заданы поля для расчёта",
                basis_note,
            )
            continue
        risk_pct = by_day[day] / starting_balance * 100.0
        ok = risk_pct <= MAX_DAILY_RISK_PCT
        note = " (день также содержит сделки без данных)" if day in unresolved_days else ""
        result[day] = Check(
            f"{day}: дневной риск ({basis})", True, ok,
            f"{risk_pct:.3f}% от старта, лимит {MAX_DAILY_RISK_PCT}%{note}",
            basis_note,
        )
    return result


# --------------------------------------------------------------------------
# абсолютный риск аккаунта
# --------------------------------------------------------------------------


def absolute_account_risk(trades: dict[str, Trade], starting_balance: float) -> Check:
    """Просадка от стартового баланса по цепочке закрытых сделок.

    Трактовка «абсолютный» = от начального баланса, не от пикового эквити —
    см. предупреждение в docstring модуля. Считается по всем закрытым сделкам
    с известным P&L, отсортированным по времени закрытия; сделки без P&L из
    расчёта выпадают явно, а не входят нулём.
    """
    _validate_starting_balance(starting_balance)
    priced = [t for t in trades.values() if not t.is_open and t.pnl is not None]
    missing = sum(1 for t in trades.values() if not t.is_open and t.pnl is None)
    if not priced:
        return _unresolved(
            "абсолютный риск аккаунта",
            "нет закрытых сделок с известным P&L — просадка не считается",
        )

    cumulative = 0.0
    trough = 0.0
    for t in sorted(priced, key=lambda x: x.closed_at or ""):
        cumulative += t.pnl
        trough = min(trough, cumulative)

    drawdown_pct = -trough / starting_balance * 100.0
    ok = drawdown_pct <= MAX_ACCOUNT_RISK_PCT
    note = f" ({missing} закрытых сделок без P&L не учтены)" if missing else ""
    return Check(
        "абсолютный риск аккаунта", True, ok,
        f"максимальная просадка от старта {drawdown_pct:.3f}%, "
        f"лимит {MAX_ACCOUNT_RISK_PCT}%{note}",
        "трактовка: просадка от начального баланса, не от пикового эквити",
    )


# --------------------------------------------------------------------------
# бездействие
# --------------------------------------------------------------------------


def inactivity_gap(trades: dict[str, Trade], as_of: datetime | None = None) -> Check:
    """Наибольший разрыв между ордерами, включая разрыв до текущего момента."""
    events: list[datetime] = []
    for t in trades.values():
        if t.opened_at:
            events.append(datetime.fromisoformat(t.opened_at).astimezone(timezone.utc))
        if t.closed_at:
            events.append(datetime.fromisoformat(t.closed_at).astimezone(timezone.utc))
    if not events:
        return _unresolved("период неактивности", "в журнале нет ни одного ордера")

    events.sort()
    now = (as_of or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    gaps = [
        (b - a).days for a, b in zip(events, events[1:])
    ] + [(now - events[-1]).days]
    max_gap = max(gaps)
    ok = max_gap <= MAX_INACTIVITY_DAYS
    return Check(
        "период неактивности", True, ok,
        f"наибольший разрыв {max_gap} дн., лимит {MAX_INACTIVITY_DAYS} дн.",
    )


# --------------------------------------------------------------------------
# дисциплинарные дни
# --------------------------------------------------------------------------


def discipline_days(trades: dict[str, Trade], starting_balance: float) -> Check:
    """Дни, засчитываемые в дисциплинарный минимум.

    День засчитывается, если в нём исполнен ордер, объём сделки не ниже
    ``MIN_DAY_NOTIONAL_PCT`` от старта, и в этот же день закрытие дало |P&L|
    не ниже ``MIN_DAY_PNL_ABS_PCT`` от старта. Протокол требует «PnL
    закрытия» буквально — день с открытием без закрытия в тот же день это
    условие выполнить не может, каким бы большим ни был объём.
    """
    _validate_starting_balance(starting_balance)
    notional_by_day: dict[date, float] = {}
    pnl_by_day: dict[date, float] = {}

    for t in trades.values():
        if t.opened_at and t.entry is not None and t.size is not None:
            day = _utc_day(t.opened_at)
            notional_by_day[day] = notional_by_day.get(day, 0.0) + abs(t.entry * t.size)
        if not t.is_open and t.closed_at and t.pnl is not None:
            day = _utc_day(t.closed_at)
            pnl_by_day[day] = pnl_by_day.get(day, 0.0) + t.pnl

    qualifying: list[date] = []
    for day, notional in notional_by_day.items():
        notional_pct = notional / starting_balance * 100.0
        day_pnl = pnl_by_day.get(day)
        if day_pnl is None:
            continue
        if notional_pct >= MIN_DAY_NOTIONAL_PCT and abs(day_pnl) / starting_balance * 100.0 >= (
            MIN_DAY_PNL_ABS_PCT
        ):
            qualifying.append(day)

    ok = len(qualifying) >= MIN_TRADING_DAYS
    return Check(
        "дисциплинарный минимум", True, ok,
        f"{len(qualifying)} из {MIN_TRADING_DAYS} требуемых дней засчитано",
        "день без закрытия в тот же день не может выполнить условие по P&L закрытия",
    )


# --------------------------------------------------------------------------
# концентрация прибыли в один день
# --------------------------------------------------------------------------


def single_day_profit_concentration(trades: dict[str, Trade]) -> Check:
    """Доля чистой прибыли, пришедшая из одного дня.

    Понятие не определено, если суммарный чистый результат не положителен —
    делить долю не на что.
    """
    by_day: dict[date, float] = {}
    for t in trades.values():
        if not t.is_open and t.closed_at and t.pnl is not None:
            day = _utc_day(t.closed_at)
            by_day[day] = by_day.get(day, 0.0) + t.pnl

    if not by_day:
        return _unresolved("концентрация прибыли в один день", "нет закрытых сделок с P&L")

    total = sum(by_day.values())
    if total <= 0:
        return _unresolved(
            "концентрация прибыли в один день",
            f"суммарный чистый результат {total:.2f} не положителен — доля не определена",
        )

    best_day, best_pnl = max(by_day.items(), key=lambda kv: kv[1])
    share_pct = best_pnl / total * 100.0
    ok = share_pct <= MAX_SINGLE_DAY_PROFIT_SHARE_PCT
    return Check(
        "концентрация прибыли в один день", True, ok,
        f"{best_day}: {share_pct:.2f}% чистой прибыли, лимит {MAX_SINGLE_DAY_PROFIT_SHARE_PCT}%",
    )


# --------------------------------------------------------------------------
# низколиквидные активы
# --------------------------------------------------------------------------


def low_liquidity_exposure(
    trades: dict[str, Trade],
    starting_balance: float,
    is_low_liquidity: dict[str, bool] | None = None,
) -> Check:
    """Суммарная экспозиция в низколиквидных активах по открытым позициям.

    Классификация «низколиквидный» (капитализация < $100M, суточный объём
    $500K–$5M) не выводится из журнала — в нём нет ни капитализации, ни
    объёма инструмента. Без явно переданной карты классификации проверка не
    выполняется, а не считается пройденной.
    """
    _validate_starting_balance(starting_balance)
    if is_low_liquidity is None:
        return _unresolved(
            "низколиквидные активы: суммарная экспозиция",
            "классификация инструментов не передана — капитализация и объём "
            "в журнале не хранятся",
        )

    open_trades = [t for t in trades.values() if t.is_open]
    exposure = 0.0
    unresolved: list[str] = []
    unclassified: list[str] = []
    for t in open_trades:
        if t.instrument not in is_low_liquidity:
            unclassified.append(t.instrument)
            continue
        if not is_low_liquidity[t.instrument]:
            continue
        if t.entry is None or t.size is None:
            unresolved.append(t.trade_id)
            continue
        exposure += abs(t.entry * t.size)

    if unclassified:
        return _unresolved(
            "низколиквидные активы: суммарная экспозиция",
            f"нет классификации для инструментов: {', '.join(sorted(set(unclassified)))}",
        )

    exposure_pct = exposure / starting_balance * 100.0
    ok = exposure_pct <= LOW_LIQ_MAX_AGGREGATE_PCT
    note = f" ({len(unresolved)} позиций без размера не учтены)" if unresolved else ""
    return Check(
        "низколиквидные активы: суммарная экспозиция", True, ok,
        f"{exposure_pct:.3f}% от старта в низколиквидных активах, "
        f"лимит {LOW_LIQ_MAX_AGGREGATE_PCT}%{note}",
    )


# --------------------------------------------------------------------------
# сводный отчёт
# --------------------------------------------------------------------------


@dataclass
class ProtocolReport:
    starting_balance: float
    checks: list[Check] = field(default_factory=list)

    @property
    def violations(self) -> list[Check]:
        return [c for c in self.checks if c.resolved and c.ok is False]

    @property
    def unresolved(self) -> list[Check]:
        return [c for c in self.checks if not c.resolved]

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_balance": self.starting_balance,
            "checks": [c.to_dict() for c in self.checks],
            "violation_count": len(self.violations),
            "unresolved_count": len(self.unresolved),
        }


def audit(
    trades: dict[str, Trade],
    starting_balance: float,
    daily_risk_basis: str = "opened",
    is_low_liquidity: dict[str, bool] | None = None,
    as_of: datetime | None = None,
) -> ProtocolReport:
    checks: list[Check] = []
    for trade_id in sorted(trades):
        checks.append(per_trade_risk(trades[trade_id], starting_balance))
    daily = daily_risk(trades, starting_balance, daily_risk_basis)
    for day in sorted(daily):
        checks.append(daily[day])
    checks.append(absolute_account_risk(trades, starting_balance))
    checks.append(inactivity_gap(trades, as_of))
    checks.append(discipline_days(trades, starting_balance))
    checks.append(single_day_profit_concentration(trades))
    checks.append(low_liquidity_exposure(trades, starting_balance, is_low_liquidity))
    return ProtocolReport(starting_balance, checks)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protocol", description="сверка журнала с риск-протоколом челленджа"
    )
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--starting-balance", type=float, required=True)
    parser.add_argument("--daily-risk-basis", choices=["opened", "realized"], default="opened")
    parser.add_argument("--json", action="store_true")
    return parser


def _render(report: ProtocolReport) -> str:
    out = [f"Стартовый баланс: {report.starting_balance:,.2f}".replace(",", " ")]
    for c in report.checks:
        if not c.resolved:
            mark = "?"
        else:
            mark = "OK" if c.ok else "НАРУШЕНО"
        out.append(f"[{mark}] {c.name}: {c.detail}")
        if c.basis_note:
            out.append(f"      база: {c.basis_note}")
    out.append("")
    out.append(f"Нарушений: {len(report.violations)} · Не разрешено: {len(report.unresolved)}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.ledger) if args.ledger else default_ledger_path()
    try:
        trades = fold_trades(read_records(path))
        report = audit(trades, args.starting_balance, args.daily_risk_basis)
    except (LedgerError, ProtocolError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render(report))
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
