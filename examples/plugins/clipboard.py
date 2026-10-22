"""A personal mediapane plugin: copy to ~/.config/mediapane/plugins/ to use it.

`y` copies the selected item's file path to the clipboard; items pushed from another machine
pop up a desktop notification. Shows the whole plugin API: a command, a key, an item hook.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys


def _copy(text: str) -> bool:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=False)
            return True
    return False


def _notify(title: str, body: str) -> None:
    if sys.platform == "darwin":
        # Values go in as arguments, never into the script's source: a pushed name could
        # otherwise inject AppleScript
        cmd = ["osascript", "-e", "on run argv", "-e",
               "display notification (item 2 of argv) with title (item 1 of argv)",
               "-e", "end run", title, body]  # fmt: skip
    elif shutil.which("notify-send"):
        cmd = ["notify-send", title, body]
    else:
        return
    # Item hooks run on the pane's event loop: hand slow work to another process
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def register(api):
    def yank(pane):
        item = pane.selected
        if item is None:
            return
        pane.say(f"copied {item.path}" if _copy(str(item.path)) else "no clipboard tool found")

    def remote_item(item):
        if item.host and item.host != socket.gethostname():
            _notify("mediapane", f"{item.name} from {item.host}")

    api.key("y", "copy the selected file's path", yank)
    api.on_item(remote_item)
    api.command(
        "hostname", "print this machine's name", run=lambda args: print(socket.gethostname())
    )
