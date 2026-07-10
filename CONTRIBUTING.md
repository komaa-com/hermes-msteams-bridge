# Contributing to hermes-msteams-bridge

Thanks for helping improve the Microsoft Teams voice/video plugin for Hermes!
This guide covers local setup, conventions, and how a change gets from your branch
to PyPI. For the architecture, read [DESIGN.md](DESIGN.md) first.

## Ground rules

- Be friendly and constructive in issues and reviews.
- Keep changes focused; one logical change per PR.
- **Leak policy:** do not document the hosted StandIn media bridge's internal
  implementation - this repo describes only the wire protocol the plugin speaks
  with it. (See DESIGN.md.)

## Dev setup

You need **Python ≥ 3.10**. A virtual environment is strongly recommended.

```bash
git clone https://github.com/komaa-com/hermes-msteams-bridge.git
cd hermes-msteams-bridge

python -m venv .venv && source .venv/bin/activate   # or your preferred venv
uv pip install -e .                                 # editable install
uv pip install -e ".[numpy]"                        # optional: faster audio resampling
```

> `uv` is used throughout, but plain `pip install -e .` works identically.

Some runtime features reach into a live Hermes install (the agent, TTS/STT tools,
config loader). Those are only importable when the plugin is installed **into a
Hermes environment**; the unit tests below do not require Hermes and run standalone.

### Tests

Tests use `pytest`:

```bash
uv pip install pytest
pytest hermes_teams_voice/tests/ -v
```

The suite covers the wire protocol, HMAC handshake + replay guard, the echo guard,
the group-call gate, verbal interrupts, viseme estimation, vision budget, and the
audio helpers - all with no network and no Hermes dependency. Please add or update
tests alongside any behavior change.

### Optional: running the bridge locally

```bash
export TEAMS_VOICE_SHARED_SECRET=dev-secret
hermes teams-voice serve --handler echo    # or: logging | realtime | streaming
```

`echo` and `logging` need no provider key and are the quickest way to exercise the
handshake and lifecycle. `realtime` needs an OpenAI/Azure realtime key; `streaming`
needs `ffmpeg` on PATH. `hermes teams-voice status` prints resolved config and
readiness.

## Branch + PR conventions

- Branch off `main`. Use a short, descriptive prefix:
  `feat/…`, `fix/…`, `docs/…`, `chore/…`.
- Keep commits clean and messages imperative ("Add …", "Fix …").
- Open a PR against `main` with a clear description of the change and how you
  verified it. CI runs the test suite on pull requests.
- Do not commit secrets. The plugin ships **no** config; secrets live in the
  Hermes `.env`.

## How the plugin is discovered (entry-point mechanism)

Hermes finds pip-installed plugins through the `hermes_agent.plugins` entry-point
group declared in `pyproject.toml`:

```toml
[project.entry-points."hermes_agent.plugins"]
teams_voice = "hermes_teams_voice"
```

The **key** (`teams_voice`) is the plugin name shown in `hermes plugins list` and
used in `plugins.enabled`. The **value** (`hermes_teams_voice`) is the import
package whose `register(ctx)` Hermes calls once, when the plugin is enabled.
`register(ctx)` wires the `teams_voice_status` tool, the `teams-voice` CLI command,
and the `on_session_end` hook.

> Entry-point plugins are **opt-in**: `hermes plugins enable` does **not** work for
> pip-installed plugins. Enable it by adding `teams_voice` to `plugins.enabled` in
> `~/.hermes/config.yaml`.

Keep `plugin.yaml`'s `version` in sync with `pyproject.toml`'s `version`.

## Release / publish to PyPI

Releases go to PyPI as `hermes-msteams-bridge`.

1. Bump the version in **both** `pyproject.toml` and `hermes_teams_voice/plugin.yaml`.
2. Update the README/wiki if user-facing behavior changed.
3. Tag the release and let CI build + publish, or build locally:

   ```bash
   python -m build
   python -m twine upload dist/*
   ```

4. Verify the new version installs cleanly into a fresh Hermes venv.

## Questions

Open an issue on
[GitHub](https://github.com/komaa-com/hermes-msteams-bridge/issues), or read
the [documentation site](https://komaa-com.github.io/hermes-msteams-bridge/).
