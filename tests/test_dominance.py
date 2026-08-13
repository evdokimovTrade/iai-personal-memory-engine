from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "dominance.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
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
def dom(tmp_path):
    mod = _load(f"dominance_{tmp_path.name}")
    yield mod
    sys.modules.pop(mod.__name__, None)


# --------------------------------------------------------------------------
# тождество D = M / T
# --------------------------------------------------------------------------


def test_identity_recovers_mcap_change(dom):
    # Рынок вырос на 10%, доля выросла на 10% => капитализация выросла на 21%.
    d = dom.decompose(50.0, 55.0, 1000.0, 1100.0)
    assert d.dominance_change_pct == pytest.approx(10.0)
    assert d.total_change_pct == pytest.approx(10.0)
    assert d.mcap_change_pct == pytest.approx(21.0)


def test_flat_market_makes_dominance_equal_mcap(dom):
    d = dom.decompose(50.0, 60.0, 1000.0, 1000.0)
    assert d.mcap_change_pct == pytest.approx(20.0)
    assert d.driver == "реальный приток"


def test_mechanical_rise_when_mcap_unchanged(dom):
    # Монета стоит, рынок сжался на 20% => доля растёт без единого доллара притока.
    d = dom.decompose(50.0, 62.5, 1000.0, 800.0)
    assert d.mcap_change_pct == pytest.approx(0.0, abs=1e-9)
    assert d.driver == "механический рост"
    assert "сжатия рынка" in d.reading


def test_dominance_up_while_coin_falls(dom):
    # Монета −10%, рынок −30% => доля растёт, хотя деньги уходят.
    d = dom.decompose(50.0, 64.2857, 1000.0, 700.0)
    assert d.mcap_change_pct == pytest.approx(-10.0, abs=0.01)
    assert d.driver == "рост доли на падении"
    assert "при оттоке денег" in d.reading


def test_dominance_down_while_coin_rises(dom):
    # Монета +10%, рынок +30% => доля падает при реальном притоке.
    d = dom.decompose(50.0, 42.3077, 1000.0, 1300.0)
    assert d.mcap_change_pct == pytest.approx(10.0, abs=0.01)
    assert d.driver == "падение доли на росте"


def test_mechanical_fall(dom):
    d = dom.decompose(50.0, 40.0, 1000.0, 1250.0)
    assert d.mcap_change_pct == pytest.approx(0.0, abs=1e-9)
    assert d.driver == "механическое падение"


def test_real_outflow(dom):
    d = dom.decompose(50.0, 45.0, 1000.0, 1000.0)
    assert d.mcap_change_pct == pytest.approx(-10.0)
    assert d.driver == "реальный отток"


def test_flat_threshold_absorbs_noise(dom):
    d = dom.decompose(50.0, 50.1, 1000.0, 1000.0, flat_threshold_pct=0.5)
    assert d.driver == "без изменений"


def test_nonpositive_inputs_rejected(dom):
    with pytest.raises(dom.DominanceError, match="положительной"):
        dom.decompose(0.0, 50.0, 1000.0, 1000.0)
    with pytest.raises(dom.DominanceError, match="TOTAL после"):
        dom.decompose(50.0, 50.0, 1000.0, -1.0)


# --------------------------------------------------------------------------
# квадранты
# --------------------------------------------------------------------------


def test_four_quadrants(dom):
    assert dom.quadrant(dom.UP, dom.UP).number == 1
    assert dom.quadrant(dom.UP, dom.DOWN).number == 2
    assert dom.quadrant(dom.DOWN, dom.UP).number == 3
    assert dom.quadrant(dom.DOWN, dom.DOWN).number == 4


def test_altseason_quadrant_is_the_only_alt_outperformance(dom):
    q = dom.quadrant(dom.UP, dom.DOWN)
    assert "альтов" in q.alt_stance


def test_risk_off_quadrant_warns_on_alts(dom):
    q = dom.quadrant(dom.DOWN, dom.UP)
    assert "быстрее биткоина" in q.regime


def test_flat_gives_no_quadrant(dom):
    assert dom.quadrant(dom.FLAT, dom.UP) is None
    assert dom.quadrant(dom.UP, dom.FLAT) is None


def test_bad_direction_rejected(dom):
    with pytest.raises(dom.DominanceError, match="не распознано"):
        dom.quadrant("вбок", dom.UP)


# --------------------------------------------------------------------------
# ротация
# --------------------------------------------------------------------------


def test_full_rotation_reaches_small_alts(dom):
    r = dom.rotation_stage(dom.DOWN, dom.UP, dom.UP, dom.UP)
    assert r.stage == "мелкие альты"
    assert len(r.confirmed) == 4
    assert r.contradictions == []


def test_btc_concentration_stage(dom):
    r = dom.rotation_stage(dom.UP, dom.DOWN, dom.DOWN, dom.DOWN)
    assert r.stage == "BTC"
    assert len(r.contradictions) == 4


def test_eth_bridge_stage(dom):
    r = dom.rotation_stage(dom.DOWN, dom.UP, dom.DOWN, dom.DOWN)
    assert r.stage == "ETH"


def test_missing_input_blocks_the_stage(dom):
    r = dom.rotation_stage(dom.DOWN, None, dom.UP, dom.UP)
    assert r.stage is None
    assert "ETH/BTC" in r.missing


def test_contradictions_are_named_not_averaged(dom):
    r = dom.rotation_stage(dom.DOWN, dom.DOWN, dom.UP, dom.UP)
    assert r.confirmed and r.contradictions
    assert any("ETH не ведёт" in c for c in r.contradictions)


# --------------------------------------------------------------------------
# ловушка OTHERS.D
# --------------------------------------------------------------------------


def test_others_trap_unresolved_without_absolute(dom):
    res = dom.others_d_trap(-9.0, None)
    assert res["resolved"] is False
    assert "сжатия знаменателя" in res["reason"]


def test_others_trap_detects_mechanical_rise(dom):
    res = dom.others_d_trap(-9.0, 0.0)
    assert "механический рост доли" in res["verdict"]


def test_others_trap_confirms_real_inflow(dom):
    res = dom.others_d_trap(-9.0, 4.0)
    assert res["verdict"] == "реальный приток в мелкие альты"


def test_others_trap_points_elsewhere_when_eth_did_not_fall(dom):
    res = dom.others_d_trap(2.0, -1.0)
    assert "top-10" in res["verdict"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_decompose(dom, capsys):
    rc = dom.main([
        "decompose", "--dom-before", "50", "--dom-after", "62.5",
        "--total-before", "1000", "--total-after", "800",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "механический рост" in out
    assert "+0.00%" in out


def test_cli_quadrant_json(dom, capsys):
    dom.main(["--json", "quadrant", "--price", "up", "--dominance", "down"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["number"] == 2


def test_cli_rotation_reports_missing(dom, capsys):
    dom.main(["rotation", "--btc-d", "down", "--ethbtc", "up"])
    out = capsys.readouterr().out
    assert "нет данных" in out
    assert "TOTAL3" in out


def test_cli_bad_input_exits_two(dom, capsys):
    rc = dom.main([
        "decompose", "--dom-before", "0", "--dom-after", "50",
        "--total-before", "1000", "--total-after", "1000",
    ])
    assert rc == 2
    assert "положительной" in capsys.readouterr().err
