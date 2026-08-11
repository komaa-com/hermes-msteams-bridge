"""Round-8 review fixes: adapter contract, offline smoke, auth alignment,
voice-delivery rate limit, honest status, consult browser-session pinning."""

from __future__ import annotations

import asyncio
import json

import pytest

from hermes_msteams_bridge.config import TeamsVoiceConfig

try:
    import gateway.platforms.base  # noqa: F401

    HAS_GATEWAY = True
except ImportError:
    HAS_GATEWAY = False


# ── P1: the adapter must satisfy the host's abstract contract ────────────────


@pytest.mark.skipif(not HAS_GATEWAY, reason="needs the Hermes gateway package")
def test_adapter_class_has_no_abstract_methods():
    """The finding that blocked 2b: get_chat_info was abstract and unimplemented,
    so every instantiation raised TypeError. Guard the whole contract."""
    from hermes_msteams_bridge.gateway_adapter import get_adapter_class

    cls = get_adapter_class()
    assert not getattr(cls, "__abstractmethods__", frozenset())


@pytest.mark.skipif(not HAS_GATEWAY, reason="needs the Hermes gateway package")
def test_adapter_get_chat_info_reports_dm():
    from hermes_msteams_bridge.gateway_adapter import get_adapter_class

    cls = get_adapter_class()
    info = asyncio.run(cls.get_chat_info(object.__new__(cls), "some-aad-id"))
    assert info["type"] == "dm"
    assert info["name"]


# ── P1: the smoke must stay offline (no durable-job resume, no reaper) ───────


def test_service_background_workers_flag():
    from hermes_msteams_bridge.smoke import free_port
    from hermes_msteams_bridge.service import VoiceBridgeService

    async def scenario():
        cfg = TeamsVoiceConfig(shared_secret="s", host="127.0.0.1", port=free_port())
        svc = VoiceBridgeService(cfg, handler_factory=lambda: None, background_workers=False)
        await svc.start()
        try:
            assert svc._tasks == []  # nothing that could deliver
        finally:
            await svc.stop()

        cfg2 = TeamsVoiceConfig(shared_secret="s", host="127.0.0.1", port=free_port())
        svc2 = VoiceBridgeService(cfg2, handler_factory=lambda: None)
        await svc2.start()
        try:
            assert {t.get_name() for t in svc2._tasks} == {
                "teams_call_reaper", "teams_call_job_resume",
            }
        finally:
            await svc2.stop()

    asyncio.run(scenario())


def test_run_smoke_never_resumes_jobs(monkeypatch):
    import hermes_msteams_bridge.background_jobs as bj
    from hermes_msteams_bridge.smoke import run_smoke

    called = []

    async def marker():
        called.append(True)

    monkeypatch.setattr(bj, "resume_pending_jobs", marker)
    results = asyncio.run(run_smoke())
    assert results["synthetic_call_ok"] is True
    assert called == []


# ── P2: voice delivery shares call_user's dial budget ────────────────────────


def test_deliver_by_voice_respects_rate_limit(monkeypatch):
    import time

    import hermes_msteams_bridge.config as config_mod
    import hermes_msteams_bridge.tools as tools_mod
    from hermes_msteams_bridge.gateway_adapter import deliver_by_voice

    cfg = TeamsVoiceConfig(shared_secret="s", tenant_id="tenant", allowlist=("aad-1",))
    monkeypatch.setattr(config_mod, "resolve_config", lambda extra=None: cfg)
    now = time.monotonic()
    monkeypatch.setattr(
        tools_mod, "_CALL_TIMES", [now] * tools_mod._CALL_LIMIT_PER_HOUR
    )
    result = asyncio.run(deliver_by_voice("aad-1", "hello"))
    assert "rate limit" in result["error"]


# ── P1: gateway auth env mirrors the resolved admission policy ───────────────


@pytest.mark.skipif(not HAS_GATEWAY, reason="needs the Hermes gateway package")
def test_connect_mirrors_allowlist_into_gateway_env(monkeypatch):
    import hermes_msteams_bridge.config as config_mod
    import hermes_msteams_bridge.service as service_mod
    from hermes_msteams_bridge.gateway_adapter import get_adapter_class

    cfg = TeamsVoiceConfig(shared_secret="s", allowlist=("aad-1", "aad-2"), allow_all=True)
    monkeypatch.setattr(config_mod, "resolve_config", lambda extra=None: cfg)
    monkeypatch.delenv("TEAMS_CALL_ALLOWLIST", raising=False)
    monkeypatch.delenv("TEAMS_CALL_ALLOW_ALL", raising=False)

    class _Svc:
        def __init__(self, *a, **k): ...
        async def start(self): ...
        async def stop(self): ...

    monkeypatch.setattr(service_mod, "VoiceBridgeService", _Svc)
    cls = get_adapter_class()
    adapter = object.__new__(cls)
    adapter._service = None
    adapter._mark_connected = lambda: None
    import os

    asyncio.run(cls.connect(adapter))
    assert os.environ["TEAMS_CALL_ALLOWLIST"] == "aad-1,aad-2"
    assert os.environ["TEAMS_CALL_ALLOW_ALL"] == "1"


# ── P2: honest status verdict ────────────────────────────────────────────────


def test_status_ok_is_false_when_unconfigured(monkeypatch):
    import hermes_msteams_bridge.tools as tools_mod

    monkeypatch.setattr(
        tools_mod, "resolve_config", lambda: TeamsVoiceConfig(shared_secret="")
    )
    payload = json.loads(tools_mod.handle_teams_call_status())
    assert payload["configured"] is False
    assert payload["ok"] is False
    assert "port_active" in payload
    assert "plugin_enabled" in payload and "platform_enabled" in payload


# ── P1: consult pins its tool-session task id; the watcher uses it ───────────


def test_consult_passes_stable_task_id():
    from hermes_msteams_bridge.agent_consult import AgentConsult

    consult = AgentConsult(session_id="thread-1")
    seen = {}

    class _Agent:
        def run_conversation(self, query, task_id=None):
            seen["task_id"] = task_id
            return {"final_response": "done"}

        def chat(self, query):  # must NOT be used when run_conversation exists
            raise AssertionError("chat() bypasses the task id")

    consult._agent = _Agent()
    assert consult._run_locked("look this up") == "done"
    assert seen["task_id"] == consult.browser_task_id
    assert consult.browser_task_id.startswith("teams_call:consult:")


def test_consult_falls_back_to_chat_on_old_host():
    from hermes_msteams_bridge.agent_consult import AgentConsult

    consult = AgentConsult()

    class _OldAgent:
        def chat(self, query):
            return "legacy path"

    consult._agent = _OldAgent()
    assert consult._run_locked("q") == "legacy path"


def test_browser_frame_uses_consult_task_id_and_content_dedupe(tmp_path, monkeypatch):
    from hermes_msteams_bridge.call_tools import CallToolRunner

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"frame-1")
    calls = []

    # The fake accepts **kw because that is the HOST's shape: `entry.handler(args, **kw)`, with
    # task_id read from kw and NEVER from args. The previous fake took (name, args) only and the
    # assertion checked args["task_id"] - green, while the real host ignored the value and every
    # capture fell through to the shared "default" browser session. Assert the contract, not our
    # own call shape.
    async def fake_dispatch(name, args, **kw):
        calls.append((name, args, kw))
        return json.dumps({"screenshot_path": str(shot)})

    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "dispatch_tool_async", fake_dispatch)

    class _Consult:
        browser_task_id = "teams_call:consult:test"

    class _Handler:
        _consult = _Consult()

    runner = CallToolRunner(_Handler())
    first = asyncio.run(runner._grab_agent_browser_frame(""))
    assert first is not None
    frame, digest = first
    name, args, kw = calls[0]
    assert kw["task_id"] == "teams_call:consult:test"
    assert "task_id" not in args  # in args the host silently ignores it
    # Same content, fresh call: content-based dedupe must suppress it even
    # though Hermes would have written a new screenshot file.
    second = asyncio.run(runner._grab_agent_browser_frame(digest))
    assert second is None


def test_browser_frame_skips_without_consult(monkeypatch):
    from hermes_msteams_bridge.call_tools import CallToolRunner

    class _Handler:
        _consult = None

    runner = CallToolRunner(_Handler())
    assert asyncio.run(runner._grab_agent_browser_frame("")) is None
