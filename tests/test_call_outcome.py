"""Call-outcome wire: the worker reports a placed call's real terminal state
and the chat fallback fires immediately with accurate wording (the 180s stale
timer remains only as the safety net for workers without the signal)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import hermes_msteams_bridge.call_session_base as csb
from hermes_msteams_bridge import hmac_auth
from hermes_msteams_bridge.config import HEADER_SIGNATURE, HEADER_TIMESTAMP, TeamsVoiceConfig


@pytest.fixture
def pending(monkeypatch, tmp_path):
    monkeypatch.setattr(csb, "_pending_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(thread_id, text):
        calls.append((thread_id, text))
        return {"success": True, "message_id": "m1"}

    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "send_teams_message", fake_send)
    return calls


# ── deliver_outcome unit behavior ────────────────────────────────────────────


def test_unanswered_outcome_delivers_immediately(pending, sent):
    csb._pending_set("call-1", "your report is ready", thread_id="19:t@thread.v2")
    result = asyncio.run(csb.deliver_outcome("call-1", "no-answer"))
    assert result == {"ok": True, "delivered": True}
    thread, text = sent[0]
    assert thread == "19:t@thread.v2"
    assert "couldn't reach you" in text and "your report is ready" in text
    assert list(pending.iterdir()) == []  # entry consumed


@pytest.mark.parametrize("outcome,phrase", [
    ("declined", "declined my call"),
    ("busy", "line was busy"),
    ("failed", "could not be completed"),
    ("weird-new-state", "could not be completed"),  # unknown -> generic
])
def test_outcome_specific_wording(pending, sent, outcome, phrase):
    csb._pending_set("call-2", "msg", thread_id="19:t@thread.v2")
    asyncio.run(csb.deliver_outcome("call-2", outcome))
    assert phrase in sent[0][1]


def test_answered_leaves_pending_for_the_session(pending, sent):
    csb._pending_set("call-3", "greet me", thread_id="19:t@thread.v2")
    result = asyncio.run(csb.deliver_outcome("call-3", "answered"))
    assert result["ignored"] is True
    assert sent == []
    assert csb._pending_pop("call-3") == "greet me"  # session.start still finds it


def test_unknown_call_is_idempotent(pending, sent):
    result = asyncio.run(csb.deliver_outcome("nope", "no-answer", grace_s=0))
    assert result == {"ok": True, "ignored": True}
    assert sent == []


def test_repeat_outcome_is_idempotent(pending, sent):
    csb._pending_set("call-4", "msg", thread_id="19:t@thread.v2")
    asyncio.run(csb.deliver_outcome("call-4", "no-answer"))
    result = asyncio.run(csb.deliver_outcome("call-4", "no-answer", grace_s=0))
    assert result["ignored"] is True
    assert len(sent) == 1


def test_failed_send_restores_entry_for_the_reaper(pending, monkeypatch):
    async def failing_send(thread_id, text):
        return {"error": "boom"}

    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "send_teams_message", failing_send)
    csb._pending_set("call-5", "msg", thread_id="19:t@thread.v2")
    result = asyncio.run(csb.deliver_outcome("call-5", "no-answer"))
    assert result["delivered"] is False
    restored = list(pending.glob("*.json"))
    assert len(restored) == 1  # back for the stale reaper's retry


def test_no_thread_target_expires_entry(pending, sent):
    csb._pending_set("call-6", "msg", thread_id="")
    result = asyncio.run(csb.deliver_outcome("call-6", "no-answer"))
    assert result["delivered"] is False
    assert sent == []
    assert list(pending.iterdir()) == []


# ── HTTP route: HMAC contract shared with the WS upgrade ─────────────────────


SECRET = "outcome-secret"


def _signed_headers(call_id: str, secret: str = SECRET, ts: int | None = None) -> dict:
    ts = ts if ts is not None else int(time.time() * 1000)
    return {
        HEADER_TIMESTAMP: str(ts),
        HEADER_SIGNATURE: hmac_auth.sign(secret, ts, call_id),
    }


def _run_server_scenario(scenario):
    from hermes_msteams_bridge.bridge_server import BridgeServer
    from hermes_msteams_bridge.smoke import free_port

    async def runner():
        import aiohttp

        port = free_port()
        cfg = TeamsVoiceConfig(shared_secret=SECRET, host="127.0.0.1", port=port)
        server = BridgeServer(config=cfg)
        await server.start()
        try:
            async with aiohttp.ClientSession() as http:
                base = f"http://127.0.0.1:{port}/voice/msteams/stream/outcome"
                return await scenario(http, base)
        finally:
            await server.stop()

    return asyncio.run(runner())


def test_outcome_route_end_to_end(pending, sent):
    csb._pending_set("call-http", "wire message", thread_id="19:t@thread.v2")

    async def scenario(http, base):
        resp = await http.post(
            f"{base}/call-http",
            headers=_signed_headers("call-http"),
            json={"outcome": "no-answer"},
        )
        return resp.status, await resp.json()

    status, body = _run_server_scenario(scenario)
    assert status == 200
    assert body["delivered"] is True
    assert "wire message" in sent[0][1]


def test_outcome_route_rejects_bad_signature(pending, sent):
    async def scenario(http, base):
        resp = await http.post(
            f"{base}/call-x",
            headers=_signed_headers("call-x", secret="wrong-secret"),
            json={"outcome": "no-answer"},
        )
        return resp.status

    assert _run_server_scenario(scenario) == 401
    assert sent == []


def test_outcome_route_rejects_replayed_tuple(pending, sent):
    ts = int(time.time() * 1000)
    headers = _signed_headers("call-r", ts=ts)

    async def scenario(http, base):
        first = await http.post(f"{base}/call-r", headers=headers, json={"outcome": "busy"})
        second = await http.post(f"{base}/call-r", headers=headers, json={"outcome": "busy"})
        return first.status, second.status

    first, second = _run_server_scenario(scenario)
    assert first == 200
    assert second == 401  # single-use tuple, same as the WS upgrade


def test_outcome_route_rejects_malformed_body(pending, sent):
    async def scenario(http, base):
        no_json = await http.post(
            f"{base}/call-m", headers=_signed_headers("call-m"), data=b"not json"
        )
        missing = await http.post(
            f"{base}/call-m2", headers=_signed_headers("call-m2"), json={}
        )
        return no_json.status, missing.status

    assert _run_server_scenario(scenario) == (400, 400)


def test_ws_route_unaffected_by_outcome_route(pending):
    """The outcome route must not shadow the WS upgrade path."""

    async def scenario(http, base):
        # A GET to the WS path with bad auth still reaches the WS handler (401),
        # proving the POST route did not swallow the single-segment pattern.
        resp = await http.get(base.replace("/outcome", "") + "/some-call")
        return resp.status

    assert _run_server_scenario(scenario) == 401


# ── round 12: race, raising sender, non-object JSON ─────────────────────────


def test_early_outcome_waits_for_pending_record(pending, sent):
    """A worker can classify an instant failure and POST before the agent's
    place-call response was even parsed - the grace loop must catch the
    pending record appearing moments later."""

    async def scenario():
        async def late_pending():
            await asyncio.sleep(0.5)
            csb._pending_set("call-race", "late message", thread_id="19:t@thread.v2")

        writer = asyncio.create_task(late_pending())
        result = await csb.deliver_outcome("call-race", "busy", grace_s=5.0)
        await writer
        return result

    result = asyncio.run(scenario())
    assert result == {"ok": True, "delivered": True}
    assert "line was busy" in sent[0][1] and "late message" in sent[0][1]


def test_raising_sender_restores_claim(pending, monkeypatch):
    """An EXCEPTION from the sender (not just an error dict) must restore the
    claim immediately - not strand a .fallback until the stale reaper."""

    async def exploding_send(thread_id, text):
        raise RuntimeError("transport blew up")

    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "send_teams_message", exploding_send)
    csb._pending_set("call-boom", "msg", thread_id="19:t@thread.v2")
    result = asyncio.run(csb.deliver_outcome("call-boom", "no-answer"))
    assert result["delivered"] is False
    assert len(list(pending.glob("*.json"))) == 1  # restored for retry
    assert list(pending.glob("*.fallback")) == []


def test_outcome_route_rejects_non_object_json(pending, sent):
    async def scenario(http, base):
        arr = await http.post(
            f"{base}/call-a", headers=_signed_headers("call-a"), json=[]
        )
        scalar = await http.post(
            f"{base}/call-s", headers=_signed_headers("call-s"), json="busy"
        )
        return arr.status, scalar.status

    assert _run_server_scenario(scenario) == (400, 400)
    assert sent == []


def test_outcome_delivers_from_in_process_store(monkeypatch, sent):
    """No Hermes home (bare install): _pending_set falls back to the
    in-process dict - the outcome path must still find and deliver it."""
    monkeypatch.setattr(csb, "_pending_dir", lambda: None)
    csb._PENDING_OUTBOUND.clear()
    csb._pending_set("call-mem", "memory message", thread_id="19:t@thread.v2")
    result = asyncio.run(csb.deliver_outcome("call-mem", "no-answer", grace_s=0))
    assert result == {"ok": True, "delivered": True}
    assert "memory message" in sent[0][1]
    assert csb._PENDING_OUTBOUND == {}  # consumed
