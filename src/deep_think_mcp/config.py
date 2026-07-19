"""Layered configuration loading for deep-think-mcp.

Three layers, lowest to highest precedence:

    packaged defaults (config/default.toml)
        < user config (<root>/config.toml, if present)
            < per-session overrides dict (e.g. start_session(overrides={...}))

Root resolution (see docs/execution-plan.md "Global Constraints"):

    DEEP_THINK_HOME env var
        > [store].root in the packaged defaults
            > ~/deep-think-mcp (hardcoded fallback)

Everything in this module is read-only except `bootstrap()`, which is the
only function that ever creates directories or files, and only ever inside
the root it is given (explicitly, or via `resolve_root()`).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

# config/default.toml lives at the repo root, alongside src/ -- not inside
# the installed package -- per docs/build-plan.md "Project layout". This
# path assumes a dev/editable checkout (the only way this project runs today).
PACKAGED_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.toml"

_USER_CONFIG_FILENAME = "config.toml"


def load_defaults() -> dict[str, Any]:
    """Parse and return the packaged config/default.toml."""
    with PACKAGED_DEFAULT_CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def resolve_root() -> Path:
    """Resolve the deep-think-mcp data root.

    Order: DEEP_THINK_HOME env var, then [store].root from the packaged
    defaults, then the hardcoded ~/deep-think-mcp fallback.
    """
    env_root = os.environ.get("DEEP_THINK_HOME")
    if env_root:
        return Path(env_root).expanduser().resolve()

    configured_root = load_defaults().get("store", {}).get("root")
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    return (Path.home() / "deep-think-mcp").resolve()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` onto `base`, returning a new dict.

    Nested dicts are merged key by key; any other value in `overlay`
    (including lists) fully replaces the corresponding value in `base`.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(
    root: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load the layered config: packaged defaults < user config < overrides.

    `root` defaults to `resolve_root()`. Read-only: if `<root>/config.toml`
    doesn't exist yet, that layer is simply skipped -- nothing is written.
    """
    root = Path(root).expanduser() if root is not None else resolve_root()

    merged = load_defaults()

    user_config_path = root / _USER_CONFIG_FILENAME
    if user_config_path.is_file():
        with user_config_path.open("rb") as f:
            user_config = tomllib.load(f)
        merged = _deep_merge(merged, user_config)

    if overrides:
        merged = _deep_merge(merged, overrides)

    return merged


def bootstrap(root: Path | str | None = None) -> Path:
    """Ensure `<root>/sessions/` and `<root>/logs/` exist, and seed
    `<root>/config.toml` from the packaged defaults if it doesn't exist yet.

    Idempotent: safe to call on every server startup. Never touches
    anything outside the resolved root, and never overwrites an existing
    `config.toml`.
    """
    root = Path(root).expanduser() if root is not None else resolve_root()

    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)

    config_path = root / _USER_CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(PACKAGED_DEFAULT_CONFIG_PATH.read_text())

    return root
