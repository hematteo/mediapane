# Changelog

## mediapane 0.2.0: first release

The pane
- A scrollable gallery of images, GIFs and videos, drawn with kitty graphics Unicode
  placeholders: it scrolls, survives tmux, and animates by swapping image data.
- Series: pushes with a `tag` become one entry with a timeline; step with ← →, time-lapse with `t`.
- Large view with zoom and pan; mouse, keyboard and bracketed-paste-safe input.
- Only visible items animate or play; images that scroll away are released from the terminal.
- Videos play once through ffmpeg, frames handed over by shared memory where the terminal
  supports it; GIFs are stored in the terminal and looped by it over SSH where supported.
- History: `mediapane pane --resume` brings back the last session's items.
- The same file pushed twice (say, by a hook and by an agent) is one item.

Sending media
- `mp FILE…`, stdin, `mediapane watch DIR` (with `--series`), `mediapane.show()` for files,
  PIL images, matplotlib figures, NumPy arrays and torch tensors (batches tile into grids, frame
  stacks animate), and a matplotlib backend.
- `mediapane ssh HOST`: pushes on a remote host land in your local pane, over a forwarded socket.
- `MEDIAPANE_SPOOL`: cluster jobs that can't reach the pane leave pushes in a shared folder for
  `mediapane relay`.
- `mediapane peek FILE`: a small copy (or a 2×2 frame sheet of a video) for AI agents to look at,
  at a fraction of the tokens.
- The wire format is one JSON line plus the bytes; `nc -U` is enough.

Extensibility
- Plugins: installed packages (entry point group `mediapane.plugins`) or personal `.py` files in
  `~/.config/mediapane/plugins/`, adding commands, item hooks and keys. First-party plugins:
  `mediapane-claude`, `mediapane-diff`, `mediapane-relay`.

Robustness and security
- Pushers are untrusted: control characters are stripped from names before they reach the
  terminal, malformed or damaged pushes are rejected without affecting the pane, ffmpeg only
  reads plain local video containers, and forwarded and spooled files are only used if they are
  yours.
- Sockets, sessions and history are private to you; on shared homes they are per host.

## mediapane-claude 0.1.0
- Claude Code plugin (`/plugin install mediapane --marketplace hematteo/mediapane`) or
  `mediapane claude install`: a PostToolUse hook that shows media Claude writes, reads or makes,
  and a skill that has Claude show media with `mp` and look at images through `mediapane peek`.
  The hook adds nothing to Claude's context.
- `mediapane mcp`: a dependency-free MCP server with a `show_media` tool, for agents without a
  shell.

## mediapane-diff 0.1.0
- `mediapane diff A B`: side by side, flicker and a difference heatmap as one series; works as a
  `git difftool`.

## mediapane-relay 0.1.0
- `mediapane relay`: forwards pushes spooled by cluster jobs to a reachable pane, each exactly
  once, only files you own.
