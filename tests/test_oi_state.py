from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "oi_state.py"


def _load(tmp_path: Path):
    name = f"oi_state_{tmp_path.name}"
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
def oi(tmp_path):
    mod = _load(tmp_path)
    yield mod
    sys.modules.pop(mod.__name__, None)


# --------------------------------------------------------------------------
# направление и порог боковика
# --------------------------------------------------------------------------


def test_flat_band_is_inclusive(oi):
    assert oi.direction_of(1.0, 1.0) == oi.FLAT
    assert oi.direction_of(-1.0, 1.0) == oi.FLAT
    assert oi.direction_of(1.01, 1.0) == oi.UP
    assert oi.direction_of(-1.01, 1.0) == oi.DOWN


def test_absent_measurement_is_not_flat(oi):
    # Отсутствие замера и нулевое изменение — разные состояния.
    assert oi.direction_of(None, 1.0) is None
    assert oi.direction_of(0.0, 1.0) == oi.FLAT


def test_threshold_changes_the_state(oi):
    # Одно и то же окно данных при разной чувствительности даёт разные состояния —
    # это свойство модели, а не рынка, поэтому порог обязателен на входе.
    # ОИ +1.5%: выше порога 1.0 (рост, состояние 1), внутри порога 2.0 (боковик, состояние 6).
    assert oi.classify(1.5, 5.0, flat_threshold_pct=1.0, phase=oi.EARLY).state == 1
    assert oi.classify(1.5, 5.0, flat_threshold_pct=2.0).state == 6


def test_negative_threshold_is_rejected(oi):
    with pytest.raises(oi.StateError, match="отрицательным"):
        oi.direction_of(1.0, -1.0)


# --------------------------------------------------------------------------
# фаза
# --------------------------------------------------------------------------


def test_phase_from_range_position(oi):
    assert oi.phase_from_range_position(10.0) == oi.EARLY
    assert oi.phase_from_range_position(90.0) == oi.LATE
    assert oi.phase_from_range_position(50.0) is None  # середина не определена
    assert oi.phase_from_range_position(None) is None


def test_phase_boundaries_are_inclusive(oi):
    assert oi.phase_from_range_position(33.0) == oi.EARLY
    assert oi.phase_from_range_position(67.0) == oi.LATE


def test_phase_thresholds_must_not_overlap(oi):
    with pytest.raises(oi.StateError, match="строго меньше"):
        oi.phase_from_range_position(50.0, early_max_pct=70.0, late_min_pct=30.0)


def test_missing_phase_blocks_the_trade_but_names_the_state(oi):
    v = oi.classify(5.0, 5.0, flat_threshold_pct=1.0)
    assert v.state == 1
    assert v.tradable is False
    assert v.direction is None
    assert "фаза не задана" in v.reason


def test_missing_phase_blocks_falling_pair_too(oi):
    v = oi.classify(-5.0, -5.0, flat_threshold_pct=1.0)
    assert v.state == 4
    assert v.tradable is False


def test_unknown_phase_is_rejected(oi):
    with pytest.raises(oi.StateError, match="не распознана"):
        oi.classify(5.0, 5.0, flat_threshold_pct=1.0, phase="середина")


# --------------------------------------------------------------------------
# девять состояний
# --------------------------------------------------------------------------


def test_state_1_early_bull_is_long(oi):
    v = oi.classify(5.0, 5.0, flat_threshold_pct=1.0, phase=oi.EARLY)
    assert (v.state, v.tradable, v.direction) == (1, True, "long")


def test_state_2_fomo_closes_longs_but_gives_no_short(oi):
    v = oi.classify(5.0, 5.0, flat_threshold_pct=1.0, phase=oi.LATE)
    assert v.state == 2
    assert v.action == "закрывать лонги"
    assert v.tradable is False
    assert v.direction is None


def test_state_3_is_never_traded(oi):
    for phase in (None, oi.EARLY, oi.LATE):
        v = oi.classify(5.0, -5.0, flat_threshold_pct=1.0, phase=phase)
        assert (v.state, v.tradable, v.direction) == (3, False, None)


def test_state_4_early_is_short_late_is_bounce(oi):
    early = oi.classify(-5.0, -5.0, flat_threshold_pct=1.0, phase=oi.EARLY)
    assert (early.state, early.direction) == (4, "short")
    late = oi.classify(-5.0, -5.0, flat_threshold_pct=1.0, phase=oi.LATE)
    assert (late.state, late.direction) == (4, "long")
    assert any("контртрендовая" in c for c in late.cautions)
    assert any("не равен развороту" in c for c in late.cautions)


def test_state_5_weak_bull_is_short(oi):
    v = oi.classify(-5.0, 5.0, flat_threshold_pct=1.0)
    assert (v.state, v.tradable, v.direction) == (5, True, "short")


def test_state_6_distribution_is_short(oi):
    v = oi.classify(0.0, 5.0, flat_threshold_pct=1.0)
    assert (v.state, v.direction) == (6, "short")


def test_state_7_accumulation_is_long(oi):
    v = oi.classify(0.0, -5.0, flat_threshold_pct=1.0)
    assert (v.state, v.direction) == (7, "long")


def test_state_8_requires_context(oi):
    v = oi.classify(5.0, 0.0, flat_threshold_pct=1.0)
    assert (v.state, v.tradable, v.direction) == (8, False, None)
    assert any("фандинг" in c for c in v.cautions)


def test_state_9_is_not_traded(oi):
    v = oi.classify(-5.0, 0.0, flat_threshold_pct=1.0)
    assert (v.state, v.tradable) == (9, False)


def test_all_nine_states_are_reachable(oi):
    seen = set()
    for oi_pct in (5.0, 0.0, -5.0):
        for price_pct in (5.0, 0.0, -5.0):
            for phase in (oi.EARLY, oi.LATE):
                seen.add(oi.classify(oi_pct, price_pct, flat_threshold_pct=1.0, phase=phase).state)
    assert seen == set(range(1, 10))


def test_assumptions_travel_with_the_verdict(oi):
    v = oi.classify(5.0, 5.0, flat_threshold_pct=1.5, phase=oi.EARLY, window="4h")
    assert v.assumptions["window"] == "4h"
    assert v.assumptions["flat_threshold_pct"] == 1.5
    assert v.assumptions["oi_change_pct"] == 5.0


# --------------------------------------------------------------------------
# нет данных
# --------------------------------------------------------------------------


def test_missing_oi_yields_no_state(oi):
    v = oi.classify(None, 5.0, flat_threshold_pct=1.0)
    assert v.state == 0
    assert v.tradable is False
    assert "нет данных" in v.reason and "ОИ" in v.reason


def test_missing_both_names_both(oi):
    v = oi.classify(None, None, flat_threshold_pct=1.0)
    assert "ОИ" in v.reason and "цена" in v.reason


# --------------------------------------------------------------------------
# сценарии боковика
# --------------------------------------------------------------------------


def test_oi_up_futures_delta_down_expects_break_down(oi):
    s = oi.oi_delta_divergence(oi.UP, oi.DOWN)
    assert s.expectation == oi.DOWN
    assert s.resolved


def test_oi_down_futures_delta_up_expects_squeeze_up(oi):
    assert oi.oi_delta_divergence(oi.DOWN, oi.UP).expectation == oi.UP


def test_aligned_oi_and_delta_is_not_a_signal(oi):
    s = oi.oi_delta_divergence(oi.UP, oi.UP)
    assert s.expectation is None
    assert not s.resolved


def test_futures_up_spot_down_is_false_breakout_up(oi):
    assert oi.cvd_divergence(oi.DOWN, oi.UP).expectation == oi.DOWN


def test_spot_up_futures_down_is_false_breakout_down(oi):
    assert oi.cvd_divergence(oi.UP, oi.DOWN).expectation == oi.UP


def test_funding_up_positioning_down_expects_break_down(oi):
    assert oi.funding_positioning(oi.UP, oi.DOWN).expectation == oi.DOWN


def test_funding_down_positioning_up_expects_squeeze(oi):
    assert oi.funding_positioning(oi.DOWN, oi.UP).expectation == oi.UP


def test_absent_scenario_input_is_reported_as_no_data(oi):
    s = oi.funding_positioning(None, oi.UP)
    assert s.expectation is None
    assert "нет данных" in s.reason
    assert "funding" in s.reason


# --------------------------------------------------------------------------
# сведение сценариев
# --------------------------------------------------------------------------


def test_confluence_agrees(oi):
    result = oi.confluence(
        [oi.oi_delta_divergence(oi.UP, oi.DOWN), oi.funding_positioning(oi.UP, oi.DOWN)]
    )
    assert result["expectation"] == oi.DOWN
    assert "согласие 2 из 2" in result["note"]


def test_confluence_names_the_conflict_instead_of_voting(oi):
    result = oi.confluence(
        [oi.oi_delta_divergence(oi.UP, oi.DOWN), oi.funding_positioning(oi.DOWN, oi.UP)]
    )
    assert result["expectation"] is None
    assert "противоречат" in result["note"]
    assert "приоритет стратегией не задан" in result["note"]


def test_confluence_separates_pending_from_resolved(oi):
    result = oi.confluence(
        [oi.oi_delta_divergence(oi.UP, oi.DOWN), oi.funding_positioning(None, None)]
    )
    assert len(result["resolved"]) == 1
    assert len(result["pending"]) == 1


def test_confluence_with_nothing_resolved(oi):
    result = oi.confluence([oi.funding_positioning(None, None)])
    assert result["expectation"] is None
    assert "ни один сценарий не разрешён" in result["note"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_requires_flat_threshold(oi, capsys):
    with pytest.raises(SystemExit):
        oi.main(["--oi-change", "5", "--price-change", "5"])


def test_cli_tradable_setup_exits_zero(oi, capsys):
    rc = oi.main(
        ["--oi-change", "5", "--price-change", "5", "--flat-threshold", "1",
         "--range-position", "10", "--window", "4h"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Состояние: 1" in out
    assert "Направление: long" in out
    assert "порог боковика 1.0%" in out


def test_cli_untradable_setup_exits_nonzero(oi, capsys):
    rc = oi.main(["--oi-change", "5", "--price-change", "-5", "--flat-threshold", "1"])
    assert rc == 1
    assert "не торговать" in capsys.readouterr().out


def test_cli_json_output(oi, capsys):
    import json

    rc = oi.main(
        ["--oi-change", "-5", "--price-change", "5", "--flat-threshold", "1", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"]["state"] == 5
    assert payload["verdict"]["direction"] == "short"


# --------------------------------------------------------------------------
# ОИ в монетах против ОИ в долларах
# --------------------------------------------------------------------------


def test_pure_revaluation_when_coins_flat(oi):
    # Число контрактов не изменилось, доллар ОИ вырос на 20% ровно вслед за ценой.
    r = oi.decompose_oi_usd(1000.0, 1200.0, 100.0, 120.0)
    assert r.oi_coins_change_pct == pytest.approx(0.0, abs=1e-9)
    assert r.driver == "чистая переоценка"
    assert "новых денег не приходило" in r.reading


def test_real_inflow_when_coins_and_usd_agree(oi):
    r = oi.decompose_oi_usd(1000.0, 1500.0, 100.0, 120.0)
    assert r.oi_coins_change_pct == pytest.approx(25.0)
    assert r.driver == "реальный приток"


def test_real_outflow(oi):
    r = oi.decompose_oi_usd(1000.0, 700.0, 100.0, 90.0)
    assert r.oi_coins_change_pct < 0
    assert r.driver == "реальный отток"


def test_closing_masked_by_rising_price(oi):
    # Доллар ОИ вырос на 10%, но цена выросла на 30% — контрактов стало меньше.
    r = oi.decompose_oi_usd(1000.0, 1100.0, 100.0, 130.0)
    assert r.oi_usd_change_pct > 0
    assert r.oi_coins_change_pct < 0
    assert r.driver == "закрытие позиций замаскировано ростом цены"
    assert "ложное" in r.reading


def test_opening_masked_by_falling_price(oi):
    # Доллар ОИ упал на 10%, но цена упала на 30% — контрактов стало больше.
    r = oi.decompose_oi_usd(1000.0, 900.0, 100.0, 70.0)
    assert r.oi_usd_change_pct < 0
    assert r.oi_coins_change_pct > 0
    assert r.driver == "открытие позиций замаскировано падением цены"


def test_flat_usd_oi_is_reported_as_flat(oi):
    r = oi.decompose_oi_usd(1000.0, 1002.0, 100.0, 100.0, flat_threshold_pct=1.0)
    assert r.driver == "без изменений"


def test_decompose_usd_rejects_nonpositive_inputs(oi):
    with pytest.raises(oi.StateError, match="положительной"):
        oi.decompose_oi_usd(0.0, 1000.0, 100.0, 110.0)
    with pytest.raises(oi.StateError, match="цена после"):
        oi.decompose_oi_usd(1000.0, 1100.0, 100.0, -1.0)


def test_cli_decompose_usd_subcommand(oi, capsys):
    rc = oi.main([
        "decompose-usd", "--oi-usd-before", "1000", "--oi-usd-after", "1100",
        "--price-before", "100", "--price-after", "130",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "замаскировано ростом цены" in out


def test_cli_decompose_usd_json(oi, capsys):
    oi.main([
        "decompose-usd", "--oi-usd-before", "1000", "--oi-usd-after", "1500",
        "--price-before", "100", "--price-after", "120", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert payload["driver"] == "реальный приток"
    assert payload["oi_coins_change_pct"] == pytest.approx(25.0)


def test_cli_decompose_usd_bad_input_exits_two(oi, capsys):
    rc = oi.main([
        "decompose-usd", "--oi-usd-before", "-1", "--oi-usd-after", "1000",
        "--price-before", "100", "--price-after", "100",
    ])
    assert rc == 2


def test_existing_classify_cli_unaffected_by_new_subcommand(oi, capsys):
    # Флат-CLI без "decompose-usd" первым аргументом обязан работать как раньше.
    rc = oi.main(["--oi-change", "5", "--price-change", "5", "--flat-threshold", "1"])
    assert rc == 1


def test_cli_reports_missing_measurements(oi, capsys):
    rc = oi.main(["--price-change", "5", "--flat-threshold", "1"])
    assert rc == 1
    assert "нет данных" in capsys.readouterr().out
