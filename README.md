# mediapane

[![CI](https://github.com/hematteo/mediapane/actions/workflows/ci.yml/badge.svg)](https://github.com/hematteo/mediapane/actions/workflows/ci.yml)

**A media sidecar for your terminal.** Keep one pane open; anything can push images, GIFs and
videos to it: your shell, scripts, a training job on a cluster, a REPL, an AI agent.

<!-- demo.gif: a remote training run in tmux pushing sample grids that step and time-lapse in
     place, an agent dropping a screenshot, a rollout video playing. To be recorded. -->

```sh
mediapane open                 # pane in a new split (tmux, Ghostty, kitty, WezTerm)
mp plot.png rollout.mp4        # push from any shell
mediapane watch outputs/       # push new files as they appear
mediapane ssh gpu-box          # then `mp` on gpu-box lands in your local pane
```

```python
import mediapane
mediapane.show(fig)                              # matplotlib figure
mediapane.show(batch)                            # (N, C, H, W) tensor or array -> grid
mediapane.show(frames, fps=12)                   # (T, H, W[, C]) -> animation
mediapane.show(samples, tag="samples", step=1000)   # a series: step through it with ← →
```

Other terminal viewers (`timg`, `icat`, `viu`, `chafa`) print an image and exit. mediapane is
a place things *arrive*. The pane is a scrollable gallery that keeps what you've sent, plays
GIFs and videos in place as you scroll, and doesn't care which process or machine sent it.

## Install

```sh
uv tool install mediapane            # or: pipx install mediapane / pip install mediapane
uv tool install 'mediapane[matplotlib]'   # adds the matplotlib backend and array support
```

The core needs only Pillow (and `ffmpeg` for video). Everything else is a
[plugin](#plugins) you add if you want it. Run `mediapane doctor` to check your terminal, tmux
and SSH setup; it prints the fix for anything missing.

## Sending things

| From | How |
|---|---|
| A shell | `mp a.png b.gif c.mp4`, `curl -s …/img.png \| mp`, `git show HEAD~3:fig.png \| mp` |
| A directory | `mediapane watch runs/ -r` pushes each new or changed image/video once it's fully written |
| Python | `mediapane.show(obj)`: paths, bytes, PIL images, matplotlib figures, NumPy arrays, torch tensors |
| matplotlib | `MPLBACKEND=mediapane python train.py`: `plt.show()` sends figures to the pane |
| A server | `mediapane ssh host` from your laptop, then push on the host as usual |
| A cluster job | set `MEDIAPANE_SPOOL` on a shared filesystem; see [Clusters](#clusters) |
| An agent | `mp` from its shell; for Claude Code, the [plugin](#claude-code-and-other-agents) does it for you |
| Anything else | One JSON header line and the bytes, on a Unix socket (see below) |

`mediapane.show` turns arrays into images: `(H, W)`, `(H, W, C)` and `(C, H, W)` are single
images; `(N, C, H, W)`, `(N, H, W, C)` and `(N, H, W)` batches are tiled into a grid; with `fps=`
a `(T, ...)` stack becomes an animation. Floats in `[0, 1]` or `[-1, 1]` map to the full range,
and anything else is stretched from its minimum to its maximum. If no pane is running, `show`
draws the image inline at the cursor instead.

### Series

Give pushes a `tag` and they become one gallery entry instead of many: a series, with a timeline
of its steps. Step through it with `←` `→`, jump to the first or last step with `<` `>`, or press
`t` to play it as a time-lapse. Sample grids over training, attention maps per layer, a figure
across commits:

```python
for step in range(0, 100_000, 1000):
    ...
    mediapane.show(model.sample(16), tag="samples", step=step)
```

```sh
mp --tag loss --step 3000 loss.png
mediapane watch runs/ --series        # samples_001000.png, samples_002000.png -> one series
```

New steps follow along while you're on the newest; step back and the series stays where you are.

## Claude Code and other agents

```
/plugin install mediapane --marketplace hematteo/mediapane
```

plus the CLI it calls, `uv tool install mediapane --with mediapane-claude`. (Or, without the
plugin system: `mediapane claude install`.) Then:

- **Media Claude writes, reads or makes appear in your pane**, from a PostToolUse hook. The hook
  prints nothing, so it adds nothing to Claude's context.
- **A skill teaches Claude to show rather than read.** Claude sees images by the pixel (a typical
  plot costs about 1,800 tokens to read); showing you one with `mp` costs a few. When Claude does
  need to look, `mediapane peek fig.png` hands it a copy at most 768 px wide (a video becomes a
  2×2 sheet of frames): about a quarter of the tokens. Only the skill's one-line description
  sits in Claude's context.
- The same image arriving twice (the hook, then Claude's own `mp`) shows once.

Any agent with a shell can do the same with `mp` and `mediapane peek`. For agents without one,
`mediapane mcp` is an MCP server with a `show_media` tool
(`claude mcp add mediapane -- mediapane mcp`).

## The pane

| Key | |
|---|---|
| `j` `k`, arrows, wheel | scroll |
| `n` `p`, tab | next / previous item |
| `1`-`9`, `g` `G` | jump to an item, the top, the bottom (`G` also follows new items) |
| `←` `→` `,` `.` `<` `>`, `t` | step through a series (first / last), time-lapse |
| enter, double-click | view one item large; esc to go back |
| `+` `-` `0`, arrows / `hjkl` | zoom in the large view, pan, reset |
| space, `r` | play / pause, replay |
| `o` | open in the default app (local files) |
| `x`, `C` | remove the item, clear everything |
| `?`, `q` | help (lists plugin keys too), quit |

New items scroll into view while you're at the bottom; scroll up and the pane stays put. The
same file pushed again selects the existing item instead of adding a copy. Only
items on screen animate or play, and images that scroll away are released from the terminal,
so a long session costs the terminal only what's visible. On quit the pane keeps its items
(up to `MEDIAPANE_HISTORY_MB`, default 1024; 0 turns it off): `mediapane pane --resume` brings
them back.

## Remote machines

`mediapane ssh` is plain `ssh` plus a forwarded Unix socket
(`/tmp/mediapane-<user>-<random>.sock` on the host, readable only by you). Every `mediapane push`
or `mediapane.show()` on that host finds it and lands in your local pane, from a shell, a tmux
session or a job you started there. Install mediapane on the host too
(`uv tool install mediapane`).

You can also run the pane itself on the server, inside tmux, and view it through SSH. Images then
travel inline through the connection (`MEDIAPANE_TRANSFER=direct`, chosen automatically over
SSH). In terminals that can play animations themselves, a GIF is sent once and the terminal
loops it, rather than mediapane streaming its frames.

### Clusters

Batch jobs on compute nodes usually can't reach the login node's socket. Point them at a folder
both can see, and run a relay where the pane is reachable:

```sh
export MEDIAPANE_SPOOL=~/.mediapane-spool     # in the job: pushes land here when no pane answers
mediapane relay                               # on the login node, inside `mediapane ssh` (plugin)
```

## Plugins

The core stays small; extras are separate packages you add if you want them:

| Plugin | Adds |
|---|---|
| `mediapane-claude` | Claude Code: the hook and skill above (`mediapane claude install`, or the Claude Code plugin); `mediapane mcp`, an MCP server with a `show_media` tool |
| `mediapane-diff` | `mediapane diff a.png b.png`: side by side, flicker and a difference heatmap, as one series; works as a `git difftool` |
| `mediapane-relay` | `mediapane relay`: forwards pushes spooled by cluster jobs |

```sh
uv tool install mediapane --with mediapane-claude --with mediapane-diff
mediapane plugins          # what's installed, what failed, what's available
```

Your own mods are plain Python files in `~/.config/mediapane/plugins/` (or the directories in
`$MEDIAPANE_PLUGINS`), no packaging needed. A plugin is a module with `register(api)`:

```python
def register(api):
    api.command("hello", "say hello", run=lambda args: print("hi"))
    api.key("y", "copy the selected file's path", lambda pane: copy(pane.selected.path))
    api.on_item(lambda item: notify(item.name) if item.host != "laptop" else None)
```

`examples/plugins/clipboard.py` is a complete one. Plugins load only when needed (never for
`mp file.png`), a broken plugin is reported and skipped, and `MEDIAPANE_NO_PLUGINS=1` turns them
all off. Packages register through the `mediapane.plugins` entry point group.

## Terminal support

The gallery is drawn with [kitty graphics](https://sw.kovidgoyal.net/kitty/graphics-protocol/)
**Unicode placeholders**: image cells are text, so they scroll, survive tmux, and animate by
swapping the image behind them.

| | Gallery, animation, video | Notes |
|---|---|---|
| kitty ≥ 0.28 | ✓ | for `mediapane open`: `allow_remote_control socket-only` and `listen_on unix:/tmp/kitty` |
| Ghostty ≥ 1.3 | ✓ | `mediapane open` uses its AppleScript on macOS |
| inside tmux ≥ 3.3 | ✓ in kitty / Ghostty | `set -g allow-passthrough all` (`on` loses images in hidden windows) and `set -g focus-events on` (re-sends images after a reattach) |
| WezTerm, iTerm2, Konsole, others | ✗ | no Unicode placeholders yet; `mediapane.show` inline works where kitty graphics does |

## How it works

* **One process, one event loop.** Keys and mouse arrive on stdin, pushes on a Unix socket, video
  frames from `ffmpeg` pipes, and timers drive animations. Nothing polls. Decoding and scaling
  run on worker threads, so a long GIF never freezes the pane.
* **Images by path when possible.** Locally the terminal reads PNGs straight from disk (`t=f`), so
  no pixel data crosses the pty. Over SSH the data goes inline, scaled to the displayed size.
* **Animations swap data, not cells.** A GIF's frames are decoded once; each tick re-sends the
  next frame under the item's image id, and the placeholders update in place. Terminal memory
  stays flat, whereas re-placing a new image per frame grows without bound. Series and their
  time-lapse work the same way.
* **Scrolling moves text, not images.** The terminal shifts the screen in a scroll region and
  only the exposed lines are drawn: about 1 KB per step instead of a full redraw.
* **Videos play once, then stop.** `ffmpeg -re` decodes them at the displayed width to an
  uncompressed PNG stream (about 5% of a core; compressed PNG costs about 18%). Frames reach the
  terminal through POSIX shared memory where it supports that, so nothing touches the disk. Space
  replays.
* **Pushers are untrusted.** Names and hosts are stripped of control characters before they reach
  the terminal, forwarded sockets are only used if they're yours, and a malformed or damaged
  push is rejected without affecting the pane. Beyond `MEDIAPANE_MAX_ITEMS` (500) pieces of
  media the oldest go (a series' oldest steps first), so a training loop can `show()` every step
  indefinitely.

### Wire format

```
{"op": "push", "name": "fig.png", "size": 1234, "tag": "samples", "step": "3000"}\n<1234 bytes>
```

sent to `~/.cache/mediapane/pane-<hostname>.sock` (or `$MEDIAPANE_SOCKET`; per host, since
homes are often shared between machines); `tag` and `step` are optional. The pane answers with
one JSON line. `{"op": "ping"}` and `{"op": "clear"}` take no payload. So, without Python:

```sh
{ printf '{"op":"push","name":"x.png","size":%d}\n' $(wc -c < x.png); cat x.png; } \
  | nc -U ~/.cache/mediapane/pane-$(hostname).sock
```

## Development

```sh
uv sync && uv run pytest     # includes end-to-end tests of the pane in a pseudo-terminal
cd plugins/mediapane-diff && uv sync && uv run pytest     # each plugin, against this core
```

MIT licence.
