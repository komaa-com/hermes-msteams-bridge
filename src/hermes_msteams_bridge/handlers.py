"""Call-session handlers — the dialogue brains the bridge dispatches into.

* :class:`EchoCallSessionHandler` — dependency-light smoke test: smiles on connect
  and echoes the caller's audio back on the ``audio.frame`` wire message.

* :class:`RealtimeCallSessionHandler` — the full speech-to-speech brain:
  recording gate, **echo guard** (self-answer fix), bidirectional resampled audio,
  expression cues + **realtime visemes**, **barge-in**, and the realtime tool set:
  **agent delegation** (`hermes_agent_consult` → `run_agent`), **vision**
  (`look_at_screen`), **show_to_caller** (image → tile), and **outbound call-back**
  (`call_me_back`, delivered on the worker's outbound leg).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path

from . import audio, expression, group_call_gate, meeting, protocol, realtime_tools, verbal_interrupts, viseme_estimate
from .bridge_server import CallSession, CallSessionHandler
from .call_session_base import BaseTeamsCallHandler
from .call_tools import CallToolRunner
from .config import BYTES_PER_FRAME, FRAME_DURATION_MS, PCM_SAMPLE_RATE_HZ, TeamsVoiceConfig
from .echo_guard import EchoGuard
from .realtime.openai_client import REALTIME_SAMPLE_RATE_HZ, RealtimeConfig, RealtimeSession
from .vision_budget import VisionBudget
from .vision_store import StoredFrame, VisionStore

PCM_SAMPLE_RATE_HZ_MS = PCM_SAMPLE_RATE_HZ // 1000  # samples per ms (16) — duration math

# Human names for the §3.8 ``languages:`` instruction clause; unknown codes
# pass through verbatim (the model copes with ISO codes fine).
_LANGUAGE_NAMES = {
    "en": "English", "ar": "Arabic", "fr": "French", "de": "German",
    "es": "Spanish", "it": "Italian", "nl": "Dutch", "pt": "Portuguese",
    "tr": "Turkish", "ru": "Russian", "zh": "Chinese", "ja": "Japanese",
    "ko": "Korean", "hi": "Hindi", "ur": "Urdu", "pl": "Polish",
    "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
}

logger = logging.getLogger(__name__)


class EchoCallSessionHandler(CallSessionHandler):
    """Smoke-test handler — visible proof the driver path works end to end."""

    def __init__(self) -> None:
        self._seq = 0
        self._ts = 0

    async def on_session_start(self, session: CallSession, msg: protocol.SessionStart) -> None:
        await super().on_session_start(session, msg)
        try:
            await session.send_expression(expression.HAPPY)
        except Exception:  # noqa: BLE001 — cosmetic; never fail the call
            logger.debug("[msteams_bridge] echo: expression send failed", exc_info=True)

    async def on_audio_frame(self, session: CallSession, msg: protocol.AudioFrame) -> None:
        if not session.recording_active:
            return
        try:
            await session.send_audio_frame(self._seq, self._ts, msg.payload_base64)
        except Exception:  # noqa: BLE001
            return
        self._seq += 1
        self._ts += FRAME_DURATION_MS


class RealtimeCallSessionHandler(BaseTeamsCallHandler):
    """Bridges a Teams call to an OpenAI/Azure realtime speech-to-speech model."""

    def __init__(self, config: RealtimeConfig, bridge_config: TeamsVoiceConfig | None = None) -> None:
        super().__init__(bridge_config)  # shared session policy / state
        self._cfg = config
        self._rt: RealtimeSession | None = None
        # Outbound (model -> worker) framing state.
        self._out_seq = 0
        self._out_ts = 0
        self._out_residual = b""
        # Dialogue state.
        self._turn_id = 0
        self._transcript = ""
        self._last_emotion: str | None = None
        self._echo = EchoGuard()
        self._vision = VisionStore()
        self._drop_response = False  # deterministic egress drop for gated turns
        # Ambient continuous vision (push the latest changed frame per source ~6s).
        self._ambient_task: asyncio.Task | None = None
        self._ambient_interval_s = 6.0
        self._ambient_last_ts: dict[str, int] = {}
        self._vision_budget = VisionBudget(bridge_config.max_vision_per_minute if bridge_config else 30)
        # Speaker attribution (``_last_speaker`` lives on the base — latest frame).
        # ``_turn_speaker`` is latched on the FIRST frame of a caller turn and is what
        # the finished turn is attributed to: input transcripts finalize a beat after
        # end-of-speech, so by then _last_speaker can already be the NEXT person and
        # a group call collapses everyone's words onto whoever spoke last.
        self._turn_speaker = ""
        # Last speaker announced to the model, so one label is injected per change
        # rather than one per 20 ms frame.
        self._announced_speaker = ""
        self._auto_on = True  # server-VAD auto-response (off until 1:1 is confirmed)
        self._tools: CallToolRunner | None = None  # built once the session is established
        # Set on every response.done — lets the walkthrough advance when the
        # model actually finished speaking a step instead of guessing by length.
        self._say_done = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def on_session_start(self, session: CallSession, msg: protocol.SessionStart) -> None:
        if not await self._begin_session(session, msg):  # state + allowlist + scope
            return
        self._tools = CallToolRunner(self)

        rt = RealtimeSession(replace(self._cfg, instructions=self._build_instructions()))
        rt.tools = realtime_tools.default_tools()
        rt.on_audio_delta = self._on_model_audio
        rt.on_transcript_delta = self._on_transcript
        rt.on_input_transcript = self._on_input_transcript
        rt.on_speech_started = self._on_barge_in
        rt.on_response_done = self._on_response_done
        rt.on_function_call = self._on_function_call
        rt.on_error = self._on_realtime_error
        rt.on_close = self._on_realtime_closed
        self._rt = rt
        try:
            await rt.connect()
        except Exception:  # noqa: BLE001 — provider unreachable at connect time
            # Without a working realtime brain the caller would sit in silent dead
            # air until they hang up. Tear the Teams
            # call down cleanly instead. (on_session_end then closes rt.)
            logger.error("[msteams_bridge] realtime connect failed for %s", session.call_id, exc_info=True)
            await self._close_call("realtime-connect-failed")
            return
        # Start in MANUAL response mode (auto-response off): until participants is
        # known, no auto-reply can leak in a meeting. We enable auto-response only
        # once we learn it's a 1:1; group/unknown stays manual (we create_response
        # ourselves for addressed turns). Race-free.
        await rt.set_auto_response(False)
        self._auto_on = False
        # Greeting normally fires on recording-active (greet-on-answer); show a neutral face now.
        await self._safe_expression(expression.NEUTRAL)
        self._ambient_task = asyncio.create_task(self._ambient_vision_loop())
        # ...but when recording is NOT required, that transition may never arrive, and the greeting
        # was the ONLY thing that started the conversation: the call connected and the bot sat mute
        # for its whole duration. Inbound is already answered, so there is nothing to wait for.
        if self._greet_without_recording(session):
            await self._run_greeting_plan(session)

    def _build_instructions(self) -> str:
        """Augment base instructions with roster name + group-gate etiquette.

        Identity comes FROM HERMES, not from this plugin: the operator's
        SOUL.md (the same slot Hermes injects into every chat prompt) leads,
        so "what is your name?" gets the same answer on a call as in chat.
        The realtime ``instructions`` config remains the voice-behaviour layer
        on top (brevity, delegation), not a second identity."""
        from .hermes_api import soul_text

        parts = []
        soul = soul_text()
        if soul:
            parts.append(
                "Your identity and persona (the same assistant the user knows "
                f"from chat):\n{soul}\n\nYou are currently speaking on a live "
                "Microsoft Teams voice call."
            )
        parts.append(self._cfg.instructions)
        from .hermes_api import skills_index_text

        skills = skills_index_text()
        if skills:
            parts.append(
                "You also have the user's installed Hermes skills, the same ones "
                "available in chat. You cannot run them inside this voice loop: "
                "delegate skill work with hermes_agent_consult (quick) or "
                "hermes_agent_task (long-running, result delivered afterwards), "
                "and tell the caller what you're doing. Installed skills:\n"
                f"{skills}"
            )
        name = self._first_name()
        if name:
            # Roster-aware presence: the model is told who it is talking to AND how the
            # per-speaker labels it will receive are meant to be used, so a meeting stops
            # sounding like one undifferentiated voice.
            parts.append(
                f"CALLER IDENTITY: You are speaking with {name}. Greet them by their "
                "first name once, warmly and briefly, then continue naturally - do not "
                "repeat their name every turn. On a group call the person talking is "
                'announced to you in brackets (e.g. "[The person now speaking is '
                'Sara.]"); use those names to address people directly when it helps, '
                "and to keep track of who said what, but never read an announcement "
                "aloud as part of your reply."
            )
        phrases = ", ".join(f'"{p}"' for p in self._gate_cfg.wake_phrases)
        parts.append(
            "If more than one person is on the call, stay silent unless someone "
            f"addresses you by name ({phrases}); in a one-on-one call respond normally."
        )
        languages = tuple(getattr(self._cfg, "languages", ()) or ())
        if languages:  # §3.8 (bilingual is resolved into this upstream)
            names = ", ".join(_LANGUAGE_NAMES.get(code, code) for code in languages)
            first = _LANGUAGE_NAMES.get(languages[0], languages[0])
            parts.append(
                f"You speak these languages: {names}. Reply in the caller's language "
                f"when it is one of them; otherwise politely continue in {first}. "
                "Switch when the caller switches, and translate on request."
            )
        else:
            parts.append(
                "Detect the caller's language and reply in it; switch when they switch."
            )
        return " ".join(parts)

    async def set_call_language(self, code: str) -> None:
        """Pin ``languages`` to one code and push rebuilt instructions live
        (persona, etiquette and skills clauses included) — D5 in action."""
        self._cfg = replace(self._cfg, languages=(code,))
        if self._rt is not None:
            await self._rt.update_instructions(self._build_instructions())

    async def _close_call(self, reason: str) -> None:
        """Tear the Teams call down.

        Closes the worker session WebSocket so the caller isn't left in dead air on
        a provider failure. The bridge read-loop then runs on_session_end teardown,
        which closes the realtime session and cancels the ambient task. Best-effort
        and idempotent: a second call on an already-closed socket is a no-op."""
        session = self._session
        if session is None or session.closed:
            return
        logger.info("[msteams_bridge] closing Teams call %s: %s", session.call_id, reason)
        try:
            await session._ws.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.debug("[msteams_bridge] error closing call ws for %s", session.call_id, exc_info=True)

    async def _on_realtime_closed(self, reason: str = "provider-closed") -> None:
        """The realtime provider dropped mid-call → end the Teams call cleanly."""
        await self._close_call(f"realtime-{reason}")

    async def _on_realtime_error(self, error: object) -> None:
        """Provider 'error' event. The session already reset _response_active so the
        next turn can speak; surface it here for observability / future recovery."""
        logger.warning("[msteams_bridge] realtime provider error on %s: %s",
                       self._session.call_id if self._session else "?", error)

    async def on_recording_status(self, session: CallSession, msg: protocol.RecordingStatus) -> None:
        await super().on_recording_status(session, msg)
        # Outbound delivery: speak the result only once the callee has answered
        # (recording active), not while the phone is still ringing (greet-on-answer).
        if not session.recording_active:
            return
        await self._run_greeting_plan(session)

    async def _run_greeting_plan(self, session: CallSession) -> None:
        """Deliver the opening line. Reached either when recording goes active (greet-on-answer) or,
        when recording is not required, as soon as an inbound session is ready."""
        if self._rt is None:
            return
        plan = self._greeting_plan()
        if plan is None:
            return
        kind, payload = plan
        if kind == "deliver":
            await self._rt.request_say(
                f"The caller just answered. Deliver this result clearly and concisely, "
                f"then say goodbye: {payload}"
            )
        else:  # greet by name, on answer (not while ringing)
            who = f" the caller, {payload}," if payload else " the caller"
            await self._rt.request_say(
                f"Greet{who} warmly and briefly, then ask how you can help."
            )

    async def on_participants(self, session: CallSession, msg: protocol.Participants) -> None:
        await super().on_participants(session, msg)  # sets session.human_count
        # Race-free group gate: enable server-VAD auto-response only for a confirmed
        # 1:1; meetings (2+ humans) stay manual (we create a response only for an
        # addressed turn), so no audio can leak before a cancel.
        enable = session.human_count < 2
        if self._rt is not None:
            await self._rt.set_auto_response(enable)
        self._auto_on = enable

    async def on_audio_frame(self, session: CallSession, msg: protocol.AudioFrame) -> None:
        if self._require_recording and not session.recording_active:
            return
        if self._rt is None:
            return
        if msg.speaker_name:  # unmixed-audio attribution
            self._last_speaker = msg.speaker_name
            if not self._turn_speaker:  # first frame of this caller turn
                self._turn_speaker = msg.speaker_name
            await self._announce_speaker(msg.speaker_name)
        pcm16 = base64.b64decode(msg.payload_base64)
        if not self._echo.allow_input(audio.pcm16_rms(pcm16)):  # echo guard
            return
        pcm24 = audio.resample_pcm16(pcm16, PCM_SAMPLE_RATE_HZ, REALTIME_SAMPLE_RATE_HZ)
        await self._rt.push_audio(pcm24)

    async def _announce_speaker(self, name: str) -> None:
        """Tell the MODEL who is talking, once per speaker change.

        The realtime model transcribes the caller's audio itself, so an unmixed
        ``speakerName`` that only ever reached the minutes left it hearing a
        five-person meeting as one voice. A non-responding user item lands in the
        conversation *before* the turn's audio is committed, so the words that
        follow are attributed to the right person. Cosmetic-cost only: no
        response is created and no vision budget is touched.
        """
        if not name or name == self._announced_speaker or self._rt is None:
            return
        self._announced_speaker = name
        try:
            await self._rt.send_user_text(
                f"[The person now speaking is {name}.]", respond=False
            )
        except Exception:  # noqa: BLE001 — attribution is best-effort, never fail a turn
            logger.debug("[msteams_bridge] speaker announce failed", exc_info=True)

    async def on_video_frame(self, session: CallSession, msg: protocol.VideoFrame) -> None:
        if self._require_recording and not session.recording_active:
            return
        self._vision.store(
            StoredFrame(
                source=msg.source,
                data_base64=msg.data_base64,
                mime=msg.mime or "image/jpeg",
                ts=msg.ts,
                participant_name=msg.participant_name,
            )
        )

    async def on_dtmf(self, session: CallSession, msg: protocol.Dtmf) -> None:
        # Surface keypad input to the realtime model (recording-gated) so it can
        # run "press 1 to…" flows.
        if (self._require_recording and not session.recording_active) or self._rt is None:
            return
        await self._rt.send_user_text(f"The caller pressed the {msg.digit} key on the keypad.")

    async def on_assistant_say(self, session: CallSession, msg: protocol.AssistantSay) -> None:
        # H4: the worker asks the agent to speak a line (e.g. a brief goodbye right before a
        # limit-cutoff teardown) in its own realtime voice. Inject it as an instruction and trigger
        # a spoken response. Not recording-gated: the worker explicitly requested this utterance.
        text = (msg.text or "").strip()
        if self._rt is None or not text:
            return
        await self._rt.request_say(f"Say this to the caller, then stop: {text}")

    async def speak_text(self, text: str) -> None:
        """Gateway delivery into this live call (phase 2b): speak ``text``."""
        if self._rt is not None and (text or "").strip():
            await self._rt.request_say(f"Relay this to the caller now: {text}")

    async def on_session_end(self, session: CallSession, msg: protocol.SessionEnd) -> None:
        await super().on_session_end(session, msg)
        if getattr(self, "_gateway_adapter", None) is not None:
            from .gateway_adapter import unregister_live_call

            unregister_live_call(self._thread_id, self)
        if self._ambient_task is not None:
            self._ambient_task.cancel()
            self._ambient_task = None
        # End-of-meeting recap (opt-in) — run detached so teardown isn't blocked.
        if self._bridge and self._bridge.meeting_recap and not self._meeting.is_empty():
            asyncio.create_task(
                meeting.post_minutes(self._consult, self._meeting, self._thread_id)
            )
        self._vision.clear()
        if self._rt is not None:
            await self._rt.close()
            self._rt = None

    async def _ambient_vision_loop(self) -> None:
        """Every ~6s, push the latest *changed* frame to the model (no forced
        response), so it stays visually aware between explicit look_at_screen calls."""
        try:
            while True:
                await asyncio.sleep(self._ambient_interval_s)
                session = self._session
                # _recording_ok, not session.recording_active: this loop open-coded the check and so
                # kept ambient vision recording-gated even where the operator turned the requirement
                # off - the same class of bug as the greeting, in the one place it was not fixed.
                if self._rt is None or session is None or not self._recording_ok(session):
                    continue
                # Push each source (screen + camera) that changed since last time.
                # The worker only emits scene-change frames, so a new ts == a new scene.
                for src in ("screenshare", "camera"):
                    frame = self._vision.latest(src)
                    if frame is None or frame.ts == self._ambient_last_ts.get(src):
                        continue
                    if not self._vision_budget.try_consume_ambient():
                        break  # ambient share spent; explicit reserve stays
                    # D1: ship the attribution the frame already carries, not
                    # bare pixels — the model learns WHOSE surface changed.
                    # §3.3: the same label feeds the minutes' visual track
                    # (scene-change only, so no extra vision spend).
                    label = f"[ambient] New frame from {frame.describe()}."
                    self._meeting.add_visual(frame.describe())
                    try:
                        await self._rt.send_image(frame.data_url(), caption=label)
                    except Exception:  # noqa: BLE001 — ambient, best-effort
                        # Give the slot back AND leave the frame un-latched: latching
                        # first marked a failed push as "already delivered", so the
                        # loop never retried it while the budget it never benefited
                        # from stayed spent, starving look_at_screen.
                        self._vision_budget.refund()
                        logger.debug(
                            "[msteams_bridge] ambient vision push failed for %s", src, exc_info=True
                        )
                        continue
                    # Latch only AFTER a successful send.
                    self._ambient_last_ts[src] = frame.ts
        except asyncio.CancelledError:
            raise

    # ── model -> worker callbacks ────────────────────────────────────────────

    async def _on_model_audio(self, pcm24: bytes) -> None:
        session = self._session
        if session is None:
            return
        if self._drop_response:  # group gate dropped this (unaddressed) turn
            self._out_residual = b""
            return
        pcm16 = audio.resample_pcm16(pcm24, REALTIME_SAMPLE_RATE_HZ, PCM_SAMPLE_RATE_HZ)
        frames, self._out_residual = audio.frame_pcm16(self._out_residual + pcm16, BYTES_PER_FRAME)
        for frame in frames:
            try:
                await session.send_audio_frame(
                    self._out_seq, self._out_ts, base64.b64encode(frame).decode("ascii")
                )
            except Exception:  # noqa: BLE001
                return
            self._echo.note_output(FRAME_DURATION_MS)  # advance the playout clock
            self._out_seq += 1
            self._out_ts += FRAME_DURATION_MS

    async def _on_transcript(self, text: str) -> None:
        session = self._session
        if session is None:
            return
        self._transcript += text
        emotion = expression.infer_emotion(self._transcript)
        if emotion != self._last_emotion:
            self._last_emotion = emotion
            await self._safe_expression(emotion)
        # Approximate realtime visemes: estimate over this delta, anchored at the
        # current playout position, and send them as speech.marks on the wire.
        marks = viseme_estimate.estimate_visemes(text, max(len(text) * 60, 60))
        if marks:
            try:
                await session.send_speech_marks(viseme_estimate.marks_to_payload(marks), ts=self._out_ts)
            except Exception:  # noqa: BLE001
                pass

    async def _cut_playback(self) -> None:
        """Stop playback immediately: flush the worker queue and cancel the model."""
        self._turn_id += 1
        self._echo.collapse()
        self._echo.mark_caller_turn()
        self._out_residual = b""
        if self._session is not None:
            try:
                await self._session.send_assistant_cancel(self._turn_id)
            except Exception:  # noqa: BLE001
                pass
        if self._rt is not None:
            await self._rt.cancel_response()

    async def _on_barge_in(self) -> None:
        await self._cut_playback()

    async def _on_input_transcript(self, text: str) -> None:
        """Caller's finished turn — drive verbal interrupts and the group gate."""
        self._echo.mark_caller_turn()
        # Capture all speech for the minutes (full meeting, not just addressed turns),
        # attributed to whoever STARTED this turn — see _turn_speaker.
        self._meeting.add(
            self._turn_speaker or self._last_speaker or self._first_name() or "Caller", text
        )
        self._turn_speaker = ""  # next turn latches its own speaker
        # 1) Deterministic verbal interrupt ("stop" / "توقف" / "⟨name⟩, stop").
        if verbal_interrupts.is_verbal_interrupt(text, self._gate_cfg.wake_phrases):
            self._drop_response = True  # suppress any reply to the interrupt itself
            await self._cut_playback()
            return
        # 2) Group-call gate: stay silent unless addressed (2+ humans).
        now = time.monotonic() * 1000.0
        _is_group, decision = self._group_decision(text, now)
        if decision.respond:
            # We ARE answering this turn, so clear any drop latched by a prior
            # unaddressed bystander turn. That turn set _drop_response=True but
            # created no response, so no response.done ever fired to reset it;
            # left latched, _on_model_audio would then eat THIS addressed reply's
            # audio (until its own response.done). Reset here so the reply we
            # intend to deliver is not silently dropped.
            self._drop_response = False
            if decision.addressed:
                self._last_addressed_ms = now
            # In manual mode (group, or 1:1 before participants is known) auto-
            # response is OFF, so trigger the reply ourselves.
            if not self._auto_on and self._rt is not None:
                await self._rt.create_response()
        else:
            # Unaddressed meeting turn: egress-drop backstop + cancel any response.
            self._drop_response = True
            if self._rt is not None:
                await self._rt.cancel_response()

    async def _on_response_done(self) -> None:
        session = self._session
        if session is not None and self._out_residual:
            pad = self._out_residual + b"\x00" * (BYTES_PER_FRAME - len(self._out_residual))
            try:
                await session.send_audio_frame(
                    self._out_seq, self._out_ts, base64.b64encode(pad).decode("ascii")
                )
                self._out_seq += 1
                self._out_ts += FRAME_DURATION_MS
            except Exception:  # noqa: BLE001
                pass
        self._out_residual = b""
        if self._transcript.strip():
            self._meeting.add("Assistant", self._transcript)
        self._transcript = ""
        self._last_emotion = None
        self._drop_response = False  # next turn starts fresh
        self._say_done.set()  # a step's narration finished (walkthrough pacing)

    # ── tool dispatch ────────────────────────────────────────────────────────

    async def _on_function_call(self, name: str, call_id: str, args_json: str) -> None:
        try:
            args = json.loads(args_json or "{}")
        except (TypeError, ValueError):
            args = {}
        # Show a "thinking" face while the tool runs; the reply re-cues the emotion.
        await self._safe_expression(expression.THINKING)
        if self._tools is None:
            return
        result = await self._tools.run_tool(name, args if isinstance(args, dict) else {})
        if self._rt is not None:
            await self._rt.send_function_result(call_id, result or "Done.")


class StreamingCallSessionHandler(BaseTeamsCallHandler):
    """Streaming voice path: STT → agent → TTS (half-duplex, turn-based).

    Segments caller audio into utterances (VAD), transcribes them, applies the
    verbal-interrupt + group gate on the transcript, runs the Hermes agent, then
    speaks the reply via TTS with expression + estimated visemes.

    Provider support — both legs follow the host's config, per the official
    Hermes feature docs:

    * **STT** (``stt.provider``): Local Whisper (faster-whisper), Groq Whisper,
      OpenAI Whisper, Mistral Voxtral, xAI Grok STT, or a custom command
      provider — with Hermes's automatic fallback chain.
    * **TTS** (``tts.provider``): Edge, OpenAI, ElevenLabs, MiniMax, Mistral,
      Gemini, xAI, DeepInfra, NeuTTS, KittenTTS, Piper, or a custom command
      provider. ElevenLabs additionally upgrades visemes to real timing.

    The **realtime** handler (above) is the second call mode: provider-native
    speech-to-speech (OpenAI/Azure Realtime), where audio never leaves the
    realtime model, so the ``stt:``/``tts:`` blocks do not apply to it.
    """

    def __init__(self, bridge_config: TeamsVoiceConfig | None = None) -> None:
        super().__init__(bridge_config)  # shared session policy / state
        from .streaming_audio import UtteranceBuffer

        self._utterance_task: asyncio.Task | None = None
        self._buf = UtteranceBuffer()
        self._out_seq = 0
        self._out_ts = 0
        self._processing = False  # half-duplex: one utterance at a time
        # Vision: ingest video.frame and auto-attach a fresh frame's description
        # to the agent turn (budget-capped, recording-gated).
        self._vision = VisionStore()
        self._vision_budget = VisionBudget(bridge_config.max_vision_per_minute if bridge_config else 30)
        self._last_frame_ts: int | None = None

    async def on_session_start(self, session: CallSession, msg: protocol.SessionStart) -> None:
        if not await self._begin_session(session, msg):  # state + allowlist + scope
            return
        await self._safe_expression(expression.NEUTRAL)
        # Same fix realtime got, which streaming did not: with require_recording_status false, the
        # recording-active transition may never arrive, and on_recording_status below was the ONLY
        # place this handler ever greets. The call connected and the bot sat mute for its whole
        # duration - the exact symptom the realtime fix was written for.
        if self._greet_without_recording(session):
            await self._greet_now()

    async def on_recording_status(self, session: CallSession, msg: protocol.RecordingStatus) -> None:
        await super().on_recording_status(session, msg)
        if not session.recording_active:
            return
        await self._greet_now()

    async def _greet_now(self) -> None:
        """Speak the pending greeting plan, if there is one. One implementation for both triggers."""
        plan = self._greeting_plan()
        if plan is None:
            return
        kind, payload = plan
        # deliver → speak the result verbatim; greet → friendly inbound greeting.
        text = payload if kind == "deliver" else f"Hello{(' ' + payload) if payload else ''}, how can I help you?"
        self._processing = True  # half-duplex: hold the turn while we greet
        self._utterance_task = asyncio.create_task(self._speak_turn(text))

    async def _speak_turn(self, text: str) -> None:
        try:
            await self._speak(text)
        finally:
            self._processing = False

    async def on_assistant_say(self, session: CallSession, msg: protocol.AssistantSay) -> None:
        # H4: speak the worker-provided line (e.g. a goodbye before a limit cutoff) via the TTS path.
        text = (msg.text or "").strip()
        if not text:
            return
        self._processing = True  # half-duplex: hold the turn while we speak
        await self._speak_turn(text)

    async def speak_text(self, text: str) -> None:
        """Gateway delivery into this live call (phase 2b): speak the agent's
        reply through the TTS pipeline (markdown-stripped in _speak)."""
        text = (text or "").strip()
        if not text:
            return
        self._meeting.add("Assistant", text)
        self._processing = True
        await self._speak_turn(text)

    async def on_session_end(self, session: CallSession, msg: protocol.SessionEnd) -> None:
        await super().on_session_end(session, msg)
        if getattr(self, "_gateway_adapter", None) is not None:
            from .gateway_adapter import unregister_live_call

            unregister_live_call(self._thread_id, self)
        # Cancel an in-flight utterance job so we don't speak after hangup.
        if self._utterance_task is not None:
            self._utterance_task.cancel()
            self._utterance_task = None
        if self._bridge and self._bridge.meeting_recap and not self._meeting.is_empty():
            asyncio.create_task(
                meeting.post_minutes(self._consult, self._meeting, self._thread_id)
            )
        self._vision.clear()

    async def on_audio_frame(self, session: CallSession, msg: protocol.AudioFrame) -> None:
        if (self._require_recording and not session.recording_active) or self._processing:
            return
        if msg.speaker_name:  # unmixed-audio attribution for the turn + the minutes
            self._last_speaker = msg.speaker_name
        pcm = base64.b64decode(msg.payload_base64)
        utterance = self._buf.push(pcm, audio.pcm16_rms(pcm))
        if utterance is not None:
            self._processing = True
            self._utterance_task = asyncio.create_task(self._handle_utterance(utterance))

    async def on_video_frame(self, session: CallSession, msg: protocol.VideoFrame) -> None:
        if self._require_recording and not session.recording_active:
            return
        self._vision.store(
            StoredFrame(
                source=msg.source,
                data_base64=msg.data_base64,
                mime=msg.mime or "image/jpeg",
                ts=msg.ts,
                participant_name=msg.participant_name,
            )
        )

    async def _vision_context(self) -> str:
        """One-line description of the freshest shared frame to prepend to the turn.

        Auto-attach: only when there's a NEW frame since the last turn and the
        per-call vision budget allows; empty string otherwise (no agent change)."""
        frame = self._vision.latest()
        if frame is None or frame.ts == self._last_frame_ts:
            return ""
        # Auto-attach is ambient use — it must not starve an explicit look.
        if not self._vision_budget.try_consume_ambient():
            return ""
        from .hermes_api import vision_ask

        try:
            desc = await vision_ask(
                "In one short sentence, describe what the caller is sharing.",
                [{"type": "image", "url": frame.data_url()}],
                max_tokens=120,
            )
        except Exception:  # noqa: BLE001 — a raising vision facade must not spend the slot
            self._vision_budget.refund()
            logger.debug("[msteams_bridge] vision auto-attach failed", exc_info=True)
            return ""
        if not desc:  # facade missing or the call failed — give the budget back
            self._vision_budget.refund()
            return ""
        # Latch only AFTER a successful describe: latching first both spent the
        # budget and marked the frame as already seen, so it was never retried.
        self._last_frame_ts = frame.ts
        # §3.3: streaming already pays for a described scene — record it for
        # the minutes' Presented/Shown section too.
        self._meeting.add_visual(f"{frame.describe()}: {desc}")
        return f"[The caller is sharing their {frame.describe()}: {desc}]\n"

    async def _handle_utterance(self, pcm: bytes) -> None:
        try:
            transcript = await self._transcribe(pcm)
            if not transcript:
                return
            if verbal_interrupts.is_verbal_interrupt(transcript, self._gate_cfg.wake_phrases):
                return  # nothing playing in half-duplex; just don't reply
            # Capture ALL caller speech for the minutes — including unaddressed
            # meeting discussion — before the respond gate. Attributed to the
            # unmixed-audio speaker when the worker supplies one, so a meeting is
            # not filed entirely under the person who placed the call.
            self._meeting.add(self._last_speaker or self._first_name() or "Caller", transcript)
            # On-demand "summarize the meeting" → post minutes instead of a normal reply.
            if meeting.is_summary_request(transcript):
                await self._speak(await meeting.post_minutes(self._consult, self._meeting, self._thread_id))
                return
            now = time.monotonic() * 1000.0
            _is_group, decision = self._group_decision(transcript, now)
            if not decision.respond:
                return
            if decision.addressed:
                self._last_addressed_ms = now
            await self._safe_expression(expression.THINKING)
            # Auto-attach vision: prepend a fresh frame's description as context.
            vision_ctx = await self._vision_context()
            # Prefix the speaker so the agent knows WHO asked on a group call. The
            # gate and interrupt checks above run on the raw transcript, so the label
            # can never be mistaken for a wake phrase.
            attributed = f"{self._last_speaker}: {transcript}" if self._last_speaker else transcript
            await self._agent_turn(f"{vision_ctx}{attributed}" if vision_ctx else attributed)
        except Exception:  # noqa: BLE001 — never let a turn crash the call
            logger.error("[msteams_bridge] streaming turn failed", exc_info=True)
        finally:
            self._buf.reset()
            self._processing = False

    async def _agent_turn(self, text: str) -> None:
        """Run one caller turn through the agent and speak the reply.

        Shared by the transcribed-utterance path and the DTMF path so a keypress
        reaches the agent through exactly the same delivery route as speech.
        """
        adapter = getattr(self, "_gateway_adapter", None)
        if adapter is not None:
            # Phase 2b: a REAL gateway agent turn — sessions, authorization
            # and approvals apply; the reply returns via adapter.send()
            # -> speak_text. Fall back to the inline consult on failure.
            if await self._emit_gateway_turn(adapter, text):
                return
        reply = await self._consult.ask(text)
        self._meeting.add("Assistant", reply)
        await self._speak(reply)

    async def on_dtmf(self, session: CallSession, msg: protocol.Dtmf) -> None:
        """Surface a keypad press as a caller turn so "press 1 to…" flows work.

        Streaming defined no handler at all, so a keypress was decoded, dispatched
        and then dropped on the base class's no-op: the caller pressed a key and got
        nothing back. Same recording gate the realtime path applies (DTMF is in-band,
        media-derived caller input) and the same half-duplex busy guard the audio
        path uses.
        """
        if self._session is None or not self._recording_ok(session) or self._processing:
            return
        digit = (msg.digit or "").strip()
        if not digit:
            return
        self._processing = True
        self._utterance_task = asyncio.create_task(self._dtmf_turn(digit))

    async def _dtmf_turn(self, digit: str) -> None:
        text = f'The caller pressed the key "{digit}".'
        try:
            self._meeting.add(self._last_speaker or self._first_name() or "Caller", text)
            await self._safe_expression(expression.THINKING)
            await self._agent_turn(text)
        except Exception:  # noqa: BLE001 — never let a keypress crash the call
            logger.error("[msteams_bridge] streaming DTMF turn failed", exc_info=True)
        finally:
            # Drop whatever partial audio the VAD had buffered before the keypress,
            # so it is not stitched onto the next utterance.
            self._buf.reset()
            self._processing = False

    async def _emit_gateway_turn(self, adapter, text: str) -> bool:
        """Hand the utterance to the gateway agent loop as a MessageEvent."""
        try:
            from gateway.platforms.base import MessageEvent, MessageType

            caller = self._caller
            source = adapter.build_source(
                chat_id=self._thread_id or (self._session.call_id if self._session else "call"),
                chat_name="Teams voice call",
                chat_type="dm",
                user_id=(caller.aad_id if caller else None),
                user_name=(caller.display_name if caller else None),
            )
            await adapter.handle_message(MessageEvent(
                text=text, message_type=MessageType.TEXT, source=source,
            ))
            return True
        except Exception:  # noqa: BLE001 — degrade to the inline consult
            logger.error("[msteams_bridge] gateway turn failed; falling back inline", exc_info=True)
            return False

    async def _transcribe(self, pcm: bytes) -> str:
        from .hermes_api import hermes_home, transcribe
        from .streaming_audio import write_wav_pcm16

        d = hermes_home() / "cache" / "msteams_bridge"
        d.mkdir(parents=True, exist_ok=True)
        wav = d / f"utt_{uuid.uuid4().hex}.wav"
        try:
            await asyncio.to_thread(write_wav_pcm16, pcm, str(wav), PCM_SAMPLE_RATE_HZ)
            res = await asyncio.to_thread(transcribe, str(wav))
            return (res.get("transcript") or "").strip() if res.get("success") else ""
        finally:
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass

    async def _speak(self, text: str) -> None:
        from .speech_text import strip_for_speech

        # D8: agent replies are chat Markdown — strip syntax before ANY synth
        # path (timed providers and the generic fallback alike) so bullets and
        # asterisks are never read aloud.
        text = strip_for_speech(text)
        session = self._session
        if not text or session is None:
            return
        await self._safe_expression(expression.infer_emotion(text))
        synth = await self._synthesize(text)
        if synth is None:
            return
        pcm16k, marks_payload = synth
        if marks_payload:
            try:
                await session.send_speech_marks(marks_payload, ts=self._out_ts)
            except Exception:  # noqa: BLE001
                pass
        frames, _ = audio.frame_pcm16(pcm16k, BYTES_PER_FRAME)
        for frame in frames:
            try:
                await session.send_audio_frame(
                    self._out_seq, self._out_ts, base64.b64encode(frame).decode("ascii")
                )
            except Exception:  # noqa: BLE001
                return
            self._out_seq += 1
            self._out_ts += FRAME_DURATION_MS

    async def _synthesize(self, text: str) -> tuple[bytes, list[dict]] | None:
        """TTS → (PCM 16k, viseme marks). Supports every Hermes TTS provider:
        the fallback dispatches the host's ``text_to_speech`` tool, so the
        operator's ``tts.provider`` config (Edge/OpenAI/ElevenLabs/MiniMax/
        Mistral/Gemini/xAI/DeepInfra/NeuTTS/KittenTTS/Piper/custom command
        providers) applies unchanged. The viseme-*timing* upgrade is provider-
        agnostic too: any provider registered with :mod:`.timed_tts` supplies
        real per-character timing; all others get estimator visemes."""
        from . import timed_tts
        from .streaming_audio import decode_bytes_to_pcm16k, decode_to_pcm16k

        timed = await timed_tts.synth_with_timing(text)
        if timed:
            audio, timing = timed
            pcm16k = await asyncio.to_thread(decode_bytes_to_pcm16k, audio)
            if pcm16k:
                marks = viseme_estimate.visemes_from_alignment(timing)  # real timing
                return pcm16k, viseme_estimate.marks_to_payload(marks)

        from .hermes_api import hermes_home, text_to_speech

        d = hermes_home() / "cache" / "msteams_bridge"
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"tts_{uuid.uuid4().hex}.mp3"
        try:
            # Off-loop: dispatch bridges async handlers with its own loop.
            res = await asyncio.to_thread(text_to_speech, text, output_path=str(out))
            path = res.get("file_path") or str(out)
            pcm16k = await asyncio.to_thread(decode_to_pcm16k, path)
            if not pcm16k:
                return None
            dur_ms = (len(pcm16k) // 2) // PCM_SAMPLE_RATE_HZ_MS
            marks = viseme_estimate.estimate_visemes(text, dur_ms)
            return pcm16k, viseme_estimate.marks_to_payload(marks)
        finally:
            try:
                Path(out).unlink(missing_ok=True)
            except OSError:
                pass
