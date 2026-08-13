from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "diary.py"


def _load_diary(tmp_path: Path):
    name = f"diary_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # `diary` uses dataclasses under `from __future__ import annotations`;
    # dataclasses resolves the string annotations via sys.modules[__module__],
    # so the module has to be registered before exec.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[name]
        raise
    return mod


@pytest.fixture
def diary(tmp_path):
    mod = _load_diary(tmp_path)
    yield mod
    sys.modules.pop(mod.__name__, None)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "trades.jsonl"


def _open_long(diary, ledger, **overrides):
    body = {
        "instrument": "BTCUSDT",
        "direction": "long",
        "entry": 100.0,
        "stop": 90.0,
        "target": 130.0,
        "logic": "снятие ликвидности под лоем",
        "size": 2.0,
        "risk_pct": 2.0,
        "account_equity": 1000.0,
    }
    body.update(overrides)
    return diary.append_record(ledger, "open", overrides.get("trade_id", "T1"), body)


# --------------------------------------------------------------------------
# append-only chain
# --------------------------------------------------------------------------


def test_empty_ledger_reads_as_no_records(diary, ledger):
    assert diary.read_records(ledger) == []
    assert diary.verify_chain([]) == []


def test_chain_links_successive_records(diary, ledger):
    first = _open_long(diary, ledger)
    second = diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 39.0})
    assert first["prev_hash"] == "0" * 64
    assert second["prev_hash"] == first["hash"]
    assert diary.verify_chain(diary.read_records(ledger)) == []


def test_retroactive_edit_breaks_the_chain(diary, ledger):
    _open_long(diary, ledger)
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 39.0})

    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    records[0]["body"]["entry"] = 80.0  # rewrite the decision after the outcome is known
    ledger.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for r in records)
        + "\n",
        encoding="utf-8",
    )

    errors = diary.verify_chain(diary.read_records(ledger))
    assert errors
    assert any("изменена задним числом" in e for e in errors)


def test_close_without_matching_open_is_rejected(diary, ledger):
    diary.append_record(ledger, "close", "GHOST", {"exit": 120.0, "fees": 1.0, "pnl": 39.0})
    with pytest.raises(diary.LedgerError, match="незаписанному сигналу"):
        diary.fold_trades(diary.read_records(ledger))


def test_double_close_is_rejected(diary, ledger):
    _open_long(diary, ledger)
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 39.0})
    diary.append_record(ledger, "close", "T1", {"exit": 125.0, "fees": 1.0, "pnl": 49.0})
    with pytest.raises(diary.LedgerError, match="уже закрыта"):
        diary.fold_trades(diary.read_records(ledger))


def test_malformed_record_is_reported_not_crashed(diary, ledger):
    # A hand-written record missing the hashed keys must fail verification
    # rather than raise out of the verifier.
    ledger.write_text(
        json.dumps({"event": "open", "trade_id": "T1", "body": {}}) + "\n", encoding="utf-8"
    )
    errors = diary.verify_chain(diary.read_records(ledger))
    assert errors
    assert any("hash" in e for e in errors)


def test_open_without_instrument_is_rejected(diary, ledger):
    diary.append_record(ledger, "open", "T1", {"direction": "long", "entry": 100.0})
    with pytest.raises(diary.LedgerError, match="instrument"):
        diary.fold_trades(diary.read_records(ledger))


def test_unknown_event_type_is_rejected(diary, ledger):
    ledger.write_text(json.dumps({"event": "amend", "trade_id": "T1"}) + "\n", encoding="utf-8")
    with pytest.raises(diary.LedgerError, match="неизвестный тип события"):
        diary.read_records(ledger)


# --------------------------------------------------------------------------
# arithmetic
# --------------------------------------------------------------------------


def test_long_pnl_matches_reported(diary, ledger):
    _open_long(diary, ledger)
    # (120 - 100) * 2 = 40 gross, minus 1.0 fees = 39.0
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 39.0})
    report = diary.audit(ledger)
    assert report.discrepancies == []
    assert report.ok


def test_short_pnl_inverts_the_move(diary, ledger):
    _open_long(diary, ledger, direction="short")
    # short from 100 to 90 => (100 - 90) * 2 = 20 gross, minus 1.0 = 19.0
    diary.append_record(ledger, "close", "T1", {"exit": 90.0, "fees": 1.0, "pnl": 19.0})
    assert diary.audit(ledger).discrepancies == []


def test_pnl_discrepancy_reports_both_numbers_and_delta(diary, ledger):
    _open_long(diary, ledger)
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 45.0})
    report = diary.audit(ledger)
    pnl_gaps = [d for d in report.discrepancies if d.kind == "P&L"]
    assert len(pnl_gaps) == 1
    gap = pnl_gaps[0]
    assert gap.declared == 45.0
    assert gap.computed == pytest.approx(39.0)
    assert gap.delta == pytest.approx(6.0)
    rendered = diary.render_audit(report)
    assert "45" in rendered and "39" in rendered and "разница" in rendered


def test_risk_pct_discrepancy_is_detected(diary, ledger):
    # |100 - 90| * 2 = 20 risk on 1000 equity = 2%, declared 5%
    _open_long(diary, ledger, risk_pct=5.0)
    gaps = [d for d in diary.audit(ledger).discrepancies if d.kind == "риск в % счёта"]
    assert len(gaps) == 1
    assert gaps[0].declared == 5.0
    assert gaps[0].computed == pytest.approx(2.0)


def test_declared_total_is_checked_against_line_sum(diary, ledger):
    _open_long(diary, ledger, trade_id="T1")
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 39.0})
    _open_long(diary, ledger, trade_id="T2")
    diary.append_record(ledger, "close", "T2", {"exit": 80.0, "fees": 1.0, "pnl": -41.0})

    trades = diary.fold_trades(diary.read_records(ledger))
    assert diary.audit_total(trades, -2.0) is None

    gap = diary.audit_total(trades, 10.0)
    assert gap is not None
    assert gap.declared == 10.0
    assert gap.computed == pytest.approx(-2.0)
    assert gap.delta == pytest.approx(12.0)


def test_tolerance_absorbs_exchange_rounding(diary, ledger):
    _open_long(diary, ledger)
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 39.0 + 1e-9})
    assert [d for d in diary.audit(ledger).discrepancies if d.kind == "P&L"] == []


# --------------------------------------------------------------------------
# unknown is not zero
# --------------------------------------------------------------------------


def test_missing_field_renders_as_unset_not_zero(diary, ledger):
    _open_long(diary, ledger, stop=None, target=None)
    trade = diary.fold_trades(diary.read_records(ledger))["T1"]
    assert trade.stop is None
    rendered = diary.render_trade(trade)
    assert "Стоп: не задано" in rendered
    assert "Стоп: 0" not in rendered


def test_missing_fees_are_not_treated_as_zero(diary, ledger):
    _open_long(diary, ledger)
    # Gross is 40. With fees unknown, netting against 0 would wrongly flag the
    # reported 39.0 as a 1.0 discrepancy.
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": None, "pnl": 39.0})
    report = diary.audit(ledger)
    assert any(m.check == "комиссии" and m.fields == ("fees",) for m in report.missing)
    gap = [d for d in report.discrepancies if d.kind == "P&L"][0]
    assert gap.computed == pytest.approx(40.0)
    assert gap.computed_label == "расчётный P&L брутто"


def test_missing_inputs_are_reported_as_no_data_not_computed(diary, ledger):
    _open_long(diary, ledger, size=None, risk_pct=None)
    report = diary.audit(ledger)
    risk_missing = [m for m in report.missing if m.check == "риск в % счёта"]
    assert risk_missing and "size" in risk_missing[0].fields
    assert [d for d in report.discrepancies if d.kind == "риск в % счёта"] == []
    assert "нет данных" in diary.render_audit(report).lower()


def test_zero_equity_does_not_divide(diary, ledger):
    _open_long(diary, ledger, account_equity=0.0)
    trade = diary.fold_trades(diary.read_records(ledger))["T1"]
    assert diary.expected_risk_pct(trade) is None


def test_zero_equity_is_named_rather_than_reported_as_empty(diary, ledger):
    _open_long(diary, ledger, account_equity=0.0)
    risk_missing = [m for m in diary.audit(ledger).missing if m.check == "риск в % счёта"]
    assert risk_missing
    assert risk_missing[0].fields  # never an empty tuple
    assert "0" in risk_missing[0].fields[0]


def test_unverifiable_total_is_not_reported_as_matching(diary, ledger, capsys):
    _open_long(diary, ledger)
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": None})
    diary.main(["--ledger", str(ledger), "verify", "--total", "500"])
    out = capsys.readouterr().out
    assert "не с чем сверять" in out
    assert "сходится" not in out


def test_real_zero_pnl_is_audited_not_skipped(diary, ledger):
    _open_long(diary, ledger)
    # Scratch at breakeven with known fees: gross 0, net -1.0, reported 0.0.
    diary.append_record(ledger, "close", "T1", {"exit": 100.0, "fees": 1.0, "pnl": 0.0})
    gaps = [d for d in diary.audit(ledger).discrepancies if d.kind == "P&L"]
    assert len(gaps) == 1
    assert gaps[0].delta == pytest.approx(1.0)


# --------------------------------------------------------------------------
# direction parsing and CLI
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("long", "long"), ("Buy", "long"), ("ЛОНГ", "long"), ("sell", "short"), ("Шорт", "short")],
)
def test_direction_aliases(diary, raw, expected):
    assert diary.normalize_direction(raw) == expected


def test_unknown_direction_is_rejected(diary):
    with pytest.raises(diary.LedgerError, match="не распознано"):
        diary.normalize_direction("вбок")


def test_cli_open_show_verify_roundtrip(diary, ledger, capsys):
    rc = diary.main(
        [
            "--ledger", str(ledger), "open", "T9",
            "--instrument", "ETHUSDT", "--direction", "short",
            "--entry", "3000", "--stop", "3100", "--size", "1",
            "--risk-pct", "1", "--equity", "10000",
        ]
    )
    assert rc == 0
    assert diary.main(["--ledger", str(ledger), "show", "T9"]) == 0
    out = capsys.readouterr().out
    assert "Инструмент: ETHUSDT" in out
    assert "Цель: не задано" in out
    assert "Статус: открыта" in out
    assert diary.main(["--ledger", str(ledger), "verify"]) == 0


def test_cli_close_without_open_exits_nonzero(diary, ledger, capsys):
    assert diary.main(["--ledger", str(ledger), "close", "NOPE", "--pnl", "5"]) == 2
    assert "нет такой сделки" in capsys.readouterr().err


def test_cli_verify_flags_bad_total(diary, ledger, capsys):
    _open_long(diary, ledger)
    diary.append_record(ledger, "close", "T1", {"exit": 120.0, "fees": 1.0, "pnl": 39.0})
    assert diary.main(["--ledger", str(ledger), "verify", "--total", "100"]) == 1
    assert "разница" in capsys.readouterr().out


def test_cli_import_from_collector_rows(diary, ledger, tmp_path, capsys):
    rows = [
        {
            "trade_id": "BTCUSDT-abc",
            "instrument": "BTCUSDT",
            "direction": "long",
            "entry": 100.0,
            "exit": 110.0,
            "size": 1.0,
            "fees": 0.5,
            "pnl": 9.5,
            "opened_at": "2026-08-13T10:00:00+00:00",
            "closed_at": "2026-08-13T12:00:00+00:00",
        }
    ]
    source = tmp_path / "rows.jsonl"
    source.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    assert diary.main(["--ledger", str(ledger), "import", str(source)]) == 0
    trades = diary.fold_trades(diary.read_records(ledger))
    assert trades["BTCUSDT-abc"].status == "закрыта"
    assert diary.audit(ledger).discrepancies == []

    # Re-importing the same closed row must not append a second close.
    capsys.readouterr()
    assert diary.main(["--ledger", str(ledger), "import", str(source)]) == 0
    assert "пропущено уже закрытых: 1" in capsys.readouterr().out
    assert diary.verify_chain(diary.read_records(ledger)) == []


def test_ledger_path_honours_env_override(diary, tmp_path, monkeypatch):
    target = tmp_path / "custom" / "ledger.jsonl"
    monkeypatch.setenv("IAI_TRADE_JOURNAL", str(target))
    assert diary.default_ledger_path() == target
