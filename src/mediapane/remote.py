"""`mediapane ssh`: ssh to a host with a socket forwarded back to the local pane, so anything
run there (shells, scripts, training jobs) can `mediapane push` straight into your pane.

The forwarded socket is /tmp/mediapane-<remote user>-<random>.sock on the remote host. sshd
creates it readable by that user only. It's unique per session because sshd only replaces stale
sockets when its own StreamLocalBindUnlink is on, which it usually isn't; clients try the newest
first and clean up dead ones (see ipc.candidate_sockets).
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import subprocess
import sys

from . import ipc


def remote_user(ssh_args: list[str]) -> str:
    """The user ssh will log in as, from `ssh -G` (honours ~/.ssh/config and -l / user@)."""
    out = subprocess.run(["ssh", "-G", *ssh_args], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("user "):
            return line.split(None, 1)[1].strip()
    return getpass.getuser()


def ssh(ssh_args: list[str]) -> int:
    if not ssh_args:
        print("usage: mediapane ssh [ssh options] destination [command]", file=sys.stderr)
        return 2
    local = ipc.default_socket()
    if not ipc.request("ping", path=local, timeout=1.0):
        print(
            "mediapane: no pane running here yet; pushes from the host will fail until you "
            "start one (`mediapane open`)",
            file=sys.stderr,
        )
    user = re.sub(r"[^\w.-]", "_", remote_user(ssh_args))
    remote = f"/tmp/mediapane-{user}-{secrets.token_hex(4)}.sock"
    os.execvp("ssh", ["ssh", "-R", f"{remote}:{local}", *ssh_args])
