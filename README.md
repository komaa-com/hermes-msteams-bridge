# hermes-plugin-teams-voice

Microsoft Teams **voice/video (Conversational Video Interface)** for **Hermes Agent**,
packaged as a standalone, pip-installable plugin — install it *on top of* a normal
Hermes install, no fork required.

The plugin (name **`teams_voice`**) hosts the HMAC-authenticated WebSocket bridge that
the companion Windows/.NET media worker dials into, and drives the call: realtime
(OpenAI/Azure speech-to-speech) **or** streaming (STT→agent→TTS), camera/screen vision,
the avatar driver cues (expression / visemes / show-to-caller), group-call etiquette,
DTMF, bilingual EN/AR, meeting recap/minutes, and SharePoint (OneDrive) file send.

## Install on Hermes

The package must go into the **same Python environment as Hermes** (Hermes discovers
it via the `hermes_agent.plugins` entry-point, then imports it in-process). Pick one:

**A — from GitHub (recommended while not on PyPI):**

```bash
# into the Hermes venv (uv example; use the python that runs `hermes`)
uv pip install --python /path/to/hermes/venv/Scripts/python.exe \
  "git+https://github.com/alaamh/hermes-plugin-teams-voice.git"
```

**B — from a local checkout:**

```bash
git clone https://github.com/alaamh/hermes-plugin-teams-voice.git
uv pip install --python /path/to/hermes/venv/Scripts/python.exe ./hermes-plugin-teams-voice
```

**C — from PyPI (once published):**

```bash
uv pip install hermes-plugin-teams-voice          # or: pip install hermes-plugin-teams-voice
```

> Finding the Hermes venv: it's the interpreter behind the `hermes` launcher (on this
> setup, `…\hermes\venv\Scripts\python.exe`). Installing into the wrong env means Hermes
> won't see the plugin.
> Optional faster audio path: append `[numpy]` to any of the specs above.

### Enable + run

```bash
hermes plugins list                       # confirm: teams_voice   (source: entrypoint)
hermes plugins enable teams_voice         # entry-point plugins are opt-in
hermes teams-voice serve --handler realtime   # voice bridge; also: streaming | echo | logging
hermes gateway run                        # (separately) the Teams chat plane + cron
```

### Configure

Config lives in Hermes's own files (not in this package):

- `~/.hermes/config.yaml` → `plugins.entries.teams_voice.config` (host/port, `realtime:` block, `allowlist`, `meeting_recap`, `share_point_site_id`, …)
- `~/.hermes/.env` → secrets (`TEAMS_VOICE_SHARED_SECRET`, `AZURE_FOUNDRY_API_KEY`, `TEAMS_SHAREPOINT_SITE_ID`, …)

`shared_secret` **must equal** the companion media worker's shared secret. Full
reference: [`hermes_teams_voice/README.md`](hermes_teams_voice/README.md).

### Upgrade / uninstall

```bash
uv pip install --upgrade "git+https://github.com/alaamh/hermes-plugin-teams-voice.git"
uv pip uninstall hermes-plugin-teams-voice    # then it disappears from `hermes plugins list`
```

## How it loads

Hermes discovers pip plugins via the `hermes_agent.plugins` entry-point group. This
package exposes:

```toml
[project.entry-points."hermes_agent.plugins"]
teams_voice = "hermes_teams_voice"
```

Hermes imports `hermes_teams_voice` and calls its `register(ctx)` — registering the
`teams-voice` CLI, the status tool, and the session hook. Entry-point plugins are
opt-in, so add `teams_voice` to `plugins.enabled` (the `hermes plugins enable` command
does this).

## Configuration

Config lives in Hermes's `config.yaml` (`plugins.entries.teams_voice.config`) and `.env`
— identical to the bundled plugin. Key settings: `shared_secret` (must equal the worker's
shared-secret), `host`/`port` (voice WS), the `realtime:` block (OpenAI/Azure), the
caller `allowlist`, `meeting_recap`, and `TEAMS_SHAREPOINT_SITE_ID` for file/minutes
attachment. See `hermes_teams_voice/README.md` for the full reference.

## Relationship to the bundled plugin

This is the same code as the in-tree `plugins/teams_voice` plugin, repackaged for pip
distribution so you don't have to fork Hermes. Install it on **vanilla** Hermes; do not
also keep a bundled `teams_voice` (same name → the entry-point would shadow it).

- **Voice/CVI** works fully on vanilla Hermes.
- **Chat-plane governance + SharePoint file attach** depend on the enhanced
  `plugins/platforms/teams` adapter; when that isn't present the plugin **degrades
  gracefully** (e.g. meeting minutes post as text instead of a SharePoint file card).

## Requirements

- A working **Hermes Agent** install (the host; not a PyPI package).
- The companion **Windows/.NET media worker** for live Teams call media.
- Python ≥ 3.10, `aiohttp`. `ffmpeg` on PATH for streaming-mode TTS decode.

## License

MIT (matches Hermes Agent). Contributed by the team at PCFC — Dubai, UAE.
