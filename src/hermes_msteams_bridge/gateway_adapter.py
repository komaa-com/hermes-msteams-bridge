"""Phase 2b — gateway-resident platform adapter (spike verdict: GO).

``msteams_bridge`` registers as a real gateway platform: ``connect()`` hosts the
bridge (a thin shell over :class:`~.service.VoiceBridgeService`, whose
lifecycle contract the ADR-6 spike proved), ``disconnect()`` stops it, and
``send()`` delivers text BY VOICE — into a live call when one exists for the
chat, else as a place-call-and-speak call-back. ``standalone_sender_fn`` gives
cron ``deliver=msteams_bridge`` the same semantics from any process.

Import discipline: this module is the ONE place allowed to import
``gateway.platforms.base`` / ``gateway.config`` — the sanctioned surface the
platform-adapter developer guide tells platform plugins to subclass (the AST
boundary test enforces exactly that scope). Everything is imported lazily so
the module stays importable on a bare interpreter; the class only exists on a
real host.

Modes under the gateway:
* **realtime** — unchanged speech-to-speech loop (ADR-1: the call is ours);
  gateway ``send()`` routes into the live call via ``request_say``.
* **streaming** — caller utterances become real gateway agent turns
  (``build_source`` -> ``MessageEvent`` -> ``handle_message``), inheriting
  gateway sessions, authorization, and approvals; the agent's reply comes
  back through ``send()`` and is spoken. ``AgentConsult`` remains only for
  standalone ``serve`` and the realtime consult tool.

``hermes msteams-bridge serve`` remains the standalone fallback (two-process
model), unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ADAPTER_CLASS: Any = None

# chat/thread id -> live call handler, shared by the adapter instance and the
# standalone sender so a delivery targets a live call when one exists.
_LIVE_CALLS: dict[str, Any] = {}


def register_live_call(thread_id: str, handler: Any) -> None:
    if thread_id:
        _LIVE_CALLS[thread_id] = handler


def unregister_live_call(thread_id: str, handler: Any) -> None:
    if thread_id and _LIVE_CALLS.get(thread_id) is handler:
        _LIVE_CALLS.pop(thread_id, None)


def live_call_for(thread_id: str) -> Any:
    return _LIVE_CALLS.get(thread_id)


async def deliver_by_voice(chat_id: str, message: str, thread_id: str = "") -> dict:
    """Voice delivery core, process-agnostic (used by send() and cron).

    A live call for ``chat_id``/``thread_id`` gets the text spoken into it;
    otherwise ``chat_id`` is treated as the callee's AAD object id and a
    call-back is placed with the message registered for greet-on-answer
    (no-answer chat fallback applies when ``thread_id`` is known).
    """
    from .call_session_base import _pending_set
    from .config import plugin_env, resolve_config
    from .outbound import OutboundError, place_call

    handler = live_call_for(thread_id or chat_id)
    if handler is not None:
        speak = getattr(handler, "speak_text", None)
        if speak is not None:
            try:
                await speak(message)
                return {"success": True, "message_id": None, "mode": "live-call"}
            except Exception as exc:  # noqa: BLE001
                logger.warning("[msteams_bridge] live-call speak failed: %s", exc)

    cfg = resolve_config()
    if not cfg.configured:
        return {"error": "msteams_bridge is not configured (no shared secret)"}
    aad_id = (chat_id or "").strip().lower()
    # Outbound dialing keeps call_user's posture: explicit allowlist only.
    if not cfg.allowlist or aad_id not in cfg.allowlist:
        return {"error": "voice delivery requires the callee on an explicit msteams_bridge allowlist"}
    if not cfg.tenant_id:
        return {"error": "no tenant configured (set MSTEAMS_BRIDGE_TENANT_ID)"}
    # Same sliding-hour dial budget as call_user (round 8): cron/gateway
    # call-backs must not be an unmetered outbound-dial path.
    from .tools import _call_user_rate_ok, _call_user_rate_record

    if not _call_user_rate_ok():
        return {"error": "outbound call rate limit reached; try again later"}
    try:
        result = await place_call(
            user_object_id=aad_id,
            tenant_id=cfg.tenant_id,
            shared_secret=cfg.shared_secret,
            worker_base_url=cfg.worker_base_url,
            allow_remote=cfg.allow_remote_worker,
        )
    except OutboundError as exc:
        return {"error": f"could not place the call: {exc}"}
    call_id = (result or {}).get("callId")
    if not call_id:
        return {"error": "the worker accepted the request but returned no callId"}
    _call_user_rate_record()  # success-only, same policy as call_user
    _pending_set(call_id, message, thread_id=thread_id or "")
    logger.info("[msteams_bridge] AUDIT voice delivery: callee=%s callId=%s", aad_id, call_id)
    return {"success": True, "message_id": call_id, "mode": "call-back"}


async def standalone_voice_send(
    pconfig: Any, chat_id: str, message: str, *, thread_id: str | None = None,
    media_files: Any = None, force_document: bool = False,
) -> dict:
    """``PlatformEntry.standalone_sender_fn``: cron ``deliver=msteams_bridge``
    calls the user and speaks the message. ``media_files`` accepted for
    signature parity; voice delivery is text-only."""
    return await deliver_by_voice(chat_id, message, thread_id or "")


_MANAGED_CONSULTS: dict[str, Any] = {}
_MANAGED_CONSULTS_MAX = 64


async def _respond_managed_chat(cfg, message) -> str:
    """One agent turn for a managed-chat message, gateway-hosted path.

    Same model as the CLI path: one AgentConsult per Teams conversation so a chat keeps its context,
    LRU-capped because continuity lives in the stable session_id rather than the instance.
    """
    from .agent_consult import AgentConsult
    from .managed_chat import build_turn_query

    key = f"{message.tenant_id}:{message.conversation_id}"
    consult = _MANAGED_CONSULTS.pop(key, None) or AgentConsult(session_id=f"msteams-chat:{key}")
    _MANAGED_CONSULTS[key] = consult
    while len(_MANAGED_CONSULTS) > _MANAGED_CONSULTS_MAX:
        _MANAGED_CONSULTS.pop(next(iter(_MANAGED_CONSULTS)))

    # Shared with the CLI path (attachments, card submits, voice-message transcription) so the two
    # hosting paths cannot drift apart on what a message even says.
    query = await build_turn_query(message, cfg)
    return await consult.ask(query, timeout_s=280.0)


def get_adapter_class() -> Any:
    """Build (once) the real adapter class. Host-only: imports the sanctioned
    ``gateway.platforms.base`` surface lazily."""
    global _ADAPTER_CLASS
    if _ADAPTER_CLASS is not None:
        return _ADAPTER_CLASS

    from gateway.config import Platform  # sanctioned for platform plugins
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class TeamsCallAdapter(BasePlatformAdapter):
        """Thin shell: VoiceBridgeService lifecycle + voice delivery."""

        def __init__(self, config: Any) -> None:
            super().__init__(config, Platform(PLATFORM_NAME))
            self._service = None
            self._managed_chat = None

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            from .config import resolve_config
            from .service import VoiceBridgeService

            cfg = resolve_config()
            if not cfg.configured:
                logger.error("[msteams_bridge] adapter: no shared secret configured")
                return False
            # Gateway authorization reads only the PlatformEntry env names
            # (allowed_users_env / allow_all_env) — it cannot see the plugin
            # config block. Mirror the RESOLVED admission policy into those
            # vars so a caller the bridge admits is also authorized for agent
            # turns (round 8: config-block allowlist/allow_all users were
            # admitted to the call, then denied by the gateway).
            if cfg.allowlist and not os.getenv("MSTEAMS_BRIDGE_ALLOWLIST", "").strip():
                os.environ["MSTEAMS_BRIDGE_ALLOWLIST"] = ",".join(cfg.allowlist)
            if cfg.allow_all and not os.getenv("MSTEAMS_BRIDGE_ALLOW_ALL", "").strip():
                os.environ["MSTEAMS_BRIDGE_ALLOW_ALL"] = "1"
            factory = _handler_factory(cfg, adapter=self)
            service = VoiceBridgeService(cfg, handler_factory=factory)
            try:
                await service.start()  # loud on bind conflict (spike criterion 2)
            except OSError as exc:
                logger.error(
                    "[msteams_bridge] adapter: port %s:%s unavailable (%s) — is a "
                    "standalone `hermes msteams-bridge serve` already running?",
                    cfg.host, cfg.port, exc,
                )
                return False
            self._service = service

            # StandIn Managed Bot chat lane, in the SAME process as voice - one connection, both
            # planes. Failure here must not take voice down: the call path is the product's core,
            # and a chat port conflict is a configuration problem, not a reason to drop calls.
            try:
                from .managed_chat import start_managed_chat_if_configured

                self._managed_chat = await start_managed_chat_if_configured(
                    cfg, lambda message: _respond_managed_chat(cfg, message)
                )
                if self._managed_chat is not None:
                    logger.info(
                        "[msteams_bridge] managed chat lane on %s:%s%s",
                        cfg.managed_chat_host, cfg.managed_chat_port, cfg.managed_chat_path,
                    )
            except Exception:
                logger.exception("[msteams_bridge] managed chat lane failed to start; voice continues")

            self._mark_connected()
            logger.info("[msteams_bridge] gateway-resident bridge on %s:%s", cfg.host, cfg.port)
            return True

        async def disconnect(self) -> None:
            if self._managed_chat is not None:
                await self._managed_chat.stop()
                self._managed_chat = None
            if self._service is not None:
                await self._service.stop()
                self._service = None
            self._mark_disconnected()

        async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
            thread_id = str((metadata or {}).get("thread_id") or "")
            result = await deliver_by_voice(chat_id, content, thread_id)
            if result.get("success"):
                return SendResult(success=True, message_id=result.get("message_id"))
            return SendResult(success=False, error=str(result.get("error")))

        async def get_chat_info(self, chat_id: str) -> dict:
            """Abstract-contract requirement. Voice legs are 1:1 by design
            (group calls gate on wake phrases but the delivery target is one
            person), so everything reports as a DM."""
            handler = live_call_for(chat_id)
            name = ""
            if handler is not None:
                name = str(
                    getattr(handler, "_caller_name", "")
                    or getattr(getattr(handler, "_session", None), "caller_name", "")
                    or ""
                )
            return {"name": name or "Teams call", "type": "dm"}

        async def send_document(
            self, chat_id: str, file_path: str, caption=None, file_name=None,
            reply_to=None, metadata=None, **kwargs,
        ) -> SendResult:
            """MEDIA:<file> delivery as a native Teams file card (round 8 —
            the base fallback only reports failure). Uses the adapter wire
            contract in :mod:`.hermes_api`; needs the chat plane's bot creds
            and a real Teams conversation id (a bare AAD id can't host one)."""
            from .hermes_api import file_sender_available, send_teams_file

            thread_id = str((metadata or {}).get("thread_id") or "")
            conv_id = thread_id or (chat_id if ":" in str(chat_id or "") else "")
            if conv_id and file_sender_available():
                try:
                    data = Path(file_path).read_bytes()
                except OSError as exc:
                    return SendResult(success=False, error=f"could not read file: {exc}")
                result = await send_teams_file(
                    conv_id, data, file_name or Path(file_path).name,
                    caption=str(caption or ""),
                )
                if result.get("success"):
                    return SendResult(success=True, message_id=result.get("message_id"))
                logger.warning("[msteams_bridge] file card failed: %s", result.get("error"))
            return await super().send_document(
                chat_id, file_path, caption=caption, file_name=file_name,
                reply_to=reply_to, metadata=metadata, **kwargs,
            )

    _ADAPTER_CLASS = TeamsCallAdapter
    return _ADAPTER_CLASS


def _handler_factory(cfg: Any, adapter: Any):
    """Pick the call brain for gateway-resident mode; hand it the adapter so
    streaming utterances become real gateway agent turns."""
    from .realtime.openai_client import realtime_config_from_env

    rt_cfg = realtime_config_from_env()
    if rt_cfg.configured:
        from .handlers import RealtimeCallSessionHandler

        def factory():
            handler = RealtimeCallSessionHandler(rt_cfg, bridge_config=cfg)
            handler._gateway_adapter = adapter
            return handler

        return factory

    from .handlers import StreamingCallSessionHandler

    def factory():
        handler = StreamingCallSessionHandler(bridge_config=cfg)
        handler._gateway_adapter = adapter
        return handler

    return factory


#: The platform name the portal, the installer and the docs all write. Must match the plugin entry point
#: and the ``platforms.<name>.enabled`` key in config.yaml, or the plugin loads and activates nothing.
PLATFORM_NAME = "msteams_bridge"


def register_platform(ctx: Any) -> None:
    """Register the gateway platform (best-effort: older hosts lack the API)."""
    try:
        ctx.register_platform(
            name=PLATFORM_NAME,
            label="Teams Call (CVI)",
            adapter_factory=lambda cfg: get_adapter_class()(cfg),
            check_fn=_check_requirements,
            # Without this, aiohttp's mere presence reads as "configured"
            # (round 8): validate against the resolved shared secret instead.
            validate_config=_validate_config,
            required_env=["MSTEAMS_BRIDGE_SHARED_SECRET"],
            allowed_users_env="MSTEAMS_BRIDGE_ALLOWLIST",
            allow_all_env="MSTEAMS_BRIDGE_ALLOW_ALL",
            standalone_sender_fn=standalone_voice_send,
            cron_deliver_env_var="MSTEAMS_BRIDGE_HOME_AAD",
            platform_hint=(
                "You are delivering by VOICE on a Microsoft Teams call: replies are "
                "spoken aloud. Keep them brief and conversational."
            ),
            emoji="📞",
        )
    except (AttributeError, TypeError) as exc:
        logger.info("[%s] platform registration unavailable on this host: %s", PLATFORM_NAME, exc)


def _check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401

        return True
    except ImportError:
        return False


def _validate_config(_platform_cfg: Any = None) -> bool:
    """PlatformEntry.validate_config: configured means a shared secret
    resolves (config block or env) — dependency presence alone is not it."""
    from .config import resolve_config

    try:
        return resolve_config().configured
    except Exception:  # noqa: BLE001 — a broken config is "not configured"
        return False
