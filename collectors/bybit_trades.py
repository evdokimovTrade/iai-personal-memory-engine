#!/usr/bin/env python3
"""Read-only collector for closed Bybit positions.

Emits normalized JSON lines for ``tools/diary.py import``. It reads; it never
places, modifies, or cancels anything.

Credentials come from the environment only — ``BYBIT_API_KEY`` and
``BYBIT_API_SECRET``. They are never accepted as CLI arguments (argv is
visible to other processes), never echoed, and never written to the output.

Unknown is not zero: a field the exchange did not return stays ``null`` in the
output so the ledger can tell "no data" apart from a real zero.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

DEFAULT_BASE_URL: str = "https://api.bybit.com"
CLOSED_PNL_PATH: str = "/v5/position/closed-pnl"
RECV_WINDOW: str = "5000"
DEFAULT_TIMEOUT: float = 15.0


class CollectorError(RuntimeError):
    pass


def env_credentials() -> tuple[str, str]:
    key = os.environ.get("BYBIT_API_KEY")
    secret = os.environ.get("BYBIT_API_SECRET")
    if not key or not secret:
        raise CollectorError(
            "не заданы переменные окружения BYBIT_API_KEY и BYBIT_API_SECRET "
            "(значения не передавать аргументами командной строки)"
        )
    return key, secret


def build_signature(secret: str, timestamp: str, api_key: str, recv_window: str, query: str) -> str:
    payload = f"{timestamp}{api_key}{recv_window}{query}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _query_string(params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return urllib.parse.urlencode(sorted(clean.items()))


def fetch_closed_pnl(
    *,
    category: str,
    symbol: str | None,
    limit: int,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    api_key, secret = env_credentials()
    query = _query_string({"category": category, "symbol": symbol, "limit": limit})
    timestamp = str(int(time.time() * 1000))
    signature = build_signature(secret, timestamp, api_key, RECV_WINDOW, query)
    request = urllib.request.Request(
        f"{base_url}{CLOSED_PNL_PATH}?{query}",
        method="GET",
        headers={
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": signature,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


def _num(row: dict[str, Any], *names: str) -> float | None:
    """First present, non-empty, numeric field among ``names``; else None.

    An absent or unparseable field yields None rather than 0.0 — the ledger
    treats those as different states.
    """
    for name in names:
        value = row.get(name)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _ms_to_iso(row: dict[str, Any], *names: str) -> str | None:
    value = _num(row, *names)
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(value / 1000.0))


def _sum_fees(row: dict[str, Any]) -> float | None:
    open_fee = _num(row, "openFee")
    close_fee = _num(row, "closeFee")
    if open_fee is None and close_fee is None:
        return None
    # A partially reported fee is still unknown in total; do not treat the
    # missing leg as zero.
    if open_fee is None or close_fee is None:
        return None
    return open_fee + close_fee


def resolve_direction(side: str | None, side_means: str) -> str | None:
    """Map Bybit's ``side`` onto the direction the position actually had.

    ``side`` on the closed-PnL endpoint denotes the *closing* order by
    default, so a closed long reports ``Sell``. Bybit has varied this across
    endpoints and API versions, so the interpretation is an explicit switch
    rather than a silent assumption baked into the parser.
    """
    if not side:
        return None
    normalized = side.strip().lower()
    if normalized not in ("buy", "sell"):
        return None
    if side_means == "closing":
        return "long" if normalized == "sell" else "short"
    return "long" if normalized == "buy" else "short"


def normalize_row(row: dict[str, Any], side_means: str = "closing") -> dict[str, Any]:
    symbol = row.get("symbol")
    order_id = row.get("orderId") or row.get("execId")
    closed_at = _ms_to_iso(row, "updatedTime")
    trade_id = row.get("trade_id") or f"{symbol}-{order_id}"
    return {
        "trade_id": trade_id,
        "instrument": symbol,
        "direction": resolve_direction(row.get("side"), side_means),
        "entry": _num(row, "avgEntryPrice"),
        "exit": _num(row, "avgExitPrice"),
        "size": _num(row, "qty", "closedSize"),
        "fees": _sum_fees(row),
        "pnl": _num(row, "closedPnl"),
        "opened_at": _ms_to_iso(row, "createdTime"),
        "closed_at": closed_at,
        # Stop, target, logic, risk and equity are decisions, not exchange
        # facts. The exchange cannot supply them, so they stay unset and the
        # diary renders them as "не задано".
        "stop": None,
        "target": None,
        "logic": None,
        "risk_pct": None,
        "account_equity": None,
    }


def normalize_payload(payload: dict[str, Any], side_means: str = "closing") -> list[dict[str, Any]]:
    if payload.get("retCode") not in (0, None):
        raise CollectorError(
            f"биржа вернула retCode={payload.get('retCode')}: {payload.get('retMsg')!r}"
        )
    rows = (payload.get("result") or {}).get("list")
    if rows is None:
        raise CollectorError("в ответе нет result.list")
    normalized = [normalize_row(row, side_means) for row in rows]
    missing_direction = [r["trade_id"] for r in normalized if r["direction"] is None]
    if missing_direction:
        print(
            "предупреждение: направление не определено для: "
            + ", ".join(missing_direction),
            file=sys.stderr,
        )
    return normalized


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bybit_trades",
        description="выгрузка закрытых позиций Bybit в формате журнала (только чтение)",
    )
    parser.add_argument("--category", default="linear", choices=["linear", "inverse", "option"])
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--from-file",
        default=None,
        help="разобрать сохранённый ответ вместо обращения к бирже (offline)",
    )
    parser.add_argument(
        "--side-means",
        default="closing",
        choices=["closing", "opening"],
        help="что означает поле side в ответе: сторону закрывающего или открывающего ордера",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.from_file:
            payload = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        else:
            payload = fetch_closed_pnl(
                category=args.category,
                symbol=args.symbol,
                limit=args.limit,
                base_url=args.base_url,
            )
        for row in normalize_payload(payload, args.side_means):
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    except CollectorError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
