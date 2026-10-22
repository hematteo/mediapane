from __future__ import annotations

import argparse

import mediapane_claude


class FakeAPI:
    def __init__(self) -> None:
        self.commands: dict[str, tuple] = {}

    def command(self, name, help, run, setup=None) -> None:
        self.commands[name] = (help, run, setup)


def test_register_commands():
    api = FakeAPI()
    mediapane_claude.register(api)
    assert set(api.commands) == {"claude", "mcp"}
    _, _, setup = api.commands["claude"]
    p = argparse.ArgumentParser()
    setup(p)
    assert p.parse_args(["hook"]).action == "hook"
    args = p.parse_args(["install", "--project", "x", "--dry-run"])
    assert args.action == "install" and args.dry_run and str(args.project) == "x"


def test_discovered_by_core():
    from mediapane import plugins

    api = plugins.load()
    assert "claude" in api.loaded, api.errors
    assert {"claude", "mcp"} <= set(api.commands)
