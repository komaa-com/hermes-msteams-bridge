"""Shared base for the realtime and streaming call brains.

Both handlers need the same session policy — caller allowlist, session-scope key,
meeting transcript, agent consult, group-call gate, greeting/outbound state. This
base (``BaseTeamsCallHandler``) holds that once so the two handlers only implement
what differs (the realtime model vs the STT→agent→TTS loop). Also owns the
process-global pending-outbound registry (call-back correlation, with TTL).
"""

from __future__ import annotations

import logging
import time

from . import group_call_gate, protocol
from .agent_consult import AgentConsult
from .bridge_server import CallSession, CallSessionHandler
from .config import TeamsVoiceConfig, caller_allowed
from .meeting import MeetingTranscript

logger = logging.getLogger(__name__)

# The inbound leg that requests a call-back, the ``call_user`` tool in the
# GATEWAY process, and the outbound leg that answers in the SERVE process are
# different connections in different processes — so the pending spoken result
# is keyed by callId in a small file store under the Hermes home (both
# processes share the filesystem per the documented topology). Entries carry a
# TTL so a never-answered call-back can't leak its text indefinitely. The
# in-process dict remains as the fallback when no Hermes home exists (tests,
# bare installs) — there the two ends are the same process anyway.
_PENDING_OUTBOUND: dict[str, tuple[str, float]] = {}
_PENDING_TTL_S = 600.0


def _pending_dir():
    try:
        from .hermes_api import hermes_home

        d = hermes_home() / "cache" / "teams_voice" / "pending_outbound"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001 — no Hermes home: fall back in-process
        return None


def _safe_call_key(call_id: str) -> str:
    import hashlib

    # callId is worker-provided: never let it shape a filesystem path.
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:32]


def _pending_prune() -> None:
    now = time.monotonic()
    for k in [k for k, (_t, exp) in _PENDING_OUTBOUND.items() if exp <= now]:
        _PENDING_OUTBOUND.pop(k, None)
    d = _pending_dir()
    if d is not None:
        wall_now = time.time()
        for f in d.glob("*.json"):
            try:
                if wall_now - f.stat().st_mtime > _PENDING_TTL_S:
                    f.unlink(missing_ok=True)
            except OSError:
                pass


def _pending_set(call_id: str, text: str, thread_id: str = "") -> None:
    """Register the message the outbound leg speaks on answer. ``thread_id``
    (when known) is the §3.7 fallback target: a call that never produces a
    session within the stale window gets its text posted to that chat instead
    of expiring silently."""
    import json as _json

    _pending_prune()
    d = _pending_dir()
    if d is not None:
        try:
            tmp = d / f".{_safe_call_key(call_id)}.tmp"
            tmp.write_text(_json.dumps({"text": text, "thread_id": thread_id}), encoding="utf-8")
            tmp.rename(d / f"{_safe_call_key(call_id)}.json")  # atomic publish
            return
        except OSError:
            logger.warning("[teams_voice] pending store write failed; using in-process", exc_info=True)
    _PENDING_OUTBOUND[call_id] = (text, time.monotonic() + _PENDING_TTL_S)


def _pending_pop(call_id: str) -> str | None:
    import json as _json

    _pending_prune()
    d = _pending_dir()
    if d is not None:
        f = d / f"{_safe_call_key(call_id)}.json"
        try:
            text = _json.loads(f.read_text(encoding="utf-8")).get("text")
            f.unlink(missing_ok=True)
            if text is not None:
                return str(text)
        except OSError:
            pass  # not in the file store — check in-process below
        except ValueError:
            f.unlink(missing_ok=True)
    entry = _PENDING_OUTBOUND.pop(call_id, None)
    return entry[0] if entry else None


class BaseTeamsCallHandler(CallSessionHandler):
    """Common session policy shared by the realtime + streaming handlers."""

    def __init__(self, bridge_config: TeamsVoiceConfig | None = None) -> None:
        self._bridge = bridge_config
        self._require_recording = bridge_config.require_recording_status if bridge_config else True
        self._session: CallSession | None = None
        self._caller: protocol.CallerInfo | None = None
        self._thread_id = ""
        self._outbound = False
        self._greeted = False
        self._pending_greeting: str | None = None
        self._meeting = MeetingTranscript()
        self._consult = AgentConsult()
        wake = tuple(bridge_config.wake_phrases) if (bridge_config and bridge_config.wake_phrases) else ("assistant", "hermes")
        self._gate_cfg = group_call_gate.GroupCallGateConfig(wake_phrases=wake)
        self._last_addressed_ms: float | None = None

    # ── shared helpers ────────────────────────────────────────────────────────

    def _first_name(self) -> str:
        name = (self._caller.display_name if self._caller else "") or ""
        return name.strip().split(" ")[0] if name.strip() else ""

    def _greeting_plan(self) -> tuple[str, str] | None:
        """Greet-on-answer decision (fires once): ('deliver', result) for an
        answered call-back, ('greet', name) for a fresh inbound call, or None."""
        if self._greeted:
            return None
        if self._outbound:
            if not self._pending_greeting:
                return None
            payload, self._pending_greeting = self._pending_greeting, None
            self._greeted = True
            return ("deliver", payload)
        self._greeted = True
        return ("greet", self._first_name())

    def _recording_ok(self, session: CallSession) -> bool:
        return (not self._require_recording) or session.recording_active

    async def _begin_session(self, session: CallSession, msg: protocol.SessionStart) -> bool:
        """Common ``session.start``: state + allowlist + scope. False = rejected."""
        await CallSessionHandler.on_session_start(self, session, msg)
        self._session = session
        self._caller = msg.caller
        self._thread_id = msg.thread_id
        self._outbound = (msg.direction or "").lower() == "outbound"
        if self._outbound:  # delivery leg of a call-back
            self._pending_greeting = _pending_pop(msg.call_id)
        elif self._bridge and not caller_allowed(
            self._bridge, msg.caller.aad_id, msg.caller.display_name
        ):
            logger.info("[teams_voice] caller not allowlisted; rejecting %s", session.call_id)
            await session._ws.close()
            return False
        scope = self._bridge.session_scope if self._bridge else "per-call"
        if scope == "per-thread":
            key = msg.thread_id or msg.call_id
        elif scope == "per-aad":
            key = msg.caller.aad_id or msg.call_id
        else:
            key = msg.call_id
        self._consult = AgentConsult(session_id=f"teams:{key}")
        return True

    def _group_decision(self, transcript: str, now_ms: float):
        """``(is_group, GateDecision)`` for a finished caller turn."""
        is_group = (self._session.human_count if self._session else 0) >= 2
        decision = group_call_gate.should_respond_to_group_turn(
            transcript=transcript, is_group=is_group, config=self._gate_cfg,
            last_addressed_at_ms=self._last_addressed_ms, now_ms=now_ms,
        )
        return is_group, decision

    async def _safe_expression(self, emotion: str) -> None:
        if self._session is None:
            return
        try:
            await self._session.send_expression(emotion)
        except Exception:  # noqa: BLE001 — cosmetic
            pass


# §3.7 plugin-side mitigation: a placed call whose callId never produced a WS
# session within ``max_age_s`` is treated as a probable no-answer. The scan
# CLAIMS such entries (deletes them) and returns any with a chat fallback
# target, so the caller can post the result instead of losing it. The wire-
# level call-outcome signal stays ON HOLD; this bounds the blind spot.
_STALE_AFTER_S = 180.0


def scan_stale_pending(max_age_s: float = _STALE_AFTER_S) -> list[tuple[str, str, str]]:
    """Two-phase claim of no-answer entries older than ``max_age_s``:
    rename to ``.fallback`` and return ``[(text, thread_id, claim_path)]`` —
    the record dies only after the chat post succeeds (see
    :func:`deliver_stale_pending`), so a failed send keeps its retry."""
    import json as _json

    claimed: list[tuple[str, str, str]] = []
    d = _pending_dir()
    if d is None:
        return claimed
    now = time.time()
    for f in d.glob("*.fallback"):  # recover claims from a crashed reaper
        try:
            if now - f.stat().st_mtime > max_age_s:
                f.rename(f.with_suffix(".json"))
        except OSError:
            pass
    for f in sorted(d.glob("*.json")):
        try:
            age = now - f.stat().st_mtime
            if age < max_age_s:
                continue
            entry = _json.loads(f.read_text(encoding="utf-8"))
            thread_id = str(entry.get("thread_id") or "")
            if not thread_id:
                f.unlink(missing_ok=True)  # nothing to deliver to — expire
                continue
            claim = f.with_suffix(".fallback")
            f.rename(claim)
            claimed.append((str(entry.get("text") or ""), thread_id, str(claim)))
        except (OSError, ValueError):
            continue
    return claimed


async def deliver_stale_pending() -> int:
    """Post claimed no-answer results to their chat threads; returns count."""
    from .hermes_api import send_teams_message

    from pathlib import Path as _Path

    delivered = 0
    for text, thread_id, claim_path in scan_stale_pending():
        result = await send_teams_message(
            thread_id,
            f"\U0001F4DE I tried to call you but couldn't reach you. Here's what I had: {text}",
        )
        claim = _Path(claim_path)
        try:
            if result.get("success"):
                claim.unlink(missing_ok=True)
                delivered += 1
                logger.info("[teams_voice] no-answer fallback delivered to %s", thread_id)
            else:
                if claim.exists():
                    claim.rename(claim.with_suffix(".json"))  # keep for retry
                logger.warning("[teams_voice] no-answer fallback failed (kept for retry): %s", result.get("error"))
        except OSError:
            pass
    return delivered
