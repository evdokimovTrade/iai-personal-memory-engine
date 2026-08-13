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
def ms(tmp_path):
    mod = _load(f"market_state_{tmp_path.name}", TOOLS / "market_state.py")
    yield mod
    sys.modules.pop(mod.__name__, None)


def _snap(ms, tf="4h", phase=None, **values):
    return ms.Snapshot(tf, dict(values), phase)


# --------------------------------------------------------------------------
# разбор таблицы
# --------------------------------------------------------------------------


def test_parses_emoji_table(ms):
    table = """
    Цена ↗️
    ОИ ↗️
    Спотовая дельта ↘️
    Фьючерсная дельта ↗️
    Long/Short Account ⬜️
    NL ↗️
    NS ↘️
    Bid ↗️
    Ask ↘️
    """
    parsed = ms.parse_table(table)
    assert parsed["price"] == ms.UP
    assert parsed["spot_cvd"] == ms.DOWN
    assert parsed["ls_account"] == ms.FLAT
    assert parsed["nl"] == ms.UP and parsed["ns"] == ms.DOWN
    assert parsed["bid_delta"] == ms.UP and parsed["ask_delta"] == ms.DOWN
    assert len(parsed) == 9


def test_variation_selector_is_stripped(ms):
    assert ms.parse_direction("↗️") == ms.UP
    assert ms.parse_direction("⬜️") == ms.FLAT
    assert ms.parse_direction("↘") == ms.DOWN


def test_word_and_ascii_forms(ms):
    assert ms.parse_direction("рост") == ms.UP
    assert ms.parse_direction("боковик") == ms.FLAT
    assert ms.parse_direction("down") == ms.DOWN
    assert ms.parse_direction("=") == ms.FLAT


def test_colon_and_alias_forms(ms):
    parsed = ms.parse_table("цена: ↗️\nои: ↘️\nфьюч: ⬜️")
    assert parsed == {"price": ms.UP, "oi": ms.DOWN, "futures_cvd": ms.FLAT}


def test_comments_and_blank_lines_ignored(ms):
    assert ms.parse_table("# BTC 4h\n\nцена ↗️\n") == {"price": ms.UP}


def test_unknown_indicator_is_rejected(ms):
    with pytest.raises(ms.TableError, match="не распознан"):
        ms.parse_table("дельта китов ↗️")


def test_unknown_direction_is_rejected(ms):
    with pytest.raises(ms.TableError, match="направление"):
        ms.parse_table("цена вбок-вверх-наискосок")


def test_conflicting_duplicate_is_rejected(ms):
    with pytest.raises(ms.TableError, match="дважды"):
        ms.parse_table("цена ↗️\nцена ↘️")


# --------------------------------------------------------------------------
# правила
# --------------------------------------------------------------------------


def test_oi_up_futures_down_is_short(ms):
    f = ms.rule_oi_vs_futures(_snap(ms, oi=ms.UP, futures_cvd=ms.DOWN))
    assert f.bias == ms.SHORT and f.tier == 2


def test_price_up_spot_down_is_short(ms):
    f = ms.rule_price_vs_spot(_snap(ms, price=ms.UP, spot_cvd=ms.DOWN))
    assert f.bias == ms.SHORT
    assert "не обеспечен" in f.reading


def test_price_down_spot_up_is_accumulation(ms):
    f = ms.rule_price_vs_spot(_snap(ms, price=ms.DOWN, spot_cvd=ms.UP))
    assert f.bias == ms.LONG and "накопление" in f.reading


def test_aligned_price_and_spot_is_confirmation(ms):
    f = ms.rule_price_vs_spot(_snap(ms, price=ms.UP, spot_cvd=ms.UP))
    assert f.bias == ms.LONG and "подтверждает" in f.reading


def test_price_up_futures_down_is_short_squeeze_fuel(ms):
    f = ms.rule_price_vs_futures(_snap(ms, price=ms.UP, futures_cvd=ms.DOWN))
    assert f.bias == ms.SHORT
    assert f.caution and "продолжаться" in f.caution


def test_crowd_shorting_a_rise_is_long_fuel(ms):
    f = ms.rule_crowd(_snap(ms, price=ms.UP, ls_account=ms.DOWN))
    assert f.bias == ms.LONG and "сквиз" in f.reading


def test_crowd_longing_a_rise_is_overheated(ms):
    f = ms.rule_crowd(_snap(ms, price=ms.UP, ls_account=ms.UP))
    assert f.bias == ms.SHORT and "перегрев" in f.reading


def test_crowd_catching_a_knife_is_short(ms):
    assert ms.rule_crowd(_snap(ms, price=ms.DOWN, ls_account=ms.UP)).bias == ms.SHORT


def test_crowd_capitulation_is_bounce(ms):
    assert ms.rule_crowd(_snap(ms, price=ms.DOWN, ls_account=ms.DOWN)).bias == ms.LONG


def test_netoe_rule_is_unresolved_pending_definition(ms):
    f = ms.rule_netoe(_snap(ms, nl=ms.UP, ns=ms.DOWN))
    assert f.resolved is False
    assert f.bias is None
    assert "знаковая договорённость" in f.reading
    assert f.weight == 0.0


def test_orderbook_is_timing_tier_only(ms):
    f = ms.rule_orderbook(_snap(ms, bid_delta=ms.UP, ask_delta=ms.DOWN))
    assert f.tier == 4 and f.bias == ms.LONG
    assert f.caution and "тайминг" in f.caution


def test_missing_input_makes_rule_unresolved(ms):
    f = ms.rule_crowd(_snap(ms, price=ms.UP))
    assert f.resolved is False
    assert "нет данных" in f.reading
    assert "Long/Short Account" in f.reading
    assert f.weight == 0.0


# --------------------------------------------------------------------------
# анализ и вето
# --------------------------------------------------------------------------


def test_state_3_vetoes_regardless_of_lower_tiers(ms):
    # ОИ вверх + цена вниз = состояние 3, стратегия его не торгует.
    # Нижние ярусы единогласно за лонг — вето обязано устоять.
    snap = _snap(
        ms, price=ms.DOWN, oi=ms.UP, spot_cvd=ms.UP, futures_cvd=ms.DOWN,
        ls_account=ms.DOWN, bid_delta=ms.UP, ask_delta=ms.DOWN,
    )
    a = ms.analyze(snap)
    assert a.veto is not None
    assert a.bias is None
    assert a.score_long > a.score_short  # перевес есть, но он не решает


def test_state_9_vetoes(ms):
    a = ms.analyze(_snap(ms, price=ms.FLAT, oi=ms.DOWN))
    assert a.veto is not None and a.bias is None


def test_missing_phase_vetoes_the_trade(ms):
    a = ms.analyze(_snap(ms, price=ms.UP, oi=ms.UP))
    assert a.veto is not None
    assert "фаза" in a.veto


def test_phase_early_unlocks_state_1(ms):
    snap = _snap(ms, phase=ms.EARLY, price=ms.UP, oi=ms.UP, spot_cvd=ms.UP)
    a = ms.analyze(snap)
    assert a.veto is None
    assert a.bias == ms.LONG


def test_analysis_lists_missing_indicators(ms):
    a = ms.analyze(_snap(ms, phase=ms.EARLY, price=ms.UP, oi=ms.UP))
    assert "Спотовая дельта" in a.missing
    assert "NetOE Long" in a.missing
    assert len(a.missing) == 7


def test_scores_use_tier_weights(ms):
    # Состояние 1 (ярус 1, вес 3) + спот подтверждает (ярус 2, вес 2) = 5.
    a = ms.analyze(_snap(ms, phase=ms.EARLY, price=ms.UP, oi=ms.UP, spot_cvd=ms.UP))
    assert a.score_long == pytest.approx(5.0)
    assert a.score_short == pytest.approx(0.0)


# --------------------------------------------------------------------------
# сведение таймфреймов
# --------------------------------------------------------------------------


def test_junior_confirms_senior(ms):
    senior = ms.analyze(_snap(ms, "4h", ms.EARLY, price=ms.UP, oi=ms.UP, spot_cvd=ms.UP))
    junior = ms.analyze(_snap(ms, "15m", ms.EARLY, price=ms.UP, oi=ms.UP, spot_cvd=ms.UP))
    logic = ms.combine(senior, junior)
    assert logic.direction == ms.LONG
    assert logic.entry_allowed is True
    assert "согласны" in logic.verdict


def test_junior_cannot_reverse_senior(ms):
    senior = ms.analyze(_snap(ms, "4h", ms.EARLY, price=ms.UP, oi=ms.UP, spot_cvd=ms.UP))
    junior = ms.analyze(_snap(ms, "15m", price=ms.UP, oi=ms.DOWN, spot_cvd=ms.DOWN))
    logic = ms.combine(senior, junior)
    assert logic.direction == ms.LONG  # направление остаётся за старшим
    assert logic.entry_allowed is False
    assert "против старшего не входить" in logic.verdict


def test_senior_veto_blocks_everything(ms):
    senior = ms.analyze(_snap(ms, "4h", price=ms.DOWN, oi=ms.UP))
    junior = ms.analyze(_snap(ms, "15m", ms.EARLY, price=ms.UP, oi=ms.UP))
    logic = ms.combine(senior, junior)
    assert logic.direction is None and logic.entry_allowed is False


def test_missing_junior_blocks_entry_but_keeps_direction(ms):
    senior = ms.analyze(_snap(ms, "4h", ms.EARLY, price=ms.UP, oi=ms.UP, spot_cvd=ms.UP))
    logic = ms.combine(senior, None)
    assert logic.direction == ms.LONG
    assert logic.entry_allowed is False
    assert any("младшему ТФ" in q for q in logic.questions)


def test_questions_enumerate_missing_data(ms):
    senior = ms.analyze(_snap(ms, "4h", ms.EARLY, price=ms.UP, oi=ms.UP))
    logic = ms.combine(senior, None)
    assert any("Спотовая дельта" in q for q in logic.questions)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_cli_full_confirmation(ms, tmp_path, capsys):
    table = "цена ↗️\nои ↗️\nспот ↗️\nфьюч ↗️\nls ↘️\nbid ↗️\nask ↘️\n"
    senior = _write(tmp_path, "s.txt", table)
    junior = _write(tmp_path, "j.txt", table)
    rc = ms.main(
        ["--senior", senior, "--senior-tf", "4h", "--senior-phase", ms.EARLY,
         "--junior", junior, "--junior-tf", "15m", "--junior-phase", ms.EARLY]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Направление: long" in out
    assert "Вход: подтверждён" in out
    assert "NetOE Long" in out  # недостающее названо


def test_cli_reports_veto_and_exits_nonzero(ms, tmp_path, capsys):
    senior = _write(tmp_path, "s.txt", "цена ↘️\nои ↗️\n")
    rc = ms.main(["--senior", senior])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Вход: не подтверждён" in out
    assert "не торговать" in out


def test_cli_json_shape(ms, tmp_path, capsys):
    senior = _write(tmp_path, "s.txt", "цена ↗️\nои ↗️\nспот ↗️\n")
    ms.main(["--senior", senior, "--senior-phase", ms.EARLY, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["direction"] == "long"
    assert payload["entry_allowed"] is False
    assert isinstance(payload["senior"]["findings"], list)
    assert payload["questions"]


def test_cli_bad_table_exits_two(ms, tmp_path, capsys):
    senior = _write(tmp_path, "s.txt", "дельта китов ↗️\n")
    assert ms.main(["--senior", senior]) == 2
    assert "не распознан" in capsys.readouterr().err
