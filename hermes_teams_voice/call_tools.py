"""CallToolRunner — runs the realtime model's tool calls.

Separates the tool surface (agent consult / background task / look_at_screen /
show_to_caller / call_me_back / post_meeting_minutes) from the realtime handler's
transport + dialogue loop. The runner reads only what it needs through a typed
:class:`CallContext` (built by the handler once the session is established),
rather than duck-typing on the whole handler.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import meeting
from .call_session_base import _pending_set
from .outbound import OutboundError, place_call

if TYPE_CHECKING:
    from .agent_consult import AgentConsult
    from .bridge_server import CallSession
    from .config import TeamsVoiceConfig
    from .meeting import MeetingTranscript
    from .protocol import CallerInfo
    from .vision_budget import VisionBudget
    from .vision_store import VisionStore

logger = logging.getLogger(__name__)


@dataclass
class CallContext:
    """The per-call state the tool runner needs (references are stable for the call)."""

    bridge: "TeamsVoiceConfig | None"
    session: "CallSession | None"
    caller: "CallerInfo | None"
    consult: "AgentConsult"
    vision: "VisionStore"
    vision_budget: "VisionBudget"
    meeting: "MeetingTranscript"
    thread_id: str


class CallToolRunner:
    def __init__(self, ctx: CallContext) -> None:
        self._ctx = ctx

    async def run_tool(self, name: str, args: dict) -> str:
        ctx = self._ctx
        try:
            if name == "hermes_agent_consult":
                return await ctx.consult.ask(str(args.get("query", "")))
            if name == "hermes_agent_task":
                return await self._agent_task(str(args.get("query", "")))
            if name == "look_at_screen":
                return await self._look_at_screen(
                    str(args.get("question", "")), args.get("source"), str(args.get("scope") or "live")
                )
            if name == "show_to_caller":
                return await self._show_to_caller(str(args.get("prompt", "")), args.get("count", 1))
            if name == "call_me_back":
                return await self._call_me_back(str(args.get("message", "")))
            if name == "post_meeting_minutes":
                return await meeting.post_minutes(ctx.consult, ctx.meeting, ctx.thread_id)
        except Exception:  # noqa: BLE001 — a tool fault must not break the call
            logger.error("[teams_voice] tool %s failed", name, exc_info=True)
            return "Sorry, that didn't work."
        return f"Unknown tool: {name}."

    async def _look_at_screen(self, question: str, source: str | None, scope: str = "live") -> str:
        ctx = self._ctx
        if not ctx.vision_budget.try_consume():
            return "I've looked at a lot just now — give me a moment before the next one."
        prompt = question.strip() or "Describe what you see."
        if scope == "history":
            frames = ctx.vision.history(limit=6)
            if not frames:
                return "I don't have any earlier frames to look back on."
            content: list[dict] = [{"type": "text", "text": prompt}]
            for f in frames:  # timestamped, attributed keyframes
                content.append({"type": "text", "text": f"(earlier, from {f.describe()})"})
                content.append({"type": "image_url", "image_url": {"url": f.data_url()}})
        else:
            want = "camera" if str(source or "").lower() == "camera" else "screenshare"
            frame = ctx.vision.latest(want) or ctx.vision.latest()
            if frame is None:
                return "I can't see a shared screen or camera right now."
            content = [
                {"type": "text", "text": f"{prompt} (looking at the {frame.describe()})"},
                {"type": "image_url", "image_url": {"url": frame.data_url()}},
            ]
        return await self._vision_consult(content)

    async def _vision_consult(self, content: list[dict]) -> str:
        try:
            from agent.auxiliary_client import async_call_llm

            resp = await async_call_llm(
                task="vision", messages=[{"role": "user", "content": content}], max_tokens=400
            )
            text = resp.choices[0].message.content if resp and resp.choices else ""
            return (text or "").strip() or "I couldn't quite make that out."
        except Exception:  # noqa: BLE001
            self._ctx.vision_budget.refund()  # consult failed before the model — give it back
            logger.error("[teams_voice] vision consult failed", exc_info=True)
            return "I had trouble looking at that."

    async def _show_to_caller(self, prompt: str, count: object = 1) -> str:
        prompt = prompt.strip()
        if not prompt:
            return "What would you like me to show?"
        try:
            n = max(1, min(int(count), 3))
        except (TypeError, ValueError):
            n = 1
        try:
            from tools.image_generation_tool import image_generate_tool

            paths: list[str] = []
            for _ in range(n):
                raw = await asyncio.to_thread(
                    lambda: image_generate_tool(prompt=prompt, aspect_ratio="landscape")
                )
                data = json.loads(raw)
                if data.get("success") and data.get("image"):
                    paths.append(data["image"])
            if not paths:
                return "I couldn't create that image."
            # Paced slideshow: 4.5s hold for non-final, 5s for the final image.
            for idx, path in enumerate(paths):
                final = idx == len(paths) - 1
                img_bytes = Path(path).read_bytes()
                mime = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
                if self._ctx.session is not None:
                    await self._ctx.session.send_display_image(
                        base64.b64encode(img_bytes).decode("ascii"),
                        mime,
                        duration_ms=5000 if final else 4500,
                        mode="overlay",
                        caption=prompt[:80],
                    )
                if not final:
                    await asyncio.sleep(4.0)
            return "I'm showing it on screen now." if len(paths) == 1 else f"Showing you {len(paths)} images."
        except Exception:  # noqa: BLE001
            logger.error("[teams_voice] show_to_caller failed", exc_info=True)
            return "I made the image but couldn't display it."

    async def _call_me_back(self, message: str) -> str:
        ctx = self._ctx
        message = message.strip()
        caller = ctx.caller
        if ctx.bridge is None or caller is None or not caller.aad_id:
            return "I can't call you back — I don't have a number to reach you."
        tenant = caller.tenant_id or ctx.bridge.tenant_id
        if not tenant:
            return "I can't call you back — missing your tenant."
        try:
            result = await place_call(
                user_object_id=caller.aad_id,
                tenant_id=tenant,
                shared_secret=ctx.bridge.shared_secret,
                worker_base_url=ctx.bridge.worker_base_url,
                allow_remote=ctx.bridge.allow_remote_worker,
            )
        except OutboundError as exc:
            logger.warning("[teams_voice] call_me_back failed: %s", exc)
            return "I couldn't place the call-back just now."
        call_id = result.get("callId")
        if call_id:
            _pending_set(call_id, message or "Here's what you asked for.")
        return "Okay — I'll call you right back with that."

    async def _agent_task(self, query: str) -> str:
        """Run a long job in the background; deliver the result to the Teams chat
        (preferred) or via a voice call-back."""
        ctx = self._ctx
        query = query.strip()
        caller = ctx.caller
        if not query:
            return "What would you like me to work on?"
        # Need either a postable thread (chat delivery) or an AAD id (call-back).
        if ctx.bridge is None or (not ctx.thread_id and (caller is None or not caller.aad_id)):
            return await ctx.consult.ask(query)  # no delivery path → inline
        asyncio.create_task(self._run_background_task(query, caller))
        return "Got it — I'll work on that in the background and send you the result."

    async def _run_background_task(self, query: str, caller) -> None:
        ctx = self._ctx
        try:
            result = await ctx.consult.ask(query, timeout_s=300.0)
        except Exception:  # noqa: BLE001
            logger.error("[teams_voice] background task failed", exc_info=True)
            result = "I couldn't complete that task."
        # Prefer delivering the result to the Teams chat (no call-back needed);
        # fall back to a voice call-back when there's no postable thread.
        if ctx.thread_id:
            from .meeting import _deliver_to_teams

            if await _deliver_to_teams(ctx.thread_id, f"✅ {result}"):
                return
        if ctx.bridge is None or caller is None or not caller.aad_id:
            return
        tenant = caller.tenant_id or ctx.bridge.tenant_id
        if not tenant:
            return
        try:
            res = await place_call(
                user_object_id=caller.aad_id,
                tenant_id=tenant,
                shared_secret=ctx.bridge.shared_secret,
                worker_base_url=ctx.bridge.worker_base_url,
                allow_remote=ctx.bridge.allow_remote_worker,
            )
        except OutboundError as exc:
            logger.warning("[teams_voice] background callback failed: %s", exc)
            return
        cid = res.get("callId")
        if cid:
            _pending_set(cid, result)
