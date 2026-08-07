"""Boundary contract: every Hermes import lives in hermes_api.py; the startup
probe reports rather than raises; and the public paths behave on BOTH sides of
the boundary.

Three test groups:

* Static  — AST-enforced import boundary; run everywhere.
* Bare    — degradation behaviour on an interpreter WITHOUT a Hermes host
            (skipped when Hermes is importable).
* Host    — contract expectations against a real Hermes install (skipped when
            it is not). The CI ``hermes-contract`` job exercises these.

Positive runtime paths (vision via a fake ctx.llm, dispatch parsing, the
send fallback, minutes delivery, ElevenLabs config) run everywhere via fakes.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

import hermes_msteams_bridge.hermes_api as hermes_api

PKG_DIR = Path(hermes_api.__file__).resolve().parent
HAS_HERMES = importlib.util.find_spec("hermes_constants") is not None

bare_only = pytest.mark.skipif(HAS_HERMES, reason="requires NO Hermes host")
host_only = pytest.mark.skipif(not HAS_HERMES, reason="requires a Hermes host")

# Hermes-host module roots. An import of any of these outside hermes_api.py is
# a boundary violation.
HERMES_ROOTS = {
    "run_agent",
    "agent",
    "tools",
    "plugins",
    "hermes_cli",
    "hermes_constants",
    "gateway",
    "model_tools",
    "hermes_state",
}


# ── Static: the import boundary ──────────────────────────────────────────────


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # absolute imports only
                roots.add(node.module.split(".")[0])
    return roots


def test_only_hermes_api_imports_hermes():
    """The whole package, except the boundary module, is Hermes-import-free."""
    offenders: dict[str, set[str]] = {}
    for py in PKG_DIR.rglob("*.py"):
        if py.name == "hermes_api.py":
            continue
        hits = _imported_roots(py) & HERMES_ROOTS
        if hits:
            offenders[str(py.relative_to(PKG_DIR))] = hits
    assert not offenders, f"Hermes imports outside hermes_api.py: {offenders}"


def test_meeting_never_imports_teams_adapter_symbols():
    """D7 acceptance: meeting.py must not touch plugins.platforms.teams at all."""
    src = (PKG_DIR / "meeting.py").read_text(encoding="utf-8")
    assert "plugins.platforms" not in src
    assert "_standalone_send" not in src


def test_probe_rows_carry_reporting_fields():
    for r in hermes_api.probe_boundaries():
        assert set(r) == {"surface", "kind", "ok", "operational", "detail"}


def test_version_note_never_raises():
    assert "hermes-agent version" in hermes_api.hermes_version_note()


# ── Bare interpreter: loud, graceful degradation ─────────────────────────────


@bare_only
def test_probe_reports_missing_surfaces_without_raising():
    results = hermes_api.probe_boundaries()
    resident_rows = [r for r in results if r["kind"] == "resident"]
    assert resident_rows and all(not r["ok"] for r in resident_rows)
    by_surface = {r["surface"]: r for r in results}
    assert by_surface["ctx (PluginContext captured)"]["ok"] is False


@bare_only
def test_status_tool_reports_not_ok_without_host():
    from hermes_msteams_bridge.tools import handle_teams_voice_status

    payload = json.loads(handle_teams_voice_status())
    assert payload["boundaries_ok"] is False
    assert "operational_ok" in payload


@bare_only
def test_dispatch_without_host_returns_error_json():
    raw = hermes_api.dispatch_tool("text_to_speech", {"text": "hi"})
    assert "error" in json.loads(raw)


@bare_only
def test_send_and_media_degrade_to_error_dicts():
    assert asyncio.run(hermes_api.send_teams_message("", "hi")) == {
        "error": "missing conversation id"
    }
    assert "error" in asyncio.run(hermes_api.send_teams_message("19:x@thread.v2", "hi"))
    assert hermes_api.generate_image("a cat").get("success") is not True
    assert hermes_api.text_to_speech("hello").get("success") is not True
    res = hermes_api.transcribe("/nonexistent.wav")
    assert res.get("success") is False and "transcript" in res


# ── Host contract: a real Hermes install must satisfy the boundary ───────────


@host_only
def test_all_non_ctx_surfaces_resolve_on_host():
    allowed_missing = {"ctx (PluginContext captured)", "ctx.llm (vision + consults)"}
    misses = [
        r["surface"]
        for r in hermes_api.probe_boundaries()
        if not r["ok"] and r["surface"] not in allowed_missing
    ]
    assert not misses, f"host is missing boundary surfaces: {misses}"


@host_only
def test_plugin_llm_contract_on_host():
    import inspect

    from agent.plugin_llm import PluginLlm, PluginLlmImageInput

    sig = inspect.signature(PluginLlm.acomplete_structured)
    for param in ("instructions", "input", "json_schema", "max_tokens", "purpose"):
        assert param in sig.parameters
    assert inspect.iscoroutinefunction(PluginLlm.acomplete_structured)
    for fld in ("url", "data", "mime_type"):
        assert fld in PluginLlmImageInput.__dataclass_fields__


# ── Positive paths via fakes (run everywhere) ────────────────────────────────


class _FakeLlm:
    def __init__(self, text="a red button on the shared screen"):
        self._text = text
        self.calls: list[dict] = []

    async def acomplete_structured(self, *, instructions, input, max_tokens=None, purpose=None, **kw):
        self.calls.append({"instructions": instructions, "input": list(input)})
        return types.SimpleNamespace(text=self._text)


class _FakeCtx:
    def __init__(self, llm=None, dispatch=None):
        self.llm = llm
        self._dispatch = dispatch or (lambda name, args, **kw: json.dumps({"error": "no fake"}))

    def dispatch_tool(self, name, args, **kwargs):
        return self._dispatch(name, args, **kwargs)


@pytest.fixture
def fake_ctx():
    """Install a fake PluginContext; always restore the module state."""
    def _install(ctx):
        hermes_api.set_plugin_context(ctx)
        return ctx

    yield _install
    hermes_api.set_plugin_context(None)


def test_vision_ask_happy_path(fake_ctx):
    llm = _FakeLlm()
    fake_ctx(_FakeCtx(llm=llm))
    out = asyncio.run(
        hermes_api.vision_ask(
            "Describe.", [{"type": "image", "url": "data:image/jpeg;base64,AA=="}], max_tokens=99
        )
    )
    assert out == "a red button on the shared screen"
    assert llm.calls[0]["input"][0]["type"] == "image"


def test_vision_ask_empty_refund_contract(fake_ctx):
    fake_ctx(_FakeCtx(llm=_FakeLlm(text="")))
    out = asyncio.run(hermes_api.vision_ask("q", [{"type": "text", "text": "x"}]))
    assert out == ""  # callers refund the vision budget on ""


def test_tts_and_image_dispatch_parse_success(fake_ctx):
    def dispatch(name, args, **kw):
        if name == "text_to_speech":
            return json.dumps({"success": True, "file_path": "/tmp/x.mp3"})
        if name == "image_generate":
            return json.dumps({"success": True, "image": "/tmp/x.png"})
        return json.dumps({"error": f"Unknown tool: {name}"})

    fake_ctx(_FakeCtx(dispatch=dispatch))
    assert hermes_api.text_to_speech("hello")["file_path"] == "/tmp/x.mp3"
    assert hermes_api.generate_image("a cat")["image"] == "/tmp/x.png"


def _install_fake_send_module(monkeypatch, result: dict):
    """Simulate the host's send engine: a real ``tools.send_message_tool``
    module whose function returns ``result`` — the exact entry our resident
    fallback imports."""
    calls: list[dict] = []

    def send_message_tool(args, **kw):
        calls.append(args)
        return json.dumps(result)

    tools_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    mod = types.ModuleType("tools.send_message_tool")
    mod.send_message_tool = send_message_tool
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", mod)
    return calls


def test_send_teams_message_uses_direct_function(monkeypatch):
    calls = _install_fake_send_module(monkeypatch, {"success": True, "message_id": "m1"})
    out = asyncio.run(hermes_api.send_teams_message("19:abc@thread.v2", "minutes body"))
    assert out == {"success": True, "message_id": "m1"}
    assert calls[0]["target"] == "teams:19:abc@thread.v2"
    assert calls[0]["message"] == "minutes body"


def test_meeting_delivery_success_via_boundary(monkeypatch):
    from hermes_msteams_bridge import meeting

    async def fake_send(conversation_id, text):
        assert conversation_id == "19:abc@thread.v2" and "minutes" in text.lower()
        return {"success": True}

    monkeypatch.setattr(hermes_api, "send_teams_message", fake_send)
    ok = asyncio.run(meeting._deliver_to_teams("19:abc@thread.v2", "📝 **Meeting minutes**\n\nhi"))
    assert ok is True


def test_elevenlabs_config_from_tts_block(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(
        hermes_api,
        "load_hermes_config",
        lambda: {"tts": {"elevenlabs": {"api_key": "k-yaml", "voice_id": "v1", "model_id": "m1"}}},
    )
    cfg = hermes_api.elevenlabs_tts_config()
    assert cfg == {"api_key": "k-yaml", "voice_id": "v1", "model_id": "m1"}
    # env still wins over the yaml block
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-env")
    assert hermes_api.elevenlabs_tts_config()["api_key"] == "k-env"


def test_elevenlabs_resolve_config_via_boundary(monkeypatch):
    from hermes_msteams_bridge import elevenlabs_tts

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("TEAMS_VOICE_ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.setattr(
        hermes_api,
        "load_hermes_config",
        lambda: {"tts": {"elevenlabs": {"api_key": "k", "voice_id": "v", "model_id": "m"}}},
    )
    assert elevenlabs_tts.resolve_config() == {"api_key": "k", "voice_id": "v", "model_id": "m"}
