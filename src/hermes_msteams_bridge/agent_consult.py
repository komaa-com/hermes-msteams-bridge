"""Agent delegation — run the full Hermes agent for a one-shot voice consult.

The realtime model handles small talk itself and delegates real work (lookups,
files, web, actions) to the Hermes agent via the ``hermes_agent_consult`` tool.
This wraps ``run_agent.AIAgent`` (synchronous, tool-capable) and runs it off the
event loop with ``asyncio.to_thread`` so the call's audio keeps flowing.

The agent is built lazily and reused across consults within a call. A consult is
time-boxed; on timeout/error a short speakable message is returned so the model
can tell the caller gracefully rather than hanging.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class AgentConsult:
    """Lazily-built, reusable one-shot agent runner for a call."""

    def __init__(self, model: str | None = None, session_id: str | None = None) -> None:
        import threading
        import uuid

        self._model = model
        self._session_id = session_id  # session continuity (per-thread/per-aad scope)
        # Stable task id for the agent's tool sessions. Without one, Hermes
        # mints a fresh UUID per turn, so the "watch it work" browser capture
        # can never target THIS consult's browser session (round 8) — it would
        # read the shared default session instead.
        self.browser_task_id = f"msteams_bridge:consult:{session_id or uuid.uuid4().hex[:8]}"
        self._agent = None  # run_agent.AIAgent, built on first use
        # Serialize consults: AIAgent is not concurrency-safe, and a timed-out
        # consult's thread keeps running — the lock stops a later consult from
        # sharing (and corrupting) the same agent while it does.
        self._run_lock = threading.Lock()

    def _agent_kwargs(self) -> dict:
        """Build AIAgent kwargs from the configured ``model`` block.

        A bare ``AIAgent()`` leaves the model empty → 'Missed model deployment',
        so we pass the same provider/model/base_url/api_mode the CLI resolves from
        config.yaml ``model:`` (e.g. provider=azure-foundry, default=gpt-5.5)."""
        import os

        from .hermes_api import model_config_block

        kwargs: dict = {"quiet_mode": True}
        m = model_config_block()
        if m.get("default"):
            kwargs["model"] = m["default"]
        if m.get("provider"):
            kwargs["provider"] = m["provider"]
        if m.get("base_url"):
            kwargs["base_url"] = m["base_url"]
        if m.get("api_mode"):
            kwargs["api_mode"] = m["api_mode"]
        if self._model:
            kwargs["model"] = self._model
        key = os.getenv("AZURE_FOUNDRY_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
        if key and "api_key" not in kwargs:
            kwargs["api_key"] = key
        if self._session_id:
            kwargs["session_id"] = self._session_id
        return kwargs

    BUSY_SENTINEL = "\x00BUSY\x00"

    def _run_sync(self, query: str) -> str:
        # Non-blocking: a timed-out consult's zombie thread may still hold the
        # lock — a new request must answer "busy", not silently queue behind it
        # until its own timeout.
        if not self._run_lock.acquire(blocking=False):
            return self.BUSY_SENTINEL
        try:
            return self._run_locked(query)
        finally:
            self._run_lock.release()

    def _run_locked(self, query: str) -> str:
        if self._agent is None:
            # Boundary resident: a tool-capable consult has no public path
            # (ctx.llm is completion-only; delegate_task needs a parent agent).
            from .hermes_api import build_consult_agent

            self._agent = build_consult_agent(**self._agent_kwargs())
        # Pin the tool-session task id (run_conversation forwards it to the
        # turn context) so browser captures can find this consult's session.
        run = getattr(self._agent, "run_conversation", None)
        if run is not None and self._accepts_task_id(run):
            result = run(query, task_id=self.browser_task_id)
            if isinstance(result, dict):
                return str(result.get("final_response") or "")
            return str(result)
        return self._agent.chat(query)  # older host without the kwarg

    @staticmethod
    def _accepts_task_id(fn) -> bool:
        import inspect

        try:
            return "task_id" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    async def ask(self, query: str, *, timeout_s: float = 45.0) -> str:
        """Run ``query`` through the agent and return a concise spoken result."""
        query = (query or "").strip()
        if not query:
            return "I didn't catch what you wanted me to look into."
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._run_sync, query), timeout=timeout_s
            )
            if result == self.BUSY_SENTINEL:
                return "I'm still finishing the previous request — give me a moment and ask again."
            return (result or "").strip() or "I didn't find anything to report."
        except asyncio.TimeoutError:
            # D4: never promise a follow-up that nothing will deliver. The
            # consult thread is abandoned on timeout; say so honestly and point
            # at the path that DOES deliver (the background task tool).
            logger.warning("[msteams_bridge] consult timed out after %.0fs; result dropped", timeout_s)
            # The timed-out thread is still running on this agent (threads
            # cannot be killed). Drop our reference so the NEXT consult builds
            # a fresh agent instead of sharing state with the zombie.
            self._agent = None
            return (
                "Sorry — that took too long and I had to stop. Ask me to work on it "
                "in the background and I'll send you the result when it's done."
            )
        except Exception:  # noqa: BLE001 — never let a consult crash the call
            logger.error("[msteams_bridge] agent consult failed", exc_info=True)
            return "Sorry, I ran into an error working on that."
