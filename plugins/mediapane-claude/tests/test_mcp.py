from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from mediapane_claude import mcp


def converse(*messages) -> list[dict]:
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    out = io.StringIO()
    mcp.serve(io.StringIO("\n".join(lines) + "\n"), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(args: dict, msg_id: int = 1, name: str = "show_media") -> dict:
    params = {"name": name, "arguments": args}
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def test_initialize_handshake():
    replies = converse(
        INIT,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert len(replies) == 2  # the notification gets no reply
    init, ping = replies
    assert init["id"] == 0
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert init["result"]["serverInfo"]["name"] == "mediapane"
    assert ping == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_initialize_unknown_version():
    msg = {**INIT, "params": {**INIT["params"], "protocolVersion": "1999-01-01"}}
    (reply,) = converse(msg)
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list():
    (reply,) = converse({"jsonrpc": "2.0", "id": "a", "method": "tools/list"})
    (tool,) = reply["result"]["tools"]
    assert reply["id"] == "a" and tool["name"] == "show_media"
    schema = tool["inputSchema"]
    assert schema["required"] == ["path"]
    assert set(schema["properties"]) == {"path", "name", "tag", "step"}
    assert all(p["type"] == "string" for p in schema["properties"].values())
    assert "tag" in tool["description"] and "step" in tool["description"]


def test_show_media_success(tmp_path, pane, monkeypatch):
    (tmp_path / "plot.png").write_bytes(b"\x89PNG data")
    monkeypatch.chdir(tmp_path)
    (reply,) = converse(call({"path": "plot.png", "tag": "samples", "step": "100"}))
    result = reply["result"]
    assert result["isError"] is False and "shown in mediapane" in result["content"][0]["text"]
    header, data = pane.received[0]
    assert header["name"] == "plot.png" and data == b"\x89PNG data"
    assert header["tag"] == "samples" and header["step"] == "100"


def test_show_media_custom_name(tmp_path, pane):
    (tmp_path / "v.mp4").write_bytes(b"video")
    (reply,) = converse(call({"path": str(tmp_path / "v.mp4"), "name": "rollout"}))
    assert reply["result"]["isError"] is False
    assert pane.names == ["rollout"] and "tag" not in pane.received[0][0]


def test_show_media_pane_rejects(tmp_path, pane):
    pane.reply = b'{"ok": false, "error": "damaged image"}\n'
    (tmp_path / "bad.png").write_bytes(b"x")
    (reply,) = converse(call({"path": str(tmp_path / "bad.png")}))
    assert reply["result"]["isError"] is True
    assert "damaged image" in reply["result"]["content"][0]["text"]


@pytest.mark.parametrize(
    "args, expect",
    [
        ({"path": "/definitely/not/here.png"}, "no such file"),
        ({"path": "notes.txt"}, "isn't a media file"),
        ({}, "`path` is required"),
        ({"path": "ok.png"}, "mediapane open"),  # no pane running
    ],
)
def test_show_media_errors(args, expect, tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "ok.png").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    (reply,) = converse(call(args))
    result = reply["result"]
    assert result["isError"] is True and expect in result["content"][0]["text"]


def test_unknown_tool_and_method():
    replies = converse(
        call({}, msg_id=1, name="nope"),
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
    )
    assert replies[0]["error"]["code"] == -32602
    assert replies[1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32601, "message": "method not found: resources/list"},
    }


def test_malformed_lines_keep_going():
    replies = converse("{nope", "", "[1, 2]", {"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert replies[0]["error"]["code"] == -32700 and replies[0]["id"] is None
    assert replies[1]["error"]["code"] == -32600
    assert replies[2]["id"] == 9 and "result" in replies[2]


def test_internal_errors_dont_kill_server(monkeypatch):
    def boom(args):
        raise RuntimeError("bug")

    monkeypatch.setattr(mcp, "show_media", boom)
    replies = converse(call({"path": "x"}), {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert replies[0]["error"]["code"] == -32603 and replies[0]["id"] == 1
    assert replies[1]["id"] == 2


def test_subprocess_stdio(tmp_path, pane):
    """`mediapane mcp` end to end: only protocol on stdout, clean exit on EOF."""
    (tmp_path / "a.gif").write_bytes(b"GIF89a")
    lines = [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
             call({"path": "a.gif"}, msg_id=2)]  # fmt: skip
    r = subprocess.run(
        [sys.executable, "-m", "mediapane", "mcp"],
        input="".join(json.dumps(m) + "\n" for m in lines),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )
    assert r.returncode == 0
    replies = [json.loads(line) for line in r.stdout.splitlines()]
    assert [m["id"] for m in replies] == [0, 2]
    assert replies[1]["result"]["isError"] is False
    assert pane.names == ["a.gif"]
