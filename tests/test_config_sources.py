"""Both config sources work: config.yaml block precedence + env fallback."""

from __future__ import annotations

import pytest

from hermes_msteams_bridge import config as cfgmod
from hermes_msteams_bridge.realtime import openai_client as rc


def test_resolve_config_prefers_block_then_env(monkeypatch):
    monkeypatch.setenv("MSTEAMS_BRIDGE_SHARED_SECRET", "from-env")
    monkeypatch.setenv("MSTEAMS_BRIDGE_PORT", "9999")
    # config.yaml block wins for the keys it sets...
    block = {"shared_secret": "from-config", "host": "0.0.0.0"}
    cfg = cfgmod.resolve_config(extra=block)
    assert cfg.shared_secret == "from-config"  # block beats env
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9999  # ...and env fills the rest


def test_resolve_config_env_only_when_block_empty(monkeypatch):
    monkeypatch.setenv("MSTEAMS_BRIDGE_SHARED_SECRET", "env-secret")
    cfg = cfgmod.resolve_config(extra={})
    assert cfg.shared_secret == "env-secret"


def test_default_ports_are_the_standin_lane_ports(monkeypatch):
    """Calling lane 9442, managed-chat lane 9444: the same numbers every StandIn plugin uses."""
    for var in ("MSTEAMS_BRIDGE_PORT", "MSTEAMS_BRIDGE_MANAGED_BOT_PORT", "MSTEAMS_BRIDGE_MANAGED_CHAT_PORT"):
        monkeypatch.delenv(var, raising=False)
    cfg = cfgmod.resolve_config(extra={"shared_secret": "s"})
    assert cfg.port == 9442
    assert cfg.managed_chat_port == 9444
    assert cfgmod.TeamsVoiceConfig(shared_secret="s").port == 9442


def test_realtime_block_overrides_env(monkeypatch):
    monkeypatch.setenv("MSTEAMS_BRIDGE_REALTIME_VOICE", "cedar")  # env
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "envkey")
    block = {
        "backend": "azure",
        "azure_endpoint": "https://res.cognitiveservices.azure.com",
        "azure_deployment": "gpt-realtime",
        "azure_api_version": "2025-04-01-preview",
        "voice": "verse",  # config.yaml beats the env voice
        "api_key": "blockkey",
    }
    cfg = rc.realtime_config_from_env(block=block)
    assert cfg.voice == "verse"
    assert cfg.api_key == "blockkey"
    assert cfg.api_key_header == "api-key"
    assert cfg.base_url.endswith("deployment=gpt-realtime")
