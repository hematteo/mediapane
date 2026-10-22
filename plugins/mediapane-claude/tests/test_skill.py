from __future__ import annotations

import json
import re
from pathlib import Path

from mediapane.cli import main as cli_main

from mediapane_claude import skill

BUNDLE = Path(__file__).resolve().parents[1] / "claude-code"


def test_bundle_is_consistent():
    plugin = json.loads((BUNDLE / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "mediapane"
    hooks = json.loads((BUNDLE / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
    from mediapane_claude.settings import MATCHER

    assert hooks[0]["matcher"] == MATCHER  # same tools as `mediapane claude install`
    assert "mediapane claude hook" in hooks[0]["hooks"][0]["command"]
    market = json.loads((BUNDLE.parents[2] / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = market["plugins"]
    assert (BUNDLE.parents[2] / entry["source"]).resolve() == BUNDLE.resolve()


def test_skill_frontmatter_and_size():
    text = skill.text()
    front = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert front and "name: mediapane" in front.group(1)
    (desc,) = re.findall(r"^description: (.+)$", front.group(1), re.M)
    assert len(desc) < 250  # it sits in Claude's context every turn: keep it one short line
    assert len(text.splitlines()) < 60


def test_install_and_uninstall_the_skill(tmp_path, capsys):
    settings = tmp_path / ".claude" / "settings.json"
    assert cli_main(["claude", "install", "--settings", str(settings)]) == 0
    installed = tmp_path / ".claude" / "skills" / "mediapane" / "SKILL.md"
    assert installed.read_text() == skill.text()
    assert cli_main(["claude", "install", "--settings", str(settings)]) == 0  # idempotent
    assert "skill already installed" in capsys.readouterr().out
    assert cli_main(["claude", "uninstall", "--settings", str(settings)]) == 0
    assert not installed.parent.exists()


def test_someone_elses_skill_is_left_alone(tmp_path):
    other = tmp_path / "skills" / "mediapane" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("---\nname: something-else\n---\nmine\n")
    assert "left" in skill.install(tmp_path / "skills")
    assert "left" in skill.uninstall(tmp_path / "skills")
    assert other.read_text().endswith("mine\n")


def test_no_skill_flag(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    assert cli_main(["claude", "install", "--no-skill", "--settings", str(settings)]) == 0
    assert not (tmp_path / ".claude" / "skills").exists()
