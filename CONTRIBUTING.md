# Contributing

```sh
uv sync && uv run pytest -q          # the core, including the pane end to end in a pty
cd plugins/mediapane-diff && uv sync && uv run pytest -q    # each plugin, against this core
uv run ruff format . && uv run ruff check .
```

`ffmpeg` on your PATH enables the video tests. CI runs everything on Linux and macOS.

## Layout

| Path | |
|---|---|
| `src/mediapane/pane/` | the pane: `app.py` owns the state and event loop; behaviour lives in mixins (`server`, `items`, `draw`, `play`, `history`, `input`), with `video.py` for ffmpeg and frame transport |
| `src/mediapane/kitty.py` | the graphics protocol: placeholders, transfers, tmux passthrough |
| `src/mediapane/ipc.py` | the socket protocol, socket discovery, the spool |
| `src/mediapane/cli.py` | commands; `plugins.py` the plugin API |
| `plugins/` | first-party plugins, each its own package |
| `.claude-plugin/` | the Claude Code marketplace (the plugin itself is in `plugins/mediapane-claude/claude-code/`) |

## Guidelines

- The core depends on Pillow only. Anything that needs more, or that only some people want, is
  a plugin.
- Keep the pane's event loop non-blocking: decoding and scaling belong on the worker pool.
- Treat everything a pusher sends as untrusted (see SECURITY.md).
- Output meant for agents (`mp`, `peek`, the hook) stays quiet: nothing on success, one line on
  error.
- Tests: the pane has end-to-end tests in a pseudo-terminal (`tests/test_pane.py`); new pane
  behaviour should come with one.

## Releasing

Bump `__version__` in the package (it's the single source), add a CHANGELOG entry, then push a
tag: `v0.2.1` for the core, `mediapane-claude-v0.1.1` (etc.) for a plugin. The release workflow
tests, builds and publishes it to PyPI. Keep `plugins/mediapane-claude/claude-code/.claude-plugin/
plugin.json`'s version in step with `mediapane-claude`.
