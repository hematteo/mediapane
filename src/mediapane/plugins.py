"""Plugins: extra commands and pane behaviour, installed only by the people who want them.

A plugin is a module with a `register(api)` function. mediapane finds them in:

* installed packages exposing the entry point group ``mediapane.plugins`` (``mediapane-claude``,
  ``mediapane-diff``, ``mediapane-relay``, or anyone's)
* ``*.py`` files in ``~/.config/mediapane/plugins/``, or in the directories listed in
  ``$MEDIAPANE_PLUGINS`` (``:``-separated): personal mods, no packaging needed

``MEDIAPANE_NO_PLUGINS=1`` turns them all off. Plugins load lazily: never for ``mp file.png``,
only for unknown commands, ``--help``, ``mediapane plugins`` and the pane itself. A plugin that
fails to load is reported and skipped, never fatal.

The API is deliberately small (and versioned, ``API.version``):

    def register(api):
        api.command("hello", "say hello", run=lambda args: print("hi") or 0)
        api.on_item(lambda item: ...)              # in the pane, after each new item
        api.key("y", "copy path", lambda pane: ...)  # in the pane, for keys it doesn't use
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

GROUP = "mediapane.plugins"
# Keys the pane uses itself: plugins can't take these (here, not in the pane, so loading
# plugins never imports the pane)
CORE_KEYS = set("qjknpJKgGrotxC ?,.<>+=-0hl123456789")

# First-party plugins: what `mediapane plugins` offers to install
FIRST_PARTY = {
    "claude": "Claude Code: show images Claude writes or reads; an MCP tool for any agent",
    "diff": "compare two images: side by side, flicker, difference heatmap; git difftool",
    "relay": "forward pushes from cluster compute nodes (via a shared spool folder)",
}


@dataclass(frozen=True)
class ItemInfo:
    """What plugins see of an item: read-only."""

    name: str
    host: str
    kind: str  # "image" | "anim" | "video"
    path: Path  # the file as received
    still: Path | None  # a PNG of it, if there is one
    width: int
    height: int
    tag: str  # series it belongs to, "" if none
    step: str  # its step within the series


class PaneContext(Protocol):
    """What a key binding can do with the pane."""

    @property
    def selected(self) -> ItemInfo | None: ...

    @property
    def items(self) -> list[ItemInfo]: ...

    def say(self, text: str) -> None: ...


@dataclass
class Command:
    name: str
    help: str
    run: Callable[[argparse.Namespace], int | None]
    setup: Callable[[argparse.ArgumentParser], None] | None
    plugin: str


@dataclass
class Key:
    key: str
    help: str
    fn: Callable[[PaneContext], None]
    plugin: str


@dataclass
class API:
    version = 1
    commands: dict[str, Command] = field(default_factory=dict)
    item_hooks: list[tuple[str, Callable[[ItemInfo], None]]] = field(default_factory=list)
    keys: dict[str, Key] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    _current: str = ""

    def command(
        self,
        name: str,
        help: str,
        run: Callable[[argparse.Namespace], int | None],
        setup: Callable[[argparse.ArgumentParser], None] | None = None,
    ) -> None:
        """Add `mediapane <name>`. `setup` adds arguments to its parser; `run` gets the parsed
        arguments and returns an exit code (None means 0)."""
        from .cli import CORE

        if name in CORE or name in self.commands:
            owner = "mediapane" if name in CORE else self.commands[name].plugin
            self.errors.append((self._current, f"command {name!r} is taken by {owner}"))
            return
        self.commands[name] = Command(name, help, run, setup, self._current)

    def on_item(self, fn: Callable[[ItemInfo], None]) -> None:
        """Call `fn` in the pane whenever an item arrives. Keep it quick: it runs on the pane's
        event loop (start a thread or process for anything slow)."""
        self.item_hooks.append((self._current, fn))

    def key(self, key: str, help: str, fn: Callable[[PaneContext], None]) -> None:
        """Bind a key in the pane: one printable character the pane doesn't use itself."""
        if len(key) != 1 or not key.isprintable() or key in CORE_KEYS or key in self.keys:
            owner = self.keys[key].plugin if key in self.keys else "mediapane"
            why = "isn't a single printable character" if len(key) != 1 or not key.isprintable() \
                else f"is taken by {owner}"  # fmt: skip
            self.errors.append((self._current, f"key {key!r} {why}"))
            return
        self.keys[key] = Key(key, help, fn, self._current)


_api: API | None = None


def load() -> API:
    """Load every plugin once; later calls return the same registry."""
    global _api
    if _api is not None:
        return _api
    _api = API()
    if os.environ.get("MEDIAPANE_NO_PLUGINS"):
        return _api
    for name, loader in _sources():
        _api._current = name
        try:
            module = loader()
            register = getattr(module, "register", module if callable(module) else None)
            if register is None:
                raise TypeError("has no register(api) function")
            register(_api)
            _api.loaded.append(name)
        except Exception:
            _api.errors.append((name, traceback.format_exc(limit=3).strip()))
    _api._current = ""
    return _api


def _sources() -> list[tuple[str, Callable[[], Any]]]:
    sources: list[tuple[str, Callable[[], Any]]] = []
    from importlib.metadata import entry_points

    for ep in entry_points(group=GROUP):
        sources.append((ep.name, ep.load))
    for d in plugin_dirs():
        for path in sorted(d.glob("*.py")) if d.is_dir() else []:
            sources.append((f"{path.stem} ({path})", lambda p=path: _load_file(p)))
    return sources


def _check_safe(path: Path) -> None:
    """Refuse to run a plugin file someone else could have written: it runs as you."""
    for p in (path, path.parent):
        st = p.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise PermissionError(f"{p} is writable by others or not yours; not loading it")


def plugin_dirs() -> list[Path]:
    env = os.environ.get("MEDIAPANE_PLUGINS")
    if env:
        return [Path(p).expanduser() for p in env.split(":") if p]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [Path(base) / "mediapane" / "plugins"]


def _load_file(path: Path) -> Any:
    _check_safe(path)
    spec = importlib.util.spec_from_file_location(f"mediapane_local_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"can't load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_hint(name: str) -> str:
    """How to add first-party plugin `name` to however mediapane itself was installed."""
    pkg = f"mediapane-{name}"
    if "/uv/tools/" in sys.prefix.replace(os.sep, "/"):
        return f"uv tool install mediapane --with {pkg}   (add any others you use with --with)"
    if os.environ.get("PIPX_HOME") or "/pipx/" in sys.prefix.replace(os.sep, "/"):
        return f"pipx inject mediapane {pkg}"
    return f"uv pip install {pkg}   (or: pip install {pkg})"


def report() -> int:
    """`mediapane plugins`: what's installed, what failed, what's available."""
    api = load()
    if os.environ.get("MEDIAPANE_NO_PLUGINS"):
        print("plugins are off (MEDIAPANE_NO_PLUGINS is set)")
        return 0
    print("installed:" if api.loaded else "installed: none")
    for name in api.loaded:
        cmds = [c.name for c in api.commands.values() if c.plugin == name]
        keys = [k.key for k in api.keys.values() if k.plugin == name]
        hooks = sum(1 for p, _ in api.item_hooks if p == name)
        what = ", ".join(
            [f"commands: {' '.join(cmds)}"] * bool(cmds)
            + [f"keys: {' '.join(keys)}"] * bool(keys)
            + [f"{hooks} item hook{'s' * (hooks != 1)}"] * bool(hooks)
        )
        print(f"  {name}" + (f"  ({what})" if what else ""))
    for name, err in api.errors:
        print(f"  {name}: FAILED TO LOAD\n    " + err.replace("\n", "\n    "))
    missing = {k: v for k, v in FIRST_PARTY.items() if k not in api.loaded}
    if missing:
        print("\navailable:")
        for name, what in missing.items():
            print(f"  {name:<8} {what}\n           {install_hint(name)}")
    print(f"\npersonal plugins: *.py files in {', '.join(map(str, plugin_dirs()))}")
    return 1 if api.errors else 0
