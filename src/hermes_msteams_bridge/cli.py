"""``hermes teams-call`` CLI subcommands."""

from __future__ import annotations

import argparse
import os
import asyncio
import json
import signal

from .config import plugin_env, resolve_config


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes teams-call`` argparse tree."""
    subs = subparser.add_subparsers(dest="teams_call_command")
    subs.add_parser("status", help="Print bridge configuration and readiness")
    subs.add_parser(
        "smoke",
        help="Offline end-to-end smoke test: probe + ephemeral synthetic call (no Teams/provider)",
    )
    serve_p = subs.add_parser("serve", help="Run the bridge WebSocket server (foreground)")
    serve_p.add_argument("--host", default=None, help="Override bind host")
    serve_p.add_argument("--port", type=int, default=None, help="Override bind port")
    serve_p.add_argument(
        "--handler",
        choices=("logging", "echo", "realtime", "streaming"),
        default="logging",
        help=(
            "Call brain: 'logging' (no audio back), 'echo' (smile + echo caller "
            "audio — smoke test), 'realtime' (OpenAI/Azure speech-to-speech), "
            "'streaming' (STT -> agent -> TTS; works with any STT/TTS provider)."
        ),
    )


def teams_call_command(args) -> int:
    """Dispatch ``hermes teams-call`` subcommands. Returns an exit code."""
    command = getattr(args, "teams_call_command", None)

    if command == "status":
        # One status shape everywhere: the CLI prints exactly what the
        # teams_call_status tool reports (honest ok, port ownership,
        # plugin/platform enablement — round 8).
        from .tools import handle_teams_call_status

        print(json.dumps(json.loads(handle_teams_call_status()), indent=2))
        return 0

    if command == "smoke":
        from .smoke import run_smoke

        results = asyncio.run(run_smoke())
        print(json.dumps(results, indent=2))
        return 0 if results.get("ok") else 1

    if command == "serve":
        # D9: without a handler, Python's last-resort logger passes WARNING+
        # only — every INFO session line (session.start, call ids, teardown)
        # vanished from the serve log. Configure once, respect an existing setup.
        import logging as _logging

        if not _logging.getLogger().handlers:
            _logging.basicConfig(
                level=_logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            )
        from .bridge_server import CallSessionHandler
        from .hermes_api import hermes_version_note, probe_boundaries

        cfg = resolve_config()
        if not cfg.configured:
            print("error: no shared secret (set TEAMS_CALL_SHARED_SECRET)")
            return 1
        # Fail loudly at startup, not silently at turn 40: probe every Hermes
        # surface the call brains depend on and name what's missing. Feature
        # probes are the gate; the version line is advisory only.
        print(hermes_version_note())
        for b in probe_boundaries():  # each miss also logs at WARNING
            if not b["ok"]:
                print(f"warning: Hermes boundary missing: {b['surface']} ({b['detail']})")
            elif b.get("operational") is False:
                print(f"warning: present but not ready (check_fn failed): {b['surface']}")
        if args.host or args.port:
            from dataclasses import replace

            cfg = replace(cfg, host=args.host or cfg.host, port=args.port or cfg.port)
        if cfg.host not in ("127.0.0.1", "localhost", "::1"):
            print(
                f"warning: bridge bound to non-loopback host {cfg.host!r} — the shared "
                "secret is exposed to that interface; prefer 127.0.0.1 in production"
            )

        handler_kind = getattr(args, "handler", "logging")
        factory = CallSessionHandler  # default: log only, no audio back
        if handler_kind == "echo":
            from .handlers import EchoCallSessionHandler

            factory = EchoCallSessionHandler
        elif handler_kind == "realtime":
            from .handlers import RealtimeCallSessionHandler
            from .realtime.openai_client import realtime_config_from_env

            rt_cfg = realtime_config_from_env()
            if not rt_cfg.configured:
                print(
                    "error: realtime handler needs an API key "
                    "(OPENAI_API_KEY, or AZURE_FOUNDRY_API_KEY / TEAMS_CALL_REALTIME_API_KEY for Azure)"
                )
                return 1
            factory = lambda: RealtimeCallSessionHandler(rt_cfg, bridge_config=cfg)  # noqa: E731
        elif handler_kind == "streaming":
            import shutil

            if shutil.which("ffmpeg") is None:
                print("warning: streaming mode needs 'ffmpeg' on PATH to decode TTS audio")
            from .handlers import StreamingCallSessionHandler

            factory = lambda: StreamingCallSessionHandler(bridge_config=cfg)  # noqa: E731

        async def _run() -> None:
            from .service import VoiceBridgeService

            service = VoiceBridgeService(config=cfg, handler_factory=factory)
            await service.start()  # listener + reaper + durable-job resume

            # StandIn managed chat lane (protocol/chat-schema.yaml): started beside the voice server when
            # configured under THIS plugin's config (managed_chat.secret, or
            # TEAMS_CALL_MANAGED_CHAT_SECRET) - same namespace as the voice lane, not a second one. One
            # AgentConsult per Teams conversation, so a chat keeps its context across messages - the same
            # session-continuity model the voice consult uses. Voice is untouched when unset.
            managed_chat = None
            chat_secret = cfg.managed_chat_secret
            if chat_secret:
                from .agent_consult import AgentConsult
                from .managed_chat import (
                    InboundChat,
                    attachments_note,
                    start_managed_chat_if_configured,
                )

                # LRU-capped - unbounded growth is a slow leak on a long-lived process.
                # Eviction is SAFE here because continuity lives in the session_id (stable per
                # conversation), not in the AgentConsult instance: a re-created consult resumes the
                # same session.
                consults: dict[str, AgentConsult] = {}
                max_consults = 64

                async def _respond(message: InboundChat) -> str:
                    key = f"{message.tenant_id}:{message.conversation_id}"
                    consult = consults.pop(key, None) or AgentConsult(session_id=f"msteams-chat:{key}")
                    consults[key] = consult  # re-insert = most recently used (dicts keep order)
                    while len(consults) > max_consults:
                        consults.pop(next(iter(consults)))
                    note = attachments_note(message.attachments)
                    # Card submits arrive with EMPTY text and the payload in card_action (protocol v1
                    # additive field): fold it in so a button press is a meaningful turn.
                    card_note = (
                        f"[card button pressed - submit payload: {json.dumps(message.card_action)}]"
                        if message.card_action else ""
                    )
                    query = "\n".join(x for x in (message.text, card_note, note) if x)
                    # ask() defaults to a 45s VOICE budget; chat turns run long. 280s stays
                    # under the server's TURN_TIMEOUT_S (300) so the consult's own timeout message
                    # reaches the user instead of the blunt turn-level one.
                    return await consult.ask(query, timeout_s=280.0)

                # Shared with the gateway-resident adapter so the two hosting paths cannot drift.
                #
                # If the chat lane fails to start - a port already in use is the ordinary case - the
                # VOICE service is already listening, and letting the exception escape here left it
                # running inside a process that was on its way out: an orphaned listener holding the
                # calling port, so the retry the operator immediately tries cannot bind either.
                try:
                    managed_chat = await start_managed_chat_if_configured(cfg, _respond)
                except Exception:
                    await service.stop()
                    raise
            # Graceful shutdown: SIGTERM (AKS/Docker rolling deploys) and SIGINT
            # (Ctrl-C) set the stop event so server.stop() drains live calls -
            # closing each with a reason so per-call teardown runs and the
            # provider realtime session is released instead of leaking + billing
            # until the provider times it out.
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    # add_signal_handler is unavailable on some platforms (Windows);
                    # SIGINT there still raises KeyboardInterrupt, handled below.
                    pass
            try:
                await stop.wait()
            finally:
                if managed_chat is not None:
                    await managed_chat.stop()
                await service.stop()

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            pass
        return 0

    print("usage: hermes teams-call {status|serve|smoke}")
    return 2
