"""`mediapane claude install` / `uninstall`: add or remove our PostToolUse hook in a Claude Code
settings file, leaving everything else in it alone."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any

MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Read|Bash"
EVENT = "PostToolUse"


def settings_path(project: Path | None = None, override: Path | None = None) -> Path:
    if override is not None:
        return override.expanduser()
    if project is not None:
        return project.expanduser() / ".claude" / "settings.json"
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base).expanduser() / "settings.json"


def hook_command() -> str:
    """The command Claude Code should run: this Python running the hook module directly. It
    works whatever PATH Claude Code has, skips the CLI (and loading other plugins) on every
    tool call, and can't be mistaken for a file to push in whatever project Claude is in."""
    # If that Python ever goes (a reinstall without the plugin), stay silent, not an error
    # after every tool call
    return f"{shlex.quote(sys.executable)} -m mediapane_claude.hook 2>/dev/null || true"


def is_ours(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if any(words[i : i + 2] == ["-m", "mediapane_claude.hook"] for i in range(len(words))):
        return True
    return (  # the form earlier versions installed
        len(words) >= 3
        and words[-2:] == ["claude", "hook"]
        and Path(words[-3]).name in ("mediapane", "mp")
    )


def install(path: Path, *, dry_run: bool = False, command: str | None = None) -> int:
    command = command or hook_command()
    settings = _load(path)
    if settings is None:
        return 1
    groups = _groups(settings)
    if groups is None:
        return _malformed(path)
    current = [(g, h) for g in groups for h in _hooks(g) if is_ours(h.get("command"))]
    if len(current) == 1 and current[0][0].get("matcher") == MATCHER:
        hook = current[0][1]
        if hook.get("command") == command and hook.get("type") == "command":
            print(f"already installed in {path}")
            return 0
    removed = _remove(settings)
    groups = _groups(settings)
    assert groups is not None
    groups.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]})
    verb = "updated" if removed else "added"
    rc = _save(path, settings, f"{verb} {EVENT} hook `{command}`", dry_run)
    if rc == 0 and not dry_run:
        print("new Claude Code sessions will show media files Claude writes or reads in your pane")
    return rc


def uninstall(path: Path, *, dry_run: bool = False) -> int:
    if not path.exists():
        print(f"not installed: {path} doesn't exist")
        return 0
    settings = _load(path)
    if settings is None:
        return 1
    if _groups(settings, create=False) is None and _has_event(settings):
        return _malformed(path)
    if not _remove(settings):
        print(f"not installed in {path}")
        return 0
    return _save(path, settings, f"removed {EVENT} hook", dry_run)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text() if path.exists() else ""
        settings = json.loads(text) if text.strip() else {}
    except (OSError, ValueError) as e:
        print(f"mediapane: can't read {path}: {e}; fix or move it, then retry", file=sys.stderr)
        return None
    if not isinstance(settings, dict):
        print(f"mediapane: {path} isn't a JSON object; not touching it", file=sys.stderr)
        return None
    return settings


def _has_event(settings: dict[str, Any]) -> bool:
    return isinstance(settings.get("hooks"), dict) and EVENT in settings["hooks"]


def _groups(settings: dict[str, Any], create: bool = True) -> list[Any] | None:
    """The PostToolUse list, created if missing; None if something unexpected is there."""
    hooks = settings.setdefault("hooks", {}) if create else settings.get("hooks")
    if not isinstance(hooks, dict):
        return None
    groups = hooks.setdefault(EVENT, []) if create else hooks.get(EVENT)
    return groups if isinstance(groups, list) else None


def _hooks(group: Any) -> list[dict[str, Any]]:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return []
    return [h for h in group["hooks"] if isinstance(h, dict)]


def _remove(settings: dict[str, Any]) -> int:
    """Drop our hook from every PostToolUse group, and groups it leaves empty. Returns how many."""
    groups = _groups(settings, create=False)
    if groups is None:
        return 0
    removed = 0
    kept = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            before = len(group["hooks"])
            group["hooks"] = [
                h for h in group["hooks"] if not (isinstance(h, dict) and is_ours(h.get("command")))
            ]
            removed += before - len(group["hooks"])
            if before and not group["hooks"]:
                continue
        kept.append(group)
    groups[:] = kept
    if not groups:  # leave no empty husk behind
        del settings["hooks"][EVENT]
        if not settings["hooks"]:
            del settings["hooks"]
    return removed


def _save(path: Path, settings: dict[str, Any], what: str, dry_run: bool) -> int:
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    if dry_run:
        # Only our section: the rest of the file can hold secrets (env), and doesn't change
        section = settings.get("hooks", {}).get(EVENT, [])
        print(f"would have {what} in {path}; {EVENT} would read:")
        print(json.dumps(section, indent=2, ensure_ascii=False))
        return 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if path.exists():
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            n = 1
            while backup.exists():  # two runs in the same second
                backup = backup.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
                n += 1
            shutil.copy2(path, backup)
        # Write through a symlink (dotfile managers link settings.json) rather than replace it
        target = path.resolve() if path.is_symlink() else path
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private until in place
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except OSError as e:
        print(f"mediapane: can't write {path}: {e}", file=sys.stderr)
        return 1
    print(f"{what} in {path}" + (f" (backup: {backup.name})" if backup else ""))
    return 0


def _malformed(path: Path) -> int:
    print(
        f"mediapane: {path} has a `hooks.{EVENT}` that isn't a list; not touching it",
        file=sys.stderr,
    )
    return 1
