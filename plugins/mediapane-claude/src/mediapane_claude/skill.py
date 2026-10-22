"""The `mediapane` skill for Claude Code: tells Claude to show media with `mp` and to look at
images through `mediapane peek`. Only its one-line description sits in Claude's context; the
rest loads when Claude uses it.

The file lives once, in the Claude Code plugin bundle (claude-code/skills/mediapane/SKILL.md);
the wheel carries a copy as package data, for `mediapane claude install`.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

NAME = "mediapane"


def text() -> str:
    packaged = resources.files("mediapane_claude") / "SKILL.md"
    if packaged.is_file():
        return packaged.read_text()
    # A source checkout (editable install): the bundle next to the package
    bundle = Path(__file__).resolve().parents[2] / "claude-code" / "skills" / NAME / "SKILL.md"
    return bundle.read_text()


def install(skills_dir: Path, *, dry_run: bool = False) -> str:
    """Write the skill into `skills_dir`; returns what was done."""
    target = skills_dir / NAME / "SKILL.md"
    new = text()
    if target.is_file() and target.read_text() == new:
        return f"skill already installed: {target}"
    if target.exists() and not _ours(target):
        return f"left {target} alone: it isn't mediapane's"
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new)
    return f"{'would install' if dry_run else 'installed'} skill: {target}"


def uninstall(skills_dir: Path, *, dry_run: bool = False) -> str:
    target = skills_dir / NAME / "SKILL.md"
    if not target.exists():
        return "skill not installed"
    if not _ours(target):
        return f"left {target} alone: it isn't mediapane's"
    if not dry_run:
        shutil.rmtree(target.parent)
    return f"{'would remove' if dry_run else 'removed'} skill: {target.parent}"


def _ours(path: Path) -> bool:
    try:
        head = path.read_text()[:400]
    except OSError:
        return False
    return f"name: {NAME}\n" in head and "mediapane peek" in path.read_text()
