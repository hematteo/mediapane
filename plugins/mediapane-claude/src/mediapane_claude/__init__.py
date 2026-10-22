"""mediapane plugin for Claude Code: a hook that shows the media Claude writes or reads, a skill
that teaches Claude to show media and look at images cheaply, and an MCP server
(`mediapane mcp`) with a `show_media` tool for agents without a shell.

    mediapane claude install      # hook in ~/.claude/settings.json + skill in ~/.claude/skills
    claude mcp add mediapane -- mediapane mcp      # only for agents that can't run `mp`
"""

from __future__ import annotations

import argparse
from pathlib import Path

__version__ = "0.1.0"


def register(api) -> None:  # noqa: ANN001 - mediapane.plugins.API
    api.command(
        "claude", "Claude Code: hook that shows images Claude writes or reads", _claude, _setup
    )
    api.command("mcp", "MCP server (stdio) with a show_media tool for any agent", _mcp)


def _setup(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="action", metavar="action", required=True)
    sub.add_parser("hook", help="the PostToolUse hook itself (Claude Code runs this)")
    for name, what in (("install", "add the hook and skill to"), ("uninstall", "remove them from")):
        s = sub.add_parser(name, help=f"{what} Claude Code")
        s.add_argument(
            "--project",
            type=Path,
            metavar="DIR",
            help="use DIR/.claude/settings.json instead of your user settings",
        )
        s.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
        s.add_argument("--no-skill", action="store_true", help="only the hook, not the skill")
        s.add_argument("--settings", type=Path, help=argparse.SUPPRESS)


def _claude(args: argparse.Namespace) -> int:
    if args.action == "hook":
        from .hook import main

        return main()
    from . import settings, skill

    path = settings.settings_path(args.project, args.settings)
    skills_dir = path.parent / "skills"  # ~/.claude/skills, or DIR/.claude/skills
    if args.action == "install":
        rc = settings.install(path, dry_run=args.dry_run)
        if rc == 0 and not args.no_skill:
            print(skill.install(skills_dir, dry_run=args.dry_run))
        return rc
    rc = settings.uninstall(path, dry_run=args.dry_run)
    if not args.no_skill:
        print(skill.uninstall(skills_dir, dry_run=args.dry_run))
    return rc


def _mcp(args: argparse.Namespace) -> int:
    from .mcp import main

    return main()
