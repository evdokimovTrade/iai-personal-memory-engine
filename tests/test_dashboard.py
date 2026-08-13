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
def dash(tmp_path):
    mod = _load(f"dashboard_{tmp_path.name}", TOOLS / "dashboard.py")
    yield mod
    sys.modules.pop(mod.__name__, None)


@pytest.fixture
def diary(tmp_path):
    mod = _load(f"diary_for_dash_{tmp_path.name}", TOOLS / "diary.py")
    yield mod
    sys.modules.pop(mod.__name__, None)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "trades.jsonl"


def _trade(diary, ledger, tid, pnl=None, entry=100.0, stop=90.0, size=1.0, closed=True):
    diary.append_record(ledger, "open", tid, {
        "instrument": "ETHUSDT", "direction": "long", "entry": entry,
        "stop": stop, "size": size, "risk_pct": 1.0, "account_equity": 10_000.0,
    })
    if closed:
        diary.append_record(ledger, "close", tid, {
            "exit": entry + 10, "fees": 0.0, "pnl": pnl,
            "closed_at": f"2026-08-{10 + int(tid[-1]):02d}T00:00:00+00:00",
        })


def _stats(dash, diary, ledger):
    records = diary.read_records(ledger)
    return dash.compute_stats(diary.fold_trades(records), diary.verify_chain(records))


# --------------------------------------------------------------------------
# пустой журнал: нет данных, а не ноль
# --------------------------------------------------------------------------


def test_empty_ledger_yields_no_data_not_zero(dash, diary, ledger):
    st = _stats(dash, diary, ledger)
    assert st.total == 0
    assert st.win_rate is None
    assert st.expectancy is None
    assert st.net_pnl is None
    assert st.profit_factor is None
    assert st.max_drawdown is None


def test_empty_ledger_page_says_so_explicitly(dash, diary, ledger):
    st = _stats(dash, diary, ledger)
    page = dash.render_html(st, dash.open_risk({}), "сейчас", str(ledger))
    assert "Журнал пуст" in page
    assert "не нулём" in page
    assert "нет данных" in page


def test_open_only_trades_give_no_result_stats(dash, diary, ledger):
    _trade(diary, ledger, "T1", closed=False)
    st = _stats(dash, diary, ledger)
    assert st.total == 1 and st.open_count == 1
    assert st.win_rate is None and st.expectancy is None


def test_closed_without_pnl_counted_separately(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=None)
    st = _stats(dash, diary, ledger)
    assert st.closed_count == 1
    assert st.with_pnl == 0
    assert st.without_pnl == 1
    assert st.win_rate is None


# --------------------------------------------------------------------------
# арифметика статистики
# --------------------------------------------------------------------------


def test_win_rate_and_expectancy(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=30.0)
    _trade(diary, ledger, "T2", pnl=30.0)
    _trade(diary, ledger, "T3", pnl=30.0)
    _trade(diary, ledger, "T4", pnl=-10.0)
    st = _stats(dash, diary, ledger)
    assert st.with_pnl == 4
    assert st.wins == 3 and st.losses == 1
    assert st.win_rate == pytest.approx(75.0)
    assert st.net_pnl == pytest.approx(80.0)
    assert st.expectancy == pytest.approx(20.0)
    assert st.avg_win == pytest.approx(30.0)
    assert st.avg_loss == pytest.approx(-10.0)
    assert st.profit_factor == pytest.approx(9.0)


def test_expectancy_in_r_uses_stop_distance(dash, diary, ledger):
    # риск на сделку = |100-90| * 1 = 10; P&L 30 => 3R, P&L -10 => -1R
    _trade(diary, ledger, "T1", pnl=30.0)
    _trade(diary, ledger, "T2", pnl=-10.0)
    st = _stats(dash, diary, ledger)
    assert st.r_sample == 2
    assert st.expectancy_r == pytest.approx(1.0)


def test_trade_without_stop_excluded_from_r_not_counted_as_zero(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=30.0)
    _trade(diary, ledger, "T2", pnl=30.0, stop=None)
    st = _stats(dash, diary, ledger)
    assert st.with_pnl == 2
    assert st.r_sample == 1
    assert st.r_missing == 1
    assert st.expectancy_r == pytest.approx(3.0)  # не размыто нулём


def test_zero_pnl_is_a_scratch_not_a_win(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=0.0)
    _trade(diary, ledger, "T2", pnl=10.0)
    st = _stats(dash, diary, ledger)
    assert st.wins == 1 and st.losses == 0 and st.scratches == 1
    assert st.win_rate == pytest.approx(50.0)


def test_profit_factor_undefined_without_losses(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=10.0)
    st = _stats(dash, diary, ledger)
    assert st.profit_factor is None


def test_max_drawdown_from_equity_curve(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=100.0)
    _trade(diary, ledger, "T2", pnl=-40.0)
    _trade(diary, ledger, "T3", pnl=-20.0)
    _trade(diary, ledger, "T4", pnl=50.0)
    st = _stats(dash, diary, ledger)
    assert st.equity_curve == [100.0, 60.0, 40.0, 90.0]
    assert st.max_drawdown == pytest.approx(60.0)


def test_small_sample_is_flagged(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=10.0)
    st = _stats(dash, diary, ledger)
    assert st.sample_sufficient is False
    page = dash.render_html(st, dash.open_risk({}), "сейчас", str(ledger))
    assert "оценка, а не измеренная величина" in page


# --------------------------------------------------------------------------
# риск в открытых позициях
# --------------------------------------------------------------------------


def test_open_risk_sums_only_priced_positions(dash, diary, ledger):
    _trade(diary, ledger, "T1", closed=False)
    _trade(diary, ledger, "T2", closed=False, stop=None)
    trades = diary.fold_trades(diary.read_records(ledger))
    risk = dash.open_risk(trades)
    assert risk["open_count"] == 2
    assert risk["priced"] == 1
    assert risk["risk_abs"] == pytest.approx(10.0)
    assert risk["unpriced"] == ["T2"]


def test_open_risk_none_when_nothing_priced(dash):
    assert dash.open_risk({})["risk_abs"] is None


# --------------------------------------------------------------------------
# целостность цепочки попадает на страницу
# --------------------------------------------------------------------------


def test_broken_chain_is_shown_on_the_page(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=10.0)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["body"]["entry"] = 1.0
    lines[0] = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st = _stats(dash, diary, ledger)
    assert st.chain_ok is False
    page = dash.render_html(st, dash.open_risk({}), "сейчас", str(ledger))
    assert "Цепочка журнала нарушена" in page


# --------------------------------------------------------------------------
# рендер
# --------------------------------------------------------------------------


def test_page_is_self_contained_and_theme_aware(dash, diary, ledger):
    _trade(diary, ledger, "T1", pnl=10.0)
    page = dash.render_html(_stats(dash, diary, ledger), dash.open_risk({}), "сейчас", "x")
    assert "http://" not in page and "https://" not in page
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page
    assert "<title>" in page


def test_sparkline_needs_two_points(dash):
    assert "нет данных" in dash._sparkline([])
    assert "нет данных" in dash._sparkline([1.0])
    assert "<svg" in dash._sparkline([1.0, 2.0])


def test_ledger_path_is_escaped(dash):
    st = dash.compute_stats({})
    page = dash.render_html(st, dash.open_risk({}), "сейчас", "<script>x</script>")
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_writes_html(dash, diary, ledger, tmp_path, capsys):
    _trade(diary, ledger, "T1", pnl=10.0)
    out = tmp_path / "dash.html"
    rc = dash.main(["--ledger", str(ledger), "--out", str(out)])
    assert rc == 0
    assert out.exists()
    assert "Торговая статистика" in out.read_text(encoding="utf-8")
    assert "записано" in capsys.readouterr().out


def test_cli_json_mode(dash, diary, ledger, capsys):
    _trade(diary, ledger, "T1", pnl=10.0)
    dash.main(["--ledger", str(ledger), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["with_pnl"] == 1
    assert payload["stats"]["win_rate"] == 100.0


def test_cli_empty_ledger_json_has_nulls_not_zeros(dash, ledger, capsys):
    dash.main(["--ledger", str(ledger), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["win_rate"] is None
    assert payload["stats"]["expectancy"] is None
