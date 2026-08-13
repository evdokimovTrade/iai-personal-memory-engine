#!/usr/bin/env python3
"""Дашборд торговой статистики из журнала сделок.

Читает append-only журнал `tools/diary.py` и считает статистику счёта.
HTML не считает ничего сам — вся арифметика здесь, страница только рисует.

Центральная величина — математическое ожидание, а не суммарный P&L. Прибыль
за период говорит, что уже случилось; ожидание говорит, чего стоит ждать от
следующей сделки при том же исполнении. Серия сделок — выборка из
распределения, а не приговор системе, поэтому рядом с ожиданием всегда стоит
размер выборки: на десяти сделках ожидание не измерено, а угадано.

Неизвестное не равно нулю. Винрейт при нуле закрытых сделок — не 0%, а «нет
данных»; сделка без стопа не даёт R-статистики и считается отдельно, а не
как R = 0.
"""

from __future__ import annotations

import argparse
import html
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diary import (  # noqa: E402
    LedgerError,
    Trade,
    default_ledger_path,
    fold_trades,
    read_records,
    verify_chain,
)

UNSET: str = "нет данных"

# Ниже этого числа закрытых сделок ожидание считается неизмеренным.
MIN_SAMPLE: int = 30


@dataclass
class Stats:
    total: int = 0
    open_count: int = 0
    closed_count: int = 0
    with_pnl: int = 0
    without_pnl: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    expectancy: float | None = None
    profit_factor: float | None = None
    r_sample: int = 0
    r_missing: int = 0
    expectancy_r: float | None = None
    avg_r: float | None = None
    max_drawdown: float | None = None
    equity_curve: list[float] = field(default_factory=list)
    sample_sufficient: bool = False
    chain_ok: bool = True
    chain_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _r_multiple(trade: Trade) -> float | None:
    """Результат сделки в R. None, если стоп или P&L неизвестны."""
    if trade.pnl is None or trade.entry is None or trade.stop is None or trade.size is None:
        return None
    risk_abs = abs(trade.entry - trade.stop) * trade.size
    if risk_abs == 0:
        return None
    return trade.pnl / risk_abs


def compute_stats(trades: dict[str, Trade], chain_errors: Sequence[str] = ()) -> Stats:
    st = Stats(chain_ok=not chain_errors, chain_errors=list(chain_errors))
    st.total = len(trades)
    closed = [t for t in trades.values() if not t.is_open]
    st.open_count = st.total - len(closed)
    st.closed_count = len(closed)

    priced = [t for t in closed if t.pnl is not None]
    st.with_pnl = len(priced)
    st.without_pnl = st.closed_count - st.with_pnl

    if not priced:
        # Ни одной закрытой сделки с известным P&L: всё производное остаётся
        # None. Ноль здесь означал бы «торговали и вышли в ноль».
        return st

    wins = [t.pnl for t in priced if t.pnl > 0]
    losses = [t.pnl for t in priced if t.pnl < 0]
    st.wins, st.losses = len(wins), len(losses)
    st.scratches = st.with_pnl - st.wins - st.losses

    st.gross_profit = sum(wins)
    st.gross_loss = sum(losses)
    st.net_pnl = st.gross_profit + st.gross_loss
    st.win_rate = st.wins / st.with_pnl * 100.0
    st.avg_win = statistics.fmean(wins) if wins else None
    st.avg_loss = statistics.fmean(losses) if losses else None
    st.expectancy = st.net_pnl / st.with_pnl
    st.profit_factor = (st.gross_profit / abs(st.gross_loss)) if losses else None

    r_values = [r for r in (_r_multiple(t) for t in priced) if r is not None]
    st.r_sample = len(r_values)
    st.r_missing = st.with_pnl - st.r_sample
    if r_values:
        st.avg_r = statistics.fmean(r_values)
        st.expectancy_r = st.avg_r

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(priced, key=lambda x: x.closed_at or ""):
        equity += t.pnl
        st.equity_curve.append(equity)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    st.max_drawdown = max_dd

    st.sample_sufficient = st.with_pnl >= MIN_SAMPLE
    return st


def open_risk(trades: dict[str, Trade]) -> dict[str, Any]:
    """Риск, связанный в открытых позициях."""
    open_trades = [t for t in trades.values() if t.is_open]
    known: list[float] = []
    unknown: list[str] = []
    for t in open_trades:
        if t.entry is None or t.stop is None or t.size is None:
            unknown.append(t.trade_id)
        else:
            known.append(abs(t.entry - t.stop) * t.size)
    return {
        "open_count": len(open_trades),
        "risk_abs": sum(known) if known else None,
        "priced": len(known),
        "unpriced": unknown,
    }


# --------------------------------------------------------------------------
# рендер
# --------------------------------------------------------------------------


def _fmt(value: Any, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return UNSET
    if isinstance(value, float):
        return f"{value:,.{digits}f}".replace(",", " ") + suffix
    return f"{value}{suffix}"


def _tile(label: str, value: str, note: str = "", tone: str = "") -> str:
    cls = f"tile {tone}".strip()
    note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
    return (
        f'<div class="{cls}"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>{note_html}</div>'
    )


def _sparkline(points: Sequence[float]) -> str:
    if len(points) < 2:
        return '<div class="empty">Кривая эквити: нет данных — нужно минимум две закрытые сделки</div>'
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    width, height = 720, 140
    step = width / (len(points) - 1)
    coords = " ".join(
        f"{i * step:.1f},{height - (p - lo) / span * (height - 12) - 6:.1f}"
        for i, p in enumerate(points)
    )
    zero_y = height - (0 - lo) / span * (height - 12) - 6 if lo <= 0 <= hi else None
    zero = (
        f'<line x1="0" y1="{zero_y:.1f}" x2="{width}" y2="{zero_y:.1f}" class="zero"/>'
        if zero_y is not None else ""
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="spark" role="img" '
        f'aria-label="Кривая эквити">{zero}'
        f'<polyline points="{coords}"/></svg>'
    )


def render_html(stats: Stats, risk: dict[str, Any], generated_at: str,
                ledger_path: str) -> str:
    if stats.total == 0:
        headline = "Журнал пуст — сделок нет"
        headline_note = (
            "Это не убыточная и не прибыльная статистика. Это отсутствие сделок: "
            "все производные величины помечены «нет данных», а не нулём."
        )
    elif stats.with_pnl == 0:
        headline = f"{stats.total} сделок, ни одной закрытой с известным P&L"
        headline_note = "Статистика результата не считается: считать не из чего."
    else:
        headline = f"{stats.with_pnl} закрытых сделок с P&L"
        headline_note = (
            f"Выборка {'достаточна' if stats.sample_sufficient else 'мала'}: "
            f"{stats.with_pnl} из {MIN_SAMPLE} для измеримого ожидания."
        )

    sample_tone = "" if stats.sample_sufficient else "warn"
    exp_note = (
        "Ожидание на сделку. При выборке меньше "
        f"{MIN_SAMPLE} это оценка, а не измеренная величина."
    )

    tiles = "".join([
        _tile("Всего сделок", _fmt(stats.total, digits=0) if stats.total else UNSET),
        _tile("Открыто", _fmt(stats.open_count, digits=0)),
        _tile("Закрыто", _fmt(stats.closed_count, digits=0),
              f"без P&L: {stats.without_pnl}" if stats.without_pnl else ""),
        _tile("Винрейт", _fmt(stats.win_rate, "%"),
              f"{stats.wins} прибыльных / {stats.losses} убыточных"
              if stats.with_pnl else "нет закрытых сделок"),
        _tile("Матожидание", _fmt(stats.expectancy), exp_note, sample_tone),
        _tile("Матожидание, R", _fmt(stats.expectancy_r, digits=3),
              f"по {stats.r_sample} сделкам со стопом"
              + (f", без стопа: {stats.r_missing}" if stats.r_missing else ""),
              sample_tone),
        _tile("Профит-фактор", _fmt(stats.profit_factor),
              "нет убыточных сделок — делить не на что" if stats.profit_factor is None
              and stats.with_pnl else ""),
        _tile("Чистый P&L", _fmt(stats.net_pnl)),
        _tile("Средний плюс", _fmt(stats.avg_win)),
        _tile("Средний минус", _fmt(stats.avg_loss)),
        _tile("Макс. просадка", _fmt(stats.max_drawdown)),
        _tile("Риск в открытых", _fmt(risk["risk_abs"]),
              f"без стопа: {len(risk['unpriced'])}" if risk["unpriced"] else ""),
    ])

    chain_block = (
        '<div class="banner ok">Цепочка журнала цела — записи не переписывались.</div>'
        if stats.chain_ok else
        '<div class="banner bad"><strong>Цепочка журнала нарушена.</strong><ul>'
        + "".join(f"<li>{html.escape(e)}</li>" for e in stats.chain_errors)
        + "</ul></div>"
    )

    return f"""<title>Торговая статистика</title>
<style>
  :root {{
    --bg: #f6f7f9; --panel: #ffffff; --ink: #14171a; --muted: #5c6570;
    --line: #e2e6ea; --accent: #2f6fd0; --ok: #1c7a4a; --bad: #b3261e;
    --warn: #8a5a00; --warn-bg: #fdf4e3;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #0f1215; --panel: #171b20; --ink: #e8ecf1; --muted: #98a2ad;
      --line: #262c33; --accent: #6ea8fe; --ok: #4ec38a; --bad: #ff6b5e;
      --warn: #e0b060; --warn-bg: #2a2216;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #0f1215; --panel: #171b20; --ink: #e8ecf1; --muted: #98a2ad;
    --line: #262c33; --accent: #6ea8fe; --ok: #4ec38a; --bad: #ff6b5e;
    --warn: #e0b060; --warn-bg: #2a2216;
  }}
  body {{
    margin: 0; padding: 32px 24px 64px; background: var(--bg); color: var(--ink);
    font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }}
  .sub {{ color: var(--muted); font-size: 13px; margin-bottom: 24px; }}
  .headline {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 18px 20px; margin-bottom: 20px;
  }}
  .headline .big {{ font-size: 19px; font-weight: 600; }}
  .headline .note {{ color: var(--muted); font-size: 13px; margin-top: 6px; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }}
  .tile {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 14px 16px;
  }}
  .tile.warn {{ background: var(--warn-bg); border-color: var(--warn); }}
  .tile .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.04em; }}
  .tile .value {{ font-size: 24px; font-weight: 600; margin-top: 4px;
    font-variant-numeric: tabular-nums; }}
  .tile .note {{ font-size: 12px; color: var(--muted); margin-top: 6px; }}
  .panel {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 18px 20px; margin-bottom: 20px;
  }}
  .panel h2 {{ font-size: 15px; margin: 0 0 12px; }}
  .spark {{ width: 100%; height: auto; }}
  .spark polyline {{ fill: none; stroke: var(--accent); stroke-width: 2;
    stroke-linejoin: round; }}
  .spark .zero {{ stroke: var(--line); stroke-width: 1; stroke-dasharray: 4 4; }}
  .empty {{ color: var(--muted); font-size: 13px; }}
  .banner {{ border-radius: 10px; padding: 12px 16px; margin-bottom: 20px;
    font-size: 14px; }}
  .banner.ok {{ background: color-mix(in srgb, var(--ok) 12%, transparent);
    border: 1px solid var(--ok); }}
  .banner.bad {{ background: color-mix(in srgb, var(--bad) 12%, transparent);
    border: 1px solid var(--bad); }}
  .banner ul {{ margin: 8px 0 0; padding-left: 20px; }}
  footer {{ color: var(--muted); font-size: 12px; border-top: 1px solid var(--line);
    padding-top: 14px; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
</style>
<div class="wrap">
  <h1>Торговая статистика</h1>
  <div class="sub">Источник: <code>{html.escape(ledger_path)}</code> · собрано {html.escape(generated_at)}</div>
  {chain_block}
  <div class="headline">
    <div class="big">{html.escape(headline)}</div>
    <div class="note">{html.escape(headline_note)}</div>
  </div>
  <div class="grid">{tiles}</div>
  <div class="panel">
    <h2>Кривая эквити по закрытым сделкам</h2>
    {_sparkline(stats.equity_curve)}
  </div>
  <footer>
    «Нет данных» и «ноль» — разные состояния и на этой странице никогда не
    смешиваются. Матожидание считается только по закрытым сделкам с известным
    P&amp;L; статистика в R — только по сделкам с заданным стопом.
  </footer>
</div>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashboard", description="дашборд торговой статистики из журнала"
    )
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--out", default=None, help="куда писать HTML")
    parser.add_argument("--json", action="store_true", help="выдать статистику JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.ledger) if args.ledger else default_ledger_path()
    try:
        records = read_records(path)
        chain_errors = verify_chain(records)
        trades = fold_trades(records)
    except LedgerError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    stats = compute_stats(trades, chain_errors)
    risk = open_risk(trades)

    if args.json:
        print(json.dumps({"stats": stats.to_dict(), "open_risk": risk},
                         ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0 if stats.chain_ok else 1

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = render_html(stats, risk, generated, str(path))
    out = Path(args.out) if args.out else Path("dashboard.html")
    out.write_text(page, encoding="utf-8")
    print(f"записано: {out}")
    return 0 if stats.chain_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
