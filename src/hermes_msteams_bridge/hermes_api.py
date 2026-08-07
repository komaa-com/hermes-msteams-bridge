"""The single boundary to the host Hermes runtime.

Every import from Hermes lives in this module and nowhere else in the package.
Each surface is either **public** (a `PluginContext` service captured in
``register(ctx)``, a registered tool invoked via ``dispatch_tool``, or an API
the developer guides sanction) or a **documented resident**: a Hermes-internal
import kept deliberately because no public equivalent exists. Residents are
feature-probed at startup (:func:`probe_boundaries`) so a missing symbol
surfaces in ``hermes teams-call status`` and the serve log, never mid-call.

Residents (each with the reason no public surface covers it):

* ``tools.transcription_tools.transcribe_audio`` — Hermes registers no
  transcription *tool* (only provider registration), so STT is invocation-only
  here; behaviour follows the operator's ``stt.provider`` chain.
* ``hermes_cli.config.load_config`` — there is no ``ctx.config`` accessor; the
  framework itself reads ``plugins.entries.<id>.config`` this way.
* ``run_agent.AIAgent`` — ``ctx.llm`` is completion-only and ``delegate_task``
  requires a parent agent context that a standalone serve process does not
  have, so a tool-capable consult has no public path today.
* ``tools.send_message_tool.send_message_tool`` — upstream is explicit that
  ``send_message`` is *"intentionally NOT registered as an agent-callable
  model tool"* (the agent must not autonomously message across platforms);
  its sanctioned callers (cron delivery, the ``hermes send`` CLI, the kanban
  notifier, ``mcp_serve``) all import this function directly, and our minutes
  delivery is the same pattern: a user-requested send, not agent initiative.
  Version-sensitive like any resident, hence probed.

Sanctioned public APIs used directly (not residents):

* ``hermes_constants.get_hermes_home`` — the contributing guide mandates it
  for all paths.

The probe distinguishes ``ok`` (the contract surface exists) from
``operational`` (the selected provider/config passes its own ``check_fn``):
``boundaries_ok`` can be true while e.g. TTS lacks an API key — status
reports both. The probe imports heavyweight Hermes modules, so it runs on
demand (``serve`` startup, ``teams-call status``), never per-call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── PluginContext capture ────────────────────────────────────────────────────

_ctx: Any = None


def set_plugin_context(ctx: Any) -> None:
    """Capture the ``PluginContext`` handed to ``register(ctx)``.

    Called exactly once by :func:`hermes_msteams_bridge.register`. Everything
    in this module degrades gracefully (with a logged reason) when the context
    is absent — e.g. under unit tests or a direct module import.
    """
    global _ctx
    _ctx = ctx


def plugin_context() -> Any:
    """The captured ``PluginContext`` (or ``None`` outside a Hermes host)."""
    return _ctx


def llm() -> Any:
    """The host-owned ``ctx.llm`` facade (``agent.plugin_llm.PluginLlm``).

    ``None`` when no context was captured or the host predates ``ctx.llm``.
    """
    if _ctx is None:
        return None
    try:
        return _ctx.llm
    except Exception:  # noqa: BLE001 — older host without the facade
        logger.debug("[teams_call] ctx.llm unavailable", exc_info=True)
        return None


# ── Tool dispatch (public: registered Hermes tools) ─────────────────────────
#
# ``registry.dispatch`` is synchronous and bridges async handlers itself by
# running a loop — so it must NEVER be called from inside our running event
# loop. ``dispatch_tool`` is the sync form (call off-loop only);
# ``dispatch_tool_async`` wraps it in a worker thread for use from handlers.
#
# NOTE: a direct dispatch bypasses the agent-loop approval pipeline (ADR-3).
# Only side-effect-free tools may be dispatched from plugin code; anything
# with side effects goes through an agent turn or a spoken confirmation.


def dispatch_tool(name: str, args: dict, **kwargs: Any) -> str:
    """Invoke a registered Hermes tool by name; returns the tool's JSON string."""
    # Hermes tools self-register at import time; in a process where discovery
    # hasn't run (standalone serve, tests) pre-import the owning module so the
    # registry entry exists before dispatch.
    module = _TOOL_MODULES.get(name)
    if module:
        try:
            __import__(module)
        except ImportError:
            pass  # dispatch below reports the miss uniformly
    if _ctx is not None:
        return _ctx.dispatch_tool(name, args, **kwargs)
    # No captured context (tests / direct import): go to the registry, which
    # is the same call ``ctx.dispatch_tool`` makes minus parent-agent wiring.
    try:
        from tools.registry import registry
    except ImportError as exc:
        return json.dumps({"error": f"Hermes tool registry unavailable: {exc}"})
    return registry.dispatch(name, args, **kwargs)


async def dispatch_tool_async(name: str, args: dict, **kwargs: Any) -> str:
    """Async-safe :func:`dispatch_tool` (runs in a worker thread)."""
    return await asyncio.to_thread(dispatch_tool, name, args, **kwargs)


# ── Vision (public: ctx.llm structured completion with image inputs) ─────────


async def vision_ask(
    instructions: str,
    blocks: list[dict],
    *,
    max_tokens: int = 400,
    purpose: str = "teams_call.vision",
) -> str:
    """Answer a question about image/text blocks via the host-owned LLM.

    ``blocks`` uses the ``PluginLlmInput`` dict shape:
    ``{"type": "text", "text": ...}`` / ``{"type": "image", "url": ...}``.
    Returns ``""`` on any failure (callers speak their own fallback line).
    """
    facade = llm()
    if facade is None:
        logger.warning("[teams_call] vision unavailable: no ctx.llm facade")
        return ""
    try:
        result = await facade.acomplete_structured(
            instructions=instructions,
            input=blocks,
            max_tokens=max_tokens,
            purpose=purpose,
        )
        return (result.text or "").strip()
    except Exception:  # noqa: BLE001 — vision must never crash the call
        logger.error("[teams_call] vision consult failed", exc_info=True)
        return ""


# ── Chat delivery (public: the send_message tool) ────────────────────────────


def _parse_tool_json(raw: Any) -> dict:
    """Normalize a tool result to a dict.

    Registry tools may return a JSON string OR a multimodal envelope dict
    (``{"_multimodal": True, "content": [...], "meta": {...}}``) — the browser
    tools do exactly that on the native-vision fast path. Passing that dict to
    ``json.loads`` would turn a perfectly good result into an error, so handle
    both shapes.
    """
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"error": str(parsed)}
    except (TypeError, ValueError):
        return {"error": f"unparseable tool result: {raw!r:.200}"}


async def send_teams_message(conversation_id: str, text: str) -> dict:
    """Deliver ``text`` to a Teams conversation (RESIDENT — see module docstring).

    Calls ``send_message_tool()`` directly, the same entry every sanctioned
    caller uses (cron, ``hermes send``, ``mcp_serve``): upstream deliberately
    does not register it as an agent-callable tool, so there is no dispatch
    path. It resolves a live gateway adapter first, then the platform's
    registered standalone sender. Runs in a worker thread (the engine bridges
    its own async). Returns the parsed result
    (``{"success": True, ...}`` / ``{"error": ...}``).
    """
    if not conversation_id:
        return {"error": "missing conversation id"}
    args = {"target": f"teams:{conversation_id}", "message": text}

    def _direct() -> Any:
        from tools.send_message_tool import send_message_tool

        return send_message_tool(args)

    try:
        return _parse_tool_json(await asyncio.to_thread(_direct))
    except ImportError as exc:
        return {"error": f"send_message unavailable: {exc}"}


# ── Media tools (public: registered tools, side-effect-free) ─────────────────


def generate_image(prompt: str, *, aspect_ratio: str = "landscape") -> dict:
    """``image_generate`` tool; returns its parsed dict (``success``/``image``).

    NOTE: ``image`` is a **URL** in the current Hermes implementation, not a
    local path — callers must localize it (see :func:`fetch_image_bytes`).
    """
    return _parse_tool_json(
        dispatch_tool("image_generate", {"prompt": prompt, "aspect_ratio": aspect_ratio})
    )


# Hard ceiling for anything we pull over HTTP and then push onto the tile.
MAX_FETCH_BYTES = 8 * 1024 * 1024
_ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}


async def fetch_image_bytes(url_or_path: str) -> tuple[bytes, str] | None:
    """Localize a generated image: read a local path, or GET a remote URL with
    scheme/size/content-type limits. Returns ``(bytes, mime)`` or ``None``.

    ``image_generate`` returns a provider URL, so the display path must fetch
    it. Bounded on purpose: http(s) only, capped body, allowlisted image types.
    """
    from pathlib import Path

    ref = (url_or_path or "").strip()
    if not ref:
        return None
    if not ref.startswith(("http://", "https://")):
        try:  # a local artifact path
            data = Path(ref).read_bytes()
        except OSError:
            return None
        if len(data) > MAX_FETCH_BYTES:
            return None
        mime = "image/png" if ref.lower().endswith(".png") else "image/jpeg"
        return data, mime

    # Same resolve-time public-IP policy as show_web_page: the URL comes from
    # a provider response, but responses can be proxied/misconfigured.
    from .display_files import url_is_public

    ok, _why = await asyncio.to_thread(url_is_public, ref)
    if not ok:
        logger.warning("[teams_call] image fetch refused non-public URL")
        return None

    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(ref) as resp:
                if resp.status != 200:
                    return None
                mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if mime not in _ALLOWED_IMAGE_MIME:
                    logger.warning("[teams_call] image fetch: unexpected type %r", mime)
                    return None
                declared = resp.content_length
                if declared is not None and declared > MAX_FETCH_BYTES:
                    return None
                data = await resp.content.read(MAX_FETCH_BYTES + 1)
                if len(data) > MAX_FETCH_BYTES:
                    logger.warning("[teams_call] image fetch exceeded the size cap")
                    return None
                return data, mime
    except Exception:  # noqa: BLE001 — display is best-effort
        logger.warning("[teams_call] image fetch failed", exc_info=True)
        return None


def active_tts_provider() -> str:
    """The operator's configured ``tts.provider`` (lowercased; "" when unset).

    The timed-TTS layer consults this so a stray ElevenLabs key can never
    silently override the provider the operator actually selected.
    """
    tts = load_hermes_config().get("tts") or {}
    return str((tts or {}).get("provider") or "").strip().lower()


def text_to_speech(text: str, *, output_path: str | None = None) -> dict:
    """``text_to_speech`` tool; returns its parsed dict (``success``/``file_path``).

    Provider/voice/language follow the operator's ``tts:`` config; there is no
    per-call language argument (ADR-10).
    """
    args: dict = {"text": text}
    if output_path:
        args["output_path"] = output_path
    raw = dispatch_tool("text_to_speech", args)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"success": False, "error": str(parsed)}
    except (TypeError, ValueError):
        return {"success": False, "error": f"unparseable text_to_speech result: {raw!r:.200}"}


async def browser_page_screenshot(url: str, task_id: str = "") -> tuple[str, str] | None:
    """Screenshot ``url`` via the host's browser tools (§3.1 ``show_web_page``).

    ``browser_navigate`` + ``browser_vision`` — both registered, side-effect-
    free dispatches; the browser stack carries Hermes's own scheme and
    private-network guards, and ``browser_vision`` persists the screenshot and
    returns its path. Returns ``(screenshot_path, page_note)`` or ``None``.
    """
    # Call-scoped session id: without one the browser tools share the "default"
    # session, so concurrent calls would overwrite each other's navigation and
    # could hand a caller someone else's screenshot (or cookies).
    args = {"url": url}
    if task_id:
        args["task_id"] = task_id
    nav = _parse_tool_json(await dispatch_tool_async("browser_navigate", args))
    if nav.get("error"):
        logger.warning("[teams_call] browser_navigate failed: %s", nav.get("error"))
        return None
    vargs: dict = {"question": "Has the page rendered completely?"}
    if task_id:
        vargs["task_id"] = task_id
    vision = _parse_tool_json(await dispatch_tool_async("browser_vision", vargs))
    # Two shapes: the plain JSON result carries screenshot_path at the top
    # level; the native-vision multimodal envelope carries it under meta.
    path = vision.get("screenshot_path") or (vision.get("meta") or {}).get("screenshot_path") or ""
    if not path:
        logger.warning("[teams_call] browser_vision returned no screenshot_path")
        return None
    note = vision.get("answer") or vision.get("text_summary") or ""
    return str(path), str(note)


# ── Residents ────────────────────────────────────────────────────────────────


# ── Teams file card: the adapter's OWN attachment contract, standalone ──────
#
# The live Teams adapter sends documents as a Bot Framework message activity
# whose attachment is a base64 data URI (adapter.py `_send_media_attachment`);
# `_standalone_send` posts the identical activity shape via REST minus the
# attachments array. We extend that same wire contract here — no SDK, no
# SharePoint, no private import: token (client credentials) + POST
# `{service_url}v3/conversations/{chat_id}/activities` with attachments.
# Constants mirror the adapter's own hardening (service-host allowlist,
# conversation-id charset) — Bot Framework wire facts, not Hermes internals.

import re as _re

_TEAMS_SERVICE_URL_DEFAULT = "https://smba.trafficmanager.net/teams/"
_TEAMS_SERVICE_HOSTS = frozenset({
    "smba.trafficmanager.net",
    "smba.infra.gov.teams.microsoft.us",
})
_TEAMS_CONV_ID = _re.compile(r"^[A-Za-z0-9:@\-_.]+$")
_MAX_FILE_CARD_BYTES = 4 * 1024 * 1024  # data-URI inline attachment cap


def _teams_bot_creds() -> tuple[str, str, str]:
    """Bot credentials via Hermes's own scope-aware reader (``get_env_value``:
    os.environ OR ``~/.hermes/.env``) — the CLI does not export .env into the
    process environment, so a bare ``os.getenv`` misses standalone serve."""
    def _get(key: str) -> str:
        try:
            from hermes_cli.config import get_env_value

            return (get_env_value(key) or "").strip()
        except Exception:  # noqa: BLE001 — bare install: environ only
            return os.getenv(key, "").strip()

    return _get("TEAMS_CLIENT_ID"), _get("TEAMS_CLIENT_SECRET"), _get("TEAMS_TENANT_ID")


def file_sender_available() -> bool:
    """True when the bot credentials the adapter itself uses are present —
    file cards ride the adapter's own attachment contract (data-URI activity),
    so creds are the only requirement."""
    client_id, client_secret, tenant_id = _teams_bot_creds()
    return bool(client_id and client_secret and tenant_id)


def build_file_activity(content: bytes, filename: str, mime: str, caption: str = "") -> dict:
    """The exact activity shape the live adapter produces for a document."""
    import base64 as _b64

    data_uri = f"data:{mime};base64,{_b64.b64encode(content).decode('ascii')}"
    activity: dict = {
        "type": "message",
        "textFormat": "markdown",
        "attachments": [
            {"contentType": mime, "contentUrl": data_uri, "name": filename}
        ],
    }
    if caption:
        activity["text"] = caption
    return activity


async def send_teams_file(
    conversation_id: str, content: bytes, filename: str, *, caption: str = "", site_id: str = ""
) -> dict:
    """Attach a file to a Teams chat via the adapter's wire contract.

    ``site_id`` is accepted for config compatibility; delivery does not need
    SharePoint (kept for a future large-file Graph path). Returns
    ``{"success": True, "message_id": ...}`` or ``{"error": ...}``.
    """
    import aiohttp
    from urllib.parse import urlparse

    if not conversation_id or not _TEAMS_CONV_ID.match(conversation_id):
        return {"error": "invalid Teams conversation id"}
    if len(content) > _MAX_FILE_CARD_BYTES:
        return {"error": "file too large for an inline Teams attachment"}
    client_id, client_secret, tenant_id = _teams_bot_creds()
    if not (client_id and client_secret and tenant_id):
        return {"error": "Teams bot credentials not configured"}
    if not _TEAMS_CONV_ID.match(tenant_id):
        return {"error": "tenant id contains unexpected characters"}
    service_url = os.getenv("TEAMS_SERVICE_URL", "").strip() or _TEAMS_SERVICE_URL_DEFAULT
    parsed_svc = urlparse(service_url)
    if parsed_svc.scheme != "https":  # the bearer token rides this request
        return {"error": "TEAMS_SERVICE_URL must be https"}
    host = (parsed_svc.hostname or "").lower()
    if host not in _TEAMS_SERVICE_HOSTS:
        return {"error": "TEAMS_SERVICE_URL host is not a known Bot Framework endpoint"}
    if not service_url.endswith("/"):
        service_url += "/"

    mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if filename.lower().endswith(".docx") else "application/octet-stream"
    )
    activity = build_file_activity(content, filename, mime, caption)
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    activities_url = f"{service_url}v3/conversations/{conversation_id}/activities"
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://api.botframework.com/.default",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as token_resp:
                if token_resp.status >= 400:
                    return {"error": f"token request failed ({token_resp.status})"}
                token = (await token_resp.json()).get("access_token")
            if not token:
                return {"error": "token response missing access_token"}
            async with session.post(
                activities_url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    return {"error": f"activity post failed ({resp.status}): {body[:200]}"}
                payload = await resp.json()
        return {"success": True, "message_id": payload.get("id")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[teams_call] file card delivery failed", exc_info=True)
        return {"error": str(exc)}


def current_chat_context() -> tuple[str, str]:
    """(platform, chat_id) of the gateway turn dispatching the current tool —
    the gateway's own concurrency-safe contextvars, via its documented
    ``get_session_env`` drop-in accessor. Empty strings outside a gateway turn.
    Used so ``call_user`` can register the originating Teams chat as the
    no-answer fallback target."""
    try:
        from gateway.session_context import get_session_env

        return (
            get_session_env("HERMES_SESSION_PLATFORM", ""),
            get_session_env("HERMES_SESSION_CHAT_ID", ""),
        )
    except Exception:  # noqa: BLE001 — no gateway in this process
        return "", ""


def transcribe(file_path: str) -> dict:
    """RESIDENT: no public transcription tool exists (provider registration
    only), so this is the one invocation-only import.

    Provider support: **every Hermes STT provider** applies unchanged, exactly
    per the official STT feature docs — Local Whisper (``faster-whisper``,
    models tiny…large-v3), Groq Whisper, OpenAI Whisper, Mistral Voxtral,
    xAI Grok STT, and ``stt.providers.<name>`` custom command providers — the
    operator's ``stt.provider`` config decides, with Hermes's documented
    automatic fallback chain and ``stt.language`` hint/auto-detect."""
    try:
        from tools.transcription_tools import transcribe_audio
    except ImportError as exc:
        logger.warning("[teams_call] STT unavailable: %s", exc)
        return {"success": False, "transcript": "", "error": str(exc)}
    return transcribe_audio(file_path)


def load_hermes_config() -> dict:
    """RESIDENT: no ``ctx.config`` accessor exists; the framework itself reads
    plugin config via ``hermes_cli.config.load_config``. ``{}`` on failure."""
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:  # noqa: BLE001 — config is optional; env fallbacks apply
        return {}


def plugin_config_block() -> dict:
    """The ``plugins.entries.teams_call.config`` block (``{}`` when unset)."""
    node = (
        load_hermes_config()
        .get("plugins", {})
        .get("entries", {})
        .get("teams_call", {})
        .get("config", {})
    )
    return node if isinstance(node, dict) else {}


def model_config_block() -> dict:
    """The host's top-level ``model:`` block (used by the consult resident)."""
    m = load_hermes_config().get("model") or {}
    return m if isinstance(m, dict) else {}


def build_consult_agent(**kwargs: Any) -> Any:
    """RESIDENT: construct a tool-capable ``run_agent.AIAgent``.

    ``ctx.llm`` is completion-only and ``delegate_task`` requires a parent
    agent a standalone serve process does not have, so the full agent (web,
    files, tools) has no public invocation path today. Raises ``ImportError``
    when the host lacks ``run_agent`` (probed at startup).
    """
    from run_agent import AIAgent  # heavy import — defer to first consult

    try:
        return AIAgent(**kwargs)
    except TypeError:  # older AIAgent without session_id — drop and retry
        kwargs.pop("session_id", None)
        return AIAgent(**kwargs)


# ── Sanctioned public APIs ───────────────────────────────────────────────────


def soul_text(max_chars: int = 6000) -> str:
    """The operator's SOUL.md persona — the SAME identity slot Hermes injects
    into every chat system prompt. Used so the voice call answers as the same
    assistant the user knows from chat (identity is Hermes's, never ours).
    Prefers Hermes's own loader (with its sanitization); falls back to reading
    the documented ``<hermes home>/SOUL.md`` file. Empty string when absent.
    """
    text = ""
    try:
        from agent.prompt_builder import load_soul_md

        text = load_soul_md() or ""
    except Exception:  # noqa: BLE001 — loader unavailable: read the file
        try:
            text = (hermes_home() / "SOUL.md").read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return ""
    text = text.strip()
    return text[:max_chars]


def skills_index_text(max_chars: int = 3500) -> str:
    """The compact skills index Hermes injects into chat prompts — the SAME
    builder (``build_skills_system_prompt``, two-layer cached), so the voice
    call knows about exactly the skills chat knows about. Empty when the host
    lacks the builder or no skills are installed. Trimmed to ``max_chars``
    (voice instructions must stay lean; every skill remains reachable through
    the agent even when its description is trimmed here)."""
    try:
        from agent.prompt_builder import build_skills_system_prompt

        text = (build_skills_system_prompt() or "").strip()
    except Exception:  # noqa: BLE001 — feature, not a requirement
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… (more skills available — the agent can list them)"
    return text


def hermes_home() -> Path:
    """``get_hermes_home()`` (mandated by the contributing guide for all paths)."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def elevenlabs_tts_config() -> dict:
    """ElevenLabs settings from the documented public schema.

    Reads ``tts.elevenlabs.{voice_id, model_id}`` from config.yaml plus the
    ``ELEVENLABS_API_KEY`` env var — replacing the former private
    ``tools.tts_tool._load_tts_config`` import.
    """
    tts = load_hermes_config().get("tts") or {}
    block = tts.get("elevenlabs") if isinstance(tts, dict) else {}
    block = block if isinstance(block, dict) else {}
    return {
        "api_key": os.getenv("ELEVENLABS_API_KEY", "").strip()
        or str(block.get("api_key") or "").strip(),
        "voice_id": str(block.get("voice_id") or "").strip(),
        "model_id": str(block.get("model_id") or "").strip(),
    }


# ── Startup probe ────────────────────────────────────────────────────────────

# Feature detection over version strings (plugins guide). Each entry:
# (surface label, probe callable) — the callable raises/returns False on a
# missing surface. Tool-presence probes go through the registry read-only.
_PROBE_MIN_VERSION = "0.19.0"  # tested floor — a WARNING, never a gate


# Tool name -> owning module. Hermes tools self-register at import time (the
# entry points trigger discovery via model_tools); a bare ``tools.registry``
# import sees an empty registry, so the probe imports the owner first.
_TOOL_MODULES = {
    "text_to_speech": "tools.tts_tool",
    "image_generate": "tools.image_generation_tool",
    "browser_navigate": "tools.browser_tool",
    "browser_vision": "tools.browser_tool",
}


def _registry_entry(name: str):
    module = _TOOL_MODULES.get(name)
    if module:
        __import__(module)  # self-registers on import; no-op when already loaded
    from tools.registry import registry

    return registry.get_entry(name)


def _probe_registry_tool(name: str) -> bool:
    return _registry_entry(name) is not None


def _tool_operational(name: str) -> bool | None:
    """Second tier: does the tool's own ``check_fn`` pass (provider, creds,
    native deps)? ``None`` = the entry has no check_fn / not applicable."""
    try:
        entry = _registry_entry(name)
        if entry is None or getattr(entry, "check_fn", None) is None:
            return None
        return bool(entry.check_fn())
    except Exception:  # noqa: BLE001 — a broken check_fn reads as not-ready
        return False


def probe_boundaries() -> list[dict]:
    """Feature-probe every Hermes surface this plugin depends on.

    Returns ``[{"surface", "kind", "ok", "detail"}, ...]``; logs each failure
    at WARNING so a broken boundary is visible before a customer call.
    """
    def _ctx_llm() -> bool:
        return llm() is not None

    def _import(module: str, symbol: str):
        def check() -> bool:
            mod = __import__(module, fromlist=[symbol])
            return hasattr(mod, symbol)

        return check

    # (surface, kind, contract check, operational check | None)
    probes: list[tuple[str, str, Any, Any]] = [
        ("ctx (PluginContext captured)", "public", lambda: _ctx is not None, None),
        ("ctx.llm (vision + consults)", "public", _ctx_llm, None),
        # send_message: upstream intentionally does NOT register it as an
        # agent-callable tool; the direct function import is a resident.
        ("resident: send_message_tool (minutes delivery)", "resident",
         _import("tools.send_message_tool", "send_message_tool"), None),
        ("tool: text_to_speech (streaming TTS fallback)", "public",
         lambda: _probe_registry_tool("text_to_speech"), lambda: _tool_operational("text_to_speech")),
        ("tool: image_generate (show_to_caller)", "public",
         lambda: _probe_registry_tool("image_generate"), lambda: _tool_operational("image_generate")),
        ("resident: transcribe_audio (streaming STT)", "resident",
         _import("tools.transcription_tools", "transcribe_audio"), None),
        ("resident: load_config (plugin config)", "resident",
         _import("hermes_cli.config", "load_config"), None),
        ("resident: AIAgent (tool-capable consult)", "resident",
         _import("run_agent", "AIAgent"), None),
        ("api: get_hermes_home (cache/workspace paths)", "public",
         _import("hermes_constants", "get_hermes_home"), None),
        ("tool: browser_navigate/browser_vision (show_web_page)", "public",
         lambda: _probe_registry_tool("browser_vision"), lambda: _tool_operational("browser_vision")),
        # Feature deps: contract is trivially "present"; operational says
        # whether the advertised feature can actually run on this install.
        ("feature: ffmpeg (streaming TTS decode)", "feature",
         lambda: True, lambda: __import__("shutil").which("ffmpeg") is not None),
        ("feature: pypdfium2 (show_file documents)", "feature",
         lambda: True, lambda: __import__("importlib.util", fromlist=["util"]).find_spec("pypdfium2") is not None),
        ("feature: soffice (show_file Office formats)", "feature",
         lambda: True, lambda: __import__("shutil").which("soffice") is not None),
        ("feature: skills index (chat/call capability parity)", "feature",
         lambda: True, lambda: bool(skills_index_text())),
        ("feature: SOUL.md persona (chat/call identity parity)", "feature",
         lambda: True, lambda: bool(soul_text())),
        ("feature: Teams file card (adapter attachment contract)", "feature",
         lambda: True, file_sender_available),
        ("feature: timed TTS provider (real visemes)", "feature",
         lambda: True, lambda: bool(__import__("hermes_msteams_bridge.timed_tts", fromlist=["timed_tts"]).timed_providers())),
    ]

    results: list[dict] = []
    for surface, kind, check, op_check in probes:
        ok, detail = False, ""
        try:
            ok = bool(check())
            if not ok:
                detail = "unavailable"
        except Exception as exc:  # noqa: BLE001 — the probe reports, never raises
            detail = f"{type(exc).__name__}: {exc}"
        operational: bool | None = None
        if ok and op_check is not None:
            try:
                operational = op_check()
            except Exception:  # noqa: BLE001
                operational = False
        if not ok:
            logger.warning("[teams_call] boundary MISSING: %s (%s)", surface, detail or "unavailable")
        elif operational is False:
            logger.warning("[teams_call] boundary present but NOT READY: %s (check_fn failed)", surface)
        results.append(
            {"surface": surface, "kind": kind, "ok": ok, "operational": operational, "detail": detail}
        )
    return results


def hermes_version_note() -> str:
    """Advisory version line (feature probes are the actual gate)."""
    try:
        import importlib.metadata as md

        version = md.version("hermes-agent")
    except Exception:  # noqa: BLE001 — source installs have no dist metadata
        return "hermes-agent version: unknown (source install) — feature probes govern"
    note = f"hermes-agent version: {version} (tested floor {_PROBE_MIN_VERSION})"
    try:
        if tuple(int(p) for p in version.split(".")[:3]) < tuple(
            int(p) for p in _PROBE_MIN_VERSION.split(".")
        ):
            note += " — BELOW tested floor; feature probes govern"
    except ValueError:
        pass
    return note
