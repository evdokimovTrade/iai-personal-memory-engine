from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "risk.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
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
def risk(tmp_path):
    mod = _load(f"risk_{tmp_path.name}")
    yield mod
    sys.modules.pop(mod.__name__, None)


def _plan(risk, **overrides):
    kwargs = dict(
        instrument="BTCUSDT", direction=risk.LONG, entry=100.0, stop=90.0,
        target=130.0, equity=10_000.0, risk_pct=1.0,
    )
    kwargs.update(overrides)
    return risk.TradePlan(**kwargs)


# --------------------------------------------------------------------------
# размер позиции
# --------------------------------------------------------------------------


def test_size_derives_from_risk_not_from_desired_notional(risk):
    # 1% от 10 000 = 100 риска; расстояние до стопа 10 => 10 единиц.
    s = risk.position_size(10_000.0, 1.0, 100.0, 90.0, risk.LONG)
    assert s["risk_abs"] == pytest.approx(100.0)
    assert s["risk_per_unit"] == pytest.approx(10.0)
    assert s["size"] == pytest.approx(10.0)
    assert s["notional"] == pytest.approx(1000.0)


def test_tighter_stop_gives_bigger_size_at_same_risk(risk):
    wide = risk.position_size(10_000.0, 1.0, 100.0, 90.0, risk.LONG)
    tight = risk.position_size(10_000.0, 1.0, 100.0, 98.0, risk.LONG)
    assert tight["size"] > wide["size"]
    assert tight["risk_abs"] == pytest.approx(wide["risk_abs"])


def test_short_side_sizing(risk):
    s = risk.position_size(10_000.0, 2.0, 100.0, 110.0, risk.SHORT)
    assert s["risk_abs"] == pytest.approx(200.0)
    assert s["size"] == pytest.approx(20.0)


def test_stop_on_the_wrong_side_is_rejected(risk):
    with pytest.raises(risk.RiskError, match="ниже входа"):
        risk.position_size(10_000.0, 1.0, 100.0, 110.0, risk.LONG)
    with pytest.raises(risk.RiskError, match="выше входа"):
        risk.position_size(10_000.0, 1.0, 100.0, 90.0, risk.SHORT)


def test_stop_equal_to_entry_is_rejected(risk):
    with pytest.raises(risk.RiskError):
        risk.position_size(10_000.0, 1.0, 100.0, 100.0, risk.LONG)


def test_nonpositive_equity_and_risk_are_rejected(risk):
    with pytest.raises(risk.RiskError, match="эквити"):
        risk.position_size(0.0, 1.0, 100.0, 90.0, risk.LONG)
    with pytest.raises(risk.RiskError, match="риск"):
        risk.position_size(10_000.0, 0.0, 100.0, 90.0, risk.LONG)


# --------------------------------------------------------------------------
# R:R и R-кратность
# --------------------------------------------------------------------------


def test_rr_ratio(risk):
    assert risk.rr_ratio(risk.LONG, 100.0, 90.0, 130.0) == pytest.approx(3.0)
    assert risk.rr_ratio(risk.SHORT, 100.0, 110.0, 70.0) == pytest.approx(3.0)


def test_target_on_the_wrong_side_is_rejected(risk):
    with pytest.raises(risk.RiskError, match="выше входа"):
        risk.rr_ratio(risk.LONG, 100.0, 90.0, 95.0)


def test_r_multiple_signs(risk):
    assert risk.r_multiple(risk.LONG, 100.0, 90.0, 110.0) == pytest.approx(1.0)
    assert risk.r_multiple(risk.LONG, 100.0, 90.0, 95.0) == pytest.approx(-0.5)
    assert risk.r_multiple(risk.SHORT, 100.0, 110.0, 90.0) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# оценка плана
# --------------------------------------------------------------------------


def test_plan_below_min_rr_is_blocked(risk):
    result = _plan(risk, target=115.0, min_rr=2.0).evaluate()
    assert result["rr"] == pytest.approx(1.5)
    assert result["acceptable"] is False
    assert any("ниже минимума" in b for b in result["blockers"])


def test_plan_meeting_min_rr_is_acceptable(risk):
    result = _plan(risk, max_add_ons=2, max_total_risk_pct=3.0).evaluate()
    assert result["acceptable"] is True
    assert result["blockers"] == []


def test_missing_target_is_noted_not_defaulted(risk):
    result = _plan(risk, target=None).evaluate()
    assert result["rr"] is None
    assert any("цель не задана" in n for n in result["notes"])


def test_missing_add_on_cap_is_flagged(risk):
    result = _plan(risk).evaluate()
    assert any("потолок доборов не задан" in n for n in result["notes"])
    assert any("первого входа" in n for n in result["notes"])


def test_total_cap_below_first_entry_is_blocked(risk):
    result = _plan(risk, risk_pct=2.0, max_add_ons=1, max_total_risk_pct=1.0).evaluate()
    assert result["acceptable"] is False
    assert any("потолок суммарного риска" in b for b in result["blockers"])


# --------------------------------------------------------------------------
# бюджет доборов
# --------------------------------------------------------------------------


def test_add_on_budget_splits_the_remainder(risk):
    budget = risk.add_on_budget(_plan(risk, risk_pct=1.0, max_add_ons=2, max_total_risk_pct=3.0))
    assert budget["resolved"] is True
    assert budget["remaining_risk_pct"] == pytest.approx(2.0)
    assert budget["per_add_on_risk_pct"] == pytest.approx(1.0)


def test_add_on_budget_unresolved_without_caps(risk):
    budget = risk.add_on_budget(_plan(risk))
    assert budget["resolved"] is False
    assert "не задан" in budget["reason"]


# --------------------------------------------------------------------------
# сопровождение
# --------------------------------------------------------------------------


def test_breakeven_triggers_at_one_r(risk):
    state = risk.manage(_plan(risk), current=110.0)
    kinds = [a.kind for a in state.actions]
    assert "перевод в безубыток" in kinds
    assert state.current_r == pytest.approx(1.0)


def test_breakeven_not_repeated_once_done(risk):
    state = risk.manage(_plan(risk), current=110.0, already_done=["breakeven"])
    assert "перевод в безубыток" not in [a.kind for a in state.actions]


def test_partials_trigger_in_order(risk):
    at_one = risk.manage(_plan(risk), current=110.0)
    assert sum(1 for a in at_one.actions if a.kind == "частичная фиксация") == 1
    at_two = risk.manage(_plan(risk), current=120.0)
    assert sum(1 for a in at_two.actions if a.kind == "частичная фиксация") == 2


def test_partial_reports_absolute_units(risk):
    state = risk.manage(_plan(risk), current=110.0)
    partial = next(a for a in state.actions if a.kind == "частичная фиксация")
    # Объём 10 ед., фиксируется 33% => 3.3 ед.
    assert "3.3" in partial.detail


def test_stop_out_short_circuits(risk):
    state = risk.manage(_plan(risk), current=90.0)
    assert [a.kind for a in state.actions] == ["стоп"]


def test_position_against_warns_that_add_on_raises_risk(risk):
    state = risk.manage(_plan(risk), current=95.0)
    assert state.actions == []
    assert any("увеличивает риск" in w for w in state.warnings)


def test_management_on_short_side(risk):
    plan = _plan(risk, direction=risk.SHORT, entry=100.0, stop=110.0, target=70.0)
    state = risk.manage(plan, current=90.0)
    assert state.current_r == pytest.approx(1.0)
    assert "перевод в безубыток" in [a.kind for a in state.actions]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_acceptable_plan_exits_zero(risk, capsys):
    rc = risk.main(
        ["--instrument", "BTCUSDT", "--direction", "long", "--entry", "100",
         "--stop", "90", "--target", "130", "--equity", "10000", "--risk-pct", "1",
         "--max-add-ons", "2", "--max-total-risk-pct", "3"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "R:R: 3.00" in out
    assert "Сделка приемлема: да" in out
    assert "по 1% на каждый" in out


def test_cli_blocked_plan_exits_nonzero(risk, capsys):
    rc = risk.main(
        ["--instrument", "BTCUSDT", "--direction", "long", "--entry", "100",
         "--stop", "90", "--target", "112", "--equity", "10000", "--risk-pct", "1"]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "БЛОКИРУЕТ" in out


def test_cli_bad_stop_exits_two(risk, capsys):
    rc = risk.main(
        ["--instrument", "BTCUSDT", "--direction", "long", "--entry", "100",
         "--stop", "110", "--equity", "10000", "--risk-pct", "1"]
    )
    assert rc == 2
    assert "должен быть ниже входа" in capsys.readouterr().err


def test_cli_management_section(risk, capsys):
    risk.main(
        ["--instrument", "BTCUSDT", "--direction", "long", "--entry", "100",
         "--stop", "90", "--target", "130", "--equity", "10000", "--risk-pct", "1",
         "--current", "110"]
    )
    out = capsys.readouterr().out
    assert "Сопровождение на 1.00R" in out
    assert "перевод в безубыток" in out
