"""Decide which entries are relevant to a repository or to a file being edited."""

from __future__ import annotations

import json
from pathlib import Path

import pathspec

from .models import Entry

_SKIP_DIRS = {"node_modules", "dist", "build", "venv", "__pycache__"}
_BODY_CHARS = 700

_NODE_PACKAGES = {
    "@nestjs/core": "nestjs",
    "next": "nextjs",
    "react": "react",
    "react-native": "react-native",
    "@mui/material": "mui",
    "typeorm": "typeorm",
    "prisma": "prisma",
    "@prisma/client": "prisma",
    "pg": "postgres",
    "bullmq": "bullmq",
    "ioredis": "redis",
    "redis": "redis",
    "minio": "minio",
    "typescript": "typescript",
}
_PYTHON_PACKAGES = ("fastapi", "django", "sqlalchemy", "celery", "pydantic")
_PYTHON_MANIFESTS = ("pyproject.toml", "requirements.txt")
_DOCKER_FILES = ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml")
DETECTABLE_TAGS = {*_NODE_PACKAGES.values(), *_PYTHON_PACKAGES, "python", "docker", "godot"}


def repo_root(start: Path) -> Path:
    """Nearest ancestor holding .git, or the starting directory itself."""
    start = start if start.is_dir() else start.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def _scan_dirs(root: Path) -> list[Path]:
    """The root and its direct children, which covers backend/ + frontend/ layouts."""
    children = [
        child
        for child in sorted(root.iterdir())
        if child.is_dir() and not child.name.startswith(".") and child.name not in _SKIP_DIRS
    ]
    return [root, *children]


def detect_stack(root: Path) -> set[str]:
    stack: set[str] = set()
    if not root.is_dir():
        return stack
    for directory in _scan_dirs(root):
        package_json = directory / "package.json"
        if package_json.is_file():
            stack |= _node_stack(package_json)
        for manifest in _PYTHON_MANIFESTS:
            path = directory / manifest
            if path.is_file():
                stack.add("python")
                text = path.read_text(encoding="utf-8", errors="replace").lower()
                stack |= {name for name in _PYTHON_PACKAGES if name in text}
        if any((directory / name).is_file() for name in _DOCKER_FILES):
            stack.add("docker")
        if (directory / "project.godot").is_file():
            stack.add("godot")
    return stack


def _node_stack(package_json: Path) -> set[str]:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    names: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict):
            names |= set(deps)
    return {tag for package, tag in _NODE_PACKAGES.items() if package in names}


def _newest_first(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: e.last_verified, reverse=True)


def for_session(entries: list[Entry], stack: set[str], limit: int = 3) -> list[Entry]:
    """Active entries sharing the repo's stack, most overlap first, then most recently verified."""
    scored = [
        (len(stack & set(entry.stack)), entry)
        for entry in _newest_first(entries)
        if entry.superseded_by is None
    ]
    relevant = [pair for pair in scored if pair[0] > 0]
    relevant.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in relevant[:limit]]


def for_file(
    entries: list[Entry], root: Path, file_path: Path, stack: set[str], limit: int = 3
) -> list[Entry]:
    """Active entries whose `paths` globs match the file, relative to the repo root."""
    try:
        relative = file_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return []
    matched: list[Entry] = []
    for entry in _newest_first(entries):
        if entry.superseded_by is not None or not entry.paths:
            continue
        # Only tags the detector can recognise may exclude an entry: "go" or
        # "postgresql" never appear in a detected stack, so they must not hide it.
        known = set(entry.stack) & DETECTABLE_TAGS
        if known and stack and not stack & known:
            continue
        try:
            spec = pathspec.PathSpec.from_lines("gitignore", entry.paths)
        except Exception:  # noqa: BLE001, S112 - a hand-edited bad glob must not silence other entries
            continue
        if spec.match_file(relative):
            matched.append(entry)
    return matched[:limit]


def render(entries: list[Entry], reason: str) -> str:
    """Text a hook puts into the agent's context."""
    blocks = [f"[grabli] Записи из журнала прошлых ошибок и решений, {reason}:"]
    for entry in entries:
        body = entry.body if len(entry.body) <= _BODY_CHARS else entry.body[: _BODY_CHARS - 1] + "…"
        origin = f", проект {entry.project}" if entry.project else ""
        blocks.append(
            f"### {entry.kind}: {entry.title}\n"
            f"({entry.id}{origin}, проверено {entry.last_verified.isoformat()})\n\n{body}"
        )
    blocks.append("Полный текст записи: инструмент grabli `search` с параметром `id`.")
    return "\n\n".join(blocks)
