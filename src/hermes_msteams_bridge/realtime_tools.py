"""Realtime function-tool schemas exposed to the speech-to-speech model.

Realtime tools use a flat shape: ``{type, name, description, parameters}`` (not
the chat-completions ``{type:"function", function:{...}}`` nesting). The handler
dispatches calls to these by ``name``.
"""

from __future__ import annotations

HERMES_AGENT_CONSULT = {
    "type": "function",
    "name": "hermes_agent_consult",
    "description": (
        "Delegate to the Hermes agent to answer a question or perform an action — "
        "lookups, calculations, files, web, running tools, or using any of the "
        "installed Hermes skills. Use this for anything beyond small talk. "
        "Returns a short result to speak to the caller."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look into or do, phrased as a task.",
            }
        },
        "required": ["query"],
    },
}

LOOK_AT_SCREEN = {
    "type": "function",
    "name": "look_at_screen",
    "description": (
        "Look at what the caller is currently showing — their shared screen or "
        "camera — and answer a question about it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What to determine from the image."},
            "source": {
                "type": "string",
                "enum": ["screen", "camera"],
                "description": "Which feed to look at; defaults to the shared screen.",
            },
            "scope": {
                "type": "string",
                "enum": ["live", "history"],
                "description": (
                    "'live' = the current frame (default); 'history' = recent "
                    "keyframes, to answer about something shown earlier."
                ),
            },
        },
        "required": ["question"],
    },
}

SHOW_TO_CALLER = {
    "type": "function",
    "name": "show_to_caller",
    "description": (
        "Generate an image from a text prompt and display it on the bot's own video "
        "tile so the caller can see it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What image to create and show."},
            "count": {
                "type": "integer",
                "description": "How many images to show as a paced slideshow (1-3). Default 1.",
            },
        },
        "required": ["prompt"],
    },
}


SHOW_FILE = {
    "type": "function",
    "name": "show_file",
    "description": (
        "Display a real file from your workspace on your video tile so the "
        "caller can see it — an image, a PDF page, or an Office document page. "
        "Use when the caller should look at actual content rather than hear it "
        "described. Workspace-relative paths only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the workspace (e.g. 'reports/q3.pdf').",
            },
            "page": {
                "type": "integer",
                "description": "Page number for PDF/Office documents (1-based). Default 1.",
            },
        },
        "required": ["path"],
    },
}


SHOW_WEB_PAGE = {
    "type": "function",
    "name": "show_web_page",
    "description": (
        "Open a web page in the browser, screenshot it, and display it on your "
        "video tile — show the caller the actual page you are describing (docs, "
        "dashboards, settings screens). http(s) URLs only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to show."}
        },
        "required": ["url"],
    },
}


WALKTHROUGH = {
    "type": "function",
    "name": "walkthrough",
    "description": (
        "Guide the caller through a sequence of steps visually: for each step, "
        "display a workspace file (screenshot, PDF page) on your video tile and "
        "speak the accompanying explanation, advancing when you finish talking. "
        "Use for 'how do I…' walkthroughs where showing beats telling. Stops "
        "immediately if the caller interrupts. Maximum 6 steps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "Ordered walkthrough steps (1-6).",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative file to display for this step.",
                        },
                        "page": {
                            "type": "integer",
                            "description": "Page number for PDF/Office files (1-based).",
                        },
                        "say": {
                            "type": "string",
                            "description": "What to say while this step is on screen.",
                        },
                    },
                    "required": ["path", "say"],
                },
            }
        },
        "required": ["steps"],
    },
}


CALL_ME_BACK = {
    "type": "function",
    "name": "call_me_back",
    "description": (
        "Place an outbound Teams call back to the current caller to deliver a "
        "result. Use when work will take a while and the caller asked to be called "
        "back, or when ending the call but a result is still pending. The result is "
        "spoken once they answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The result/message to speak when they answer.",
            }
        },
        "required": ["message"],
    },
}


HERMES_AGENT_TASK = {
    "type": "function",
    "name": "hermes_agent_task",
    "description": (
        "Run a long-running job in the background (multi-step work, research, or "
        "skill-based work that takes more than a few seconds). Acknowledge to the caller that you're on it; "
        "the result is delivered by calling them back when it's done. Use this instead "
        "of hermes_agent_consult when the work won't finish within the conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The task to run in the background."}
        },
        "required": ["query"],
    },
}


SET_CALL_LANGUAGE = {
    "type": "function",
    "name": "set_call_language",
    "description": (
        "Pin the call to a specific language for the rest of the conversation "
        "(applies immediately). Use when the caller asks to continue in another "
        "language, e.g. 'let's speak French from now on'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "ISO 639-1 code, e.g. 'fr', 'de', 'ar', 'en'.",
            }
        },
        "required": ["language"],
    },
}


POST_MEETING_MINUTES = {
    "type": "function",
    "name": "post_meeting_minutes",
    "description": (
        "Summarize the meeting so far and post the minutes (key points, decisions, "
        "action items) to the Teams chat. Use when the caller asks to 'summarize the "
        "meeting' or send notes."
    ),
    "parameters": {"type": "object", "properties": {}},
}


POST_CHAT_MESSAGE = {
    "type": "function",
    "name": "post_chat_message",
    "description": (
        "Post a text message into the Teams chat for THIS call, while the call is still going. "
        "Use when the caller asks you to 'send that to the chat', 'post it', 'write it down', or "
        "'message me the link' - anything they want to keep after the call ends. The message appears "
        "from StandIn in the same Teams conversation. Say what you posted; do not read long text aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The message to post. Markdown is supported.",
            }
        },
        "required": ["text"],
    },
}


def default_tools() -> list[dict]:
    return [
        HERMES_AGENT_CONSULT,
        HERMES_AGENT_TASK,
        LOOK_AT_SCREEN,
        SHOW_TO_CALLER,
        SHOW_FILE,
        SHOW_WEB_PAGE,
        WALKTHROUGH,
        SET_CALL_LANGUAGE,
        CALL_ME_BACK,
        POST_MEETING_MINUTES,
        POST_CHAT_MESSAGE,
    ]
