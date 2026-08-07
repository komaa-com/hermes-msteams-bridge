""""Watch it work" — a rendered status panel for background tasks (§3.2 case a).

While ``hermes_agent_task`` runs, the tile shows an honest progress panel
(what's being worked on, elapsed time) as refreshed fullscreen ``display.image``
frames — no streaming transport, no receiver coupling (ADR-7). Rendering uses
Pillow (already pulled in by the ``documents`` extra); without it the feature
silently sits out and the caller just gets the spoken acknowledgement.
"""

from __future__ import annotations

import io
import logging
import textwrap

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 640, 360  # the tile's native render size


def render_panel(task_text: str, elapsed_s: float) -> bytes | None:
    """PNG of a simple dark status panel, or ``None`` when Pillow is absent."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        img = Image.new("RGB", (WIDTH, HEIGHT), (18, 20, 26))
        draw = ImageDraw.Draw(img)
        # Header bar + title
        draw.rectangle([0, 0, WIDTH, 54], fill=(30, 34, 44))
        draw.text((24, 18), "Working on it…", fill=(235, 238, 245))
        # Task text, wrapped
        y = 84
        for line in textwrap.wrap(task_text.strip() or "(background task)", width=58)[:6]:
            draw.text((24, y), line, fill=(200, 205, 215))
            y += 26
        # Elapsed + a simple activity bar that sweeps with time
        mins, secs = divmod(int(elapsed_s), 60)
        draw.text((24, HEIGHT - 64), f"elapsed {mins:02d}:{secs:02d}", fill=(150, 156, 168))
        bar_y = HEIGHT - 30
        draw.rectangle([24, bar_y, WIDTH - 24, bar_y + 8], fill=(40, 44, 56))
        span = WIDTH - 48 - 120
        x = 24 + int((elapsed_s * 60) % span)
        draw.rectangle([x, bar_y, x + 120, bar_y + 8], fill=(86, 156, 214))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — cosmetic; never break the task
        logger.debug("[teams_voice] progress panel render failed", exc_info=True)
        return None
