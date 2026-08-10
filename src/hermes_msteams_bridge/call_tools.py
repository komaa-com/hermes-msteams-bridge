"""CallToolRunner — runs the realtime model's tool calls.

Separates the tool surface (agent consult / background task / look_at_screen /
show_to_caller / call_me_back / post_meeting_minutes) from the realtime handler's
transport + dialogue loop. Reads per-call state off the handler it's given.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib as _hashlib
import logging
from pathlib import Path

from . import managed_chat, meeting
from .call_session_base import _pending_set
from .outbound import OutboundError, place_call

logger = logging.getLogger(__name__)


class CallToolRunner:
    def __init__(self, handler) -> None:
        self._h = handler

    async def run_tool(self, name: str, args: dict) -> str:
        h = self._h
        try:
            if name == "hermes_agent_consult":
                return await h._consult.ask(str(args.get("query", "")))
            if name == "hermes_agent_task":
                return await self._agent_task(str(args.get("query", "")))
            if name == "look_at_screen":
                return await self._look_at_screen(
                    str(args.get("question", "")), args.get("source"), str(args.get("scope") or "live")
                )
            if name == "show_to_caller":
                return await self._show_to_caller(str(args.get("prompt", "")), args.get("count", 1))
            if name == "show_file":
                return await self._show_file(str(args.get("path", "")), args.get("page", 1))
            if name == "show_web_page":
                return await self._show_web_page(str(args.get("url", "")))
            if name == "walkthrough":
                return await self._walkthrough(args.get("steps") or [])
            if name == "set_call_language":
                return await self._set_call_language(str(args.get("language", "")))
            if name == "call_me_back":
                return await self._call_me_back(str(args.get("message", "")))
            if name == "post_meeting_minutes":
                return await meeting.post_minutes(h._consult, h._meeting, h._thread_id)
            if name == "post_chat_message":
                return await self._post_chat_message(str(args.get("text", "")))
        except Exception:  # noqa: BLE001 — a tool fault must not break the call
            logger.error("[teams_call] tool %s failed", name, exc_info=True)
            return "Sorry, that didn't work."
        return f"Unknown tool: {name}."

    async def _post_chat_message(self, text: str) -> str:
        """Post to the call's Teams chat through the gateway - the managed connection's own messages hop.

        The minutes tool posts via the HOST's Teams platform, which needs the customer's own Bot
        Framework credentials. A managed customer has none, so on that tier "post this to the chat"
        could only ever fail. The plugin already holds both sockets; this uses the one meant for it.
        """
        h = self._h
        text = text.strip()
        if not text:
            return "There was nothing to post."
        cfg = getattr(h, "_bridge", None)
        tenant_id = getattr(h, "_tenant_id", None)
        chat_secret = getattr(cfg, "managed_chat_secret", "") if cfg else ""
        reply_url = getattr(cfg, "managed_chat_gateway_reply_url", "") if cfg else ""
        # A BYO/free deployment has no gateway to post through: the tenant is absent because the caller
        # only asserts it for managed calls. Say so plainly instead of failing silently mid-sentence.
        if not (tenant_id and chat_secret and reply_url):
            return "I can only post to the chat on a StandIn managed connection - this call is not on one."
        # A 1:1 call has no chat thread of its own: the worker sends threadId=callId as a fallback, and
        # Teams cannot resolve that as a conversation (the minutes tool fails the same way, with
        # "Could not resolve <callId> on teams"). A MEETING call does have one - "19:meeting_...@thread.v2".
        # Posting to a call id would be a silent no-op dressed up as success, so say what is true.
        conversation_id = h._thread_id or ""
        if not conversation_id.startswith("19:"):
            return (
                "I can only post to the chat from a meeting call - a 1:1 call has no Teams chat thread "
                "to post into. Ask me in our chat instead, or start this from a meeting."
            )
        ok = await managed_chat.post_message(
            chat_secret=chat_secret,
            gateway_reply_url=reply_url,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            text=text,
            # Scoped to the call AND the content, so a retried tool call does not double-post while two
            # genuinely different posts in one call both go out.
            #
            # sha256, NOT hash(): Python salts hash() per process (PYTHONHASHSEED), so the same text
            # produced a different key after every restart - the one situation a retry actually spans.
            # Matches the OpenClaw side, which has always hashed with sha256.
            idempotency_key=(
                f"call-{h._session.call_id if h._session else 'x'}-"
                f"{_hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
            ),
        )
        return (
            "I've posted that to the Teams chat."
            if ok
            else "I couldn't post to the Teams chat just now."
        )

    async def _look_at_screen(self, question: str, source: str | None, scope: str = "live") -> str:
        h = self._h
        if not h._vision_budget.try_consume():
            return "I've looked at a lot just now — give me a moment before the next one."
        prompt = question.strip() or "Describe what you see."
        if scope == "history":
            frames = h._vision.history(limit=6)
            if not frames:
                return "I don't have any earlier frames to look back on."
            blocks: list[dict] = [{"type": "text", "text": prompt}]
            for f in frames:  # timestamped, attributed keyframes
                blocks.append({"type": "text", "text": f"(earlier, from {f.describe()})"})
                blocks.append({"type": "image", "url": f.data_url()})
        else:
            want = "camera" if str(source or "").lower() == "camera" else "screenshare"
            frame = h._vision.latest(want) or h._vision.latest()
            if frame is None:
                return "I can't see a shared screen or camera right now."
            blocks = [
                {"type": "text", "text": f"{prompt} (looking at the {frame.describe()})"},
                {"type": "image", "url": frame.data_url()},
            ]
        return await self._vision_consult(blocks)

    async def _vision_consult(self, blocks: list[dict]) -> str:
        from .hermes_api import vision_ask

        text = await vision_ask(
            "You are looking at frames from a live Microsoft Teams call. Answer the "
            "question concisely, in a form suitable to be spoken aloud.",
            blocks,
        )
        if not text:  # facade missing or the call failed — give the budget back
            self._h._vision_budget.refund()
            return "I had trouble looking at that."
        return text

    async def _show_to_caller(self, prompt: str, count: object = 1) -> str:
        prompt = prompt.strip()
        if not prompt:
            return "What would you like me to show?"
        try:
            n = max(1, min(int(count), 3))
        except (TypeError, ValueError):
            n = 1
        try:
            from .hermes_api import generate_image

            paths: list[str] = []
            for _ in range(n):
                # Off-loop: dispatch bridges async handlers with its own loop.
                data = await asyncio.to_thread(generate_image, prompt)
                if data.get("success") and data.get("image"):
                    paths.append(data["image"])
            if not paths:
                return "I couldn't create that image."
            # Paced slideshow — fullscreen (content must be legible; overlay is
            # a small PiP inset for thumbnails only) and barge-in aware (D3): a
            # caller interruption bumps the handler's turn id, which stops the
            # deck instead of pushing the next slide over them.
            turn0 = getattr(self._h, "_turn_id", None)

            def _interrupted() -> bool:
                session = self._h._session
                if session is None or session.closed:
                    return True
                return turn0 is not None and getattr(self._h, "_turn_id", turn0) != turn0

            shown = 0
            for idx, path in enumerate(paths):
                if _interrupted():
                    break
                final = idx == len(paths) - 1
                # image_generate returns a provider URL (not a file); localize
                # with the bounded fetcher rather than treating it as a path.
                from .hermes_api import fetch_image_bytes

                fetched = await fetch_image_bytes(str(path))
                if fetched is None:
                    continue
                img_bytes, mime = fetched
                await self._h._session.send_display_image(
                    base64.b64encode(img_bytes).decode("ascii"),
                    mime,
                    duration_ms=5000,
                    caption=prompt[:80],
                )
                shown += 1
                if not final:  # hold the slide, but react to a barge-in fast
                    for _ in range(9):
                        await asyncio.sleep(0.5)
                        if _interrupted():
                            break
            if shown == 0:
                return "Okay, I've stopped."
            if shown < len(paths):
                return f"Stopped after {shown} of {len(paths)} images."
            return "I'm showing it on screen now." if shown == 1 else f"Showing you {shown} images."
        except Exception:  # noqa: BLE001
            logger.error("[teams_call] show_to_caller failed", exc_info=True)
            return "I made the image but couldn't display it."

    def _show_root(self) -> Path | None:
        """The show_file containment root (config, else the Hermes workspace)."""
        from .hermes_api import hermes_home

        root_cfg = getattr(self._h._bridge, "show_file_root", "") if self._h._bridge else ""
        try:
            if root_cfg:
                return Path(root_cfg)
            # Default is a DEDICATED presentation directory, not the whole
            # workspace: a benign-looking filename in a broad root is still a
            # disclosure. Operators widen it explicitly via show_file_root.
            return hermes_home() / "workspace" / "teams_call_show"
        except Exception:  # noqa: BLE001 — no Hermes home (bare install)
            return None

    async def _show_file(self, rel_path: str, page: object = 1) -> str:
        """§3.1: render a real workspace file (contained) onto the tile."""
        from .display_files import ShowFileError, render_for_display

        h = self._h
        if h._session is None:
            return "There's no active call to show it on."
        try:
            page_no = max(1, int(page))
        except (TypeError, ValueError):
            page_no = 1
        root = self._show_root()
        if root is None:
            return "I don't have a workspace to show files from on this install."
        try:
            # Containment + render are CPU/subprocess-bound — off the loop.
            data, mime, caption = await asyncio.to_thread(
                render_for_display, root, rel_path, page_no
            )
        except ShowFileError as exc:
            return exc.spoken
        except Exception:  # noqa: BLE001 — render failure must not break the call
            logger.error("[teams_call] show_file failed", exc_info=True)
            return "I couldn't display that file."
        if len(data) > 6 * 1024 * 1024:  # keep the WS frame bounded post-base64
            return "That file renders too large to put on screen."
        await h._session.send_display_image(
            base64.b64encode(data).decode("ascii"), mime,
            duration_ms=15000, caption=caption,
        )
        return f"I'm showing {caption} on screen now."

    async def _show_web_page(self, url: str) -> str:
        """§3.1: browse the actual page via the host's browser tools and show it."""
        from .hermes_api import browser_page_screenshot

        h = self._h
        url = (url or "").strip()
        if h._session is None:
            return "There's no active call to show it on."
        if not (url.startswith("https://") or url.startswith("http://")):
            return "I can only show regular web pages (http or https)."
        # Mandatory plugin-side SSRF gate: the Hermes browser relaxes its
        # private-network protection for local backends (its threat model
        # assumes a local operator authored the URL); ours is a remote caller.
        from .display_files import url_is_public

        ok, reason = await asyncio.to_thread(url_is_public, url)
        if not ok:
            return reason
        # Call-scoped browser session: without a task_id every call shares
        # Hermes's "default" session (cookies + navigation collisions).
        task_id = f"teams_call:{getattr(h._session, 'call_id', '') or 'call'}"
        try:
            result = await asyncio.wait_for(
                browser_page_screenshot(url, task_id=task_id), timeout=45.0
            )
        except asyncio.TimeoutError:
            # The dispatch keeps running in its worker thread (no public
            # cancel/close tool exists) — park the session on about:blank so
            # the stray navigation cannot keep mutating it (task #2).
            from .hermes_api import dispatch_tool_async

            asyncio.create_task(
                dispatch_tool_async("browser_navigate", {"url": "about:blank", "task_id": task_id})
            )
            return "That page took too long to load."
        if result is None:
            return "I couldn't open that page."
        shot_path, _note = result
        try:
            data = Path(shot_path).read_bytes()
        except OSError:
            return "I opened the page but couldn't capture it."
        if len(data) > 8 * 1024 * 1024:  # a tile frame never needs more
            return "The page capture came out too large to display."
        await h._session.send_display_image(
            base64.b64encode(data).decode("ascii"), "image/png",
            duration_ms=15000, caption=url[:80],
        )
        return "I've put the page up on screen."

    async def _walkthrough(self, steps: object) -> str:
        """§3.1 Step B: paced visual walkthrough — show a file, speak the step,
        advance; a barge-in (turn bump) or hangup stops the deck immediately."""
        from .display_files import ShowFileError, render_for_display

        h = self._h
        if h._session is None:
            return "There's no active call to walk through this on."
        if getattr(h, "_rt", None) is None:
            return "Walkthroughs need the realtime voice mode."
        if not isinstance(steps, list) or not steps:
            return "I need at least one step to walk through."
        steps = steps[:6]
        root = self._show_root()
        if root is None:
            return "I don't have a workspace to show files from on this install."

        turn0 = getattr(h, "_turn_id", None)

        def _interrupted() -> bool:
            session = h._session
            if session is None or session.closed:
                return True
            return turn0 is not None and getattr(h, "_turn_id", turn0) != turn0

        done = 0
        for step in steps:
            if _interrupted():
                break
            rel_path = str((step or {}).get("path", ""))
            say = str((step or {}).get("say", "")).strip()
            try:
                page = max(1, int((step or {}).get("page", 1)))
            except (TypeError, ValueError):
                page = 1
            try:
                data, mime, caption = await asyncio.to_thread(
                    render_for_display, root, rel_path, page
                )
            except ShowFileError as exc:
                return (
                    f"I stopped the walkthrough at step {done + 1}: {exc.spoken}"
                    if done else exc.spoken
                )
            if len(data) > 6 * 1024 * 1024:  # same outbound cap as show_file
                return f"Step {done + 1} renders too large to put on screen."
            await h._session.send_display_image(
                base64.b64encode(data).decode("ascii"), mime,
                duration_ms=25000, caption=caption,
            )
            say_done = getattr(h, "_say_done", None)
            if say and say_done is not None:
                say_done.clear()
            if say:
                await h._rt.request_say(say)
            done += 1
            # Advance when the model finished SPEAKING the step (response.done
            # sets the event); the length-based hold is only the ceiling and
            # the fallback when no event exists. Poll in 0.5 s slices so a
            # barge-in stops the deck fast either way.
            hold_s = min(30.0, max(4.0, len(say) * 0.08))
            waited = 0.0
            while waited < hold_s:
                if say_done is not None and say_done.is_set() and waited >= 1.0:
                    break
                await asyncio.sleep(0.5)
                waited += 0.5
                if _interrupted():
                    break
        if done == 0:
            return "Okay, I've stopped."
        if done < len(steps):
            return f"Stopped after step {done} of {len(steps)}."
        return f"That's the whole walkthrough — {done} steps."

    async def _set_call_language(self, language: str) -> str:
        """D5 made user-visible (task #4): pin the running call to a language
        via ``session.update`` — no reconnect, applies to the next reply."""
        h = self._h
        code = (language or "").strip().lower()
        if not code.isalpha() or not (2 <= len(code) <= 8):
            return "I didn't recognize that language code."
        rt = getattr(h, "_rt", None)
        set_lang = getattr(h, "set_call_language", None)
        if rt is None or set_lang is None:
            return "Language pinning needs the realtime voice mode."
        await set_lang(code)
        return f"Done — continuing in {code} from now on."

    async def _call_me_back(self, message: str) -> str:
        h = self._h
        message = message.strip()
        caller = h._caller
        if h._bridge is None or caller is None or not caller.aad_id:
            return "I can't call you back — I don't have a number to reach you."
        tenant = caller.tenant_id or h._bridge.tenant_id
        if not tenant:
            return "I can't call you back — missing your tenant."
        try:
            result = await place_call(
                user_object_id=caller.aad_id,
                tenant_id=tenant,
                shared_secret=h._bridge.shared_secret,
                worker_base_url=h._bridge.worker_base_url,
                allow_remote=h._bridge.allow_remote_worker,
            )
        except OutboundError as exc:
            logger.warning("[teams_call] call_me_back failed: %s", exc)
            return "I couldn't place the call-back just now."
        call_id = result.get("callId")
        if not call_id:
            return "The call-back was accepted but I got no call id, so it may not ring."
        _pending_set(call_id, message or "Here's what you asked for.", thread_id=h._thread_id or "")
        return "Okay — I'll call you right back with that."

    async def _agent_task(self, query: str) -> str:
        """Run a long job in the background; deliver the result to the Teams chat
        (preferred) or via a voice call-back."""
        h = self._h
        query = query.strip()
        caller = h._caller
        if not query:
            return "What would you like me to work on?"
        # Need either a postable thread (chat delivery) or an AAD id (call-back).
        if h._bridge is None or (not h._thread_id and (caller is None or not caller.aad_id)):
            return await h._consult.ask(query)  # no delivery path → inline
        # Durability (task #1): persist the promise BEFORE starting; a restart
        # mid-run resumes it from the job store instead of losing it.
        from .background_jobs import job_create

        job_id = job_create(query, h._thread_id or "")
        job = asyncio.create_task(self._run_background_task(query, caller, job_id))
        # §3.2 "watch it work": refresh a status panel on the tile while the
        # job runs. Fire-and-forget; exits on job end, hangup, or barge-in.
        asyncio.create_task(self._progress_loop(job, query))
        return "Got it — I'll work on that in the background and send you the result."

    async def _progress_loop(self, job: asyncio.Task, query: str) -> None:
        """Refresh the tile while ``job`` runs: the honest progress panel, and
        (opt-in, phase 4b) periodic real frames of the agent's browser when the
        task is browser-shaped — throttled because each capture may invoke the
        auxiliary vision model."""
        import time as _time

        from .progress_panel import render_panel

        h = self._h
        turn0 = getattr(h, "_turn_id", None)
        started = _time.monotonic()
        bridge = getattr(h, "_bridge", None)
        watch_browser = bool(getattr(bridge, "watch_browser_tasks", False)) if bridge else False
        last_browser_try = 0.0
        last_digest = ""
        try:
            while not job.done():
                session = h._session
                if session is None or session.closed:
                    return
                if turn0 is not None and getattr(h, "_turn_id", turn0) != turn0:
                    return  # the caller moved on; free the tile
                now = _time.monotonic()
                if now - started > 300:
                    return  # panel is a courtesy, not wallpaper
                shown_browser = False
                if watch_browser and now - last_browser_try >= 10.0:
                    last_browser_try = now
                    shot = await self._grab_agent_browser_frame(last_digest)
                    if shot is not None:
                        frame_bytes, last_digest = shot
                        try:
                            await session.send_display_image(
                                base64.b64encode(frame_bytes).decode("ascii"), "image/png",
                                duration_ms=9000, caption="working… (live view)",
                            )
                            shown_browser = True
                        except Exception:  # noqa: BLE001
                            return
                if not shown_browser:
                    png = await asyncio.to_thread(render_panel, query, now - started)
                    if png is None:
                        return  # Pillow absent — spoken ack only
                    try:
                        await session.send_display_image(
                            base64.b64encode(png).decode("ascii"), "image/png",
                            duration_ms=4000, caption="working…",
                        )
                    except Exception:  # noqa: BLE001
                        return
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise

    async def _grab_agent_browser_frame(self, last_digest: str) -> tuple[bytes, str] | None:
        """One throttled capture of the consult agent's OWN browser session
        (its pinned ``browser_task_id`` — the default session belongs to
        whoever else is browsing, round 8). ``None`` when the agent isn't
        browsing, the frame is unchanged, or capture fails. Change detection
        is content-based: Hermes writes a fresh screenshot file per capture,
        so path comparison would always report "changed"."""
        import hashlib

        from .hermes_api import dispatch_tool_async, _parse_tool_json

        consult = getattr(self._h, "_consult", None)
        task_id = str(getattr(consult, "browser_task_id", "") or "")
        if not task_id:
            return None  # no consult agent -> nothing of ours to watch
        try:
            result = _parse_tool_json(await asyncio.wait_for(
                dispatch_tool_async(
                    "browser_vision", {"question": "Progress check.", "task_id": task_id}
                ),
                timeout=20.0,
            ))
        except asyncio.TimeoutError:
            return None
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        path = str(result.get("screenshot_path") or meta.get("screenshot_path") or "")
        if not path:
            return None
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        if not data.startswith(b"\x89PNG") or len(data) > 6 * 1024 * 1024:
            return None
        digest = hashlib.sha256(data).hexdigest()
        if digest == last_digest:
            return None
        return data, digest

    async def _run_background_task(self, query: str, caller, job_id: str | None = None) -> None:
        from .background_jobs import job_begin, job_complete

        job_begin(job_id)  # two-phase for the live path too (double-delivery guard)
        h = self._h
        try:
            result = await h._consult.ask(query, timeout_s=300.0)
        except Exception:  # noqa: BLE001
            logger.error("[teams_call] background task failed", exc_info=True)
            result = "I couldn't complete that task."
        # Prefer delivering the result to the Teams chat (no call-back needed);
        # fall back to a voice call-back when there's no postable thread.
        if h._thread_id:
            from .meeting import _deliver_to_teams

            if await _deliver_to_teams(h._thread_id, f"✅ {result}"):
                job_complete(job_id)
                return
        if h._bridge is None or caller is None or not caller.aad_id:
            return
        tenant = caller.tenant_id or h._bridge.tenant_id
        if not tenant:
            return
        try:
            res = await place_call(
                user_object_id=caller.aad_id,
                tenant_id=tenant,
                shared_secret=h._bridge.shared_secret,
                worker_base_url=h._bridge.worker_base_url,
                allow_remote=h._bridge.allow_remote_worker,
            )
        except OutboundError as exc:
            logger.warning("[teams_call] background callback failed: %s", exc)
            return
        cid = res.get("callId")
        if cid:
            _pending_set(cid, result, thread_id=h._thread_id or "")
            job_complete(job_id)  # the pending store owns delivery from here
