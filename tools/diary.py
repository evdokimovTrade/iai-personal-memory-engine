#!/usr/bin/env python3
"""Append-only trade ledger.

Two invariants the analyst layer is not allowed to work around:

1. A decision cannot be rewritten after the fact. Events are appended, never
   edited, and each one carries a hash of its predecessor, so a silent edit
   breaks the chain and ``verify`` reports it.
2. An outcome cannot be attached to a signal that was never recorded. A
   ``close`` event must reference a ``trade_id`` that already exists as an
   open position in the ledger.

Unknown is not zero. A field that was never supplied stays ``None`` and
renders as "не задано"; it is never coerced to 0, and arithmetic that would
need it is skipped and reported as missing data rather than computed against
a fabricated value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION: int = 1

UNSET: str = "не задано"

# Money comparisons run against exchange-rounded values; anything tighter
# than this reports rounding noise as a discrepancy.
DEFAULT_TOLERANCE: float = 1e-6

_EVENTS: frozenset[str] = frozenset({"open", "close"})

_DIRECTIONS: dict[str, str] = {
    "long": "long",
    "buy": "long",
    "лонг": "long",
    "short": "short",
    "sell": "short",
    "шорт": "short",
}

_GENESIS_HASH: str = "0" * 64


class LedgerError(RuntimeError):
    pass


def default_ledger_path() -> Path:
    env_path = os.environ.get("IAI_TRADE_JOURNAL")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[1] / "memory" / "journal" / "trades.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_hash(record: dict[str, Any]) -> str:
    # `.get` rather than `[]`: a hand-edited record missing one of these keys
    # must surface as a hash mismatch, not crash the verifier.
    core = {k: record.get(k) for k in ("seq", "ts", "event", "trade_id", "body", "prev_hash")}
    return hashlib.sha256(_canonical(core).encode("utf-8")).hexdigest()


def normalize_direction(raw: str) -> str:
    key = raw.strip().lower()
    if key not in _DIRECTIONS:
        raise LedgerError(
            f"направление {raw!r} не распознано; ожидается одно из: "
            + ", ".join(sorted(set(_DIRECTIONS)))
        )
    return _DIRECTIONS[key]


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass
class Trade:
    trade_id: str
    opened_at: str
    instrument: str
    direction: str
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    logic: str | None = None
    size: float | None = None
    risk_pct: float | None = None
    account_equity: float | None = None
    closed_at: str | None = None
    exit: float | None = None
    fees: float | None = None
    pnl: float | None = None
    close_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def status(self) -> str:
        return "открыта" if self.is_open else "закрыта"


@dataclass
class Discrepancy:
    trade_id: str
    kind: str
    declared_label: str
    declared: float
    computed_label: str
    computed: float

    @property
    def delta(self) -> float:
        return self.declared - self.computed


@dataclass
class MissingData:
    trade_id: str
    check: str
    fields: tuple[str, ...]


@dataclass
class AuditReport:
    chain_ok: bool
    chain_errors: list[str]
    discrepancies: list[Discrepancy]
    missing: list[MissingData]

    @property
    def ok(self) -> bool:
        return self.chain_ok and not self.discrepancies


# --------------------------------------------------------------------------
# ledger I/O
# --------------------------------------------------------------------------


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"{path}:{line_no}: строка не разбирается как JSON: {exc}") from exc
            if record.get("event") not in _EVENTS:
                raise LedgerError(
                    f"{path}:{line_no}: неизвестный тип события {record.get('event')!r}"
                )
            records.append(record)
    return records


def _last_hash(records: Sequence[dict[str, Any]]) -> str:
    return records[-1]["hash"] if records else _GENESIS_HASH


def append_record(path: Path, event: str, trade_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if event not in _EVENTS:
        raise LedgerError(f"неизвестный тип события {event!r}")
    records = read_records(path)
    record = {
        "seq": len(records) + 1,
        "ts": _utc_now_iso(),
        "event": event,
        "trade_id": trade_id,
        "body": body,
        "prev_hash": _last_hash(records),
        "schema_version": SCHEMA_VERSION,
    }
    record["hash"] = _record_hash(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(record) + "\n")
    return record


def verify_chain(records: Sequence[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    expected_prev = _GENESIS_HASH
    for i, record in enumerate(records, start=1):
        if record.get("seq") != i:
            errors.append(f"позиция {i}: seq={record.get('seq')!r}, ожидалось {i}")
        if record.get("prev_hash") != expected_prev:
            errors.append(
                f"seq={record.get('seq')}: prev_hash={record.get('prev_hash')!r}, "
                f"ожидалось {expected_prev!r} — цепочка разорвана"
            )
        recomputed = _record_hash(record)
        if record.get("hash") != recomputed:
            errors.append(
                f"seq={record.get('seq')}: hash={record.get('hash')!r}, "
                f"пересчитанный {recomputed!r} — запись изменена задним числом"
            )
        expected_prev = record.get("hash", "")
    return errors


# --------------------------------------------------------------------------
# folding events into trades
# --------------------------------------------------------------------------


def fold_trades(records: Sequence[dict[str, Any]]) -> dict[str, Trade]:
    trades: dict[str, Trade] = {}
    for record in records:
        trade_id = record["trade_id"]
        body = record.get("body") or {}
        if record["event"] == "open":
            if trade_id in trades:
                raise LedgerError(f"{trade_id}: повторное открытие уже существующей сделки")
            for required in ("instrument", "direction"):
                if not body.get(required):
                    raise LedgerError(
                        f"{trade_id}: в записи открытия нет обязательного поля {required!r}"
                    )
            trades[trade_id] = Trade(
                trade_id=trade_id,
                opened_at=body.get("opened_at") or record["ts"],
                instrument=body["instrument"],
                direction=body["direction"],
                entry=body.get("entry"),
                stop=body.get("stop"),
                target=body.get("target"),
                logic=body.get("logic"),
                size=body.get("size"),
                risk_pct=body.get("risk_pct"),
                account_equity=body.get("account_equity"),
            )
        else:
            trade = trades.get(trade_id)
            if trade is None:
                raise LedgerError(
                    f"{trade_id}: закрытие ссылается на сделку, которой нет в журнале — "
                    f"исход нельзя привязать к незаписанному сигналу"
                )
            if not trade.is_open:
                raise LedgerError(f"{trade_id}: сделка уже закрыта в {trade.closed_at}")
            trade.closed_at = body.get("closed_at") or record["ts"]
            trade.exit = body.get("exit")
            trade.fees = body.get("fees")
            trade.pnl = body.get("pnl")
            trade.close_reason = body.get("reason")
    return trades


# --------------------------------------------------------------------------
# arithmetic audit
# --------------------------------------------------------------------------


def expected_gross_pnl(trade: Trade) -> float | None:
    if trade.entry is None or trade.exit is None or trade.size is None:
        return None
    move = trade.exit - trade.entry
    if trade.direction == "short":
        move = -move
    return move * trade.size


def expected_risk_pct(trade: Trade) -> float | None:
    if (
        trade.entry is None
        or trade.stop is None
        or trade.size is None
        or trade.account_equity in (None, 0)
    ):
        return None
    risk_abs = abs(trade.entry - trade.stop) * trade.size
    return risk_abs / float(trade.account_equity) * 100.0


def _missing_fields(trade: Trade, names: Sequence[str]) -> tuple[str, ...]:
    return tuple(n for n in names if getattr(trade, n) is None)


def audit_trades(
    trades: dict[str, Trade], tolerance: float = DEFAULT_TOLERANCE
) -> tuple[list[Discrepancy], list[MissingData]]:
    discrepancies: list[Discrepancy] = []
    missing: list[MissingData] = []

    for trade_id in sorted(trades):
        trade = trades[trade_id]

        if not trade.is_open:
            gross = expected_gross_pnl(trade)
            if gross is None:
                missing.append(
                    MissingData(trade_id, "P&L", _missing_fields(trade, ("entry", "exit", "size")))
                )
            elif trade.pnl is None:
                missing.append(MissingData(trade_id, "P&L", ("pnl",)))
            else:
                # Fees are only netted out when they are actually known; a
                # missing fee is reported separately rather than treated as 0.
                if trade.fees is None:
                    missing.append(MissingData(trade_id, "комиссии", ("fees",)))
                    computed = gross
                    computed_label = "расчётный P&L брутто"
                else:
                    computed = gross - trade.fees
                    computed_label = "расчётный P&L нетто"
                if abs(trade.pnl - computed) > tolerance:
                    discrepancies.append(
                        Discrepancy(
                            trade_id=trade_id,
                            kind="P&L",
                            declared_label="заявленный P&L",
                            declared=trade.pnl,
                            computed_label=computed_label,
                            computed=computed,
                        )
                    )

        risk = expected_risk_pct(trade)
        if risk is None:
            fields = _missing_fields(trade, ("entry", "stop", "size", "account_equity"))
            # Nothing is None but the calculation still refused: equity is a
            # real 0, which is a data problem in its own right, not an absence.
            if not fields:
                fields = ("account_equity = 0 — деление невозможно",)
            missing.append(MissingData(trade_id, "риск в % счёта", fields))
        elif trade.risk_pct is None:
            missing.append(MissingData(trade_id, "риск в % счёта", ("risk_pct",)))
        elif abs(trade.risk_pct - risk) > tolerance:
            discrepancies.append(
                Discrepancy(
                    trade_id=trade_id,
                    kind="риск в % счёта",
                    declared_label="заявленный риск",
                    declared=trade.risk_pct,
                    computed_label="расчётный риск",
                    computed=risk,
                )
            )

    return discrepancies, missing


def audit_total(
    trades: dict[str, Trade], declared_total: float, tolerance: float = DEFAULT_TOLERANCE
) -> Discrepancy | None:
    known = [t.pnl for t in trades.values() if t.pnl is not None]
    if not known:
        return None
    line_sum = sum(known)
    if abs(declared_total - line_sum) <= tolerance:
        return None
    return Discrepancy(
        trade_id="ИТОГО",
        kind="сумма по строкам против итога",
        declared_label="заявленный итог",
        declared=declared_total,
        computed_label="сумма по строкам",
        computed=line_sum,
    )


def audit(path: Path, tolerance: float = DEFAULT_TOLERANCE) -> AuditReport:
    records = read_records(path)
    chain_errors = verify_chain(records)
    trades = fold_trades(records)
    discrepancies, missing = audit_trades(trades, tolerance)
    return AuditReport(
        chain_ok=not chain_errors,
        chain_errors=chain_errors,
        discrepancies=discrepancies,
        missing=missing,
    )


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def fmt(value: Any) -> str:
    if value is None:
        return UNSET
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def render_trade(trade: Trade) -> str:
    if trade.size is None and trade.risk_pct is None:
        size_risk = UNSET
    else:
        size_risk = f"{fmt(trade.size)} @ {fmt(trade.risk_pct)}% счёта"
    lines = [
        f"Инструмент: {fmt(trade.instrument)}",
        f"Направление: {fmt(trade.direction)}",
        f"Точка входа: {fmt(trade.entry)}",
        f"Стоп: {fmt(trade.stop)}",
        f"Цель: {fmt(trade.target)}",
        f"Логика входа: {fmt(trade.logic)}",
        f"Размер и риск в % счёта: {size_risk}",
        f"Статус: {trade.status}",
    ]
    if not trade.is_open:
        lines += [
            f"Выход: {fmt(trade.exit)}",
            f"Комиссии: {fmt(trade.fees)}",
            f"P&L: {fmt(trade.pnl)}",
            f"Причина закрытия: {fmt(trade.close_reason)}",
        ]
    return "\n".join(lines)


def render_audit(report: AuditReport) -> str:
    out: list[str] = []
    if report.chain_errors:
        out.append("Цепочка журнала нарушена:")
        out += [f"  {e}" for e in report.chain_errors]
    else:
        out.append("Цепочка журнала цела.")

    if report.discrepancies:
        out.append("")
        out.append("Расхождения:")
        for d in report.discrepancies:
            out.append(
                f"  {d.trade_id} · {d.kind}: {d.declared_label} {fmt(d.declared)}, "
                f"{d.computed_label} {fmt(d.computed)}, разница {fmt(d.delta)}"
            )
    else:
        out.append("Расхождений в арифметике не найдено.")

    if report.missing:
        out.append("")
        out.append("Нет данных (не ноль — именно отсутствие):")
        for m in report.missing:
            out.append(f"  {m.trade_id} · {m.check}: {', '.join(m.fields)}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _iter_import_rows(raw: str) -> Iterator[dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return
    if raw.startswith("["):
        yield from json.loads(raw)
        return
    for line in raw.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)


def _cmd_open(args: argparse.Namespace, path: Path) -> int:
    body = {
        "instrument": args.instrument,
        "direction": normalize_direction(args.direction),
        "entry": args.entry,
        "stop": args.stop,
        "target": args.target,
        "logic": args.logic,
        "size": args.size,
        "risk_pct": args.risk_pct,
        "account_equity": args.equity,
    }
    records = read_records(path)
    trades = fold_trades(records)
    if args.trade_id in trades:
        raise LedgerError(f"{args.trade_id}: сделка уже есть в журнале")
    append_record(path, "open", args.trade_id, body)
    print(f"записано: open {args.trade_id}")
    return 0


def _cmd_close(args: argparse.Namespace, path: Path) -> int:
    records = read_records(path)
    trades = fold_trades(records)
    trade = trades.get(args.trade_id)
    if trade is None:
        raise LedgerError(
            f"{args.trade_id}: в журнале нет такой сделки — "
            f"исход нельзя привязать к незаписанному сигналу"
        )
    if not trade.is_open:
        raise LedgerError(f"{args.trade_id}: сделка уже закрыта в {trade.closed_at}")
    body = {
        "exit": args.exit,
        "fees": args.fees,
        "pnl": args.pnl,
        "reason": args.reason,
    }
    append_record(path, "close", args.trade_id, body)
    print(f"записано: close {args.trade_id}")
    return 0


def _cmd_list(args: argparse.Namespace, path: Path) -> int:
    trades = fold_trades(read_records(path))
    if not trades:
        print("журнал пуст")
        return 0
    for trade_id in sorted(trades):
        t = trades[trade_id]
        print(
            f"{trade_id}\t{t.instrument}\t{t.direction}\t{t.status}\tP&L {fmt(t.pnl)}"
        )
    return 0


def _cmd_show(args: argparse.Namespace, path: Path) -> int:
    trades = fold_trades(read_records(path))
    trade = trades.get(args.trade_id)
    if trade is None:
        raise LedgerError(f"{args.trade_id}: в журнале нет такой сделки")
    print(render_trade(trade))
    return 0


def _cmd_verify(args: argparse.Namespace, path: Path) -> int:
    report = audit(path, tolerance=args.tolerance)
    print(render_audit(report))
    if args.total is not None:
        trades = fold_trades(read_records(path))
        total_gap = audit_total(trades, args.total, tolerance=args.tolerance)
        if not any(t.pnl is not None for t in trades.values()):
            # No line has a P&L, so the declared total is unverified — that is
            # not the same as it having been checked and matched.
            print("Итог не с чем сверять: ни в одной строке нет P&L.")
        elif total_gap is None:
            print("Итог сходится с суммой по строкам.")
        else:
            print(
                f"  {total_gap.trade_id} · {total_gap.kind}: "
                f"{total_gap.declared_label} {fmt(total_gap.declared)}, "
                f"{total_gap.computed_label} {fmt(total_gap.computed)}, "
                f"разница {fmt(total_gap.delta)}"
            )
            return 1
    return 0 if report.ok else 1


def _cmd_import(args: argparse.Namespace, path: Path) -> int:
    raw = Path(args.source).read_text(encoding="utf-8") if args.source != "-" else sys.stdin.read()
    imported = 0
    skipped = 0
    for row in _iter_import_rows(raw):
        trade_id = row["trade_id"]
        trades = fold_trades(read_records(path))
        if trade_id not in trades:
            append_record(
                path,
                "open",
                trade_id,
                {
                    "instrument": row["instrument"],
                    "direction": normalize_direction(row["direction"]),
                    "entry": row.get("entry"),
                    "stop": row.get("stop"),
                    "target": row.get("target"),
                    "logic": row.get("logic"),
                    "size": row.get("size"),
                    "risk_pct": row.get("risk_pct"),
                    "account_equity": row.get("account_equity"),
                    "opened_at": row.get("opened_at"),
                },
            )
            imported += 1
            trades = fold_trades(read_records(path))
        if row.get("closed_at") and trades[trade_id].is_open:
            append_record(
                path,
                "close",
                trade_id,
                {
                    "exit": row.get("exit"),
                    "fees": row.get("fees"),
                    "pnl": row.get("pnl"),
                    "reason": row.get("reason") or "импорт с биржи",
                    "closed_at": row.get("closed_at"),
                },
            )
        elif trade_id in trades and not trades[trade_id].is_open:
            skipped += 1
    print(f"импортировано открытий: {imported}, пропущено уже закрытых: {skipped}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diary", description="журнал сделок (только добавление)")
    parser.add_argument("--ledger", default=None, help="путь к JSONL-журналу")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="записать открытие позиции")
    p_open.add_argument("trade_id")
    p_open.add_argument("--instrument", required=True)
    p_open.add_argument("--direction", required=True)
    p_open.add_argument("--entry", type=float)
    p_open.add_argument("--stop", type=float)
    p_open.add_argument("--target", type=float)
    p_open.add_argument("--logic")
    p_open.add_argument("--size", type=float)
    p_open.add_argument("--risk-pct", dest="risk_pct", type=float)
    p_open.add_argument("--equity", type=float)
    p_open.set_defaults(func=_cmd_open)

    p_close = sub.add_parser("close", help="записать закрытие позиции")
    p_close.add_argument("trade_id")
    p_close.add_argument("--exit", dest="exit", type=float)
    p_close.add_argument("--fees", type=float)
    p_close.add_argument("--pnl", type=float)
    p_close.add_argument("--reason")
    p_close.set_defaults(func=_cmd_close)

    p_list = sub.add_parser("list", help="перечислить сделки")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="карточка сделки")
    p_show.add_argument("trade_id")
    p_show.set_defaults(func=_cmd_show)

    p_verify = sub.add_parser("verify", help="проверить цепочку и арифметику")
    p_verify.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    p_verify.add_argument("--total", type=float, help="сверить заявленный итог с суммой по строкам")
    p_verify.set_defaults(func=_cmd_verify)

    p_import = sub.add_parser("import", help="импортировать нормализованные строки коллектора")
    p_import.add_argument("source", help="путь к файлу или '-' для stdin")
    p_import.set_defaults(func=_cmd_import)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.ledger) if args.ledger else default_ledger_path()
    try:
        return int(args.func(args, path))
    except LedgerError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
