"""A read-only facade over the connected projects: who is working, on what, what is next.

Two sources, neither of which asks an agent to report anything:

- `activity.jsonl`, written by the hooks on every session start and end, prompt, edit,
  compaction, finished tool call, end of turn and notification. It says whether an agent
  is working or waiting for the user, when a project was last touched and which files
  were edited.
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

WORKING_WITHIN = timedelta(hours=1)  # a turn with no event for this long is taken as dead
_DAY = timedelta(hours=24)
WORKING, WAITING, QUIET = "working", "waiting", "quiet"
# Notification kinds that mean the agent cannot go on without the user.
_NEEDS_USER = {
    "permission_prompt",
    "idle_prompt",
    "agent_needs_input",
    "elicitation_dialog",
    "elicitation_url_dialog",
}
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
        "now": _clip(text if now is None else now, _NOW_CHARS),
        "next": [_clip(step, 160) for step in steps[:_NEXT_ITEMS]],
        "updated": updated,
    }


_NO_HOME = {"pre-edit", "tool", "install", "forget"}  # say nothing of where a session lives


def _advance(session: dict, event: dict) -> None:
    """Move one session's state by one event.

    `stop` ends a turn, so the agent waits for the user; so does a fresh session and a
    permission or idle notification. A prompt, an edit or a finished tool call means a turn
    is running. A compaction and an informational notification change nothing: a session
    compacted by hand while it waited still waits.
    """
    name, before = event["event"], session.get("state")
    state = before
    needs_user = name == "notification" and event.get("notification") in _NEEDS_USER
    opened = name == "session-start" and event.get("source") != "compact"
    if name == "session-end":
        state = QUIET
    elif name == "stop" or needs_user or opened:
        state = WAITING
    elif name in ("prompt", "pre-edit", "tool") or before is None:
        state = WORKING
    if state == WAITING and before != WAITING:
        session["since"] = event["at"]  # the first moment of this wait, not its last reminder
    session["state"], session["last"] = state, event["at"]


def _expired(session: dict, now: datetime) -> str:
    age = now - session["last"]
    if age > _DAY or (session["state"] == WORKING and age > WORKING_WITHIN):
        return QUIET  # a session killed without a SessionEnd, or a turn that died silently
    return session["state"]


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
        if not (isinstance(record.get("root"), str) and isinstance(record.get("event"), str)):
            continue
        if _scalar(record.get("session_id")) and _scalar(record.get("notification")):
            events.append(record)
    return events


def _scalar(value: object) -> bool:
    return value is None or isinstance(value, str)


def stale_hooks(project: Path) -> list[str] | None:
    """grabli hooks the project's settings lack; None when no settings file runs grabli."""
    from .install import hooked_in, missing_hooks  # install pulls in subprocess; keep it lazy

    path = hooked_in(project)
    return None if path is None else missing_hooks(path)


def collect(store: Store, now: datetime | None = None) -> list[dict]:
    """One record per project the hooks have seen, most recently active first."""
    now = now or datetime.now(UTC)
    events = _events(store)
    # A session belongs to the project it runs in, wherever its edits land: an edit of a file
    # in another repository must not leave a "working" session behind in that repository.
    home: dict[str, str] = {}
    for event in events:
        session_id = event.get("session_id")
        if session_id and event["event"] not in _NO_HOME:
            home.setdefault(session_id, event["root"])
    by_root: dict[str, list[dict]] = {}
    for order, event in enumerate(events):
        event["order"] = order  # timestamps have one-second resolution; the file order breaks ties
        root = home.get(event.get("session_id") or "", event["root"])
        by_root.setdefault(root, []).append(event)
    lessons: dict[str, int] = {}
    for entry in store.load()[0]:
        if entry.project:
            lessons[entry.project] = lessons.get(entry.project, 0) + 1

    projects = []
    for root, history in by_root.items():
        if history[-1]["event"] == "forget":
            continue
        path = Path(root)
        work = [event for event in history if event["event"] not in ("install", "forget")]
        last = work[-1] if work else None
        files: list[str] = []
        sessions: dict[str, dict] = {}
        for event in work:
            if event.get("session_id"):
                _advance(sessions.setdefault(event["session_id"], {}), event)
        for event in reversed(work):
            name = event.get("file")
            if name and name not in files:
                files.append(name)
        today = {e.get("session_id") for e in work if now - e["at"] <= _DAY and e.get("session_id")}
        states = [(_expired(session, now), session) for session in sessions.values()]
        waiting = [session["since"] for state, session in states if state == WAITING]
        working = sum(state == WORKING for state, _ in states)
        projects.append(
            {
                "name": path.name,
                "path": root,
                "exists": path.is_dir(),
                "state": WAITING if waiting else (WORKING if working else QUIET),
                "waiting_since": min(waiting) if waiting else None,
                "sessions_waiting": len(waiting),
                "sessions_working": working,
                "last_activity": last["at"] if last else None,
                "last_event": last["event"] if last else None,
                "sessions_24h": len(today),
                "files": files[:_RECENT_FILES],
                "lessons": lessons.get(path.name, 0),
                "stale_hooks": stale_hooks(path),
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
        state = "тихо"
        if project["state"] == WORKING:
            state = "РАБОТАЕТ"
        elif project["state"] == WAITING:
            since = ago(project["waiting_since"], now).removesuffix(" назад")
            state = f"ЖДЁТ ТЕБЯ, {since}"
            if project["sessions_working"]:
                state += f"; ещё работает: {project['sessions_working']}"
        if not project["exists"]:
            state = "каталог не найден"
        head = (
            f"{project['name']}  [{state}]  активность: {ago(project['last_activity'], now)}"
            f" · сессий за сутки: {project['sessions_24h']} · уроков: {project['lessons']}"
        )
        lines = [head]
        memory = project["memory"]
        if memory:
            lines.append(f"  сейчас: {memory['now'] or 'раздел статуса пуст'}")
            for step in memory["next"]:
                lines.append(f"  дальше: {step}")
            lines.append(f"  (MEMORY.md обновлён {ago(memory['updated'], now)})")
        else:
            lines.append("  сейчас: нет .claude/memory/MEMORY.md, статус взять неоткуда")
        if project["stale_hooks"] is None and project["exists"]:
            lines.append("  внимание: хуки grabli не найдены, состояние не обновляется")
        elif project["stale_hooks"]:
            lines.append(
                f"  внимание: нет хуков {', '.join(project['stale_hooks'])}, состояние неточное;"
                " выполни grabli install в проекте"
            )
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
                **{
                    k: v
                    for k, v in project.items()
                    if k not in ("memory", "last_activity", "waiting_since")
                },
                "waiting_since": project["waiting_since"].isoformat()
                if project["waiting_since"]
                else None,
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
            p
            for p in projects
            if p["path"].lower() == full or p["path"].lower().endswith("/" + wanted.lstrip("/"))
        ]
    exact = [p for p in projects if p["name"].lower() == wanted]
    return exact or [p for p in projects if wanted in p["name"].lower()]
