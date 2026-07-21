"""Tests for the CLI/transport wiring added for the Streamable HTTP daemon.

These exercise argument parsing and settings application only — they never
bind a socket or call ``FastMCP.run``, so they stay fast and hermetic.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from deep_think_mcp.server import _configure_transport, _parse_args


def test_default_transport_is_stdio():
    """No args → stdio, preserving the historical entrypoint behaviour."""
    args = _parse_args([])
    assert args.transport == "stdio"

    server = FastMCP("test")
    default_host = server.settings.host
    default_port = server.settings.port

    transport = _configure_transport(server, args)

    assert transport == "stdio"
    # stdio must not touch HTTP bind settings.
    assert server.settings.host == default_host
    assert server.settings.port == default_port


def test_streamable_http_applies_host_port_path():
    args = _parse_args(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9999",
            "--path",
            "/dt",
        ]
    )
    server = FastMCP("test")
    transport = _configure_transport(server, args)

    assert transport == "streamable-http"
    assert server.settings.host == "0.0.0.0"
    assert server.settings.port == 9999
    assert server.settings.streamable_http_path == "/dt"
    assert server.settings.stateless_http is False


def test_defaults_target_local_daemon_on_8182_mcp():
    """The daemon defaults line up with the shipped systemd unit + Hermes url."""
    args = _parse_args(["--transport", "streamable-http"])
    server = FastMCP("test")
    _configure_transport(server, args)

    assert server.settings.host == "127.0.0.1"
    assert server.settings.port == 8182
    assert server.settings.streamable_http_path == "/mcp"


def test_stateless_flag():
    args = _parse_args(["--transport", "streamable-http", "--stateless"])
    server = FastMCP("test")
    _configure_transport(server, args)
    assert server.settings.stateless_http is True


def test_env_var_fallbacks(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("DEEP_THINK_MCP_PORT", "7000")
    monkeypatch.setenv("DEEP_THINK_MCP_HOST", "127.0.0.5")
    monkeypatch.setenv("DEEP_THINK_MCP_STATELESS", "true")

    args = _parse_args([])
    assert args.transport == "streamable-http"
    assert args.port == 7000
    assert args.host == "127.0.0.5"
    assert args.stateless is True
