"""Meeting transcript accumulation + minutes prompt building.

The voice handlers append each caller/assistant turn to a :class:`MeetingTranscript`.
At call end (opt-in ``meeting_recap``) or on the ``post_meeting_minutes`` tool, the
agent summarizes the transcript into minutes which are posted to the Teams chat via
the adapter's standalone Bot Framework sender. A Word-openable ``.docx`` is also
generated; when ``sharePointSiteId`` is configured it is uploaded to SharePoint
(OneDrive) and attached to the chat as a native file card, otherwise it posts as text.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class MeetingTranscript:
    """Ordered (speaker, text) turns for end-of-call minutes."""

    turns: list[tuple[str, str]] = field(default_factory=list)

    def add(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.turns.append((speaker or "Caller", text))

    def is_empty(self) -> bool:
        return not self.turns

    def render(self, max_chars: int = 12_000) -> str:
        body = "\n".join(f"{sp}: {tx}" for sp, tx in self.turns)
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
        "minutes with three sections — **Key Points**, **Decisions**, and **Action "
        "Items** (name owners where stated). Output only the minutes, briefly and "
        f"factually.\n\nTranscript:\n{transcript}"
    )


async def _deliver_to_teams(conversation_id: str, text: str) -> bool:
    """Post text to a Teams conversation via the adapter's standalone REST sender
    (works without the gateway running; reads bot creds from env)."""
    try:
        from plugins.platforms.teams.adapter import _standalone_send
    except Exception:  # noqa: BLE001 — teams adapter unavailable
        return False
    pconfig = type("_PConfig", (), {"extra": {}})()
    try:
        result = await _standalone_send(pconfig, chat_id=conversation_id, message=text)
        if not result.get("success"):
            logger.warning(
                "[teams_voice] minutes delivery to %s failed: %s",
                conversation_id, result.get("error"),
            )
        return bool(result.get("success"))
    except Exception:  # noqa: BLE001
        logger.error("[teams_voice] recap delivery failed", exc_info=True)
        return False


def _save_docx_artifact(minutes: str) -> str | None:
    """Write a Word-openable minutes .docx under the Hermes workspace; return the path.

    Best-effort: the file is uploaded + attached to the chat when SharePoint is
    configured (see :func:`_deliver_file_to_teams`), and kept as a local artifact."""
    try:
        from hermes_constants import get_hermes_home

        from .meeting_docx import write_minutes_docx

        d = Path(get_hermes_home()) / "workspace" / "teams_minutes"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"minutes_{uuid.uuid4().hex[:8]}.docx"
        write_minutes_docx("Meeting minutes", minutes, str(path))
        logger.info("[teams_voice] minutes document saved: %s", path)
        return str(path)
    except Exception:  # noqa: BLE001 — artifact is optional
        logger.warning("[teams_voice] minutes .docx generation failed", exc_info=True)
        return None


async def _deliver_file_to_teams(conversation_id: str, file_path: str, display_name: str, *, caption: str = "") -> bool:
    """Upload a file to SharePoint and post it to the Teams chat as a file card.

    Returns False (caller degrades to text) when SharePoint isn't configured or the
    upload fails. The Word document content type keeps Teams' Open-in-Word working."""
    try:
        from plugins.platforms.teams.adapter import _standalone_send_file
    except Exception:  # noqa: BLE001 — teams adapter unavailable
        return False
    try:
        content = Path(file_path).read_bytes()
    except OSError:
        return False
    # Pass the resolved SharePoint site id (config.yaml or env) so the standalone
    # sender can upload even though its own pconfig has no platform extra.
    extra: dict = {}
    try:
        from .config import resolve_config

        site_id = resolve_config().share_point_site_id
        if site_id:
            extra["share_point_site_id"] = site_id
    except Exception:  # noqa: BLE001
        pass
    pconfig = type("_PConfig", (), {"extra": extra})()
    docx_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    try:
        result = await _standalone_send_file(
            pconfig, chat_id=conversation_id, content=content,
            filename=display_name, content_type=docx_ct, caption=caption,
        )
        return bool(result.get("success"))
    except Exception:  # noqa: BLE001
        logger.error("[teams_voice] minutes file delivery failed", exc_info=True)
        return False


async def post_minutes(
    consult, transcript: "MeetingTranscript", conversation_id: str, *, deliver=None
) -> str:
    """Summarize the transcript via the agent, then post the minutes to Teams.

    ``deliver`` is an injectable ``async (conversation_id, text) -> bool`` (defaults
    to the Teams standalone sender) — decouples voice from the chat adapter and
    keeps this unit-testable. Returns a short spoken-result string.
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
    docx_path = _save_docx_artifact(minutes)  # Word-openable artifact
    # When SharePoint file-send is configured, attach the Word document to the chat;
    # otherwise (or for an injected test deliver) post the minutes as text.
    if docx_path and deliver is None:
        if await _deliver_file_to_teams(conversation_id, docx_path, "Meeting minutes.docx", caption=body):
            return "I've posted the minutes (with the Word document) to your Teams chat."
    deliver = deliver or _deliver_to_teams
    ok = await deliver(conversation_id, body)
    return (
        "I've posted the minutes to your Teams chat."
        if ok
        else "I summarized the meeting but couldn't post it to the chat."
    )
