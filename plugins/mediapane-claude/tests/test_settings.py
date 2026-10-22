from __future__ import annotations

import json
from pathlib import Path

import pytest

from mediapane_claude import settings

CMD = "/opt/bin/mediapane claude hook"
OURS = {"matcher": settings.MATCHER, "hooks": [{"type": "command", "command": CMD}]}
OTHER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo done"}]}
EXISTING = {
    "model": "opus",
    "env": {"FOO": "1"},
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [OTHER_HOOK],
        "PostToolUse": [OTHER_HOOK],
    },
}


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


def test_install_creates_missing_file(tmp_path, capsys):
    path = tmp_path / "proj" / ".claude" / "settings.json"
    assert settings.install(path, command=CMD) == 0
    assert read(path) == {"hooks": {"PostToolUse": [OURS]}}
    assert backups(path) == []
    assert "added" in capsys.readouterr().out


def test_install_preserves_and_is_idempotent(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(EXISTING))  # compact: formatting needn't survive, content must
    assert settings.install(path, command=CMD) == 0
    after = read(path)
    assert after == {**EXISTING, "hooks": {**EXISTING["hooks"], "PostToolUse": [OTHER_HOOK, OURS]}}
    assert len(backups(path)) == 1 and json.loads(backups(path)[0].read_text()) == EXISTING
    capsys.readouterr()

    assert settings.install(path, command=CMD) == 0
    assert read(path) == after
    assert len(backups(path)) == 1  # nothing changed, nothing backed up
    assert "already installed" in capsys.readouterr().out


def test_install_replaces_stale_command(tmp_path):
    path = tmp_path / "settings.json"
    stale = {"matcher": "Write", "hooks": [{"type": "command", "command": "mediapane claude hook"}]}
    path.write_text(json.dumps({"hooks": {"PostToolUse": [stale, OTHER_HOOK]}}))
    settings.install(path, command=CMD)
    assert read(path)["hooks"]["PostToolUse"] == [OTHER_HOOK, OURS]


def test_uninstall_removes_only_ours(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(EXISTING))
    settings.install(path, command=CMD)
    assert settings.uninstall(path) == 0
    assert read(path) == EXISTING
    capsys.readouterr()
    assert settings.uninstall(path) == 0
    assert "not installed" in capsys.readouterr().out
    assert read(path) == EXISTING


def test_uninstall_shared_group_and_empty_cleanup(tmp_path):
    path = tmp_path / "settings.json"
    shared = {
        "matcher": "Write",
        "hooks": [{"type": "command", "command": "'/a b/mp' claude hook"}, OTHER_HOOK["hooks"][0]],
    }
    path.write_text(json.dumps({"hooks": {"PostToolUse": [shared]}}))
    settings.uninstall(path)
    assert read(path) == {"hooks": {"PostToolUse": [{**shared, "hooks": shared["hooks"][1:]}]}}

    path.write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [OURS]}}))
    settings.uninstall(path)
    assert read(path) == {"model": "x"}


def test_uninstall_missing_file(tmp_path, capsys):
    path = tmp_path / "nope" / "settings.json"
    assert settings.uninstall(path) == 0
    assert not path.exists()


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(EXISTING))
    assert settings.install(path, dry_run=True, command=CMD) == 0
    assert read(path) == EXISTING and backups(path) == []
    out = capsys.readouterr().out
    assert CMD in out and "FOO" not in out  # only the hooks section is shown


@pytest.mark.parametrize("text", ["{not json", "[1]", '{"hooks": {"PostToolUse": {}}}'])
def test_refuses_malformed(text, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(text)
    assert settings.install(path, command=CMD) == 1
    assert path.read_text() == text and backups(path) == []


def test_is_ours():
    assert settings.is_ours("mediapane claude hook")
    assert settings.is_ours("/Users/x/.local/bin/mediapane claude hook")
    assert settings.is_ours("'/path with space/mp' claude hook")
    assert not settings.is_ours("echo mediapane claude hook now")
    assert not settings.is_ours("other claude hook")
    assert not settings.is_ours(None)


def test_hook_command_is_runnable():
    cmd = settings.hook_command()
    assert settings.is_ours(cmd)


def test_settings_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert settings.settings_path() == tmp_path / "cfg" / "settings.json"
    assert settings.settings_path(tmp_path) == tmp_path / ".claude" / "settings.json"
    assert settings.settings_path(tmp_path, tmp_path / "s.json") == tmp_path / "s.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert settings.settings_path() == Path.home() / ".claude" / "settings.json"


def test_cli_install_uninstall(tmp_path, capsys):
    from mediapane import cli

    path = tmp_path / "s.json"
    assert cli.main(["claude", "install", "--settings", str(path)]) == 0
    (group,) = read(path)["hooks"]["PostToolUse"]
    assert group["matcher"] == settings.MATCHER and settings.is_ours(group["hooks"][0]["command"])
    assert cli.main(["claude", "uninstall", "--settings", str(path)]) == 0
    assert read(path) == {}

    project = tmp_path / "proj"
    assert cli.main(["claude", "install", "--project", str(project), "--dry-run"]) == 0
    assert not (project / ".claude").exists()


def test_new_command_form_is_ours():
    assert settings.is_ours("/opt/py/bin/python -m mediapane_claude.hook")
    assert settings.is_ours("'/a b/python3' -m mediapane_claude.hook")


def test_symlinked_settings_stay_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"theme": "dark"}\n')
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    assert settings.install(link, command=CMD) == 0
    assert link.is_symlink()
    assert json.loads(real.read_text())["theme"] == "dark"
    assert "PostToolUse" in json.loads(real.read_text())["hooks"]
