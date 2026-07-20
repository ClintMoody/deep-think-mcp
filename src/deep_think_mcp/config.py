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


# [F7 SECURITY] Per-session `overrides` can redirect the [subagent]/[autopilot]
# endpoint to a caller-chosen URL. To keep the OPERATOR's api_key from being
# POSTed to such a URL, `load_config` records the endpoints the operator
# configured (packaged defaults + user config, BEFORE overrides) under this
# reserved key; the adapter-construction seams consult it via
# `api_key_allowed_for()`. The operator's key travels ONLY to operator-
# configured endpoints -- an override-injected endpoint runs keyless.
_OPERATOR_ENDPOINTS_KEY = "_operator_endpoints"


def _subagent_endpoints(cfg: dict[str, Any]) -> list[str]:
    """The [subagent] endpoint(s) as `endpoints_from_cfg` would resolve them
    (list wins over single, blanks dropped). Kept here -- not imported from
    subagent_engine -- so config.py stays dependency-free."""
    sub = cfg.get("subagent", {})
    multi = [str(e).strip() for e in (sub.get("endpoints") or []) if str(e).strip()]
    if multi:
        return multi
    single = str(sub.get("endpoint", "") or "").strip()
    return [single] if single else []


def _autopilot_endpoint(cfg: dict[str, Any]) -> str:
    return str(cfg.get("autopilot", {}).get("endpoint", "") or "").strip()


def api_key_allowed_for(cfg: dict[str, Any], section: str, endpoint: str) -> bool:
    """Whether the operator's api_key may travel to `endpoint` for `section`
    ("subagent" | "autopilot").

    `False` when `endpoint` appears only because of per-session overrides --
    i.e. it is NOT among the endpoints the operator configured, recorded by
    `load_config` (before overrides were merged). When no override marker is
    present (no overrides were applied) every endpoint is operator-configured,
    so `True`. See F7 / SECURITY.
    """
    marker = cfg.get(_OPERATOR_ENDPOINTS_KEY)
    if not isinstance(marker, dict):
        return True
    endpoint = str(endpoint or "").strip()
    if section == "subagent":
        return endpoint in set(marker.get("subagent") or [])
    if section == "autopilot":
        return endpoint == str(marker.get("autopilot") or "")
    return True


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
        # [F7 SECURITY] `merged` is the operator config (defaults + user config,
        # NO overrides) -- record its endpoints so `api_key_allowed_for` can
        # tell an override-injected endpoint from an operator-trusted one.
        operator_marker = {
            "subagent": _subagent_endpoints(merged),
            "autopilot": _autopilot_endpoint(merged),
        }
        merged = _deep_merge(merged, overrides)
        merged[_OPERATOR_ENDPOINTS_KEY] = operator_marker

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
