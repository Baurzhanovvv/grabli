"""`grabli install`: connect a project to the journal.

Writes the five hooks into the project's `.claude/settings.local.json` and
registers the MCP server in Claude Code's local scope. Both are private to
the user, so nothing enters the project's git. Running it again replaces
grabli's own hooks and leaves everything else in the file alone.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_MARK = "grabli hook "
# A hook this command wrote: one plain command ending in `grabli hook <event>`. A compound
# command that merely contains it belongs to the user and is left alone.
_OWN = re.compile(
    r"[^;&|]*\bgrabli hook (session-start|session-end|pre-edit|pre-compact|post-compact)"
    r"( --block)?\s*"
)
_HOOKS: tuple[tuple[str, str, int, str | None], ...] = (
    ("SessionStart", "session-start", 15, None),
    ("PreToolUse", "pre-edit", 15, "Edit|Write|MultiEdit"),
    ("PreCompact", "pre-compact", 15, None),
    ("PostCompact", "post-compact", 30, None),
    ("SessionEnd", "session-end", 15, None),
)


class InstallError(Exception):
    pass


def launcher() -> list[str]:
    """How to start grabli from another directory: the checkout via uv, else the installed tool."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file():
        return ["uv", "run", "--directory", str(root), "grabli"]
    # Installed as a package: hooks run in Claude Code's environment, where a bare `grabli`
    # may not be on PATH (uvx, an inactive venv), so the path is written in full.
    found = shutil.which("grabli")
    if found:
        return [str(Path(found).resolve())]
    script = Path(sys.argv[0]).resolve()
    if script.is_file() and os.access(script, os.X_OK):
        return [str(script)]
    raise InstallError("cannot find the grabli executable to put into the hooks")


def _without_grabli(event: str, groups: object) -> list:
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise InstallError(f"`hooks.{event}` in the settings file is not a list; not touching it")
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        hooks = [
            hook
            for hook in group["hooks"]
            if not (isinstance(hook, dict) and _OWN.fullmatch(str(hook.get("command", ""))))
        ]
        if hooks:
            kept.append({**group, "hooks": hooks})
    return kept


def merge_hooks(settings: dict, command: list[str], block: bool = False) -> dict:
    """Settings with grabli's hooks replaced by the current ones; other content is kept."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("`hooks` in the settings file is not an object")
    merged = dict(hooks)
    for event, name, timeout, matcher in _HOOKS:
        line = shlex.join([*command, "hook", name])
        if block and name == "pre-edit":
            line += " --block"
        group: dict = {"hooks": [{"type": "command", "command": line, "timeout": timeout}]}
        if matcher:
            group = {"matcher": matcher, **group}
        merged[event] = [*_without_grabli(event, hooks.get(event)), group]
    return {**settings, "hooks": merged}


def write_hooks(project: Path, command: list[str], block: bool = False) -> Path:
    path = project / ".claude" / "settings.local.json"
    settings: dict = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InstallError(f"{path} is not valid JSON ({exc}); not touching it") from exc
        if not isinstance(settings, dict):
            raise InstallError(f"{path} does not hold a JSON object; not touching it")
    text = json.dumps(merge_hooks(settings, command, block), indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    # A settings file kept in dotfiles is a symlink: write its target, not over the link.
    target = path.resolve() if path.is_symlink() else path
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        if target.exists():
            shutil.copymode(target, temporary)
        else:
            os.chmod(temporary, 0o644)  # mkstemp gives 0600
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def hooked_elsewhere(project: Path) -> Path | None:
    """A settings file other than `settings.local.json` that already runs grabli's hooks.

    Installing next to it would run every hook twice: double delivery, doubled events.
    """
    for path in (project / ".claude" / "settings.json", Path.home() / ".claude" / "settings.json"):
        try:
            if _MARK in path.read_text(encoding="utf-8"):
                return path
        except (OSError, UnicodeDecodeError):
            continue
    return None


def missing_hooks(path: Path) -> list[str]:
    """Events of ours that a settings file with grabli's hooks does not run."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [name for _, name, _, _ in _HOOKS if f"{_MARK}{name}" not in text]


def tracked_by_git(project: Path, path: Path) -> bool:
    """True when the settings file would show up in the project's git."""
    if shutil.which("git") is None:
        return False
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=project, capture_output=True, text=True
    )
    if inside.returncode != 0:
        return False
    ignored = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=project)
    return ignored.returncode == 1


def mcp_command(command: list[str]) -> list[str]:
    return ["claude", "mcp", "add", "-s", "local", "grabli", "--", *command, "serve"]


def add_mcp(project: Path, command: list[str]) -> str:
    """Register the server; returns "added", "updated" or "manual" (no `claude` on PATH)."""
    if shutil.which("claude") is None:
        return "manual"

    def add() -> tuple[int, str]:
        result = subprocess.run(mcp_command(command), cwd=project, capture_output=True, text=True)
        return result.returncode, (result.stderr + result.stdout).strip()

    code, output = add()
    if code == 0:
        return "added"
    if "already exists" not in output:
        raise InstallError(f"`claude mcp add` failed: {output or f'exit {code}'}")
    # The existing registration may point at an old checkout: replace it, do not trust it.
    subprocess.run(
        ["claude", "mcp", "remove", "-s", "local", "grabli"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    code, output = add()
    if code != 0:
        raise InstallError(f"`claude mcp add` failed: {output or f'exit {code}'}")
    return "updated"
