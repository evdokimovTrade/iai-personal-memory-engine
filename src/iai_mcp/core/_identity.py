"""L0 identity seed: written at boot, refreshed when config.json changes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from iai_mcp.aaak import (
    CJK,
    CYRILLIC,
    HIRAGANA_KATAKANA,
    enforce_english_raw,
    generate_aaak_index,
)
from iai_mcp.store import MemoryStore
from iai_mcp.types import MemoryRecord

L0_ID = UUID("00000000-0000-0000-0000-000000000001")

IDENTITY_FIELDS = ("name", "languages", "role", "project", "extra")

_BASE_IDENTITY_TAGS = ("identity", "l0", "pinned")

_DEFAULT_L0_SEED = (
    "User identity not yet configured. "
    "Run `iai-mcp config identity` to set your name, language, and role."
)


def identity_config_path() -> Path:
    env = os.environ.get("IAI_MCP_STORE")
    root = Path(env) if env else Path.home() / ".iai-mcp"
    return root / "config.json"


def load_identity_config() -> dict:
    """The ``identity`` block of config.json, or {} when absent/unreadable."""
    path = identity_config_path()
    if not path.is_file():
        return {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    identity = cfg.get("identity")
    return identity if isinstance(identity, dict) else {}


def render_identity_surface(identity: dict) -> str:
    parts = []
    if identity.get("name"):
        parts.append(f"User: {identity['name']}.")
    if identity.get("languages"):
        parts.append(f"Primary languages: {identity['languages']}.")
    if identity.get("role"):
        parts.append(f"Role: {identity['role']}.")
    if identity.get("project"):
        parts.append(f"Active project: {identity['project']}.")
    if identity.get("extra"):
        parts.append(str(identity["extra"]))
    if not parts:
        return _DEFAULT_L0_SEED
    return " ".join(parts)


def _load_l0_identity_seed() -> str:
    return render_identity_surface(load_identity_config())


def identity_language(surface: str) -> str:
    """Coarse script detection for the anchor's ``language`` field.

    Script, not language: it only has to be precise enough to declare the
    raw capture. A surface mixing scripts resolves to the first match in
    this order, which is also the order that disambiguates Japanese (kana
    plus han) from Chinese (han alone).
    """
    if HIRAGANA_KATAKANA.search(surface):
        return "ja"
    if CJK.search(surface):
        return "zh"
    if CYRILLIC.search(surface):
        return "ru"
    return "en"


def identity_tags(surface: str) -> list[str]:
    """Anchor tags, carrying a ``raw:<lang>`` declaration when needed.

    An identity written in the user's own language is the normal case, not
    an anomaly — a Russian or Japanese `name`/`extra` must not make the
    anchor unstorable. `raw:<lang>` is the documented way to declare a
    non-English verbatim surface, and `enforce_english_raw` honors it.
    """
    tags = list(_BASE_IDENTITY_TAGS)
    lang = identity_language(surface)
    if lang != "en":
        tags.append(f"raw:{lang}")
    return tags


def _embed_identity_surface(store: MemoryStore, surface: str) -> list[float]:
    # The identity anchor must be semantically findable: a zero-embedded seed
    # has cosine ~0 to every cue and never surfaces on identity questions.
    # Embed failure degrades to zeros (seeding must not block store init) —
    # the record stays verbatim/lexically findable either way.
    try:
        from iai_mcp.embed import embedder_for_store
        return list(embedder_for_store(store).embed(surface))
    except Exception:  # noqa: BLE001 -- init must not fail on the embedder
        return [0.0] * store.embed_dim


def _seed_l0_identity(store: MemoryStore) -> None:
    existing = store.get(L0_ID)
    if existing is not None:
        return
    now = datetime.now(timezone.utc)
    surface = _load_l0_identity_seed()
    seed = MemoryRecord(
        id=L0_ID,
        tier="semantic",
        literal_surface=surface,
        aaak_index="",
        embedding=_embed_identity_surface(store, surface),
        community_id=None,
        centrality=1.0,
        detail_level=5,
        pinned=True,
        stability=0.0,
        difficulty=0.0,
        last_reviewed=None,
        never_decay=True,
        never_merge=True,
        provenance=[],
        created_at=now,
        updated_at=now,
        tags=identity_tags(surface),
        language=identity_language(surface),
    )
    enforce_english_raw(seed)
    seed.aaak_index = generate_aaak_index(seed)
    store.insert(seed)


def refresh_l0_identity(store: MemoryStore) -> bool:
    """Bring the L0 anchor in line with config.json. True when it changed.

    Seeding is one-time by design, so without this an identity edited after
    first boot would never reach the store — the anchor would keep serving
    the placeholder that told the user to configure it.
    """
    existing = store.get(L0_ID)
    if existing is None:
        _seed_l0_identity(store)
        return True

    surface = _load_l0_identity_seed()
    if existing.literal_surface == surface:
        return False

    existing.literal_surface = surface
    existing.embedding = _embed_identity_surface(store, surface)
    existing.language = identity_language(surface)
    store.update(existing)

    # store.update carries the surface but not the tags: reconcile the
    # raw:<lang> declaration separately or a re-scripted identity keeps the
    # previous language's tag.
    desired = identity_tags(surface)
    current = list(existing.tags or [])
    stale = [t for t in current if t.startswith("raw:") and t not in desired]
    if stale:
        store.remove_tags(L0_ID, stale)
    missing = [t for t in desired if t not in current]
    if missing:
        store.add_tags(L0_ID, missing)
    return True
