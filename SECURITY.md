# Security

## Reporting

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button on
the repository's Security tab, not in a public issue.

## What mediapane trusts, and what it doesn't

**The pane's socket** is a Unix socket in `~/.cache/mediapane/`, created readable and writable
by you only. Anything that can connect to it can show you media; that is its purpose.

**Pushes are untrusted input**, including those from remote hosts via `mediapane ssh`:

- Names, hosts, tags and steps are stripped of control characters before they reach your
  terminal, so a push can't inject escape sequences (window titles, clipboard writes, kitty
  remote control).
- Payloads are size-capped and stored in a private session folder; a malformed header, an
  unreadable image or a disk error rejects that push and nothing else.
- Images are decoded with Pillow. ffmpeg only reads the pushed file itself, restricted to
  common video containers (`-protocol_whitelist file,pipe` and a format whitelist), so a
  crafted playlist can't make it open other files or URLs.
- `o` only opens files with media extensions in your default app.

**Remote and shared machines**: forwarded sockets in `/tmp` and files in a spool folder are only
used if they are owned by you. Session folders and history are created mode 0700, and are named
per host, since cluster homes are often shared between machines.

**Plugins run code as you.** Installed plugin packages are trusted like any Python package you
install. Personal plugin files are only loaded if they and their folder are owned by you and not
writable by others. `MEDIAPANE_NO_PLUGINS=1` turns all plugins off.

**The Claude Code integration** (`mediapane-claude`) never sends anything to Claude: the hook
exits 0 with no output, so it adds nothing to Claude's context. `mediapane claude install` backs
up your settings file before changing it, and only touches its own hook entry and skill.
