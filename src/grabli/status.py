"""A read-only facade over the connected projects: who is working, on what, what is next.

Two sources, neither of which asks an agent to report anything:

- `activity.jsonl`, written by the hooks on every session start, edit, compaction and
  session end. It says when a project was last touched and which files were edited.
- each project's own `.claude/memory/MEMORY.md`, read when the status is shown. It is
  the project's source of truth for "now" and "next"; grabli keeps no second copy.

grabli observes. It starts no agents and assigns no work.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .store import Store

ACTIVE_WITHIN = timedelta(minutes=15)
_DAY = timedelta(hours=24)
_NOW_CHARS = 320
_NEXT_ITEMS = 3
_RECENT_FILES = 8
_MEMORY = Path(".claude") / "memory" / "MEMORY.md"
_SECTION = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
_TODO = re.compile(r"^[ \t]*[-*][ \t]+\[ \][ \t]+(.+?)[ \t]*$", re.M)
_NOW_HEADS = ("current status", "текущий статус", "статус")
_NEXT_HEADS = ("next steps", "что дальше", "дальше", "следующие шаги")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def read_memory(project: Path) -> dict | None:
    """`now` and `next` from the project's MEMORY.md, or None when it has none.

    Projects keep this file in different shapes. A "Current Status" section is used when
    there is one; otherwise the first section, which by convention is the latest.
    """
    path = project / _MEMORY
    try:
        text = path.read_text(encoding="utf-8")
        updated = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except (OSError, UnicodeDecodeError):
        return None
    heads = list(_SECTION.finditer(text))
    sections = [
        (head.group(1), text[head.end() : following.start() if following else len(text)])
        for head, following in zip(heads, [*heads[1:], None], strict=True)
    ]

    def find(names: tuple[str, ...]) -> str | None:
        for title, body in sections:
            if title.strip().lower().startswith(names):
                return body
        return None

    now = find(_NOW_HEADS)
    if now is None and sections:
        title, body = sections[0]
        now = f"{title}. {body}"
    steps = _TODO.findall(find(_NEXT_HEADS) or "")
    return {
        "now": _clip(now or text, _NOW_CHARS),
        "next": [_clip(step, 160) for step in steps[:_NEXT_ITEMS]],
        "updated": updated,
    }


def _events(store: Store) -> list[dict]:
    if not store.activity_path.is_file():
        return []
    events = []
    # One damaged line (a hand edit, a cut-off write) must not take the whole view down.
    for line in store.activity_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
            record["at"] = datetime.fromisoformat(record["ts"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        if record["at"].tzinfo is None:
            record["at"] = record["at"].replace(tzinfo=UTC)
        if isinstance(record.get("root"), str) and isinstance(record.get("event"), str):
            events.append(record)
    return events


def collect(store: Store, now: datetime | None = None) -> list[dict]:
    """One record per project the hooks have seen, most recently active first."""
    now = now or datetime.now(UTC)
    by_root: dict[str, list[dict]] = {}
    for order, event in enumerate(_events(store)):
        event["order"] = order  # timestamps have one-second resolution; the file order breaks ties
        by_root.setdefault(event["root"], []).append(event)
    lessons: dict[str, int] = {}
    for entry in store.load()[0]:
        if entry.project:
            lessons[entry.project] = lessons.get(entry.project, 0) + 1

    projects = []
    for root, events in by_root.items():
        if events[-1]["event"] == "forget":
            continue
        path = Path(root)
        work = [event for event in events if event["event"] not in ("install", "forget")]
        last = work[-1] if work else None
        files: list[str] = []
        for event in reversed(work):
            name = event.get("file")
            if name and name not in files:
                files.append(name)
        today = {e.get("session_id") for e in work if now - e["at"] <= _DAY and e.get("session_id")}
        projects.append(
            {
                "name": path.name,
                "path": root,
                "exists": path.is_dir(),
                # A session that has just ended is recent activity, not ongoing work.
                "active": bool(last)
                and last["event"] != "session-end"
                and now - last["at"] <= ACTIVE_WITHIN,
                "last_activity": last["at"] if last else None,
                "last_event": last["event"] if last else None,
                "sessions_24h": len(today),
                "files": files[:_RECENT_FILES],
                "lessons": lessons.get(path.name, 0),
                "memory": read_memory(path),
                "_order": last["order"] if last else -1,
            }
        )
    projects.sort(key=lambda p: p.pop("_order"), reverse=True)
    return projects


def ago(moment: datetime | None, now: datetime | None = None) -> str:
    if moment is None:
        return "ни разу"
    minutes = int(((now or datetime.now(UTC)) - moment).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 60 * 24:
        return f"{minutes // 60} ч назад"
    return f"{minutes // (60 * 24)} дн назад"


def render(projects: list[dict], detail: bool = False, now: datetime | None = None) -> str:
    if not projects:
        return "Проектов нет. Подключи первый: grabli install <каталог>."
    blocks = []
    for project in projects:
        state = "АКТИВЕН" if project["active"] else "тихо"
        if not project["exists"]:
            state = "каталог не найден"
        head = (
            f"{project['name']}  [{state}]  активность: {ago(project['last_activity'], now)}"
            f" · сессий за сутки: {project['sessions_24h']} · уроков: {project['lessons']}"
        )
        lines = [head]
        memory = project["memory"]
        if memory:
            lines.append(f"  сейчас: {memory['now']}")
            for step in memory["next"]:
                lines.append(f"  дальше: {step}")
            lines.append(f"  (MEMORY.md обновлён {ago(memory['updated'], now)})")
        else:
            lines.append("  сейчас: нет .claude/memory/MEMORY.md, статус взять неоткуда")
        files = project["files"] if detail else project["files"][:3]
        if files:
            lines.append(f"  правки: {', '.join(files)}")
        if detail:
            lines.append(f"  путь: {project['path']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def as_data(projects: list[dict]) -> list[dict]:
    """The same records with datetimes as ISO strings, for the MCP tool."""
    out = []
    for project in projects:
        memory = project["memory"]
        out.append(
            {
                **{k: v for k, v in project.items() if k not in ("memory", "last_activity")},
                "last_activity": project["last_activity"].isoformat()
                if project["last_activity"]
                else None,
                "now": memory["now"] if memory else None,
                "next": memory["next"] if memory else [],
                "memory_updated": memory["updated"].isoformat(timespec="seconds")
                if memory
                else None,
            }
        )
    return out


def find(projects: list[dict], name: str) -> list[dict]:
    """By name, or by path when two projects share a directory name."""
    wanted = name.strip().rstrip("/").lower()
    if "/" in wanted:
        full = str(Path(name).expanduser()).rstrip("/").lower()
        return [
            p for p in projects if p["path"].lower() == full or p["path"].lower().endswith(wanted)
        ]
    exact = [p for p in projects if p["name"].lower() == wanted]
    return exact or [p for p in projects if wanted in p["name"].lower()]
