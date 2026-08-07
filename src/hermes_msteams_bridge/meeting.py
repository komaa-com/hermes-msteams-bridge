"""Meeting transcript accumulation + minutes prompt building.

The voice handlers append each caller/assistant turn to a :class:`MeetingTranscript`.
At call end (opt-in ``meeting_recap``) or on the ``post_meeting_minutes`` tool, the
agent summarizes the transcript into minutes which are posted to the Teams chat via
the host's ``send_message`` tool (live adapter first, then the platform's registered
standalone sender). A Word-openable ``.docx`` is also generated and kept as a local
artifact under the Hermes workspace.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MeetingTranscript:
    """Ordered (speaker, text) turns + visual observations for the minutes.

    The audio track records who *said* what; the visual track records who
    *showed* what (screen shares, camera scenes) — the one thing a
    transcript-first post-meeting pipeline structurally cannot see.
    """

    turns: list[tuple[str, str]] = field(default_factory=list)
    visuals: list[str] = field(default_factory=list)

    def add(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.turns.append((speaker or "Caller", text))

    def add_visual(self, what: str) -> None:
        """Record a visual observation (e.g. "Sara's shared screen: the Q3
        dashboard"). Deduplicates consecutive repeats (scene-change semantics)."""
        what = (what or "").strip()
        if what and (not self.visuals or self.visuals[-1] != what):
            self.visuals.append(what)

    def is_empty(self) -> bool:
        return not self.turns and not self.visuals

    def render(self, max_chars: int = 12_000) -> str:
        body = "\n".join(f"{sp}: {tx}" for sp, tx in self.turns)
        if self.visuals:
            shown = "\n".join(f"- {v}" for v in self.visuals[-30:])
            body += f"\n\n[Shared on screen during the call]\n{shown}"
        # Keep the tail if very long (recent context matters most for minutes).
        return body[-max_chars:] if len(body) > max_chars else body


def is_summary_request(text: str) -> bool:
    """True if the caller asked to summarize / send minutes of the meeting."""
    t = (text or "").lower()
    summary = any(w in t for w in ("summarize", "summarise", "minutes", "recap", "notes"))
    subject = any(w in t for w in ("meeting", "call", "conversation", "discussion"))
    return summary and subject


def summarize_prompt(transcript: str) -> str:
    """Prompt the agent to produce minutes text only (no posting)."""
    return (
        "Summarize the transcript of this Microsoft Teams meeting into concise "
        "minutes with these sections — **Key Points**, **Decisions**, **Action "
        "Items** (name owners where stated), and, when the transcript includes a "
        "[Shared on screen during the call] block, **Presented / Shown** — list only "
        "what that block states (sources shared, and content details only where "
        "given); never infer what was on screen. Output only the minutes, briefly and "
        f"factually.\n\nTranscript:\n{transcript}"
    )


async def _deliver_to_teams(conversation_id: str, text: str) -> bool:
    """Post text to a Teams conversation via the host's ``send_message`` tool
    (works without the gateway running: the tool falls back to the platform's
    registered standalone sender; reads bot creds from env)."""
    from .hermes_api import send_teams_message

    result = await send_teams_message(conversation_id, text)
    if not result.get("success"):
        logger.warning(
            "[teams_voice] minutes delivery to %s failed: %s",
            conversation_id, result.get("error"),
        )
    return bool(result.get("success"))


def _save_docx_artifact(minutes: str) -> str | None:
    """Write a Word-openable minutes .docx under the Hermes workspace; return the path.

    Best-effort local artifact — delivery to the chat is text (see
    :func:`post_minutes`); the document stays on disk for the operator."""
    try:
        from .hermes_api import hermes_home
        from .meeting_docx import write_minutes_docx

        d = hermes_home() / "workspace" / "teams_minutes"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"minutes_{uuid.uuid4().hex[:8]}.docx"
        write_minutes_docx("Meeting minutes", minutes, str(path))
        logger.info("[teams_voice] minutes document saved: %s", path)
        return str(path)
    except Exception:  # noqa: BLE001 — artifact is optional
        logger.warning("[teams_voice] minutes .docx generation failed", exc_info=True)
        return None


async def _deliver_file_card(conversation_id: str, docx_path: str, caption: str) -> bool:
    from pathlib import Path as _Path

    from .config import resolve_config
    from .hermes_api import send_teams_file

    site_id = resolve_config().share_point_site_id  # optional (future Graph path)
    try:
        content = _Path(docx_path).read_bytes()
    except OSError:
        return False
    result = await send_teams_file(
        conversation_id, content, "Meeting minutes.docx", caption=caption, site_id=site_id
    )
    return bool(result.get("success"))


async def post_minutes(
    consult, transcript: "MeetingTranscript", conversation_id: str, *, deliver=None
) -> str:
    """Summarize the transcript via the agent, then post the minutes to Teams.

    ``deliver`` is an injectable ``async (conversation_id, text) -> bool`` (defaults
    to the ``send_message``-tool sender) — decouples voice from the host delivery
    path and keeps this unit-testable. Returns a short spoken-result string.
    """
    if transcript.is_empty() or not conversation_id:
        return "There wasn't enough of a conversation to summarize."
    try:
        minutes = await consult.ask(summarize_prompt(transcript.render()), timeout_s=120.0)
    except Exception:  # noqa: BLE001 — recap must never crash teardown
        logger.error("[teams_voice] meeting summary failed", exc_info=True)
        return "I couldn't summarize the meeting."
    minutes = (minutes or "").strip()
    if not minutes:
        return "I couldn't summarize the meeting."
    body = f"📝 **Meeting minutes**\n\n{minutes}"
    docx_path = _save_docx_artifact(minutes)  # Word-openable local artifact
    # File card (share_point_site_id): attempted when the host exposes a
    # sender; degrades to text until then (hermes_api.file_sender_available).
    if docx_path and deliver is None:
        if await _deliver_file_card(conversation_id, docx_path, body):
            return "I've posted the minutes, with the Word document, to your Teams chat."
    deliver = deliver or _deliver_to_teams
    ok = await deliver(conversation_id, body)
    return (
        "I've posted the minutes to your Teams chat."
        if ok
        else "I summarized the meeting but couldn't post it to the chat."
    )
