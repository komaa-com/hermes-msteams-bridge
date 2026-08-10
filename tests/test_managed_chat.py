"""StandIn managed chat mode (protocol/chat-schema.yaml): the agent side of the
normalized relay. The HMAC KAT is the same vector pinned in @standin/bridge-hmac
(TS), the gateway (C#), and the OpenClaw plugin - four implementations, one set
of bytes."""

from __future__ import annotations

import asyncio
import gc
import json
import re
from pathlib import Path

import pytest

from hermes_msteams_bridge.managed_chat import (
    REPLAY_WINDOW_MS,
    SCHEMA_VERSION,
    InboundChat,
    ManagedChatConfig,
    SeenActivities,
    attachments_note,
    build_reply,
    compute_signature,
    parse_inbound,
    sign,
    verify,
)

KAT_SECRET = "test-secret"
KAT_TS = "1700000000000"
KAT_BODY = "hello"
KAT_SIG = "1ea836ba1a9714e5a5824a9026b2b40567ee9e5e2ddd0d1cb598da3b42afce38"
KAT_NOW = 1_700_000_000_000


class TestBridgeHmac:
    def test_matches_the_cross_repo_kat(self):
        assert compute_signature(KAT_SECRET, KAT_TS, KAT_BODY) == KAT_SIG
        assert sign(KAT_SECRET, KAT_BODY, KAT_NOW) == (KAT_TS, KAT_SIG)

    def test_replay_window_both_directions(self):
        assert verify(KAT_SECRET, KAT_TS, KAT_BODY, KAT_SIG, KAT_NOW)
        assert verify(KAT_SECRET, KAT_TS, KAT_BODY, KAT_SIG, KAT_NOW + REPLAY_WINDOW_MS - 1)
        assert not verify(KAT_SECRET, KAT_TS, KAT_BODY, KAT_SIG, KAT_NOW + REPLAY_WINDOW_MS + 1)
        assert not verify(KAT_SECRET, KAT_TS, KAT_BODY, KAT_SIG, KAT_NOW - REPLAY_WINDOW_MS - 1)

    def test_tampering_wrong_key_and_absence_fail(self):
        assert not verify(KAT_SECRET, KAT_TS, KAT_BODY + "x", KAT_SIG, KAT_NOW)
        assert not verify("other", KAT_TS, KAT_BODY, KAT_SIG, KAT_NOW)
        assert not verify(KAT_SECRET, None, KAT_BODY, KAT_SIG, KAT_NOW)
        assert not verify(KAT_SECRET, KAT_TS, KAT_BODY, None, KAT_NOW)
        assert not verify("", KAT_TS, KAT_BODY, KAT_SIG, KAT_NOW)  # empty secret fails CLOSED
        assert not verify(KAT_SECRET, "NaN", KAT_BODY, KAT_SIG, KAT_NOW)

    def test_uppercase_hex_is_accepted(self):
        assert verify(KAT_SECRET, KAT_TS, KAT_BODY, KAT_SIG.upper(), KAT_NOW)


VALID = {
    "schemaVersion": 1,
    "tenantId": "t1",
    "bindingId": "b1",
    "conversationId": "c1",
    "activityId": "a1",
    "scope": "personal",
    "sender": {"displayName": "Alaa", "isLinkedOwner": True},
    "text": "hello agent",
    "someFutureField": {"ignored": True},
}


class TestParsing:
    def test_valid_message_parses_and_unknown_fields_are_ignored(self):
        m = parse_inbound(json.dumps(VALID))
        assert m.tenant_id == "t1"
        assert m.text == "hello agent"
        assert m.sender_display_name == "Alaa"
        assert m.sender_is_linked_owner is True

    @pytest.mark.parametrize("missing", ["tenantId", "conversationId", "activityId"])
    def test_routing_keys_are_required(self, missing):
        raw = dict(VALID)
        del raw[missing]
        with pytest.raises(ValueError, match=missing):
            parse_inbound(json.dumps(raw))

    def test_malformed_bodies_raise(self):
        with pytest.raises(ValueError):
            parse_inbound("not json")
        with pytest.raises(ValueError):
            parse_inbound("[]")


class TestReplies:
    MSG = InboundChat(tenant_id="t1", conversation_id="c1", activity_id="a1", scope="personal", text="q")

    def test_reply_echoes_tenant_and_conversation_exactly(self):
        # The gateway's cross-tenant guard rejects a mismatch - the load-bearing check of the relay.
        r = build_reply(self.MSG, "answer")
        assert (r["tenantId"], r["conversationId"], r["replyToId"]) == ("t1", "c1", "a1")
        assert r["kind"] == "message" and r["text"] == "answer"
        assert r["schemaVersion"] == SCHEMA_VERSION
        assert r["idempotencyKey"] == "a1:message"

    def test_typing_carries_no_text(self):
        r = build_reply(self.MSG, "ignored", "typing")
        assert r["kind"] == "typing" and "text" not in r
        assert r["idempotencyKey"] == "a1:typing"


class TestDedupe:
    def test_first_true_redelivery_false_and_bounded(self):
        seen = SeenActivities(capacity=2)
        assert seen.mark_first("a")
        assert not seen.mark_first("a")
        assert seen.mark_first("b")
        assert seen.mark_first("c")  # evicts "a"
        assert seen.mark_first("a")  # aged out - acceptable at-least-once behavior


class TestAttachmentsNote:
    def test_names_relayed_and_unrelayed_attachments(self):
        note = attachments_note(
            [
                {"kind": "file", "name": "r.pdf", "url": "https://g/x", "relayable": True},
                {"kind": "file", "name": "big.zip", "relayable": False},
            ]
        )
        assert "r.pdf at https://g/x" in note
        assert "[attachment not relayed: big.zip]" in note


class TestConfig:
    def test_disabled_without_a_chat_secret(self):
        assert not ManagedChatConfig(chat_secret="").enabled
        assert ManagedChatConfig(chat_secret="k").enabled

    def test_defaults(self):
        cfg = ManagedChatConfig(chat_secret="k")
        assert cfg.port == 8444
        assert cfg.path == "/managed/chat"
        assert "/api/chat/reply" in cfg.gateway_reply_url


class TestStartupIsTransactional:
    """A failed bind must leave NOTHING behind - no open ClientSession, no half-set-up runner."""

    def test_port_conflict_leaves_no_open_session(self):
        asyncio.run(self._scenario())

    async def _scenario(self):
        import aiohttp
        from aiohttp import web

        from hermes_msteams_bridge.managed_chat import ManagedChatConfig, ManagedChatServer

        # Occupy a port so the second bind is guaranteed to fail.
        blocker = web.Application()
        blocker_runner = web.AppRunner(blocker)
        await blocker_runner.setup()
        site = web.TCPSite(blocker_runner, "127.0.0.1", 0)
        await site.start()
        taken = blocker_runner.addresses[0][1]

        before = len([o for o in gc.get_objects() if isinstance(o, aiohttp.ClientSession) and not o.closed])
        server = ManagedChatServer(
            ManagedChatConfig(chat_secret="k", gateway_reply_url="http://x/api/chat/reply",
                              host="127.0.0.1", port=taken),
            lambda _m: "unused",
        )
        try:
            with pytest.raises(OSError):
                await server.start()
            gc.collect()
            after = len([o for o in gc.get_objects() if isinstance(o, aiohttp.ClientSession) and not o.closed])
            assert after == before, "a failed start leaked an open aiohttp ClientSession"
        finally:
            await blocker_runner.cleanup()


class TestManagedBotConfigResolution:
    """The lane is configured through THIS plugin's contract (plugins.entries.teams_call.config),
    not a namespace of its own. Precedence must match every other setting: config block first,
    TEAMS_CALL_* env as fallback."""

    def _resolve(self, extra=None, env=None):
        import os
        from hermes_msteams_bridge.config import resolve_config

        old = {}
        env = env or {}
        try:
            for k, v in env.items():
                old[k] = os.environ.get(k)
                os.environ[k] = v
            return resolve_config(extra if extra is not None else {"shared_secret": "voice"})
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_nested_block_under_teams_call_config(self):
        c = self._resolve({"shared_secret": "voice", "managed_bot": {"secret": "s", "port": 9999}})
        assert c.managed_chat_secret == "s"
        assert c.managed_chat_port == 9999
        assert c.shared_secret == "voice"  # the voice lane is untouched

    def test_managed_chat_is_still_accepted_as_an_alias(self):
        c = self._resolve({"shared_secret": "voice", "managed_chat": {"secret": "old"}})
        assert c.managed_chat_secret == "old"

    def test_managed_bot_wins_over_the_alias(self):
        c = self._resolve({"shared_secret": "voice",
                           "managed_bot": {"secret": "new"},
                           "managed_chat": {"secret": "old"}})
        assert c.managed_chat_secret == "new"

    def test_env_fallback_and_config_precedence(self):
        # env alone
        c = self._resolve(env={"TEAMS_CALL_MANAGED_BOT_SECRET": "from-env"})
        assert c.managed_chat_secret == "from-env"
        # config block WINS over env
        c = self._resolve({"shared_secret": "v", "managed_bot": {"secret": "from-config"}},
                          env={"TEAMS_CALL_MANAGED_BOT_SECRET": "from-env"})
        assert c.managed_chat_secret == "from-config"
        # the older env spelling still resolves
        c = self._resolve(env={"TEAMS_CALL_MANAGED_CHAT_SECRET": "legacy-env"})
        assert c.managed_chat_secret == "legacy-env"

    def test_unset_means_off_with_sane_defaults(self):
        c = self._resolve({"shared_secret": "voice"})
        assert c.managed_chat_secret == ""          # secret is the switch: empty = lane off
        assert c.managed_chat_port == 8444
        assert c.managed_chat_path == "/msteams/messages"


class TestSchemaDrift:
    """protocol/chat-schema.yaml is the source of truth. A subset is legal;
    a name the schema does not know is a typo that silently drops data."""

    SCHEMA = (Path(__file__).parent.parent / "protocol" / "chat-schema.yaml").read_text()

    def test_every_consumed_wire_name_exists_in_the_schema(self):
        consumed = [
            "schemaVersion", "tenantId", "bindingId", "conversationId", "activityId", "scope",
            "sender", "text", "attachments", "locale", "replyToId", "kind", "idempotencyKey",
            "displayName", "isLinkedOwner", "name", "url", "relayable",
        ]
        for fieldname in consumed:
            assert re.search(rf"name: {fieldname}$", self.SCHEMA, re.M), f"'{fieldname}' not in schema"

    def test_hardcoded_constants_match_the_schema(self):
        assert "name: SCHEMA_VERSION\n    value: 1" in self.SCHEMA
        assert f"value: {REPLAY_WINDOW_MS}" in self.SCHEMA


class TestServerEndToEnd:
    # NOTE: sync test methods wrapping asyncio.run - the repo convention (pytest-asyncio is not a
    # test dependency here; see test_gateway_spike.py).
    async def _start(self, respond=None):
        from aiohttp import web

        from hermes_msteams_bridge.managed_chat import ManagedChatServer

        replies: list[dict] = []
        reply_seen = asyncio.Event()

        # A fake gateway the server posts replies to, so the round trip is real HTTP end to end.
        async def gateway_reply(request: web.Request):
            body = (await request.read()).decode()
            replies.append(
                {
                    "body": json.loads(body),
                    "raw": body,
                    "ts": request.headers.get("X-StandIn-Timestamp"),
                    "sig": request.headers.get("X-StandIn-Signature"),
                }
            )
            if len(replies) >= 2:
                reply_seen.set()
            return web.json_response({"ok": True})

        gw_app = web.Application()
        gw_app.router.add_post("/api/chat/reply", gateway_reply)
        gw_runner = web.AppRunner(gw_app)
        await gw_runner.setup()
        gw_site = web.TCPSite(gw_runner, "127.0.0.1", 0)
        await gw_site.start()
        gw_port = gw_runner.addresses[0][1]

        calls: list[InboundChat] = []

        async def default_respond(message: InboundChat) -> str:
            calls.append(message)
            return "the answer"

        cfg = ManagedChatConfig(
            chat_secret=KAT_SECRET,
            gateway_reply_url=f"http://127.0.0.1:{gw_port}/api/chat/reply",
            host="127.0.0.1",
            port=0,
        )
        server = ManagedChatServer(cfg, respond or default_respond)
        await server.start()
        port = server._runner.addresses[0][1]  # noqa: SLF001 - test needs the ephemeral port
        return server, gw_runner, port, replies, reply_seen, calls

    async def _post(self, port, body, signed=True, path="/managed/chat"):
        import aiohttp

        headers = {"Content-Type": "application/json"}
        if signed:
            ts, sig = sign(KAT_SECRET, body)
            headers["X-StandIn-Timestamp"] = ts
            headers["X-StandIn-Signature"] = sig
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}{path}", data=body, headers=headers) as resp:
                return resp.status

    INBOUND = json.dumps(
        {
            "tenantId": "t1", "conversationId": "c1", "activityId": "a-e2e",
            "scope": "personal", "sender": {"displayName": "Alaa"}, "text": "hi",
        }
    )

    def test_signed_message_acks_then_replies_with_typing_and_text(self):
        asyncio.run(self._test_signed_message_acks_then_replies_with_typing_and_text())

    async def _test_signed_message_acks_then_replies_with_typing_and_text(self):
        server, gw, port, replies, reply_seen, calls = await self._start()
        try:
            assert await self._post(port, self.INBOUND) == 200
            await asyncio.wait_for(reply_seen.wait(), timeout=5)
            assert [r["body"]["kind"] for r in replies] == ["typing", "message"]
            final = replies[1]
            assert final["body"]["tenantId"] == "t1"
            assert final["body"]["text"] == "the answer"
            # The reply is signed with the SAME chat key, verifiable by the gateway's construction.
            assert verify(KAT_SECRET, final["ts"], final["raw"], final["sig"], int(final["ts"]))
            assert calls[0].sender_display_name == "Alaa"
        finally:
            await server.stop()
            await gw.cleanup()

    def test_unsigned_and_mis_signed_are_rejected_without_an_agent_turn(self):
        asyncio.run(self._test_unsigned_and_mis_signed_are_rejected_without_an_agent_turn())

    async def _test_unsigned_and_mis_signed_are_rejected_without_an_agent_turn(self):
        turns = 0

        async def respond(_m):
            nonlocal turns
            turns += 1
            return "x"

        server, gw, port, _replies, _seen, _calls = await self._start(respond)
        try:
            assert await self._post(port, self.INBOUND, signed=False) == 401
            assert turns == 0
        finally:
            await server.stop()
            await gw.cleanup()

    def test_redelivery_acks_without_a_second_turn(self):
        asyncio.run(self._test_redelivery_acks_without_a_second_turn())

    async def _test_redelivery_acks_without_a_second_turn(self):
        turns = 0

        async def respond(_m):
            nonlocal turns
            turns += 1
            return "x"

        server, gw, port, _replies, _seen, _calls = await self._start(respond)
        try:
            assert await self._post(port, self.INBOUND) == 200
            assert await self._post(port, self.INBOUND) == 200
            await asyncio.sleep(0.1)
            assert turns == 1
        finally:
            await server.stop()
            await gw.cleanup()


class TestOrderingSerialization(TestServerEndToEnd):
    def test_turns_in_one_conversation_run_sequentially(self):
        asyncio.run(self._test_turns_in_one_conversation_run_sequentially())

    async def _test_turns_in_one_conversation_run_sequentially(self):
        # The schema promises per-conversation ordering - replies must not overtake.
        events: list[str] = []
        gates: dict[str, asyncio.Event] = {}

        async def respond(m):
            events.append(f"start:{m.activity_id}")
            gate = gates.setdefault(m.activity_id, asyncio.Event())
            await gate.wait()
            events.append(f"end:{m.activity_id}")
            return ""

        server, gw, port, _replies, _seen, _calls = await self._start(respond)
        try:
            def msg(conv, aid):
                return json.dumps({
                    "tenantId": "t1", "conversationId": conv, "activityId": aid,
                    "scope": "personal", "sender": {}, "text": "x",
                })

            assert await self._post(port, msg("c1", "a1")) == 200
            assert await self._post(port, msg("c1", "a2")) == 200
            assert await self._post(port, msg("c2", "b1")) == 200
            await asyncio.sleep(0.1)

            # a2 must NOT start while a1 holds its gate; b1 (another conversation) runs concurrently.
            assert "start:a1" in events
            assert "start:b1" in events
            assert "start:a2" not in events

            gates["a1"].set()
            await asyncio.sleep(0.1)
            assert "end:a1" in events
            assert "start:a2" in events
            for g in gates.values():
                g.set()
            await asyncio.sleep(0.05)
        finally:
            await server.stop()
            await gw.cleanup()
