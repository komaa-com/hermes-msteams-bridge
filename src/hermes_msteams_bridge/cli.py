"""``hermes teams-voice`` CLI subcommands."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal

from .config import resolve_config


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes teams-voice`` argparse tree."""
    subs = subparser.add_subparsers(dest="teams_voice_command")
    subs.add_parser("status", help="Print bridge configuration and readiness")
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


def teams_voice_command(args) -> int:
    """Dispatch ``hermes teams-voice`` subcommands. Returns an exit code."""
    command = getattr(args, "teams_voice_command", None)

    if command == "status":
        cfg = resolve_config()
        print(
            json.dumps(
                {
                    "configured": cfg.configured,
                    "host": cfg.host,
                    "port": cfg.port,
                    "path": cfg.path,
                },
                indent=2,
            )
        )
        return 0

    if command == "serve":
        from .bridge_server import BridgeServer, CallSessionHandler

        cfg = resolve_config()
        if not cfg.configured:
            print("error: no shared secret (set TEAMS_VOICE_SHARED_SECRET)")
            return 1
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
                    "(OPENAI_API_KEY, or AZURE_FOUNDRY_API_KEY / TEAMS_VOICE_REALTIME_API_KEY for Azure)"
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
            server = BridgeServer(config=cfg, handler_factory=factory)
            await server.start()
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
                await server.stop()

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            pass
        return 0

    print("usage: hermes teams-voice {status|serve}")
    return 2
