from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[name]
        raise
    return mod


@pytest.fixture
def proto(tmp_path):
    mod = _load(f"protocol_{tmp_path.name}", TOOLS / "protocol.py")
    yield mod
    sys.modules.pop(mod.__name__, None)


@pytest.fixture
def diary(tmp_path):
    mod = _load(f"diary_for_proto_{tmp_path.name}", TOOLS / "diary.py")
    yield mod
    sys.modules.pop(mod.__name__, None)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "trades.jsonl"


START = 100_000.0


def _open(diary, ledger, tid, day, entry=100.0, stop=99.0, size=1.0, instrument="BTCUSDT"):
    diary.append_record(ledger, "open", tid, {
        "instrument": instrument, "direction": "long", "entry": entry, "stop": stop,
        "size": size, "opened_at": f"2026-08-{day:02d}T10:00:00+00:00",
    })


def _close(diary, ledger, tid, day, pnl):
    diary.append_record(ledger, "close", tid, {
        "pnl": pnl, "closed_at": f"2026-08-{day:02d}T12:00:00+00:00",
    })


def _trades(diary, ledger):
    return diary.fold_trades(diary.read_records(ledger))


# --------------------------------------------------------------------------
# риск на сделку
# --------------------------------------------------------------------------


def test_per_trade_risk_within_internal_limit(proto, diary, ledger):
    # |100-99.5|*1 = 0.5 => 0.5% от 100k — ровно на границе внутреннего лимита.
    _open(diary, ledger, "T1", 1, stop=99.5)
    c = proto.per_trade_risk(_trades(diary, ledger)["T1"], START)
    assert c.resolved and c.ok is True
    assert "внутреннего регламента" in c.detail


def test_per_trade_risk_above_internal_but_within_fund(proto, diary, ledger):
    # |100-98|*1000 = 2000 => 2% от 100k — выше 0.5%, но ниже лимита фонда 3%.
    _open(diary, ledger, "T1", 1, stop=98.0, size=1000.0)
    c = proto.per_trade_risk(_trades(diary, ledger)["T1"], START)
    assert c.ok is True
    assert "выше внутреннего регламента" in c.detail


def test_per_trade_risk_breaches_fund_limit(proto, diary, ledger):
    # |100-96|*1000 = 4000 => 4% от 100k — выше лимита фонда 3%.
    _open(diary, ledger, "T1", 1, stop=96.0, size=1000.0)
    c = proto.per_trade_risk(_trades(diary, ledger)["T1"], START)
    assert c.ok is False
    assert "лимит фонда" in c.detail


def test_per_trade_risk_unresolved_without_stop(proto, diary, ledger):
    diary.append_record(ledger, "open", "T1", {
        "instrument": "BTCUSDT", "direction": "long", "entry": 100.0,
        "opened_at": "2026-08-01T10:00:00+00:00",
    })
    c = proto.per_trade_risk(_trades(diary, ledger)["T1"], START)
    assert c.resolved is False


def test_per_trade_risk_uses_starting_balance_not_trade_equity(proto, diary, ledger):
    # account_equity на сделке — не 100k, но протокол мерит от старта, не от него.
    diary.append_record(ledger, "open", "T1", {
        "instrument": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 99.0,
        "size": 1.0, "account_equity": 5_000.0, "opened_at": "2026-08-01T10:00:00+00:00",
    })
    c = proto.per_trade_risk(_trades(diary, ledger)["T1"], START)
    assert "0.001%" in c.detail  # 1.0 / 100000 * 100, а не 1.0/5000*100 = 0.02%


def test_nonpositive_starting_balance_rejected(proto, diary, ledger):
    _open(diary, ledger, "T1", 1)
    with pytest.raises(proto.ProtocolError, match="положительным"):
        proto.per_trade_risk(_trades(diary, ledger)["T1"], 0.0)


# --------------------------------------------------------------------------
# дневной риск: две базы дают разные вердикты
# --------------------------------------------------------------------------


def test_daily_risk_opened_basis(proto, diary, ledger):
    # Два входа в один UTC-день с риском 2% + 2.5% = 4.5% > лимита 4%.
    _open(diary, ledger, "T1", 1, stop=98.0, size=1000.0)    # риск 2000 = 2%
    _open(diary, ledger, "T2", 1, stop=97.5, size=1000.0)    # риск 2500 = 2.5%
    result = proto.daily_risk(_trades(diary, ledger), START, basis="opened")
    (day, check), = result.items()
    assert check.ok is False
    assert "4.500%" in check.detail


def test_daily_risk_realized_basis_ignores_wins(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, stop=98.0)
    _close(diary, ledger, "T1", 1, pnl=500.0)  # прибыль не считается риском дня
    result = proto.daily_risk(_trades(diary, ledger), START, basis="realized")
    (day, check), = result.items()
    assert check.detail.startswith("0.000%")


def test_daily_risk_realized_basis_sums_only_losses(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, stop=98.0)
    _close(diary, ledger, "T1", 1, pnl=-3000.0)
    _open(diary, ledger, "T2", 1, stop=98.0)
    _close(diary, ledger, "T2", 1, pnl=-2000.0)
    result = proto.daily_risk(_trades(diary, ledger), START, basis="realized")
    (day, check), = result.items()
    assert check.ok is False
    assert "5.000%" in check.detail


def test_daily_risk_two_bases_can_disagree(proto, diary, ledger):
    # Открыт с большим стопом (риск взят), но закрылся почти в ноль — базы расходятся.
    _open(diary, ledger, "T1", 1, stop=95.0, size=1000.0)  # риск 5000 = 5% — нарушение "opened"
    _close(diary, ledger, "T1", 1, pnl=-10.0)              # убыток мизерный — ок по "realized"
    opened = proto.daily_risk(_trades(diary, ledger), START, basis="opened")
    realized = proto.daily_risk(_trades(diary, ledger), START, basis="realized")
    assert list(opened.values())[0].ok is False
    assert list(realized.values())[0].ok is True


def test_daily_risk_bad_basis_rejected(proto, diary, ledger):
    with pytest.raises(proto.ProtocolError, match="basis"):
        proto.daily_risk({}, START, basis="bogus")


# --------------------------------------------------------------------------
# абсолютный риск аккаунта
# --------------------------------------------------------------------------


def test_absolute_account_risk_tracks_trough_not_final(proto, diary, ledger):
    # Просадка до -7000 (7%) в середине пути, потом отскок до -3000 — считается худшая точка.
    _open(diary, ledger, "T1", 1)
    _close(diary, ledger, "T1", 1, pnl=-7000.0)
    _open(diary, ledger, "T2", 2)
    _close(diary, ledger, "T2", 2, pnl=4000.0)
    c = proto.absolute_account_risk(_trades(diary, ledger), START)
    assert c.ok is False
    assert "7.000%" in c.detail


def test_absolute_account_risk_within_limit(proto, diary, ledger):
    _open(diary, ledger, "T1", 1)
    _close(diary, ledger, "T1", 1, pnl=-3000.0)
    c = proto.absolute_account_risk(_trades(diary, ledger), START)
    assert c.ok is True


def test_absolute_account_risk_unresolved_when_nothing_closed(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, size=None)
    c = proto.absolute_account_risk({}, START)
    assert c.resolved is False


# --------------------------------------------------------------------------
# бездействие
# --------------------------------------------------------------------------


def test_inactivity_gap_between_orders(proto, diary, ledger):
    from datetime import datetime, timezone
    _open(diary, ledger, "T1", 1)
    _close(diary, ledger, "T1", 1, pnl=10.0)
    diary.append_record(ledger, "open", "T2", {
        "instrument": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 99.0,
        "size": 1.0, "opened_at": "2026-09-15T10:00:00+00:00",
    })
    c = proto.inactivity_gap(
        _trades(diary, ledger),
        as_of=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert c.ok is False  # разрыв между 1 авг и 15 сент > 30 дней


def test_inactivity_gap_within_limit(proto, diary, ledger):
    from datetime import datetime, timezone
    _open(diary, ledger, "T1", 1)
    c = proto.inactivity_gap(
        _trades(diary, ledger),
        as_of=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )
    assert c.ok is True


def test_inactivity_gap_unresolved_when_empty(proto):
    c = proto.inactivity_gap({})
    assert c.resolved is False


# --------------------------------------------------------------------------
# дисциплинарные дни
# --------------------------------------------------------------------------


def test_discipline_day_requires_notional_and_closed_pnl(proto, diary, ledger):
    # Объём 6000 = 6% (>=5%), P&L -1500 = 1.5% (>=1%) — день засчитан.
    _open(diary, ledger, "T1", 1, entry=100.0, size=60.0)  # notional 6000
    _close(diary, ledger, "T1", 1, pnl=-1500.0)
    c = proto.discipline_days(_trades(diary, ledger), START)
    assert "1 из 5" in c.detail


def test_discipline_day_fails_without_same_day_close(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, entry=100.0, size=60.0)
    _close(diary, ledger, "T1", 3, pnl=-1500.0)  # закрыт не в тот же день
    c = proto.discipline_days(_trades(diary, ledger), START)
    assert "0 из 5" in c.detail


def test_discipline_day_fails_on_scratch_pnl(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, entry=100.0, size=60.0)
    _close(diary, ledger, "T1", 1, pnl=-50.0)  # 0.05%, ниже 1%
    c = proto.discipline_days(_trades(diary, ledger), START)
    assert "0 из 5" in c.detail


def test_discipline_days_five_qualifying_days(proto, diary, ledger):
    for d in range(1, 6):
        _open(diary, ledger, f"T{d}", d, entry=100.0, size=60.0)
        _close(diary, ledger, f"T{d}", d, pnl=-1500.0)
    c = proto.discipline_days(_trades(diary, ledger), START)
    assert c.ok is True
    assert "5 из 5" in c.detail


# --------------------------------------------------------------------------
# концентрация прибыли в один день
# --------------------------------------------------------------------------


def test_single_day_concentration_over_limit(proto, diary, ledger):
    _open(diary, ledger, "T1", 1)
    _close(diary, ledger, "T1", 1, pnl=5000.0)
    _open(diary, ledger, "T2", 2)
    _close(diary, ledger, "T2", 2, pnl=1000.0)
    c = proto.single_day_profit_concentration(_trades(diary, ledger))
    assert c.ok is False
    assert "83.33%" in c.detail


def test_single_day_concentration_undefined_without_net_profit(proto, diary, ledger):
    _open(diary, ledger, "T1", 1)
    _close(diary, ledger, "T1", 1, pnl=-500.0)
    c = proto.single_day_profit_concentration(_trades(diary, ledger))
    assert c.resolved is False


# --------------------------------------------------------------------------
# низколиквидные активы
# --------------------------------------------------------------------------


def test_low_liquidity_unresolved_without_classification(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, instrument="TINYCOIN", size=1.0, entry=1000.0)
    c = proto.low_liquidity_exposure(_trades(diary, ledger), START, None)
    assert c.resolved is False


def test_low_liquidity_unresolved_when_instrument_unclassified(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, instrument="TINYCOIN", size=1.0, entry=1000.0)
    c = proto.low_liquidity_exposure(
        _trades(diary, ledger), START, {"OTHERCOIN": True}
    )
    assert c.resolved is False
    assert "TINYCOIN" in c.detail
    assert "T1" not in c.detail


def test_low_liquidity_sums_only_flagged_instruments(proto, diary, ledger):
    _open(diary, ledger, "T1", 1, instrument="TINYCOIN", size=10.0, entry=1000.0)  # 10000
    _open(diary, ledger, "T2", 1, instrument="BTCUSDT", size=1.0, entry=100000.0)  # не считается
    c = proto.low_liquidity_exposure(
        _trades(diary, ledger), START, {"TINYCOIN": True, "BTCUSDT": False}
    )
    assert c.resolved is True
    assert "10.000%" in c.detail
    assert c.ok is False  # выше лимита 5%


# --------------------------------------------------------------------------
# сводный отчёт и CLI
# --------------------------------------------------------------------------


def test_audit_collects_all_checks(proto, diary, ledger):
    _open(diary, ledger, "T1", 1)
    _close(diary, ledger, "T1", 1, pnl=-1500.0)
    report = proto.audit(_trades(diary, ledger), START)
    names = [c.name for c in report.checks]
    assert any("риск на сделку" in n for n in names)
    assert any("абсолютный риск аккаунта" in n for n in names)
    assert any("дисциплинарный минимум" in n for n in names)


def test_cli_reports_violations_and_exits_nonzero(proto, diary, ledger, capsys):
    _open(diary, ledger, "T1", 1, stop=96.0)  # 4% риск — нарушает лимит фонда
    rc = proto.main(["--ledger", str(ledger), "--starting-balance", str(START)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "НАРУШЕНО" in out


def test_cli_json_mode(proto, diary, ledger, capsys):
    _open(diary, ledger, "T1", 1)
    proto.main(["--ledger", str(ledger), "--starting-balance", str(START), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["starting_balance"] == START
    assert isinstance(payload["checks"], list)


def test_cli_requires_starting_balance(proto):
    with pytest.raises(SystemExit):
        proto.main(["--ledger", "x.jsonl"])
