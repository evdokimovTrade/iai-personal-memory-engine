"""The identity anchor: non-English surfaces, refresh, and the CLI that writes it."""

from __future__ import annotations

import json

import pytest

from iai_mcp.cli import _build_parser
from iai_mcp.core._identity import (
    L0_ID,
    _seed_l0_identity,
    identity_language,
    identity_tags,
    refresh_l0_identity,
)
from iai_mcp.store import MemoryStore


def _write_identity(root, identity: dict, **other) -> None:
    payload = dict(other)
    payload["identity"] = identity
    (root / "config.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _run_cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


def test_language_detection_by_script():
    assert identity_language("User: Alice.") == "en"
    assert identity_language("User: Максим.") == "ru"
    assert identity_language("User: 世界.") == "zh"
    # Kana beats han: Japanese mixes both scripts, Chinese uses han alone.
    assert identity_language("ユーザー: 世界.") == "ja"


def test_non_english_identity_is_tagged_raw():
    assert identity_tags("User: Alice.") == ["identity", "l0", "pinned"]
    assert identity_tags("User: Максим.") == ["identity", "l0", "pinned", "raw:ru"]


def test_seed_accepts_cyrillic_identity(tmp_path, monkeypatch):
    """A Russian identity used to raise at boot: the anchor is English-only
    unless it declares a raw capture, and the seed declared none."""
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    _write_identity(tmp_path, {"name": "Максим", "role": "трейдер"})
    store = MemoryStore(path=tmp_path)

    _seed_l0_identity(store)

    l0 = store.get(L0_ID)
    assert l0 is not None
    assert "Максим" in l0.literal_surface
    assert "raw:ru" in l0.tags
    assert l0.language == "ru"


def test_refresh_picks_up_edited_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    _write_identity(tmp_path, {"name": "Alice", "role": "developer"})
    store = MemoryStore(path=tmp_path)
    _seed_l0_identity(store)

    _write_identity(tmp_path, {"name": "Alice", "role": "developer",
                               "extra": "Ships on Fridays."})
    assert refresh_l0_identity(store) is True

    l0 = store.get(L0_ID)
    assert "Ships on Fridays." in l0.literal_surface


def test_refresh_is_a_noop_when_config_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    _write_identity(tmp_path, {"name": "Alice"})
    store = MemoryStore(path=tmp_path)
    _seed_l0_identity(store)

    assert refresh_l0_identity(store) is False


def test_refresh_seeds_when_the_anchor_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    _write_identity(tmp_path, {"name": "Alice"})
    store = MemoryStore(path=tmp_path)

    assert refresh_l0_identity(store) is True
    assert store.get(L0_ID) is not None


def test_refresh_retags_when_the_script_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    _write_identity(tmp_path, {"name": "Alice"})
    store = MemoryStore(path=tmp_path)
    _seed_l0_identity(store)
    assert "raw:ru" not in store.get(L0_ID).tags

    _write_identity(tmp_path, {"name": "Максим"})
    refresh_l0_identity(store)
    assert "raw:ru" in store.get(L0_ID).tags
    # The stored language backs the cross-lingual identity audit, so it has to
    # follow the surface rather than stay at whatever the first write said.
    assert store.get(L0_ID).language == "ru"

    _write_identity(tmp_path, {"name": "Alice"})
    refresh_l0_identity(store)
    assert "raw:ru" not in store.get(L0_ID).tags
    assert store.get(L0_ID).language == "en"


def test_cli_writes_identity_and_keeps_unrelated_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    _write_identity(tmp_path, {}, user={"timezone": "Europe/Moscow"})

    rc = _run_cli([
        "config", "identity",
        "--name", "Максим",
        "--languages", "ru",
        "--role", "трейдер, product manager",
        "--no-refresh",
    ])
    assert rc == 0

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["user"] == {"timezone": "Europe/Moscow"}
    assert cfg["identity"]["name"] == "Максим"
    assert cfg["identity"]["languages"] == "ru"
    assert "трейдер" in cfg["identity"]["role"]


def test_cli_show_reports_unconfigured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))

    assert _run_cli(["config", "identity"]) == 0
    assert "not configured" in capsys.readouterr().out


def test_cli_reads_extra_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    doc = tmp_path / "instruction.md"
    doc.write_text("Отвечай таблицами, без воды.\n", encoding="utf-8")

    rc = _run_cli([
        "config", "identity", "--extra-file", str(doc), "--no-refresh",
    ])
    assert rc == 0

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["identity"]["extra"] == "Отвечай таблицами, без воды."


def test_cli_rejects_extra_and_extra_file_together(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    doc = tmp_path / "instruction.md"
    doc.write_text("x", encoding="utf-8")

    rc = _run_cli([
        "config", "identity", "--extra", "y", "--extra-file", str(doc),
        "--no-refresh",
    ])
    assert rc == 2
    assert not (tmp_path / "config.json").exists()


def test_cli_clear_extra_drops_the_block(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    _write_identity(tmp_path, {"name": "Alice", "extra": "stale"})

    assert _run_cli(["config", "identity", "--clear-extra", "--no-refresh"]) == 0

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "extra" not in cfg["identity"]
    assert cfg["identity"]["name"] == "Alice"


def test_cli_refuses_to_overwrite_a_corrupt_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    corrupt = '{"user": {"timezone": "Europe/Moscow"'
    (tmp_path / "config.json").write_text(corrupt, encoding="utf-8")

    rc = _run_cli(["config", "identity", "--name", "Alice", "--no-refresh"])

    assert rc == 1
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == corrupt


def test_cli_missing_extra_file_fails_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))

    rc = _run_cli([
        "config", "identity", "--extra-file", str(tmp_path / "nope.md"),
        "--no-refresh",
    ])
    assert rc == 1
    assert not (tmp_path / "config.json").exists()


def test_cli_refresh_updates_the_stored_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))
    store = MemoryStore(path=tmp_path)
    _seed_l0_identity(store)
    assert "not yet configured" in store.get(L0_ID).literal_surface
    del store

    assert _run_cli(["config", "identity", "--name", "Максим"]) == 0

    assert "Максим" in MemoryStore(path=tmp_path).get(L0_ID).literal_surface


@pytest.mark.parametrize("field", ["name", "languages", "role", "project"])
def test_cli_sets_each_scalar_field(tmp_path, monkeypatch, field):
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path))

    assert _run_cli([
        "config", "identity", f"--{field}", "value", "--no-refresh",
    ]) == 0

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["identity"][field] == "value"
