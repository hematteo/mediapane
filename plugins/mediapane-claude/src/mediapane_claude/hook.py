"""`mediapane claude hook`: Claude Code's PostToolUse hook. Shows media files a tool wrote or
read, and ones a Bash command just made.

It runs after every matching tool call, so it must be quick and must never get in Claude's way:
it always exits 0, writes nothing to stdout, and gives up after a short socket timeout.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import traceback
from pathlib import Path
from typing import Any

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Read"}
RECENT = 120.0  # seconds: a Bash command's outputs, not every image it mentions
MAX_FILES = 10
TIMEOUT = 2.0
# Header field naming the pusher. Ideally "source", but ipc.request already takes `source` (the
# file to stream), so it can't also be a header field
ORIGIN = {"origin": "claude"}


def main() -> int:
    try:
        if sys.stdin.isatty():
            return 0
        payload = json.loads(sys.stdin.read() or "null")
        if isinstance(payload, dict):
            for path in media_paths(payload):
                _push(path)
    except Exception:
        _debug(traceback.format_exc())
    return 0


def media_paths(payload: dict[str, Any]) -> list[Path]:
    """The files this tool call should show, in order."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    cwd = Path(payload["cwd"]) if isinstance(payload.get("cwd"), str) else Path.cwd()
    if tool in FILE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw:
            return []
        path = _resolve(raw, cwd)
        return [path] if _is_media(path) and path.is_file() else []
    if tool == "Bash" and isinstance(tool_input.get("command"), str):
        if _runs_mediapane(tool_input["command"]):
            return []  # `mp x.png` pushed it already; `mediapane peek` output is for Claude only
        return _recent_outputs(tool_input["command"], cwd)
    return []


SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "{", "}", "!", "do", "then", "else", "elif"}
# Words that run the command after them: `time mp x.png`, `uv run mp x.png`
WRAPPERS = {"env", "time", "nohup", "exec", "command", "sudo", "nice", "builtin", "uvx"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _runs_mediapane(command: str) -> bool:
    """Whether any command in `command` is mediapane's own CLI (`mp`, `mediapane`, `python -m
    mediapane`), allowing for env assignments and wrappers like `time` or `uv run`."""
    tokens = _tokens(command.replace("\n", " ; "))
    start = True
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in SEPARATORS:
            start = True
        elif start and (ASSIGNMENT.match(token) or token in WRAPPERS or token.startswith("-")):
            pass  # still before the real command word (wrapper options like `env -i` too)
        elif start and token in ("uv", "pipx") and tokens[i + 1 : i + 2] == ["run"]:
            i += 1
        elif start and Path(token).name.startswith("python") and tokens[i + 1 : i + 2] == ["-m"]:
            if tokens[i + 2 : i + 3] == ["mediapane"]:
                return True
            start = False
        elif start:
            if Path(token).name in ("mp", "mediapane"):
                return True
            start = False
        i += 1
    return False


def _recent_outputs(command: str, cwd: Path) -> list[Path]:
    tokens = _tokens(command)
    # `cd out && python plot.py` writes relative to out/, so try cd targets as well as cwd
    bases = [cwd] + [_resolve(tokens[i + 1], cwd) for i, t in enumerate(tokens[:-1]) if t == "cd"]
    now = time.time()
    found: list[Path] = []
    for word in _candidates(command, tokens):
        for base in bases:
            path = _resolve(word, base)
            if path in found or not _is_media(path):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if path.is_file() and now - st.st_mtime <= RECENT:
                found.append(path)
                break
        if len(found) >= MAX_FILES:
            break
    return found


_EXT = r"(?:png|jpe?g|gif|webp|bmp|tiff?|apng|mp4|mov|m4v|webm|mkv|avi)"
# A path-ish run of characters ending in a media extension, found inside a larger word:
# `--out=fig.png`, `savefig('runs/a b.png')` (quotes delimit), `>plot.gif`
_PATH_RE = re.compile(r"[^\s'\"`=:,;()<>|&{}\[\]*?$]+\." + _EXT + r"(?![\w.])", re.IGNORECASE)


def _tokens(command: str) -> list[str]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:  # unbalanced quotes: let the regex find what it can
        return []


def _candidates(command: str, tokens: list[str]) -> list[str]:
    words: list[str] = []
    for token in tokens or [command]:
        if not any(c in token for c in "*?["):  # globs name what exists, not what was made
            words.append(token)
        words.extend(_PATH_RE.findall(token))
    return list(dict.fromkeys(w for w in words if w and len(w) < 4096))


def _is_media(path: Path) -> bool:
    from mediapane.formats import IMAGE_EXTS, VIDEO_EXTS  # no Pillow: hooks run often
    from mediapane.ipc import cache_dir

    if path.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS:
        return False
    # mediapane's own files (e.g. `peek` copies Claude reads) aren't news to the user
    return cache_dir().resolve() not in path.resolve().parents


def _resolve(raw: str, base: Path) -> Path:
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else base / path


def _push(path: Path) -> None:
    from mediapane import ipc

    reply = ipc.push_file(path, path.name, timeout=TIMEOUT, **ORIGIN)
    _debug(f"pushed {path}: {reply}")


def _debug(text: str) -> None:
    if os.environ.get("MEDIAPANE_DEBUG") == "1":
        print(f"mediapane claude hook: {text}", file=sys.stderr)


if __name__ == "__main__":  # how the installed hook runs: python -m mediapane_claude.hook
    raise SystemExit(main())
