"""F7 regression: the operator's api_key must never travel to a caller-chosen
endpoint injected via per-session `overrides`.

The containment decision lives at the config-resolution seam
(`config.api_key_allowed_for`, fed by a marker `load_config` stamps when
overrides are applied), so BOTH engine paths are covered:

  - subagent necort  -> `subagent_engine._make_adapter`
  - autopilot        -> `autopilot.client_from_cfg`

No network: the necort adapter only loads the vendored core (offline) and we
inspect its `api_key` (the sole source of the `Authorization: Bearer` header,
necort_adapter.py:437-438); the autopilot client's `api_key` is likewise the
sole source of its header (autopilot.py:127). Both are asserted None for an
override-injected endpoint and present for the operator's own endpoint.
"""

from __future__ import annotations

from deep_think_mcp import autopilot, config, subagent_engine


# ---------------------------------------------------------------------------
# The pure containment predicate
# ---------------------------------------------------------------------------


def test_api_key_allowed_when_no_override_marker():
    # No overrides applied -> no marker -> every endpoint is operator-configured.
    cfg = {"subagent": {"endpoints": ["http://x/v1"]}}
    assert config.api_key_allowed_for(cfg, "subagent", "http://x/v1") is True


def test_api_key_denied_for_override_injected_subagent_endpoint():
    cfg = {
        "subagent": {"endpoints": ["http://attacker/v1"], "api_key": "sk-op"},
        config._OPERATOR_ENDPOINTS_KEY: {
            "subagent": ["http://operator/v1"],
            "autopilot": "",
        },
    }
    assert config.api_key_allowed_for(cfg, "subagent", "http://attacker/v1") is False
    assert config.api_key_allowed_for(cfg, "subagent", "http://operator/v1") is True


def test_api_key_denied_for_override_injected_autopilot_endpoint():
    cfg = {
        config._OPERATOR_ENDPOINTS_KEY: {
            "subagent": [],
            "autopilot": "http://operator/v1",
        }
    }
    assert config.api_key_allowed_for(cfg, "autopilot", "http://attacker/v1") is False
    assert config.api_key_allowed_for(cfg, "autopilot", "http://operator/v1") is True


# ---------------------------------------------------------------------------
# load_config records operator endpoints only when overrides are applied
# ---------------------------------------------------------------------------


def test_load_config_stamps_operator_endpoints_only_with_overrides(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[subagent]\nendpoint = "http://operator/v1"\napi_key = "sk-op"\n'
    )
    plain = config.load_config(root=tmp_path)
    assert config._OPERATOR_ENDPOINTS_KEY not in plain

    overridden = config.load_config(
        root=tmp_path, overrides={"subagent": {"endpoints": ["http://attacker/v1"]}}
    )
    marker = overridden[config._OPERATOR_ENDPOINTS_KEY]
    assert marker["subagent"] == ["http://operator/v1"]


# ---------------------------------------------------------------------------
# Seam 1: subagent necort adapter
# ---------------------------------------------------------------------------


def _subagent_config_toml(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[subagent]\n"
        'engine = "necort"\n'
        'endpoint = "http://operator/v1"\n'
        'api_key = "sk-operator-secret"\n'
        'model = "m"\n'
    )


def test_make_adapter_drops_operator_key_for_overridden_endpoint(tmp_path):
    _subagent_config_toml(tmp_path)
    cfg = config.load_config(
        root=tmp_path, overrides={"subagent": {"endpoints": ["http://attacker/v1"]}}
    )
    endpoint = subagent_engine.endpoints_from_cfg(cfg)[0]
    assert endpoint == "http://attacker/v1"

    adapter = subagent_engine._make_adapter(endpoint, cfg, ["Analysis"])
    # No Authorization header will be built (necort_adapter.py:437 keys off this).
    assert adapter.api_key is None


def test_make_adapter_keeps_operator_key_for_operator_endpoint(tmp_path):
    _subagent_config_toml(tmp_path)
    # Overrides present (marker stamped) but they do NOT redirect the endpoint.
    cfg = config.load_config(
        root=tmp_path, overrides={"subagent": {"model": "other"}}
    )
    endpoint = subagent_engine.endpoints_from_cfg(cfg)[0]
    assert endpoint == "http://operator/v1"

    adapter = subagent_engine._make_adapter(endpoint, cfg, ["Analysis"])
    assert adapter.api_key == "sk-operator-secret"


# ---------------------------------------------------------------------------
# Seam 2: autopilot client
# ---------------------------------------------------------------------------


def _autopilot_config_toml(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[autopilot]\n"
        "enabled = true\n"
        'endpoint = "http://operator/v1"\n'
        'api_key = "sk-operator-secret"\n'
        'model = "m"\n'
    )


def test_client_from_cfg_drops_operator_key_for_overridden_endpoint(tmp_path):
    _autopilot_config_toml(tmp_path)
    cfg = config.load_config(
        root=tmp_path, overrides={"autopilot": {"endpoint": "http://attacker/v1"}}
    )
    client = autopilot.client_from_cfg(cfg)
    assert client.endpoint == "http://attacker/v1"
    # No Authorization header will be built (autopilot.py:127 keys off this).
    assert client.api_key is None


def test_client_from_cfg_keeps_operator_key_for_operator_endpoint(tmp_path):
    _autopilot_config_toml(tmp_path)
    cfg = config.load_config(
        root=tmp_path, overrides={"autopilot": {"temperature": 0.1}}
    )
    client = autopilot.client_from_cfg(cfg)
    assert client.endpoint == "http://operator/v1"
    assert client.api_key == "sk-operator-secret"
