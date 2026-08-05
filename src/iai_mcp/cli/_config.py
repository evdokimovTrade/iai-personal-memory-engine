"""`iai-mcp config` — the identity block of ~/.iai-mcp/config.json.

The L0 anchor is seeded from this file and served at the head of every
session, so it is the one piece of memory the user writes by hand. Until
now the only way to set it was to hand-edit JSON that no command created,
while the unconfigured anchor told the user to run a command that did not
exist.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from iai_mcp.core._identity import (
    IDENTITY_FIELDS,
    identity_config_path,
    identity_language,
    load_identity_config,
    render_identity_surface,
)

logger = logging.getLogger(__name__)

# A whole system instruction is a legitimate `extra`, a pasted repository is
# not: the anchor is prepended to every session, so its cost is paid on each
# one.
EXTRA_SOFT_LIMIT_CHARS = 8000


class ConfigReadError(Exception):
    """config.json exists but cannot be parsed — refuse to overwrite it."""


def _read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigReadError(f"cannot read {path}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ConfigReadError(f"{path} is not a JSON object")
    return cfg


def _write_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    # The identity can carry personal detail the rest of the store encrypts;
    # at minimum keep it off other local accounts.
    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # noqa: BLE001 -- best effort, e.g. on odd mounts
        logger.debug("config chmod failed: %s", exc)


def _print_identity(identity: dict, path: Path) -> None:
    print(f"config: {path}")
    if not identity:
        print("identity: not configured")
        print("  set it with: iai-mcp config identity --name ... --role ...")
        return
    print("identity:")
    for field in IDENTITY_FIELDS:
        value = identity.get(field)
        if not value:
            continue
        value = str(value)
        if field == "extra" and len(value) > 200:
            head = value[:200].replace("\n", " ")
            print(f"  {field}: {head}... ({len(value)} chars)")
        else:
            print(f"  {field}: {value}")
    surface = render_identity_surface(identity)
    print(f"anchor language: {identity_language(surface)}")
    print(f"anchor size: {len(surface)} chars")


def cmd_config_identity(args: argparse.Namespace) -> int:
    path = identity_config_path()

    updates: dict[str, str] = {}
    for field in ("name", "languages", "role", "project"):
        value = getattr(args, field, None)
        if value is not None:
            updates[field] = value

    if getattr(args, "extra", None) is not None and getattr(args, "extra_file", None):
        print("config: pass --extra or --extra-file, not both", file=sys.stderr)
        return 2

    if getattr(args, "extra", None) is not None:
        updates["extra"] = args.extra
    elif getattr(args, "extra_file", None):
        extra_path = Path(args.extra_file).expanduser()
        try:
            updates["extra"] = extra_path.read_text().strip()
        except OSError as exc:
            print(f"config: cannot read {extra_path}: {exc}", file=sys.stderr)
            return 1

    if args.clear_extra:
        if "extra" in updates:
            print("config: --clear-extra conflicts with --extra/--extra-file",
                  file=sys.stderr)
            return 2
        updates["extra"] = ""

    if not updates:
        _print_identity(load_identity_config(), path)
        return 0

    try:
        cfg = _read_config(path)
    except ConfigReadError as exc:
        # Rewriting from {} here would drop the timezone and anything else the
        # file holds, so a hand-edit gone wrong stays the user's to fix.
        print(f"config: {exc}", file=sys.stderr)
        return 1

    identity = cfg.get("identity")
    if not isinstance(identity, dict):
        identity = {}
    for field, value in updates.items():
        if value == "":
            identity.pop(field, None)
        else:
            identity[field] = value
    cfg["identity"] = identity
    _write_config(path, cfg)

    surface = render_identity_surface(identity)
    if len(surface) > EXTRA_SOFT_LIMIT_CHARS:
        print(
            f"config: warning — the anchor is {len(surface)} chars and is "
            f"served at the head of every session; consider keeping it under "
            f"{EXTRA_SOFT_LIMIT_CHARS}",
            file=sys.stderr,
        )

    _print_identity(identity, path)

    if args.no_refresh:
        print("anchor: not refreshed (--no-refresh); it updates on next boot")
        return 0

    try:
        from iai_mcp.core._identity import refresh_l0_identity
        from iai_mcp.store import MemoryStore
        store = MemoryStore()
        try:
            # close(), not just refresh: record writes go through a buffer that
            # a short-lived CLI process would otherwise drop on exit.
            changed = refresh_l0_identity(store)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 -- config is written; the store is secondary
        print(f"anchor: not refreshed ({type(exc).__name__}: {exc}); "
              f"it updates on next boot", file=sys.stderr)
        return 0

    print("anchor: refreshed" if changed else "anchor: already current")
    return 0
