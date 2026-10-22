"""`mediapane doctor`: what works here, and the one-line fix for what doesn't."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

from . import ipc, term

PLACEHOLDER_TERMINALS = {"ghostty", "kitty"}  # Unicode placeholders, which the pane needs


def doctor() -> int:
    problems = 0

    def row(status: bool | None, label: str, detail: str = "", fix: str = "") -> None:
        nonlocal problems
        mark = {True: "✓", False: "✗", None: "·"}[status]
        problems += status is False
        print(f" {mark} {label}" + (f": {detail}" if detail else ""))
        if fix:
            print(f"     → {fix}")

    name = _terminal_name()
    row(None, "terminal", name or "unknown")

    if term.in_tmux():
        version = _run(["tmux", "-V"])
        passthrough = _run(["tmux", "show", "-gv", "allow-passthrough"])
        # "on" drops graphics while the pane is in a hidden window; "all" doesn't
        row(
            {"all": True, "on": None}.get(passthrough, False),
            "tmux passthrough",
            f"{passthrough or '?'} ({version})",
            ""
            if passthrough == "all"
            else "tmux set -g allow-passthrough all  (and add it to ~/.tmux.conf)",
        )
        focus = _run(["tmux", "show", "-gv", "focus-events"])
        row(
            focus == "on" or None,
            "tmux focus events",
            focus or "?",
            ""
            if focus == "on"
            else "tmux set -g focus-events on  (re-sends images after reattach)",
        )
    if term.over_ssh():
        row(None, "over SSH", "images are sent inline through the connection")

    if sys.stdin.isatty() and sys.stdout.isatty():
        ok = term.query_graphics(sys.stdin.fileno(), sys.stdout.fileno())
        if ok is None:
            row(False, "kitty graphics", "the terminal didn't answer")
        else:
            row(
                ok,
                "kitty graphics",
                "supported" if ok else "not supported",
                "" if ok else "use kitty or Ghostty",
            )
        rows, cols, xpix, ypix = term.winsize(sys.stdout.fileno())
        row(
            bool(xpix) or None,
            "cell size",
            f"{xpix / cols:.0f}×{ypix / rows:.0f} px"
            if xpix
            else "not reported; images assume 1:2 cells",
        )
    else:
        row(None, "kitty graphics", "not checked (run doctor in the terminal itself)")

    if name and name.lower() in PLACEHOLDER_TERMINALS:
        row(True, "Unicode placeholders", f"{name} supports them")
    elif name:
        row(
            None,
            "Unicode placeholders",
            f"unknown for {name}; the pane needs them "
            "(kitty and Ghostty have them, WezTerm, iTerm2 and Konsole don't)",
        )

    has_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    row(
        has_ffmpeg or None,
        "ffmpeg",
        "found" if has_ffmpeg else "missing: videos won't play",
        "" if has_ffmpeg else "install ffmpeg (brew / apt / conda)",
    )

    reply = ipc.request("ping", path=ipc.default_socket(), timeout=1.0)
    if reply:
        row(True, "pane on this host", f"running, {reply.get('items', 0)} items")
    else:
        row(None, "pane on this host", "not running", "mediapane open")
    forwarded = ipc.forwarded_sockets()
    live = [p for p in forwarded if ipc.request("ping", path=p, timeout=1.0)]
    if live:
        row(True, "forwarded pane", f"reachable via {live[0]}")
    elif term.over_ssh():
        row(None, "forwarded pane", "none", "connect with `mediapane ssh <host>` from your laptop")

    for mod, why in (("numpy", "arrays and tensors"), ("matplotlib", "the matplotlib backend")):
        found = importlib.util.find_spec(mod) is not None
        row(found or None, mod, "installed" if found else f"not installed (needed for {why})")

    return 1 if problems else 0


def _terminal_name() -> str | None:
    if os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    if os.environ.get("GHOSTTY_RESOURCES_DIR"):
        return "ghostty"
    return os.environ.get("TERM_PROGRAM") or os.environ.get("TERM")


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
