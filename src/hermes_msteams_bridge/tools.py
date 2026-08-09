"""Agent-facing tools for the teams_call bridge.

Two global agent tools: the read-only ``teams_call_status`` probe and
``call_user`` — chat-to-call (§3.6): a user asks in Teams chat, the agent
places a voice call and speaks the message on answer. The realtime call tools
(``look_at_screen``, ``show_to_caller``, ``post_meeting_minutes``) are surfaced
to the *realtime model* per-call by the dialogue handler, not registered here —
they only make sense inside an active call.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict

from .config import plugin_env, resolve_config

TEAMS_CALL_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "teams_call_status",
    "description": (
        "Report the Microsoft Teams voice/video (CVI) bridge configuration and "
        "readiness: bind host/port, whether a shared secret is configured, "
        "whether the aiohttp dependency is available, and a probe of every "
        "Hermes surface the bridge depends on (vision, STT, TTS, delivery). "
        "Does not reveal the secret."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def check_requirements() -> bool:
    """The bridge needs aiohttp (already a Hermes gateway dependency)."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def _port_active(host: str, port: int) -> bool:
    """True when something is accepting on host:port — the bridge itself, or
    a conflicting process (either way: the port is owned)."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def handle_teams_call_status(args: dict | None = None, **_kwargs: Any) -> str:
    """Status probe. Hermes dispatches ``handler(args, **kwargs)``, so the
    positional args dict must be accepted even though this tool takes none."""
    from .hermes_api import hermes_version_note, load_hermes_config, probe_boundaries

    cfg = resolve_config()
    boundaries = probe_boundaries()  # logs each missing surface at WARNING
    deps = check_requirements()
    boundaries_ok = all(b["ok"] for b in boundaries)
    try:
        hermes_cfg = load_hermes_config() or {}
    except Exception:  # noqa: BLE001 — status must never crash on bad config
        hermes_cfg = {}
    enabled = (hermes_cfg.get("plugins") or {}).get("enabled") or []
    plat = (hermes_cfg.get("platforms") or {}).get("teams_call") or {}
    return json.dumps(
        {
            # Honest verdict (round 8): unconfigured or missing surfaces is
            # NOT ok — "ok": true used to be unconditional.
            "ok": bool(cfg.configured and deps and boundaries_ok),
            "configured": cfg.configured,  # bool — never the secret itself
            "host": cfg.host,
            "port": cfg.port,
            "path": cfg.path,
            # Someone (bridge or a conflicting process) owns the configured port.
            "port_active": _port_active(cfg.host, cfg.port),
            # StandIn Managed Bot chat lane. Reported alongside voice because "is chat working?"
            # was previously unanswerable from here - the status tool only knew about the voice
            # socket, so a dead chat lane looked identical to a healthy one.
            "managed_bot": {
                "chat_configured": bool(cfg.managed_chat_secret),  # bool - never the secret
                "chat_host": cfg.managed_chat_host,
                "chat_port": cfg.managed_chat_port,
                "chat_path": cfg.managed_chat_path,
                "chat_port_active": (
                    _port_active(
                        "127.0.0.1" if cfg.managed_chat_host in ("0.0.0.0", "::") else cfg.managed_chat_host,
                        cfg.managed_chat_port,
                    )
                    if cfg.managed_chat_secret
                    else False
                ),
            },
            "plugin_enabled": "teams_call" in enabled if isinstance(enabled, list) else False,
            "platform_enabled": bool(plat.get("enabled")) if isinstance(plat, dict) else False,
            "deps_available": deps,
            "hermes": hermes_version_note(),
            "boundaries": boundaries,
            # contract_available: every surface exists. operational_ready:
            # additionally, no provider/config check_fn failed (None = n/a).
            "boundaries_ok": boundaries_ok,
            "operational_ok": all(b.get("operational") is not False for b in boundaries),
        }
    )


# ── call_user: chat-to-call ──────────────────────────────────────────────────

CALL_USER_SCHEMA: Dict[str, Any] = {
    "name": "call_user",
    "description": (
        "Place an outbound Microsoft Teams voice call to a user and speak a "
        "message when they answer. Use when someone asks to be called with an "
        "answer or update ('call me and explain X'). The call is placed by the "
        "StandIn media bridge; the voice agent delivers the message and can "
        "then continue the conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_aad_id": {
                "type": "string",
                "description": "Azure AD object id of the user to call.",
            },
            "message": {
                "type": "string",
                "description": "What to say when they answer (spoken, so keep it natural).",
            },
        },
        "required": ["user_aad_id", "message"],
    },
}


logger = __import__("logging").getLogger(__name__)

# Outbound dial rate limit (review round 5): a chat surface must not be able
# to burst-dial. Sliding hour window, process-local.
_CALL_TIMES: list = []
_CALL_LIMIT_PER_HOUR = 6


def _call_user_rate_ok() -> bool:
    """Check only — the slot is recorded on successful placement, so failed
    dials and worker errors never burn the hourly budget."""
    import time as _time

    now = _time.monotonic()
    _CALL_TIMES[:] = [t for t in _CALL_TIMES if now - t < 3600]
    return len(_CALL_TIMES) < _CALL_LIMIT_PER_HOUR


def _call_user_rate_record() -> None:
    import time as _time

    _CALL_TIMES.append(_time.monotonic())


def _run_coro_blocking(coro):
    """Run a coroutine from a sync tool handler, inside or outside a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is running in this thread (gateway-side dispatch): hop threads.
    box: dict = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            box["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def handle_call_user(args: dict | None = None, **kwargs: Any) -> str:
    """Place the call and register the message for greet-on-answer delivery."""
    params = {**(args or {}), **kwargs}
    aad_id = str(params.get("user_aad_id") or "").strip()
    message = str(params.get("message") or "").strip()
    if not aad_id or not message:
        return json.dumps({"error": "user_aad_id and message are required"})

    cfg = resolve_config()
    if not cfg.configured:
        return json.dumps({"error": "teams_call is not configured (no shared secret)"})
    # STRICTER than inbound (review round 5): outbound dialing requires an
    # EXPLICIT allowlist entry — allow_all covers who may call the bot, never
    # who the bot may dial. Otherwise a chat user (or a prompt injection in
    # any chat surface) turns the bot into a model-controlled dialer.
    if not cfg.allowlist or (aad_id or "").strip().lower() not in cfg.allowlist:
        return json.dumps(
            {"error": "outbound calls require the callee on an explicit teams_call allowlist"}
        )
    if not _call_user_rate_ok():
        return json.dumps({"error": "outbound call rate limit reached; try again later"})
    # Tenant comes from operator config only — never from the model.
    tenant = cfg.tenant_id
    if not tenant:
        return json.dumps({"error": "no tenant configured (set TEAMS_CALL_TENANT_ID)"})

    from .call_session_base import _pending_set
    from .outbound import OutboundError, place_call

    try:
        result = _run_coro_blocking(
            place_call(
                user_object_id=aad_id,
                tenant_id=tenant,
                shared_secret=cfg.shared_secret,
                worker_base_url=cfg.worker_base_url,
                allow_remote=cfg.allow_remote_worker,
            )
        )
    except OutboundError as exc:
        return json.dumps({"error": f"could not place the call: {exc}"})
    call_id = (result or {}).get("callId")
    if not call_id:
        # place_call returns {"raw": ...} on malformed 200s — that is not success.
        return json.dumps({"error": "the worker accepted the request but returned no callId"})
    _call_user_rate_record()
    # The outbound leg's session.start pops this and speaks it on answer. The
    # originating Teams chat (gateway session context) is the §3.7 no-answer
    # fallback target — without it an unanswered chat-to-call died silently.
    from .hermes_api import current_chat_context

    platform, chat_id = current_chat_context()
    fallback_thread = chat_id if platform == "teams" else ""
    _pending_set(call_id, message, thread_id=fallback_thread)
    logger.info(
        "[teams_call] AUDIT call_user: callee=%s callId=%s fallback_thread=%s",
        aad_id, call_id, "yes" if fallback_thread else "no",
    )
    return json.dumps(
        {"success": True, "callId": call_id, "note": "calling now; message is spoken on answer"}
    )
