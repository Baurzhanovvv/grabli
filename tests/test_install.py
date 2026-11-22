"""`grabli install`: hooks in settings.local.json and the MCP registration."""

from __future__ import annotations

import json
import subprocess

import pytest

from grabli import cli, install

COMMAND = ["uv", "run", "--directory", "/opt/my tools/grabli", "grabli"]


def commands(settings: dict) -> dict[str, list[str]]:
    return {
        event: [hook["command"] for group in groups for hook in group["hooks"]]
        for event, groups in settings["hooks"].items()
    }


def test_merge_adds_the_hooks_and_quotes_paths_with_spaces():
    merged = install.merge_hooks({}, COMMAND)
    assert set(merged["hooks"]) == {
        "SessionStart",
        "SessionEnd",
        "PreToolUse",
        "PreCompact",
        "PostCompact",
        "UserPromptSubmit",
        "Stop",
        "Notification",
        "PostToolUse",
    }
    assert merged["hooks"]["PreToolUse"][0]["matcher"] == "Edit|Write|MultiEdit"
    assert commands(merged)["PreCompact"] == [
        "uv run --directory '/opt/my tools/grabli' grabli hook pre-compact"
    ]
    assert commands(install.merge_hooks({}, COMMAND, block=True))["PreToolUse"][0].endswith(
        "hook pre-edit --block"
    )


def test_merge_keeps_foreign_content_and_replaces_only_its_own_hooks():
    foreign = {"hooks": [{"type": "command", "command": "echo other"}]}
    mixed = {
        "matcher": "Bash",
        "hooks": [
            {"type": "command", "command": "lint.sh"},
            {"type": "command", "command": "uv run --directory /old grabli hook pre-edit"},
        ],
    }
    settings = {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {"Stop": [foreign], "PreToolUse": [mixed], "SessionStart": [foreign]},
    }

    once = install.merge_hooks(settings, COMMAND)
    twice = install.merge_hooks(once, COMMAND, block=True)

    assert twice["permissions"] == settings["permissions"]
    assert twice["hooks"]["Stop"][0] == foreign  # kept, ours added after it, once
    assert len(twice["hooks"]["Stop"]) == 2
    assert commands(twice)["SessionStart"][0] == "echo other"
    assert commands(twice)["PreToolUse"][0] == "lint.sh"  # the old grabli hook is gone
    assert [c for c in commands(twice)["PreToolUse"] if "grabli hook" in c] == [
        "uv run --directory '/opt/my tools/grabli' grabli hook pre-edit --block"
    ]
    assert settings["hooks"]["PreToolUse"] == [mixed]  # the input is not mutated


def test_write_creates_the_file_and_refuses_to_overwrite_broken_json(tmp_path):
    path = install.write_hooks(tmp_path, COMMAND)
    assert path == tmp_path / ".claude" / "settings.local.json"
    assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]) == 9
    assert [p.name for p in path.parent.iterdir()] == ["settings.local.json"]  # no temp left

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(install.InstallError, match="not valid JSON"):
        install.write_hooks(tmp_path, COMMAND)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_add_mcp_reports_added_exists_manual_and_failure(tmp_path, monkeypatch):
    calls = []

    def fake_run(returncode, stderr=""):
        def run(args, **kwargs):
            calls.append((args, kwargs["cwd"]))
            return subprocess.CompletedProcess(args, returncode, "", stderr)

        return run

    monkeypatch.setattr(install.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(install.subprocess, "run", fake_run(0))
    assert install.add_mcp(tmp_path, COMMAND) == "added"
    assert calls[0] == (
        ["claude", "mcp", "add", "-s", "local", "grabli", "--", *COMMAND, "serve"],
        tmp_path,
    )

    # An existing registration may point at an old checkout: it is replaced, not trusted.
    calls.clear()
    answers = iter([(1, "MCP server grabli already exists"), (0, ""), (0, "")])

    def replay(args, **kwargs):
        calls.append((args, kwargs["cwd"]))
        code, stderr = next(answers)
        return subprocess.CompletedProcess(args, code, "", stderr)

    monkeypatch.setattr(install.subprocess, "run", replay)
    assert install.add_mcp(tmp_path, COMMAND) == "updated"
    assert [call[0][2] for call in calls] == ["add", "remove", "add"]

    monkeypatch.setattr(install.subprocess, "run", fake_run(1, "boom"))
    with pytest.raises(install.InstallError, match="boom"):
        install.add_mcp(tmp_path, COMMAND)

    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert install.add_mcp(tmp_path, COMMAND) == "manual"


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path, monkeypatch):
    """The user's own ~/.claude/settings.json must not decide what these tests see."""
    monkeypatch.setattr(install.Path, "home", lambda: tmp_path / "no-home")
    monkeypatch.setattr(install, "launcher", lambda: COMMAND)


def test_cli_install_writes_hooks_and_prints_manual_mcp_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)  # no claude, no git

    assert cli.main(["install", str(tmp_path)]) == 0

    out, err = capsys.readouterr()
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text("utf-8"))
    assert "grabli hook post-compact" in commands(settings)["PostCompact"][0]
    assert "settings.local.json" in out
    assert "claude mcp add -s local grabli --" in err


def test_cli_install_no_mcp_and_missing_directory(tmp_path, monkeypatch, capsys):
    def no_subprocess(*args, **kwargs):
        raise AssertionError("claude must not be called with --no-mcp")

    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install, "add_mcp", no_subprocess)
    assert cli.main(["install", str(tmp_path), "--no-mcp"]) == 0
    assert cli.main(["install", str(tmp_path / "nope")]) == 1
    assert "not a directory" in capsys.readouterr().err


def test_tracked_by_git_distinguishes_ignored_from_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")  # a global excludes file may ignore it
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # ~/.config/git/ignore
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = install.write_hooks(tmp_path, COMMAND)
    assert install.tracked_by_git(tmp_path, path) is True
    (tmp_path / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    assert install.tracked_by_git(tmp_path, path) is False


# --- review of the install command ------------------------------------------


def test_compound_command_and_malformed_event_are_not_destroyed():
    compound = {"type": "command", "command": "./lint.sh && uv run grabli hook pre-edit"}
    merged = install.merge_hooks({"hooks": {"PreToolUse": [{"hooks": [compound]}]}}, COMMAND)
    assert compound in merged["hooks"]["PreToolUse"][0]["hooks"]

    with pytest.raises(install.InstallError, match="hooks.PreToolUse"):
        install.merge_hooks({"hooks": {"PreToolUse": {"matcher": "Bash", "hooks": []}}}, COMMAND)


def test_write_follows_a_symlink_keeps_mode_and_reports_bad_encoding(tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "app.json"
    real.write_text('{"permissions": {}}', encoding="utf-8")
    real.chmod(0o640)
    link = tmp_path / "app" / ".claude" / "settings.local.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    install.write_hooks(tmp_path / "app", COMMAND)

    assert link.is_symlink()
    assert set(json.loads(real.read_text(encoding="utf-8"))) == {"permissions", "hooks"}
    assert real.stat().st_mode & 0o777 == 0o640

    real.write_bytes('{"имя": 1}'.encode("utf-16"))
    with pytest.raises(install.InstallError, match="not touching it"):
        install.write_hooks(tmp_path / "app", COMMAND)


def test_cli_skips_hooks_already_connected_in_project_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    shared = tmp_path / ".claude" / "settings.json"
    shared.parent.mkdir()
    shared.write_text('{"hooks": {"x": "uv run grabli hook pre-edit"}}', encoding="utf-8")

    assert cli.main(["install", str(tmp_path), "--no-mcp"]) == 0

    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert "второй раз не ставлю" in capsys.readouterr().out


def test_cli_installs_into_the_repo_root_when_given_a_subdirectory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    (tmp_path / ".git").mkdir()
    (tmp_path / "backend").mkdir()

    assert cli.main(["install", str(tmp_path / "backend"), "--no-mcp"]) == 0

    assert (tmp_path / ".claude" / "settings.local.json").is_file()
    assert not (tmp_path / "backend" / ".claude").exists()


def test_launcher_writes_a_full_path_when_installed_as_a_package(tmp_path, monkeypatch):
    monkeypatch.undo()  # the real launcher, not the fixture's stand-in
    monkeypatch.setattr(install, "__file__", str(tmp_path / "a" / "b" / "install.py"))
    tool = tmp_path / "bin" / "grabli"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(install.shutil, "which", lambda name: str(tool))
    assert install.launcher() == [str(tool.resolve())]

    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install.sys, "argv", [str(tmp_path / "missing")])
    with pytest.raises(install.InstallError, match="cannot find"):
        install.launcher()


def test_tests_never_see_the_real_home(tmp_path):
    from grabli.store import home

    assert home() == tmp_path / "home"
