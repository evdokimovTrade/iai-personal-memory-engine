from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "collectors" / "bybit_trades.py"

# Obviously-fake credentials; the signature test only needs determinism.
DUMMY_KEY = "unit-test-key"
DUMMY_SECRET = "unit-test-dummy"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses can resolve the string
    # annotations produced by `from __future__ import annotations`.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[name]
        raise
    return mod


@pytest.fixture
def collector(tmp_path):
    mod = _load_module(f"bybit_{tmp_path.name}", SCRIPT_PATH)
    yield mod
    sys.modules.pop(mod.__name__, None)


def _row(**overrides):
    row = {
        "symbol": "BTCUSDT",
        "orderId": "abc123",
        "side": "Sell",
        "qty": "2",
        "avgEntryPrice": "100",
        "avgExitPrice": "120",
        "openFee": "0.6",
        "closeFee": "0.4",
        "closedPnl": "39",
        "createdTime": "1786000000000",
        "updatedTime": "1786003600000",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


def test_missing_credentials_raise(collector, monkeypatch):
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    with pytest.raises(collector.CollectorError, match="BYBIT_API_KEY"):
        collector.env_credentials()


def test_credentials_read_from_environment(collector, monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("BYBIT_API_SECRET", DUMMY_SECRET)
    assert collector.env_credentials() == (DUMMY_KEY, DUMMY_SECRET)


def test_parser_exposes_no_credential_flags(collector):
    options = {a.dest for a in collector.build_parser()._actions}
    assert not options & {"api_key", "secret", "api_secret", "key"}


def test_signature_matches_bybit_concatenation_order(collector):
    signature = collector.build_signature(DUMMY_SECRET, "1700000000000", DUMMY_KEY, "5000", "a=1&b=2")
    expected = hmac.new(
        DUMMY_SECRET.encode(),
        f"1700000000000{DUMMY_KEY}5000a=1&b=2".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected


def test_query_string_is_sorted_and_drops_none(collector):
    query = collector._query_string({"category": "linear", "symbol": None, "limit": 50})
    assert query == "category=linear&limit=50"


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


def test_normalize_closed_long(collector):
    row = collector.normalize_row(_row())
    assert row["trade_id"] == "BTCUSDT-abc123"
    assert row["instrument"] == "BTCUSDT"
    assert row["direction"] == "long"  # closing Sell => the position was long
    assert row["entry"] == 100.0
    assert row["exit"] == 120.0
    assert row["size"] == 2.0
    assert row["fees"] == pytest.approx(1.0)
    assert row["pnl"] == 39.0
    # createdTime/updatedTime are epoch ms, one hour apart in the fixture.
    assert row["opened_at"] == "2026-08-06T07:06:40+00:00"
    assert row["closed_at"] == "2026-08-06T08:06:40+00:00"


def test_closing_buy_means_short(collector):
    assert collector.normalize_row(_row(side="Buy"))["direction"] == "short"


def test_side_means_opening_flips_the_mapping(collector):
    assert collector.resolve_direction("Buy", "opening") == "long"
    assert collector.resolve_direction("Sell", "opening") == "short"
    assert collector.resolve_direction("Buy", "closing") == "short"


def test_unparseable_side_yields_none(collector):
    assert collector.resolve_direction("", "closing") is None
    assert collector.resolve_direction("Both", "closing") is None


def test_decision_fields_are_unset_not_zero(collector):
    row = collector.normalize_row(_row())
    for field in ("stop", "target", "logic", "risk_pct", "account_equity"):
        assert row[field] is None, field


def test_missing_numeric_field_is_none_not_zero(collector):
    row = collector.normalize_row(_row(avgEntryPrice=""))
    assert row["entry"] is None


def test_partial_fee_is_unknown_not_summed(collector):
    # Only one leg reported: the total fee is genuinely unknown, so treating
    # the missing leg as 0 would understate costs.
    assert collector.normalize_row(_row(closeFee=""))["fees"] is None
    assert collector.normalize_row(_row(openFee="", closeFee=""))["fees"] is None


def test_zero_fee_is_preserved_as_zero(collector):
    assert collector.normalize_row(_row(openFee="0", closeFee="0"))["fees"] == 0.0


def test_normalize_payload_rejects_error_code(collector):
    payload = {"retCode": 10003, "retMsg": "API key is invalid", "result": {"list": []}}
    with pytest.raises(collector.CollectorError, match="retCode=10003"):
        collector.normalize_payload(payload)


def test_normalize_payload_requires_result_list(collector):
    with pytest.raises(collector.CollectorError, match="result.list"):
        collector.normalize_payload({"retCode": 0, "result": {}})


# --------------------------------------------------------------------------
# offline CLI path
# --------------------------------------------------------------------------


def test_from_file_emits_jsonl_without_network(collector, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    payload = {"retCode": 0, "result": {"list": [_row(), _row(orderId="def456", side="Buy")]}}
    source = tmp_path / "closed.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    assert collector.main(["--from-file", str(source)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [r["trade_id"] for r in lines] == ["BTCUSDT-abc123", "BTCUSDT-def456"]
    assert [r["direction"] for r in lines] == ["long", "short"]


def test_collector_output_feeds_the_diary(collector, tmp_path, capsys):
    payload = {"retCode": 0, "result": {"list": [_row()]}}
    source = tmp_path / "closed.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    collector.main(["--from-file", str(source)])
    rows_path = tmp_path / "rows.jsonl"
    rows_path.write_text(capsys.readouterr().out, encoding="utf-8")

    diary = _load_module(
        f"diary_from_collector_{tmp_path.name}",
        Path(__file__).resolve().parents[1] / "tools" / "diary.py",
    )
    ledger = tmp_path / "trades.jsonl"
    assert diary.main(["--ledger", str(ledger), "import", str(rows_path)]) == 0
    report = diary.audit(ledger)
    # (120 - 100) * 2 - 1.0 fees = 39.0, matching the reported closedPnl.
    assert report.discrepancies == []
    assert report.chain_ok
    # Stop and equity never existed on the exchange side, so risk stays unknown.
    assert any(m.check == "риск в % счёта" for m in report.missing)
