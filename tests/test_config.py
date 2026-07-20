"""Tests for deep_think_mcp.config: layered config loading + bootstrap.

Per Global Constraints (docs/execution-plan.md), these tests must never
touch the real home directory -- every root used here is either an explicit
tmp_path or a DEEP_THINK_HOME env var pointed at a tmp_path/decoy directory.
"""

import tomllib
from pathlib import Path

from deep_think_mcp import config


# ---------------------------------------------------------------------------
# load_defaults
# ---------------------------------------------------------------------------


def test_load_defaults_has_every_config_surface_section():
    defaults = config.load_defaults()
    assert set(defaults) == {
        "store",
        "modes",
        "serial",
        "subagent",
        "stages",
        "autopilot",
    }


def test_load_defaults_matches_documented_values():
    defaults = config.load_defaults()
    assert defaults["serial"]["max_rounds"] == 3
    assert defaults["serial"]["score_threshold"] == 0.05
    assert defaults["serial"]["fast_mode"] is False
    assert defaults["subagent"]["max_rounds"] == 2
    assert defaults["subagent"]["equilibrium_threshold"] == 0.75
    assert defaults["subagent"]["sequential_fallback"] is True
    assert defaults["modes"]["default_prompt_user"] is True
    assert defaults["stages"]["default"] == [
        "Problem Definition",
        "Research",
        "Analysis",
        "Synthesis",
        "Conclusion",
    ]
    assert defaults["autopilot"]["enabled"] is False


# ---------------------------------------------------------------------------
# resolve_root precedence: DEEP_THINK_HOME > [store].root in defaults > hardcoded fallback
# ---------------------------------------------------------------------------


def test_resolve_root_uses_deep_think_home_when_set(tmp_path, monkeypatch):
    injected = tmp_path / "injected-root"
    monkeypatch.setenv("DEEP_THINK_HOME", str(injected))
    assert config.resolve_root() == injected.resolve()


def test_resolve_root_deep_think_home_wins_over_configured_store_root(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        config,
        "load_defaults",
        lambda: {"store": {"root": str(tmp_path / "config-root")}},
    )
    injected = tmp_path / "env-root"
    monkeypatch.setenv("DEEP_THINK_HOME", str(injected))
    assert config.resolve_root() == injected.resolve()


def test_resolve_root_reads_store_root_from_defaults_when_no_env_var(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DEEP_THINK_HOME", raising=False)
    custom_root = tmp_path / "custom-default-root"
    monkeypatch.setattr(
        config, "load_defaults", lambda: {"store": {"root": str(custom_root)}}
    )
    assert config.resolve_root() == custom_root.resolve()


def test_resolve_root_falls_back_to_hardcoded_default_when_store_root_missing(
    tmp_path, monkeypatch
):
    decoy_home = tmp_path / "decoy-home"
    decoy_home.mkdir()
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.delenv("DEEP_THINK_HOME", raising=False)
    monkeypatch.setattr(config, "load_defaults", lambda: {"store": {}})
    assert config.resolve_root() == (decoy_home / "deep-think-mcp").resolve()


# ---------------------------------------------------------------------------
# load_config layering precedence
# ---------------------------------------------------------------------------


def test_load_config_with_no_user_file_returns_packaged_defaults(tmp_path):
    cfg = config.load_config(root=tmp_path)
    assert cfg["serial"]["max_rounds"] == 3
    assert cfg["subagent"]["agents"] == ["Analysis", "Creativity", "Skeptic"]


def test_load_config_user_config_overrides_defaults(tmp_path):
    (tmp_path / "config.toml").write_text("[serial]\nmax_rounds = 7\n")
    cfg = config.load_config(root=tmp_path)
    assert cfg["serial"]["max_rounds"] == 7
    # Sibling keys not touched by the user file still come from defaults.
    assert cfg["serial"]["score_threshold"] == 0.05


def test_load_config_session_overrides_beat_user_config_and_defaults(tmp_path):
    (tmp_path / "config.toml").write_text("[serial]\nmax_rounds = 7\n")
    cfg = config.load_config(
        root=tmp_path, overrides={"serial": {"max_rounds": 1}}
    )
    assert cfg["serial"]["max_rounds"] == 1
    assert cfg["serial"]["score_threshold"] == 0.05


def test_load_config_default_root_honors_deep_think_home(tmp_path, monkeypatch):
    injected = tmp_path / "env-root"
    injected.mkdir()
    (injected / "config.toml").write_text("[serial]\nmax_rounds = 42\n")
    monkeypatch.setenv("DEEP_THINK_HOME", str(injected))
    cfg = config.load_config()
    assert cfg["serial"]["max_rounds"] == 42


def test_load_config_does_not_create_any_files_or_dirs(tmp_path):
    # load_config is read-only; bootstrap() is the only thing that writes.
    config.load_config(root=tmp_path)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_creates_sessions_and_logs_dirs(tmp_path):
    root = config.bootstrap(root=tmp_path)
    assert root == tmp_path
    assert (tmp_path / "sessions").is_dir()
    assert (tmp_path / "logs").is_dir()


def test_bootstrap_writes_config_toml_from_defaults_when_missing(tmp_path):
    config.bootstrap(root=tmp_path)
    written = tmp_path / "config.toml"
    assert written.is_file()
    parsed = tomllib.loads(written.read_text())
    assert parsed == config.load_defaults()


def test_bootstrap_is_idempotent_and_does_not_clobber_existing_config(tmp_path):
    config.bootstrap(root=tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[serial]\nmax_rounds = 99\n")

    config.bootstrap(root=tmp_path)  # second call must be a no-op on config.toml

    parsed = tomllib.loads(config_path.read_text())
    assert parsed["serial"]["max_rounds"] == 99


def test_bootstrap_second_call_does_not_error_and_dirs_stay(tmp_path):
    config.bootstrap(root=tmp_path)
    config.bootstrap(root=tmp_path)
    assert (tmp_path / "sessions").is_dir()
    assert (tmp_path / "logs").is_dir()


def test_bootstrap_default_root_honors_deep_think_home(tmp_path, monkeypatch):
    injected = tmp_path / "env-root"
    monkeypatch.setenv("DEEP_THINK_HOME", str(injected))
    root = config.bootstrap()
    assert root == injected.resolve()
    assert (injected / "sessions").is_dir()


def test_bootstrap_writes_nothing_outside_the_injected_root(tmp_path, monkeypatch):
    decoy_home = tmp_path / "decoy-home"
    decoy_home.mkdir()
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.delenv("DEEP_THINK_HOME", raising=False)

    actual_root = tmp_path / "actual-root"
    config.bootstrap(root=actual_root)

    assert not (decoy_home / "deep-think-mcp").exists()
    created = {p.relative_to(actual_root) for p in actual_root.rglob("*")}
    assert created == {Path("sessions"), Path("logs"), Path("config.toml")}
