"""mediapane-relay: `mediapane relay` forwards pushes that cluster jobs left in $MEDIAPANE_SPOOL
(a folder on a shared filesystem) to a pane, from a machine that can reach one."""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mediapane.plugins import API


def register(api: API) -> None:
    api.command(
        "relay",
        "forward pushes spooled by cluster jobs ($MEDIAPANE_SPOOL) to the pane",
        run=_run,
        setup=_setup,
    )


def _setup(p: argparse.ArgumentParser) -> None:
    p.add_argument("--spool", type=Path, help="the spool folder (default: $MEDIAPANE_SPOOL)")
    p.add_argument(
        "--interval", type=float, default=0.5, help="seconds between scans (default: 0.5)"
    )
    p.add_argument("--once", action="store_true", help="forward what's there now, then exit")


def _run(args: argparse.Namespace) -> int:
    from .relay import main

    return main(args.spool, interval=args.interval, once=args.once)
