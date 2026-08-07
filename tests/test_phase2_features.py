"""Phase 2: §3.8 multilingual, D2 budget reserve, D5 mid-call instructions,
§3.1 Step B walkthrough, §9.6 Windows-footgun guard."""

from __future__ import annotations

import ast
import asyncio
import base64
from pathlib import Path

import pytest

import hermes_msteams_bridge.handlers as handlers_mod
from hermes_msteams_bridge import verbal_interrupts
from hermes_msteams_bridge.realtime.openai_client import realtime_config_from_env
from hermes_msteams_bridge.vision_budget import VisionBudget

PKG_DIR = Path(handlers_mod.__file__).resolve().parent


# ── §3.8: deterministic interrupts in FR/DE (plus the EN/AR regressions) ─────


@pytest.mark.parametrize(
    "utterance",
    [
        "stop",                     # en
        "توقف",                     # ar
        "arrête",                   # fr
        "Arrêtez !",                # fr, punctuation + case
        "attends",                  # fr
        "ça suffit",                # fr
        "stopp",                    # de
        "halt",                     # de
        "warte mal",                # de: filler-trailing
        "das reicht",               # de
        "euh, arrête",              # fr with filler
        "bitte stopp",              # de with filler
    ],
)
def test_interrupts_fire_across_languages(utterance):
    assert verbal_interrupts.is_verbal_interrupt(utterance, ("assistant",))


@pytest.mark.parametrize(
    "utterance",
    [
        "stop by the store",              # en substring guard
        "arrête de m'envoyer des emails quand tu peux",  # fr: not a bare interrupt
        "der Halt ist weit weg von hier oben",           # de: sentence, not command
    ],
)
def test_non_interrupt_sentences_pass_through(utterance):
    assert not verbal_interrupts.is_verbal_interrupt(utterance, ("assistant",))


# ── §3.8: languages config + instruction clause ──────────────────────────────


def test_languages_from_env_csv(monkeypatch):
    monkeypatch.setenv("TEAMS_CALL_LANGUAGES", "en, FR ,de")
    cfg = realtime_config_from_env({})
    assert cfg.languages == ("en", "fr", "de")


def test_languages_from_block_list(monkeypatch):
    monkeypatch.delenv("TEAMS_CALL_LANGUAGES", raising=False)
    cfg = realtime_config_from_env({"languages": ["En", "ar"]})
    assert cfg.languages == ("en", "ar")


def test_bilingual_alias_maps_to_ar_en(monkeypatch):
    monkeypatch.delenv("TEAMS_CALL_LANGUAGES", raising=False)
    cfg = realtime_config_from_env({"bilingual": "true"})
    assert cfg.languages == ("ar", "en")


def _instructions_for(languages):
    from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig

    handler = handlers_mod.RealtimeCallSessionHandler(
        RealtimeConfig(api_key="k", languages=languages)
    )
    return handler._build_instructions()


def test_instruction_clause_names_languages():
    text = _instructions_for(("en", "fr", "de"))
    for name in ("English", "French", "German"):
        assert name in text


def test_instruction_clause_auto_detect_when_empty():
    text = _instructions_for(())
    assert "Detect the caller's language" in text


def test_unknown_language_code_passes_through():
    assert "xx" in _instructions_for(("xx",))


# ── D5: mid-call instruction update ──────────────────────────────────────────


def test_update_instructions_sends_session_update():
    from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig, RealtimeSession

    session = RealtimeSession(RealtimeConfig(api_key="k"))
    sent: list[dict] = []

    async def fake_send(payload):
        sent.append(payload)

    session._send = fake_send
    asyncio.run(session.update_instructions("Now reply only in French."))
    assert sent[0]["type"] == "session.update"
    assert sent[0]["session"]["instructions"] == "Now reply only in French."
    asyncio.run(session.update_instructions(""))  # empty → no-op
    assert len(sent) == 1


# ── D2: ambient use cannot starve explicit look_at_screen ────────────────────


def test_ambient_reserve_protects_explicit():
    budget = VisionBudget(max_per_minute=8)  # reserve = 2
    ambient_grants = sum(budget.try_consume_ambient() for _ in range(20))
    assert ambient_grants == 6  # stops at max - reserve
    assert budget.try_consume() and budget.try_consume()  # the reserve
    assert not budget.try_consume()  # cap holds overall


def test_unlimited_budget_stays_unlimited():
    budget = VisionBudget(max_per_minute=0)
    assert all(budget.try_consume_ambient() for _ in range(100))


def test_explicit_can_use_whole_window():
    budget = VisionBudget(max_per_minute=4)
    assert sum(budget.try_consume() for _ in range(10)) == 4


# ── §3.1 Step B: walkthrough ─────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class _Session:
    closed = False

    def __init__(self):
        self.shown: list[str] = []

    async def send_display_image(self, data_b64, mime, *, duration_ms=None, mode=None, caption=None):
        self.shown.append(caption)


class _Rt:
    def __init__(self):
        self.said: list[str] = []

    async def request_say(self, text):
        self.said.append(text)


def _walk_runner(root):
    from hermes_msteams_bridge.call_tools import CallToolRunner

    class _Handler:
        _session = _Session()
        _rt = _Rt()
        _turn_id = 0
        _bridge = type("B", (), {"show_file_root": str(root)})()

    return CallToolRunner(_Handler()), _Handler


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "step1.png").write_bytes(PNG)
    (root / "step2.png").write_bytes(PNG)
    return root


def test_walkthrough_shows_and_says_each_step(ws, monkeypatch):
    runner, handler = _walk_runner(ws)
    import hermes_msteams_bridge.call_tools as ct

    async def instant(_s):
        return None

    monkeypatch.setattr(ct.asyncio, "sleep", instant)
    out = asyncio.run(
        runner.run_tool(
            "walkthrough",
            {"steps": [
                {"path": "step1.png", "say": "First, open settings."},
                {"path": "step2.png", "say": "Then toggle it off."},
            ]},
        )
    )
    assert "2 steps" in out
    assert handler._session.shown == ["step1.png", "step2.png"]
    assert handler._rt.said == ["First, open settings.", "Then toggle it off."]


def test_walkthrough_stops_on_barge_in(ws, monkeypatch):
    runner, handler = _walk_runner(ws)
    import hermes_msteams_bridge.call_tools as ct

    async def bump(_s):
        handler._turn_id += 1  # the caller interrupts during the hold

    monkeypatch.setattr(ct.asyncio, "sleep", bump)
    out = asyncio.run(
        runner.run_tool(
            "walkthrough",
            {"steps": [
                {"path": "step1.png", "say": "one"},
                {"path": "step2.png", "say": "two"},
            ]},
        )
    )
    assert "Stopped after step 1 of 2" in out
    assert handler._session.shown == ["step1.png"]


def test_walkthrough_contained_and_realtime_only(ws):
    runner, handler = _walk_runner(ws)
    out = asyncio.run(
        runner.run_tool("walkthrough", {"steps": [{"path": "../x.png", "say": "s"}]})
    )
    assert "workspace" in out

    handler._rt = None
    out = asyncio.run(
        runner.run_tool("walkthrough", {"steps": [{"path": "step1.png", "say": "s"}]})
    )
    assert "realtime" in out


# ── §9.6: Windows-footgun guard over the package source ──────────────────────


def test_no_windows_footguns_in_package():
    banned_attrs = {("signal", "SIGKILL"), ("os", "fork"), ("os", "setsid"), ("os", "forkpty")}
    banned_modules = {"termios", "pty", "fcntl"}
    offenders: list[str] = []
    for py in PKG_DIR.rglob("*.py"):
        rel = str(py.relative_to(PKG_DIR))
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any(n.split(".")[0] in banned_modules for n in names):
                    offenders.append(f"{rel}:{node.lineno} imports {names}")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if (node.value.id, node.attr) in banned_attrs:
                    offenders.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = ""
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                kwargs = {k.arg for k in node.keywords}
                if "b" not in mode and "encoding" not in kwargs:
                    offenders.append(f"{rel}:{node.lineno} text-mode open() without encoding")
    assert not offenders, offenders


# ── §3.7: no-answer fallback + §3.2(a): progress panel ───────────────────────


def test_stale_pending_claimed_and_delivered(tmp_path, monkeypatch):
    import os
    import time as _time

    from hermes_msteams_bridge import call_session_base as csb

    monkeypatch.setattr(csb, "_pending_dir", lambda: tmp_path)
    csb._pending_set("call-1", "your report is ready", thread_id="19:t@thread.v2")
    csb._pending_set("call-2", "no fallback target")  # no thread_id -> dropped
    # age the files past the stale window
    old = _time.time() - 400
    for f in tmp_path.glob("*.json"):
        os.utime(f, (old, old))
    claimed = csb.scan_stale_pending(max_age_s=180)
    assert [(c[0], c[1]) for c in claimed] == [("your report is ready", "19:t@thread.v2")]
    assert list(tmp_path.glob("*.json")) == []  # untargeted expired, targeted claimed
    assert len(list(tmp_path.glob("*.fallback"))) == 1  # two-phase: survives until posted


def test_fresh_pending_not_claimed(tmp_path, monkeypatch):
    from hermes_msteams_bridge import call_session_base as csb

    monkeypatch.setattr(csb, "_pending_dir", lambda: tmp_path)
    csb._pending_set("call-3", "still ringing", thread_id="19:t@thread.v2")
    assert csb.scan_stale_pending(max_age_s=180) == []
    assert csb._pending_pop("call-3") == "still ringing"  # answer path intact


def test_deliver_stale_pending_posts_to_thread(tmp_path, monkeypatch):
    import os
    import time as _time

    from hermes_msteams_bridge import call_session_base as csb
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(csb, "_pending_dir", lambda: tmp_path)
    csb._pending_set("call-4", "the answer is 42", thread_id="19:t@thread.v2")
    old = _time.time() - 400
    for f in tmp_path.glob("*.json"):
        os.utime(f, (old, old))

    sent = []

    async def fake_send(thread_id, text):
        sent.append((thread_id, text))
        return {"success": True}

    monkeypatch.setattr(hermes_api, "send_teams_message", fake_send)
    delivered = asyncio.run(csb.deliver_stale_pending())
    assert delivered == 1
    assert sent[0][0] == "19:t@thread.v2" and "couldn't reach you" in sent[0][1]


def test_progress_panel_renders_png():
    pytest.importorskip("PIL")
    from hermes_msteams_bridge.progress_panel import render_panel

    png = render_panel("research the quarterly numbers", 65)
    assert png is not None and png.startswith(b"\x89PNG")


def test_progress_loop_refreshes_until_job_done(monkeypatch):
    pytest.importorskip("PIL")
    from hermes_msteams_bridge.call_tools import CallToolRunner

    frames = []

    class _Session:
        closed = False
        async def send_display_image(self, b64, mime, *, duration_ms=None, mode=None, caption=None):
            frames.append(caption)

    class _Handler:
        _session = _Session()
        _turn_id = 0

    runner = CallToolRunner(_Handler())

    import hermes_msteams_bridge.call_tools as ct

    async def scenario():
        real_sleep = asyncio.sleep

        async def fast(s):
            await real_sleep(0.01)

        monkeypatch.setattr(ct.asyncio, "sleep", fast)
        job = asyncio.get_event_loop().create_task(real_sleep(0.05))
        await runner._progress_loop(job, "do the thing")
        return job

    job = asyncio.run(scenario())
    assert job.done() and len(frames) >= 1 and frames[0] == "working…"


# ── identity parity: the call answers as Hermes, not as our default ──────────


def test_soul_text_reads_hermes_soul(monkeypatch, tmp_path):
    import hermes_msteams_bridge.hermes_api as hermes_api

    (tmp_path / "SOUL.md").write_text("You are Hermes Agent, created by Nous Research.", encoding="utf-8")
    monkeypatch.setattr(hermes_api, "hermes_home", lambda: tmp_path)
    assert "Nous Research" in hermes_api.soul_text()


def test_soul_text_empty_without_home(monkeypatch):
    import sys
    import types

    import hermes_msteams_bridge.hermes_api as hermes_api

    # Block BOTH sources: the Hermes loader (present on a real host, where it
    # supplies a built-in default persona) and the SOUL.md file fallback.
    fake_pb = types.ModuleType("agent.prompt_builder")
    fake_pb.load_soul_md = lambda: ""
    agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", fake_pb)

    def boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(hermes_api, "hermes_home", boom)
    assert hermes_api.soul_text() == ""


def test_instructions_lead_with_hermes_persona(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(
        hermes_api, "soul_text", lambda max_chars=6000: "You are Hermes Agent, created by Nous Research."
    )
    text = _instructions_for(())
    assert text.index("Hermes Agent") < text.index("voice")  # identity first
    assert "live Microsoft Teams voice call" in text


def test_instructions_unchanged_without_soul(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "soul_text", lambda max_chars=6000: "")
    text = _instructions_for(())
    assert "persona" not in text


# ── capability parity: the call knows the same skills as chat ────────────────


def test_instructions_include_skills_and_delegation(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "soul_text", lambda max_chars=6000: "")
    monkeypatch.setattr(
        hermes_api, "skills_index_text",
        lambda max_chars=3500: "<available_skills>\n- research/deep-dive: thorough research\n</available_skills>",
    )
    text = _instructions_for(())
    assert "deep-dive" in text
    assert "hermes_agent_consult" in text  # delegation instruction present


def test_instructions_clean_without_skills(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "soul_text", lambda max_chars=6000: "")
    monkeypatch.setattr(hermes_api, "skills_index_text", lambda max_chars=3500: "")
    assert "Installed skills" not in _instructions_for(())


def test_skills_index_trims_to_cap(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    class _FakePB:
        @staticmethod
        def build_skills_system_prompt():
            return "x" * 10000

    import sys, types
    mod = types.ModuleType("agent.prompt_builder")
    mod.build_skills_system_prompt = _FakePB.build_skills_system_prompt
    agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", mod)
    out = hermes_api.skills_index_text(max_chars=100)
    assert len(out) < 200 and "more skills available" in out


# ── Teams file card via the adapter's own attachment contract ────────────────


def test_file_activity_matches_adapter_shape():
    from hermes_msteams_bridge.hermes_api import build_file_activity

    act = build_file_activity(b"DOCX", "Meeting minutes.docx",
                              "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              caption="minutes")
    assert act["type"] == "message" and act["text"] == "minutes"
    att = act["attachments"][0]
    assert att["contentUrl"].startswith("data:application/vnd.openxmlformats") 
    assert att["contentUrl"].endswith("RE9DWA==")  # base64("DOCX")
    assert att["name"] == "Meeting minutes.docx"


def test_file_sender_available_needs_all_creds(monkeypatch):
    import sys
    import types

    from hermes_msteams_bridge.hermes_api import file_sender_available

    # On a real host get_env_value also reads ~/.hermes/.env — pin the reader
    # to os.environ only so the test controls both sources.
    import os as _os

    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.get_env_value = lambda key: _os.getenv(key)
    hermes_cli_pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)

    for var in ("TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"):
        monkeypatch.delenv(var, raising=False)
    assert not file_sender_available()
    monkeypatch.setenv("TEAMS_CLIENT_ID", "c")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "s")
    monkeypatch.setenv("TEAMS_TENANT_ID", "t")
    assert file_sender_available()


def test_send_teams_file_validates_before_network(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setenv("TEAMS_CLIENT_ID", "c")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "s")
    monkeypatch.setenv("TEAMS_TENANT_ID", "t")
    out = asyncio.run(hermes_api.send_teams_file("../bad/id", b"x", "a.docx"))
    assert "invalid" in out["error"]
    out = asyncio.run(hermes_api.send_teams_file("19:x@thread.v2", b"x" * (5 * 1024 * 1024), "a.docx"))
    assert "too large" in out["error"]
    monkeypatch.setenv("TEAMS_SERVICE_URL", "https://evil.example.com/teams/")
    out = asyncio.run(hermes_api.send_teams_file("19:x@thread.v2", b"x", "a.docx"))
    assert "Bot Framework" in out["error"]


# ── task #1: durable background jobs ─────────────────────────────────────────


def test_job_lifecycle_create_complete(tmp_path, monkeypatch):
    from hermes_msteams_bridge import background_jobs as bj

    monkeypatch.setattr(bj, "_jobs_dir", lambda: tmp_path)
    job_id = bj.job_create("research the numbers", "19:t@thread.v2")
    assert job_id and len(list(tmp_path.glob("*.json"))) == 1
    bj.job_complete(job_id)
    assert list(tmp_path.glob("*.json")) == []


def test_claim_skips_stale_and_untargeted(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    from hermes_msteams_bridge import background_jobs as bj

    monkeypatch.setattr(bj, "_jobs_dir", lambda: tmp_path)
    (tmp_path / "fresh.json").write_text(
        _json.dumps({"query": "q1", "thread_id": "19:t@thread.v2", "created": _time.time()}),
        encoding="utf-8",
    )
    (tmp_path / "stale.json").write_text(
        _json.dumps({"query": "old", "thread_id": "19:t@thread.v2", "created": _time.time() - 3 * 3600}),
        encoding="utf-8",
    )
    (tmp_path / "notarget.json").write_text(
        _json.dumps({"query": "orphan", "thread_id": "", "created": _time.time()}),
        encoding="utf-8",
    )
    claimed = bj.claim_pending_jobs()
    assert [j["query"] for j in claimed] == ["q1"]
    assert list(tmp_path.glob("*.json")) == []  # stale/untargeted deleted, fresh claimed
    assert len(list(tmp_path.glob("*.claimed"))) == 1  # two-phase: record survives the claim
    # failed delivery restores the record for retry; success retires it
    bj.finish_job(claimed[0], delivered=False)
    assert len(list(tmp_path.glob("*.json"))) == 1
    reclaimed = bj.claim_pending_jobs()
    bj.finish_job(reclaimed[0], delivered=True)
    assert list(tmp_path.glob("*")) == []


def test_resume_runs_and_delivers(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    from hermes_msteams_bridge import background_jobs as bj
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(bj, "_jobs_dir", lambda: tmp_path)
    (tmp_path / "j1.json").write_text(
        _json.dumps({"query": "sum the report", "thread_id": "19:t@thread.v2", "created": _time.time()}),
        encoding="utf-8",
    )

    class _FakeConsult:
        def __init__(self, session_id=None):
            pass

        async def ask(self, query, timeout_s=300.0):
            return f"done: {query}"

    sent = []

    async def fake_send(thread_id, text):
        sent.append((thread_id, text))
        return {"success": True}

    import hermes_msteams_bridge.agent_consult as ac

    monkeypatch.setattr(ac, "AgentConsult", _FakeConsult)
    monkeypatch.setattr(hermes_api, "send_teams_message", fake_send)
    delivered = asyncio.run(bj.resume_pending_jobs())
    assert delivered == 1
    assert "done: sum the report" in sent[0][1] and "restart" in sent[0][1]


# ── task #4: live language switch ────────────────────────────────────────────


def test_set_call_language_pushes_new_instructions(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api
    from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig

    monkeypatch.setattr(hermes_api, "soul_text", lambda max_chars=6000: "")
    monkeypatch.setattr(hermes_api, "skills_index_text", lambda max_chars=3500: "")
    handler = handlers_mod.RealtimeCallSessionHandler(RealtimeConfig(api_key="k"))

    pushed = []

    class _Rt:
        async def update_instructions(self, text):
            pushed.append(text)

    handler._rt = _Rt()
    asyncio.run(handler.set_call_language("fr"))
    assert handler._cfg.languages == ("fr",)
    assert pushed and "French" in pushed[0]


def test_set_call_language_tool_validates(monkeypatch):
    from hermes_msteams_bridge.call_tools import CallToolRunner

    class _Handler:
        _rt = object()

        async def set_call_language(self, code):
            self.pinned = code

    handler = _Handler()
    runner = CallToolRunner(handler)
    out = asyncio.run(runner.run_tool("set_call_language", {"language": "FR"}))
    assert "fr" in out and handler.pinned == "fr"
    out = asyncio.run(runner.run_tool("set_call_language", {"language": "12!"}))
    assert "didn't recognize" in out


# ── round 6: https-only token transport, busy consult, serialized tools ──────


def test_send_teams_file_requires_https(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setenv("TEAMS_CLIENT_ID", "c")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "s")
    monkeypatch.setenv("TEAMS_TENANT_ID", "t")
    monkeypatch.setenv("TEAMS_SERVICE_URL", "http://smba.trafficmanager.net/teams/")
    out = asyncio.run(hermes_api.send_teams_file("19:x@thread.v2", b"x", "a.docx"))
    assert "https" in out["error"]


def test_busy_consult_answers_instead_of_queueing():
    import threading
    import time as _time

    from hermes_msteams_bridge.agent_consult import AgentConsult

    consult = AgentConsult()
    release = threading.Event()

    def slow(query):
        release.wait(timeout=5)
        return "slow done"

    consult._run_locked = slow  # first call holds the lock in its thread

    async def scenario():
        first = asyncio.create_task(asyncio.to_thread(consult._run_sync, "one"))
        await asyncio.sleep(0.1)  # let the first thread take the lock
        out = await consult.ask("two", timeout_s=2.0)
        release.set()
        await first
        return out

    out = asyncio.run(scenario())
    assert "still finishing" in out  # answered busy, did not queue to timeout


def test_parallel_tool_callbacks_are_serialized():
    from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig, RealtimeSession

    session = RealtimeSession(RealtimeConfig(api_key="k"))
    order: list[str] = []

    async def scenario():
        async def slow():
            order.append("slow-start")
            await asyncio.sleep(0.1)
            order.append("slow-end")

        async def fast():
            order.append("fast-start")
            order.append("fast-end")

        session._spawn_tool_task(slow())
        session._spawn_tool_task(fast())
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert order == ["slow-start", "slow-end", "fast-start", "fast-end"]  # arrival order, no interleave


# ── round 7: call_user fallback wiring, timed gate, close awaits ─────────────


def _cfg(**over):
    from hermes_msteams_bridge.config import TeamsVoiceConfig

    base = dict(shared_secret="s3cret", tenant_id="tenant-1", allowlist=("aad-1",))
    base.update(over)
    return TeamsVoiceConfig(**base)


def test_call_user_registers_chat_fallback_thread(monkeypatch, tmp_path):
    import hermes_msteams_bridge.tools as tools_mod
    import hermes_msteams_bridge.hermes_api as hermes_api
    from hermes_msteams_bridge import call_session_base as csb

    monkeypatch.setattr(csb, "_pending_dir", lambda: tmp_path)
    monkeypatch.setattr(tools_mod, "resolve_config", lambda: _cfg())
    monkeypatch.setattr(hermes_api, "current_chat_context", lambda: ("teams", "19:orig@thread.v2"))

    async def fake_place_call(**kw):
        return {"callId": "call-77"}

    import hermes_msteams_bridge.outbound as outbound

    monkeypatch.setattr(outbound, "place_call", fake_place_call)
    import json as _json

    out = _json.loads(tools_mod.handle_call_user({"user_aad_id": "aad-1", "message": "news"}))
    assert out["success"] is True

    # No answer: the stale scan must find a DELIVERABLE entry (the P1 fix).
    import os
    import time as _time

    old = _time.time() - 400
    for f in tmp_path.glob("*.json"):
        os.utime(f, (old, old))
    claimed = csb.scan_stale_pending(max_age_s=180)
    assert [(c[0], c[1]) for c in claimed] == [("news", "19:orig@thread.v2")]


def test_timed_tts_empty_provider_does_not_divert(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api
    from hermes_msteams_bridge import timed_tts

    monkeypatch.delenv("TEAMS_CALL_ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.setattr(hermes_api, "active_tts_provider", lambda: "")
    # Leftover key alone must NOT enable the timed path.
    assert timed_tts._elevenlabs_available() is False
    # Explicit provider selection enables it (subject to config presence).
    monkeypatch.setattr(hermes_api, "active_tts_provider", lambda: "elevenlabs")
    import hermes_msteams_bridge.elevenlabs_tts as el

    monkeypatch.setattr(el, "resolve_config", lambda: {"api_key": "k", "voice_id": "v", "model_id": "m"})
    assert timed_tts._elevenlabs_available() is True


def test_close_awaits_inflight_tool_tasks():
    from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig, RealtimeSession

    session = RealtimeSession(RealtimeConfig(api_key="k"))
    state = {"cancelled": False}

    async def scenario():
        async def slow_tool():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        session._spawn_tool_task(slow_tool())
        await asyncio.sleep(0.05)  # let it start
        await session.close()
        assert state["cancelled"] is True  # awaited through cancellation
        assert not session._tool_tasks

    asyncio.run(scenario())


def test_languages_yaml_string_honoured(monkeypatch):
    monkeypatch.delenv("TEAMS_CALL_LANGUAGES", raising=False)
    cfg = realtime_config_from_env({"languages": "en, FR"})
    assert cfg.languages == ("en", "fr")


def test_rate_limit_only_counts_successes(monkeypatch):
    import hermes_msteams_bridge.tools as tools_mod
    from hermes_msteams_bridge.outbound import OutboundError

    tools_mod._CALL_TIMES.clear()
    monkeypatch.setattr(tools_mod, "resolve_config", lambda: _cfg())

    async def failing_place_call(**kw):
        raise OutboundError("worker down")

    import hermes_msteams_bridge.outbound as outbound

    monkeypatch.setattr(outbound, "place_call", failing_place_call)
    import json as _json

    for _ in range(10):  # far past the hourly limit
        out = _json.loads(tools_mod.handle_call_user({"user_aad_id": "aad-1", "message": "x"}))
        assert "could not place the call" in out["error"]
    assert tools_mod._CALL_TIMES == []  # failures never burned the budget


# ── phase 4(b): browser task view (opt-in) ───────────────────────────────────


def test_progress_loop_shows_browser_frames_when_opted_in(monkeypatch, tmp_path):
    pytest.importorskip("PIL")
    from hermes_msteams_bridge.call_tools import CallToolRunner

    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)

    frames = []

    class _Session:
        closed = False

        async def send_display_image(self, b64, mime, *, duration_ms=None, mode=None, caption=None):
            frames.append(caption)

    class _Consult:
        browser_task_id = "teams_call:consult:test"

    class _Handler:
        _session = _Session()
        _turn_id = 0
        _bridge = type("B", (), {"watch_browser_tasks": True})()
        _consult = _Consult()

    runner = CallToolRunner(_Handler())

    import hermes_msteams_bridge.call_tools as ct
    import hermes_msteams_bridge.hermes_api as hermes_api

    async def fake_dispatch(name, args, **kw):
        assert name == "browser_vision"
        assert args["task_id"] == "teams_call:consult:test"  # round 8: pinned session
        import json as _json

        return _json.dumps({"answer": "ok", "screenshot_path": str(shot)})

    monkeypatch.setattr(hermes_api, "dispatch_tool_async", fake_dispatch)

    async def scenario():
        real_sleep = asyncio.sleep

        async def fast(s):
            await real_sleep(0.01)

        monkeypatch.setattr(ct.asyncio, "sleep", fast)
        job = asyncio.get_event_loop().create_task(real_sleep(0.08))
        await runner._progress_loop(job, "browse the docs")

    asyncio.run(scenario())
    assert "working… (live view)" in frames  # real browser frame shown
    # unchanged screenshot on later ticks -> falls back to the panel
    assert "working…" in frames


def test_progress_loop_panel_only_without_opt_in(monkeypatch, tmp_path):
    pytest.importorskip("PIL")
    from hermes_msteams_bridge.call_tools import CallToolRunner

    frames = []

    class _Session:
        closed = False

        async def send_display_image(self, b64, mime, *, duration_ms=None, mode=None, caption=None):
            frames.append(caption)

    class _Handler:
        _session = _Session()
        _turn_id = 0
        _bridge = type("B", (), {"watch_browser_tasks": False})()

    runner = CallToolRunner(_Handler())
    import hermes_msteams_bridge.call_tools as ct
    import hermes_msteams_bridge.hermes_api as hermes_api

    async def must_not_dispatch(name, args, **kw):
        raise AssertionError("browser_vision dispatched without opt-in")

    monkeypatch.setattr(hermes_api, "dispatch_tool_async", must_not_dispatch)

    async def scenario():
        real_sleep = asyncio.sleep

        async def fast(s):
            await real_sleep(0.01)

        monkeypatch.setattr(ct.asyncio, "sleep", fast)
        job = asyncio.get_event_loop().create_task(real_sleep(0.05))
        await runner._progress_loop(job, "do a thing")

    asyncio.run(scenario())
    assert frames and all(c == "working…" for c in frames)


# ── phase 2b: gateway-resident adapter ───────────────────────────────────────


def test_deliver_by_voice_prefers_live_call(monkeypatch):
    from hermes_msteams_bridge import gateway_adapter as ga

    spoken = []

    class _Live:
        async def speak_text(self, text):
            spoken.append(text)

    ga.register_live_call("19:t@thread.v2", _Live())
    try:
        out = asyncio.run(ga.deliver_by_voice("aad-1", "your build is green", "19:t@thread.v2"))
        assert out["success"] is True and out["mode"] == "live-call"
        assert spoken == ["your build is green"]
    finally:
        ga._LIVE_CALLS.clear()


def test_deliver_by_voice_places_callback_when_no_live_call(monkeypatch, tmp_path):
    from hermes_msteams_bridge import gateway_adapter as ga
    from hermes_msteams_bridge import call_session_base as csb
    import hermes_msteams_bridge.gateway_adapter as ga_mod

    monkeypatch.setattr(csb, "_pending_dir", lambda: tmp_path)
    import hermes_msteams_bridge.config as config_mod

    monkeypatch.setattr(config_mod, "resolve_config", lambda extra=None: _cfg())

    async def fake_place_call(**kw):
        assert kw["user_object_id"] == "aad-1"
        return {"callId": "cb-1"}

    import hermes_msteams_bridge.outbound as outbound

    monkeypatch.setattr(outbound, "place_call", fake_place_call)
    out = asyncio.run(ga.deliver_by_voice("aad-1", "cron says hi", "19:home@thread.v2"))
    assert out["success"] is True and out["mode"] == "call-back"
    assert csb._pending_pop("cb-1") == "cron says hi"


def test_deliver_by_voice_enforces_allowlist(monkeypatch):
    from hermes_msteams_bridge import gateway_adapter as ga
    import hermes_msteams_bridge.config as config_mod

    monkeypatch.setattr(config_mod, "resolve_config", lambda extra=None: _cfg(allowlist=()))
    out = asyncio.run(ga.deliver_by_voice("aad-x", "hi"))
    assert "allowlist" in out["error"]


def test_standalone_voice_send_signature_parity():
    from hermes_msteams_bridge import gateway_adapter as ga
    import hermes_msteams_bridge.config as config_mod
    import pytest as _p

    # media_files/force_document accepted; unconfigured -> clean error dict
    out = asyncio.run(ga.standalone_voice_send(
        object(), "aad-1", "msg", thread_id=None, media_files=["x"], force_document=True
    ))
    assert "error" in out or "success" in out


def test_streaming_gateway_turn_roundtrip(monkeypatch):
    """Utterance -> MessageEvent -> (fake) gateway -> adapter.send -> spoken."""
    import sys
    import types

    import hermes_msteams_bridge.handlers as handlers_mod
    from hermes_msteams_bridge.config import TeamsVoiceConfig

    # Fake the sanctioned base surface for a bare interpreter.
    base = types.ModuleType("gateway.platforms.base")

    class MessageType:
        TEXT = "text"

    class MessageEvent:
        def __init__(self, text, message_type, source):
            self.text, self.message_type, self.source = text, message_type, source

    base.MessageEvent = MessageEvent
    base.MessageType = MessageType
    gw = sys.modules.get("gateway") or types.ModuleType("gateway")
    plat = types.ModuleType("gateway.platforms")
    monkeypatch.setitem(sys.modules, "gateway", gw)
    monkeypatch.setitem(sys.modules, "gateway.platforms", plat)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)

    handler = handlers_mod.StreamingCallSessionHandler(
        bridge_config=TeamsVoiceConfig(shared_secret="s")
    )
    handler._thread_id = "19:t@thread.v2"

    received = []

    class _Adapter:
        def build_source(self, **kw):
            received.append(("source", kw["chat_id"], kw.get("user_name")))
            return {"chat": kw["chat_id"]}

        async def handle_message(self, event):
            received.append(("event", event.text))

    handler._gateway_adapter = _Adapter()
    ok = asyncio.run(handler._emit_gateway_turn(handler._gateway_adapter, "what's our revenue?"))
    assert ok is True
    assert ("event", "what's our revenue?") in received
    assert received[0][1] == "19:t@thread.v2"


# ── rename: teams_call only (no legacy teams_voice support) ──────────────────


def test_env_reads_teams_call_only(monkeypatch):
    from hermes_msteams_bridge.config import plugin_env

    monkeypatch.setenv("TEAMS_CALL_PORT", "1111")  # legacy name: ignored
    monkeypatch.delenv("TEAMS_CALL_PORT", raising=False)
    assert plugin_env("TEAMS_CALL_PORT") == ""
    monkeypatch.setenv("TEAMS_CALL_PORT", "2222")
    assert plugin_env("TEAMS_CALL_PORT") == "2222"


def test_config_entry_teams_call_only(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "load_hermes_config", lambda: {
        "plugins": {"entries": {
            "teams_voice": {"config": {"port": 8443, "meeting_recap": True}},  # legacy: ignored
            "teams_call": {"config": {"port": 9443}},
        }}
    })
    block = hermes_api.plugin_config_block()
    assert block == {"port": 9443}


def test_register_wires_teams_call_surfaces_only():
    import hermes_msteams_bridge as pkg

    calls = []

    class _Ctx:
        def register_tool(self, **kw):
            calls.append(("tool", kw["name"]))

        def register_cli_command(self, **kw):
            calls.append(("cli", kw["name"]))

        def register_hook(self, name, cb):
            calls.append(("hook", name))

        def register_platform(self, **kw):
            calls.append(("platform", kw["name"]))

    try:
        pkg.register(_Ctx())
        cli_names = [n for kind, n in calls if kind == "cli"]
        assert cli_names == ["teams-call"]  # no legacy alias
        assert ("platform", "teams_call") in calls
        assert ("tool", "teams_call_status") in calls
    finally:
        import hermes_msteams_bridge.hermes_api as hermes_api

        hermes_api.set_plugin_context(None)
