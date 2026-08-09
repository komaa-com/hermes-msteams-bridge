"""Configuration resolution for the teams_call bridge.

Values come from (in priority order): the plugin's ``config.extra`` block in
``config.yaml`` (when wired through the gateway), then environment variables,
then safe defaults. Secrets are never logged.

The wire contract is fixed by the StandIn media bridge, so the header
names, HMAC payload shape, and default path mirror it exactly - see
``protocol.py`` and ``hmac_auth.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

# Audio wire format - single source of truth, mirrors the bridge (PCM 16 kHz,
# 16-bit signed, mono, little-endian; 20 ms / 640-byte frames).
PCM_SAMPLE_RATE_HZ = 16_000
FRAME_DURATION_MS = 20
BYTES_PER_FRAME = PCM_SAMPLE_RATE_HZ * FRAME_DURATION_MS // 1000 * 2  # 640

def plugin_env(name: str, default: str = "") -> str:
    """Read a ``TEAMS_CALL_*`` plugin env var (single indirection point)."""
    return os.getenv(name, default)


# Default WebSocket path the worker connects to: ``/voice/msteams/stream/{callId}``.
DEFAULT_PATH = "/voice/msteams/stream"

# HMAC upgrade header names — MUST match the companion worker byte-for-byte (it
# sends these on the WS upgrade and reads them on the outbound-call endpoint).
# Do not rename without a matching change in the worker, or the handshake fails.
HEADER_TIMESTAMP = "X-StandIn-Timestamp"
HEADER_SIGNATURE = "X-StandIn-Signature"
# Legacy header names (pre-rename). Still accepted on receive, and still SENT on
# outbound worker calls alongside the new pair, so either side of the wire can
# upgrade in any order during the transition.
LEGACY_HEADER_TIMESTAMP = "X-OpenClawTeamsBridge-Timestamp"
LEGACY_HEADER_SIGNATURE = "X-OpenClawTeamsBridge-Signature"


@dataclass(frozen=True)
class TeamsVoiceConfig:
    """Resolved bridge configuration."""

    shared_secret: str
    host: str = "127.0.0.1"
    port: int = 8443
    path: str = DEFAULT_PATH
    # Replay/clock-skew window for the HMAC handshake, in milliseconds.
    hmac_window_ms: int = 60_000
    # Connection caps (DoS guards) — mirror the TS driver's defaults.
    max_connections: int = 64
    max_connections_per_ip: int = 8
    # A connection must send ``session.start`` within this window or it is reaped.
    pre_start_timeout_s: float = 10.0
    # Server-initiated WebSocket ping interval, in seconds (0 = disabled). aiohttp sends a PING every
    # ``heartbeat_s`` and tears the socket down when the PONG doesn't return, so a caller that dies UNCLEANLY
    # (half-open TCP, killed process — no close/FIN) is reaped for the WHOLE call lifetime, not just the
    # pre-start / max-duration windows. Without it a wedged half-open peer leaks its slot + billed worker
    # socket until ``max_call_duration_s`` (which defaults to 0 = unlimited).
    heartbeat_s: float = 20.0
    # Hard bound on a single call's wall-clock duration, in seconds (0 = unlimited).
    # Mirrors OpenClaw's maxDurationMs concept: a wedged/never-ending call is torn
    # down once it exceeds this, so it can't run forever and leak a live socket.
    max_call_duration_s: float = 0.0
    require_recording_status: bool = True
    # Outbound "call me back": the worker's loopback HTTP endpoint + default tenant.
    worker_base_url: str = "http://127.0.0.1:9440"
    tenant_id: str = ""
    # Caller allowlist (AAD object ids). Empty = deny ALL inbound callers unless
    # ``allow_all`` is set (explicit opt-in). Display-name matching is weaker
    # (spoofable) and off unless ``allowlist_allow_names`` is set.
    allowlist: tuple[str, ...] = ()
    allowlist_allow_names: bool = False
    # Explicit opt-in: accept any inbound caller when the allowlist is empty.
    # Deny-by-default otherwise — an unset allowlist must not mean "open to all".
    allow_all: bool = False
    # Refuse outbound place-call to a non-loopback worker unless explicitly allowed
    # (the shared secret would otherwise be sent to that host).
    allow_remote_worker: bool = False
    # Per-call vision spend cap across look_at_screen + ambient push (0 = unlimited).
    max_vision_per_minute: int = 30
    # Agent session continuity: "per-call" | "per-thread" | "per-aad".
    session_scope: str = "per-call"
    # Group-call wake phrases (speak only when addressed).
    wake_phrases: tuple[str, ...] = ("assistant", "hermes")
    # StandIn MANAGED chat lane. Part of THIS plugin's config (plugins.entries.teams_call.config
    # -> managed_chat, env TEAMS_CALL_MANAGED_CHAT_*) rather than a separate config root: it is the
    # same Teams integration as the voice lane, just the chat plane, and an operator should not have
    # to learn a second namespace to turn it on. Empty secret = lane off, fail closed.
    managed_chat_secret: str = ""
    managed_chat_host: str = "0.0.0.0"
    managed_chat_port: int = 8444
    managed_chat_path: str = "/managed/chat"
    managed_chat_gateway_reply_url: str = "https://teams.standin.komaa.com/api/chat/reply"
    # Post end-of-call meeting minutes to the Teams chat (opt-in).
    meeting_recap: bool = False
    # Root directory show_file may display from (containment). Empty =
    # <hermes home>/workspace/teams_call_show (a dedicated presentation dir,
    # not the whole workspace). Everything outside it is refused (§3.1).
    show_file_root: str = ""
    # Phase 4(b) "watch it work" browser view: while a background task runs,
    # periodically screenshot the agent's browser session and show it on the
    # tile instead of the plain progress panel. OPT-IN because each capture
    # dispatches browser_vision, which can invoke the auxiliary vision model
    # (cost); throttled to one capture per 10 s, bounded per task.
    watch_browser_tasks: bool = False
    # Optional; reserved for a future large-file SharePoint delivery path.
    # The minutes .docx file card itself needs no SharePoint: it rides the Bot
    # Framework attachment contract using the bot credentials.
    share_point_site_id: str = ""

    @property
    def configured(self) -> bool:
        """True when a shared secret is present (the bridge can authenticate)."""
        return bool(self.shared_secret)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def plugin_config_block() -> dict[str, Any]:
    """Return the ``plugins.entries.teams_call.config`` block from config.yaml.

    Empty dict when unset or config can't be loaded. ``${VAR}`` references are
    already expanded by Hermes's config loader, so secrets can live in ``.env``
    and be referenced here (e.g. ``shared_secret: ${TEAMS_CALL_SHARED_SECRET}``).
    Delegates to the :mod:`.hermes_api` boundary (the config loader is one of
    its documented residents).
    """
    from .hermes_api import plugin_config_block as _block

    return _block()


def resolve_config(extra: Mapping[str, Any] | None = None) -> TeamsVoiceConfig:
    """Build a :class:`TeamsVoiceConfig` from config.yaml + environment.

    ``extra`` is the per-plugin config block; when omitted it is read from
    ``plugins.entries.teams_call.config`` in config.yaml. Environment variables
    are the fallback so the bridge still works with no config file.
    """
    extra = extra if extra is not None else plugin_config_block()

    shared_secret = (
        str(extra.get("shared_secret") or "").strip()
        or plugin_env("TEAMS_CALL_SHARED_SECRET", "").strip()
    )
    host = (
        str(extra.get("host") or "").strip()
        or plugin_env("TEAMS_CALL_HOST", "").strip()
        or "127.0.0.1"
    )
    port = _coerce_int(
        extra.get("port") or plugin_env("TEAMS_CALL_PORT", ""), 8443
    )
    path = str(extra.get("path") or "").strip() or DEFAULT_PATH
    window = _coerce_int(
        extra.get("hmac_window_ms") or plugin_env("TEAMS_CALL_HMAC_WINDOW_MS", ""),
        60_000,
    )
    worker_base_url = (
        str(extra.get("worker_base_url") or "").strip()
        or plugin_env("TEAMS_CALL_WORKER_BASE_URL", "").strip()
        or "http://127.0.0.1:9440"
    )
    tenant_id = (
        str(extra.get("tenant_id") or "").strip()
        or plugin_env("TEAMS_CALL_TENANT_ID", "").strip()
        or os.getenv("TEAMS_TENANT_ID", "").strip()
    )
    # Allowlist: TEAMS_CALL_ALLOWLIST; when empty, inherit the chat plane's
    # TEAMS_ALLOWED_USERS so voice + chat share one AAD allowlist.
    allowlist = _coerce_list(extra.get("allowlist"), plugin_env("TEAMS_CALL_ALLOWLIST", "")) or _coerce_list(
        None, os.getenv("TEAMS_ALLOWED_USERS", "")
    )
    rr = extra.get("require_recording_status")
    if rr is None:
        rr = plugin_env("TEAMS_CALL_REQUIRE_RECORDING_STATUS", "true")
    require_recording = str(rr).strip().lower() not in ("0", "false", "no", "off")  # default True
    max_vision = _coerce_int(
        extra.get("max_vision_per_minute") or plugin_env("TEAMS_CALL_MAX_VISION_PER_MINUTE", ""), 30
    )
    max_call_duration_s = _coerce_float(
        extra.get("max_call_duration_s") or plugin_env("TEAMS_CALL_MAX_CALL_DURATION_S", ""), 0.0
    )
    heartbeat_s = _coerce_float(
        extra.get("heartbeat_s") or plugin_env("TEAMS_CALL_HEARTBEAT_S", ""), 20.0
    )
    session_scope = (
        str(extra.get("session_scope") or "").strip()
        or plugin_env("TEAMS_CALL_SESSION_SCOPE", "").strip()
        or "per-call"
    )
    wake = _coerce_list(extra.get("wake_phrases"), plugin_env("TEAMS_CALL_WAKE_PHRASES", ""))
    meeting_recap = str(
        extra.get("meeting_recap", "") or plugin_env("TEAMS_CALL_MEETING_RECAP", "")
    ).strip().lower() in ("1", "true", "yes", "on")

    return TeamsVoiceConfig(
        shared_secret=shared_secret,
        host=host,
        port=port,
        path=path,
        hmac_window_ms=window,
        require_recording_status=require_recording,
        max_call_duration_s=max_call_duration_s,
        heartbeat_s=heartbeat_s,
        worker_base_url=worker_base_url,
        tenant_id=tenant_id,
        allowlist=allowlist,
        max_vision_per_minute=max_vision,
        session_scope=session_scope,
        wake_phrases=wake or ("assistant", "hermes"),
        meeting_recap=meeting_recap,
        show_file_root=(
            str(extra.get("show_file_root") or "").strip()
            or plugin_env("TEAMS_CALL_SHOW_FILE_ROOT", "").strip()
        ),
        share_point_site_id=_resolve_sharepoint(extra),
        watch_browser_tasks=_coerce_bool(extra.get("watch_browser_tasks"), "TEAMS_CALL_WATCH_BROWSER_TASKS"),
        allowlist_allow_names=_coerce_bool(extra.get("allowlist_allow_names"), "TEAMS_CALL_ALLOWLIST_ALLOW_NAMES"),
        allow_remote_worker=_coerce_bool(extra.get("allow_remote_worker"), "TEAMS_CALL_ALLOW_REMOTE_WORKER"),
        allow_all=_coerce_bool(extra.get("allow_all"), "TEAMS_CALL_ALLOW_ALL"),
        **_resolve_managed_chat(extra),
    )


def _resolve_managed_chat(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the managed chat lane from ``managed_chat:`` under this plugin's config block, with
    ``TEAMS_CALL_MANAGED_CHAT_*`` env as the fallback - the same precedence every other setting here
    uses. Nested under the plugin instead of a top-level namespace so config.yaml reads as one
    Teams integration:

        plugins:
          entries:
            teams_call:
              config:
                shared_secret: ${TEAMS_CALL_SHARED_SECRET}
                managed_chat:
                  secret: ${TEAMS_CALL_MANAGED_CHAT_SECRET}
                  port: 8444
    """
    # `managed_bot` is the name - the StandIn Managed Bot connection, of which chat is one lane.
    # `managed_chat` stays accepted as an alias so an early adopter's config keeps working.
    block = extra.get("managed_bot")
    if not isinstance(block, Mapping):
        block = extra.get("managed_chat")
    block = block if isinstance(block, Mapping) else {}

    def _env(suffix: str) -> str:
        """TEAMS_CALL_MANAGED_BOT_* first, the older MANAGED_CHAT_* spelling as fallback."""
        return (
            plugin_env(f"TEAMS_CALL_MANAGED_BOT_{suffix}", "").strip()
            or plugin_env(f"TEAMS_CALL_MANAGED_CHAT_{suffix}", "").strip()
        )
    return {
        "managed_chat_secret": (
            str(block.get("secret") or "").strip()
            or _env("SECRET")
        ),
        "managed_chat_host": (
            str(block.get("host") or "").strip()
            or _env("HOST")
            or "0.0.0.0"
        ),
        "managed_chat_port": _coerce_int(
            block.get("port") or _env("PORT"), 8444
        ),
        "managed_chat_path": (
            str(block.get("path") or "").strip()
            or _env("PATH")
            or "/managed/chat"
        ),
        "managed_chat_gateway_reply_url": (
            str(block.get("gateway_reply_url") or "").strip()
            or _env("GATEWAY_REPLY_URL")
            or "https://teams.standin.komaa.com/api/chat/reply"
        ),
    }


def _resolve_sharepoint(extra: Mapping[str, Any]) -> str:
    _sp = str(extra.get("share_point_site_id") or extra.get("sharePointSiteId") or "").strip()
    if _sp.startswith("${"):  # an unexpanded ${VAR} reference — ignore, use env
        _sp = ""
    return _sp or os.getenv("TEAMS_SHAREPOINT_SITE_ID", "").strip()


def caller_allowed(config: "TeamsVoiceConfig", aad_id: str | None, display_name: str | None) -> bool:
    """Allowlist check: AAD id by default; display name only if opted in.

    Empty allowlist = deny all, unless ``allow_all`` is explicitly set."""
    if not config.allowlist:
        return config.allow_all
    if (aad_id or "").strip().lower() in config.allowlist:
        return True
    if config.allowlist_allow_names and (display_name or "").strip().lower() in config.allowlist:
        return True
    return False


def _coerce_bool(value: Any, env: str) -> bool:
    return str(value if value not in (None, "") else plugin_env(env, "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _coerce_list(value: Any, env: str) -> tuple[str, ...]:
    """List from a config list or a comma-separated env string (lowercased, trimmed)."""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [p.strip() for p in (env or "").split(",")]
    return tuple(i.lower() for i in items if i)
