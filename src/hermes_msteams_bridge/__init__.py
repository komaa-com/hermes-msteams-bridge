"""teams_call plugin — Microsoft Teams real-time voice/video (CVI) bridge driver.

Hosts an HMAC-authenticated WebSocket the StandIn media bridge
dials into, and drives the call: dialogue (realtime
or streaming), perception (camera/screen vision), and the avatar rendering cues
(expression / visemes / show-to-caller). The StandIn media bridge renders the avatar tile;
this plugin sends the drivers.

Chat-plane integration (Teams messages, message actions, meeting-recap posting)
is handled by the existing ``plugins/platforms/teams`` adapter — this plugin is
the *media/voice* half and deliberately does not duplicate it.

Status: implemented. Realtime (OpenAI/Azure speech-to-speech) and streaming
(STT->agent->TTS) call modes; vision, tools (consult/agent_task/look_at_screen/
show_to_caller/call_me_back/post_meeting_minutes), group gate, verbal interrupts,
DTMF, bilingual, meeting recap. The StandIn media bridge renders the avatar.
"""

from __future__ import annotations

import logging

from .cli import register_cli as _register_cli
from .cli import teams_call_command as _teams_call_command
from .tools import (
    CALL_USER_SCHEMA,
    TEAMS_CALL_STATUS_SCHEMA,
    check_requirements,
    handle_call_user,
    handle_teams_call_status,
)

logger = logging.getLogger(__name__)


def _on_session_end(**_kwargs) -> None:
    """Best-effort hook placeholder.

    The bridge runs as its own server process, so there is nothing call-scoped to
    tear down on agent-session end today. Kept registered so the lifecycle wiring
    is stable as the realtime brain lands.
    """
    return None


def register(ctx) -> None:
    """Plugin entry point — register the status tool, CLI, and lifecycle hook.

    Called once by the plugin loader when ``teams_call`` is enabled via
    ``plugins.enabled`` in config.yaml. The context is captured so the rest of
    the package reaches Hermes only through its public services (``ctx.llm``,
    ``ctx.dispatch_tool``) via the :mod:`.hermes_api` boundary.
    """
    from . import hermes_api

    hermes_api.set_plugin_context(ctx)

    ctx.register_tool(
        name="teams_call_status",
        toolset="teams_call",
        schema=TEAMS_CALL_STATUS_SCHEMA,
        handler=handle_teams_call_status,
        check_fn=check_requirements,
        emoji="📞",
    )

    # Chat-to-call (§3.6): reachable from any Hermes chat surface (the Teams
    # adapter included) with zero new ingress — the agent calls the tool, the
    # plugin places the call, the message is spoken on answer.
    ctx.register_tool(
        name="call_user",
        toolset="teams_call",
        schema=CALL_USER_SCHEMA,
        handler=handle_call_user,
        check_fn=check_requirements,
        emoji="📲",
    )

    ctx.register_cli_command(
        name="teams-call",
        help="Microsoft Teams voice/video (CVI) bridge (serve, status)",
        setup_fn=_register_cli,
        handler_fn=_teams_call_command,
        description=(
            "Run the HMAC-authenticated bridge the StandIn media bridge "
            "connects to. See: hermes teams-call status"
        ),
    )


    ctx.register_hook("on_session_end", _on_session_end)

    # Phase 2b: gateway platform registration (spike verdict GO). The gateway
    # hosts the voice bridge when the platform is enabled; `hermes teams-call
    # serve` remains the standalone fallback. Best-effort on older hosts.
    from .gateway_adapter import register_platform as _register_platform

    _register_platform(ctx)
