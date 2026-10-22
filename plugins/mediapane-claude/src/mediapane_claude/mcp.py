"""`mediapane mcp`: a Model Context Protocol server on stdio with one tool, `show_media`.

JSON-RPC 2.0, one message per line. stdout carries protocol messages only; logs go to stderr.

    claude mcp add mediapane -- mediapane mcp
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import IO, Any

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_VERSION = "2025-06-18"
TIMEOUT = 30.0  # the pane answers once it has decoded the file; a long GIF takes a while

TOOL = {
    "name": "show_media",
    "title": "Show media",
    "description": (
        "Show an image, GIF or video file to the user in their mediapane viewer. No need to "
        "read it first. `tag` + `step` group related images into one steppable series."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "the media file (absolute, or relative "
                     "to the working directory)"},
            "name": {"type": "string", "description": "display name (default: the file name)"},
            "tag": {"type": "string", "description": "series to add it to"},
            "step": {"type": "string", "description": "its step within the series"},
        },
        "required": ["path"],
    },
}  # fmt: skip

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND = -32700, -32600, -32601
INVALID_PARAMS, INTERNAL_ERROR = -32602, -32603


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    out, sys.stdout = sys.stdout, sys.stderr  # a stray print() would corrupt the protocol
    try:
        serve(sys.stdin, out)
    finally:
        sys.stdout = out
    return 0


def serve(inp: IO[str], out: IO[str]) -> None:
    for line in inp:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send(out, _error(None, PARSE_ERROR, "parse error"))
            continue
        try:
            reply = handle(message)
        except Exception as e:  # one bad request mustn't take the server down
            _log(f"error handling {line.strip()[:200]}: {e!r}")
            msg_id = message.get("id") if isinstance(message, dict) else None
            reply = _error(msg_id, INTERNAL_ERROR, f"internal error: {e}")
        if reply is not None:
            _send(out, reply)


def handle(message: Any) -> dict[str, Any] | None:
    """The reply to one JSON-RPC message; None for notifications and responses."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "invalid request: expected a JSON object")
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), INVALID_REQUEST, "invalid request: not JSON-RPC 2.0")
    if "method" not in message:
        return None  # a response to something we never ask: ignore
    method, msg_id = message["method"], message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if "id" not in message:
        return None  # notifications (notifications/initialized, cancelled, ...) get no reply
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(
            msg_id,
            {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else DEFAULT_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mediapane", "version": _version()},
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [TOOL]})
    if method == "tools/call":
        if params.get("name") != TOOL["name"]:
            return _error(msg_id, INVALID_PARAMS, f"unknown tool: {params.get('name')}")
        args = params.get("arguments")
        text, failed = show_media(args if isinstance(args, dict) else {})
        return _result(msg_id, {"content": [{"type": "text", "text": text}], "isError": failed})
    return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")


def show_media(args: dict[str, Any]) -> tuple[str, bool]:
    """Push one file; (message for the model, whether it failed)."""
    from mediapane import ipc
    from mediapane.formats import IMAGE_EXTS, VIDEO_EXTS  # no Pillow: hooks run often

    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return "`path` is required: the image, GIF or video file to show", True
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        return f"no such file: {path}", True
    if path.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS:
        exts = " ".join(sorted(IMAGE_EXTS | VIDEO_EXTS))
        return f"{path.name} isn't a media file mediapane shows ({exts})", True
    meta = {k: str(args[k]) for k in ("tag", "step") if args.get(k) not in (None, "")}
    name = str(args.get("name") or path.name)
    reply = ipc.push_file(path, name, timeout=TIMEOUT, origin="mcp", **meta)
    if reply is None:
        return (
            "no mediapane pane is running, so nothing was shown. The user can start one with "
            "`mediapane open` (or run `mediapane` in a spare terminal pane), then ask again.",
            True,
        )
    if not reply.get("ok"):
        return f"mediapane couldn't show {path.name}: {reply.get('error', 'unknown error')}", True
    if reply.get("spooled"):
        return f"queued {path.name} for mediapane (no pane here; a relay will forward it)", False
    series = f" (series {meta['tag']!r})" if "tag" in meta else ""
    return f"shown in mediapane: {name}{series}", False


def _version() -> str:
    try:
        from mediapane import __version__
    except ImportError:
        return "unknown"
    return __version__


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _log(text: str) -> None:
    print(f"mediapane mcp: {text}", file=sys.stderr, flush=True)


def _send(out: IO[str], message: dict[str, Any]) -> None:
    out.write(json.dumps(message) + "\n")
    out.flush()
