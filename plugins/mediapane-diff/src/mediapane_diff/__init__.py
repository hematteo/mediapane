"""mediapane-diff: `mediapane diff A B` compares two images in the pane, side by side, as a
flicker and as a difference heatmap, all in one series stepped with ←/→."""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mediapane.plugins import API

MODES = ("all", "side", "flicker", "heat")

GIT_HINT = """as a git difftool:
  git config --global difftool.mediapane.cmd 'mediapane diff "$LOCAL" "$REMOTE" --name "$MERGED"'
  git difftool -y -t mediapane -- fig.png"""


def register(api: API) -> None:
    api.command(
        "diff",
        "compare two images: side by side, flicker, difference heatmap",
        run=_run,
        setup=_setup,
    )


def _setup(p: argparse.ArgumentParser) -> None:
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    p.epilog = GIT_HINT
    p.add_argument("a", help="first image (/dev/null or a missing file: show only B)")
    p.add_argument("b", help="second image (/dev/null or a missing file: show only A)")
    p.add_argument("--name", help="what to call the comparison (default: 'A vs B')")
    p.add_argument(
        "--mode",
        choices=MODES,
        default="all",
        help="side: side by side; flicker: A and B alternating; heat: difference heatmap",
    )


def _run(args: argparse.Namespace) -> int:
    from .diff import run  # Pillow work only when the command runs, not when plugins load

    return run(args.a, args.b, name=args.name, mode=args.mode)
