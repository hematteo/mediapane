# mediapane-claude

A [mediapane](https://github.com/hematteo/mediapane) plugin for coding agents. It costs Claude
almost no tokens: it shows you media instead of having Claude read it.

* **A Claude Code hook.** Images, GIFs and videos Claude writes, edits or reads appear in your
  pane, and so do media files a Bash command just made (`python plot.py`, `ffmpeg … out.mp4`).
  The hook prints nothing, so it adds nothing to Claude's context.
* **A skill.** Tells Claude to show you files with `mp` rather than reading them (reading a
  typical plot costs about 1,800 tokens), and to look at images itself through
  `mediapane peek`, a copy at most 768 px wide. Only its one-line description is always in
  context.
* **An MCP server**, `mediapane mcp`, with a `show_media` tool, for agents that can't run `mp`.

## Install

```sh
uv tool install mediapane --with mediapane-claude
```

then either the Claude Code plugin (hook and skill, managed with `/plugin`):

```
/plugin install mediapane --marketplace hematteo/mediapane
```

or, without the plugin system:

```sh
mediapane claude install       # hook in ~/.claude/settings.json, skill in ~/.claude/skills/
```

`mediapane claude install --project .` uses the project's `.claude/` instead, `--no-skill` adds
only the hook, `--dry-run` shows the change without making it. Install is idempotent, keeps
everything else in the settings file (and follows a symlinked one), and leaves a timestamped
backup beside it. `mediapane claude uninstall` removes both again.

For agents without a shell: `claude mcp add mediapane -- mediapane mcp` (Claude Code), or the
equivalent in your MCP client.

## Usage

Start a pane (`mediapane open`) and use Claude Code as usual. The hook does nothing when no pane
is running and always exits 0, so it can't get in Claude's way. For Bash it only shows media
files named in the command and modified in the last two minutes (at most 10), and skips
commands that run `mp` or `mediapane` themselves; the pane also folds the same file pushed twice
into one item. Set `MEDIAPANE_DEBUG=1` to see what it does on stderr.

The `show_media` tool takes `path`, and optionally `name`, and `tag` + `step` to group images
into a series (one gallery entry, stepped with ←/→).
