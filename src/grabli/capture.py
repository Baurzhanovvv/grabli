"""Capture lessons when Claude Code compacts the context.

The model gets no turn at compaction time, so it cannot call `propose`.
Instead `pre-compact` asks the summarizer to append a block of lessons to
the summary (Claude Code uses the hook's stdout as summary instructions),
and `post-compact` turns that block into entries: straight into the journal
in auto mode (marked `reviewed: false`), into the queue in review mode.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .context import repo_root
from .models import title_key
from .store import Store

MAX_LESSONS = 3
_TITLE_LIMIT = 200

INSTRUCTIONS = f"""\
В самом конце summary добавь блок уроков этой сессии для журнала grabli, строго в формате:

<grabli-lessons>
### mistake: короткий заголовок ошибки
stack: nestjs, postgres
paths: **/reports/**
Что случилось и почему не поймали.
Как делать: что именно делать в следующий раз.

### decision: короткий заголовок решения
Что выбрали и почему.
Как применять: когда и как этим пользоваться.
</grabli-lessons>

Правила блока:
- mistake — реальная ошибка из этой сессии: её нашло ревью, упавший тест, откат или пользователь.
- decision — решение с причиной, которое пригодится в других проектах.
- Не больше {MAX_LESSONS} записей. Строки stack и paths необязательны.
- Каждая запись должна быть понятна без контекста этой сессии.
- Вывод начинай словами «Как делать:» или «Как применять:»: при показе записи он идёт первым.
- Никаких ключей, паролей и токенов.
- Только то, что появилось после предыдущего сжатия: блок из прошлого summary не повторяй.
- Если уроков нет, блок не добавляй.
"""

_OPEN = re.compile(r"^[ \t]*<grabli-lessons>[ \t]*$", re.I)
_CLOSE = re.compile(r"^[ \t]*</grabli-lessons>[ \t]*$", re.I)
_HEAD = re.compile(r"^[ \t]*#{1,6}[ \t]*(mistake|decision)[ \t]*:[ \t]*(.+?)[ \t]*$", re.I)
_FIELD = re.compile(r"^\s*(?:[-*]\s+)?\**(stack|paths)\**\s*:\**\s*(.*)$", re.I)
_TAG = re.compile(r"[\w.+#-]{1,30}")
# The example from INSTRUCTIONS, retold by a summary, is not a lesson.
_PLACEHOLDERS = {"короткий заголовок ошибки", "короткий заголовок решения"}


def _blocks(summary: str) -> list[list[str]]:
    """Line groups between the tags. Tags count only alone on a line, so prose that
    mentions the tag opens nothing; only the last block may lack its closing tag."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in summary.split("\n"):
        if _OPEN.match(line):
            current = []  # an earlier block that was never closed is dropped
        elif current is not None and _CLOSE.match(line):
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _items(value: str) -> list[str]:
    """Comma-separated values; commas inside `{}` and brackets inside a glob are kept."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        depth, closes_at = 0, -1
        for position, char in enumerate(value):
            depth += (char == "[") - (char == "]")
            if depth == 0:
                closes_at = position
                break
        if closes_at == len(value) - 1:  # the first bracket closes at the very end: a YAML list
            value = value[1:-1]
    items: list[str] = []
    depth, item = 0, ""
    for char in value + ",":
        if char == "," and depth == 0:
            if item.strip(" \t`'\""):
                items.append(item.strip(" \t`'\""))
            item = ""
            continue
        depth += (char == "{") - (char == "}")
        item += char
    return items


def _field(line: str) -> tuple[str, list[str]] | None:
    """A `stack:` / `paths:` line, unless it reads as prose that merely starts that way."""
    match = _FIELD.match(line)
    if not match:
        return None
    name, items = match.group(1).lower(), _items(match.group(2))
    if name == "stack" and not all(_TAG.fullmatch(item) for item in items):
        return None
    if name == "paths" and any(" " in item for item in items):
        return None
    return name, items


def parse_lessons(summary: str) -> list[dict]:
    lessons: list[dict] = []
    for block in _blocks(summary):
        sections: list[tuple[str, str, list[str]]] = []
        fenced = False
        for line in block:
            if line.lstrip().startswith("```"):
                fenced = not fenced
            head = None if fenced else _HEAD.match(line)
            if head:
                sections.append((head.group(1).lower(), head.group(2).strip("*` "), []))
            elif sections:
                sections[-1][2].append(line)
        for kind, title, lines in sections:
            if _key(title) in _PLACEHOLDERS:
                continue
            lesson: dict = {"kind": kind, "title": title[:_TITLE_LIMIT], "stack": [], "paths": []}
            while lines:
                field = _field(lines[0])
                if field:
                    lesson[field[0]] = field[1]
                elif lines[0].strip():
                    break
                lines.pop(0)
            lesson["body"] = "\n".join(lines).strip()
            lessons.append(lesson)
    return lessons


_key = title_key


def _seen(store: Store) -> set[str]:
    """Titles already handled: in the journal, in the queue, or queued by an earlier
    compaction. The last covers drafts the human rejected, which leave no file but
    come back in every later summary of the session."""
    seen = {_key(e.title) for pending in (False, True) for e in store.load(pending=pending)[0]}
    if store.log_path.is_file():
        for line in store.log_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("event") == "compact_capture":
                seen.update(record.get("titles") or [])
    return seen


def pre_compact(payload: dict, store: Store) -> str:
    store.log("pre_compact", session_id=payload.get("session_id"), trigger=payload.get("trigger"))
    return INSTRUCTIONS


def post_compact(payload: dict, store: Store) -> None:
    from .server import do_propose  # the MCP SDK is slow to import; other hooks must not pay

    lessons = parse_lessons(payload.get("compact_summary") or "")
    project = repo_root(Path(payload["cwd"])).name if payload.get("cwd") else None
    seen = _seen(store)
    # Duplicates are dropped before the cap, or carried-over lessons would use up its slots.
    fresh: list[dict] = []
    for lesson in lessons:
        if _key(lesson["title"]) not in seen:
            seen.add(_key(lesson["title"]))
            fresh.append(lesson)
    ids: list[str] = []
    titles: list[str] = []
    rejected: list[str] = []
    duplicates = len(lessons) - len(fresh)
    for lesson in fresh[:MAX_LESSONS]:
        try:
            result = do_propose(
                store,
                lesson["kind"],
                lesson["title"],
                lesson["body"],
                project=project,
                stack=lesson["stack"],
                paths=lesson["paths"],
                repo=project,
                found_by="compact",
                via="compact",
            )
        except Exception as exc:  # noqa: BLE001 - one bad lesson must not lose the others
            rejected.append(f"{type(exc).__name__}: {exc}")
            continue
        if result["status"] in ("pending", "saved"):
            ids.append(result["id"])
            titles.append(_key(lesson["title"]))
        elif result["status"] == "exists":  # proposed by someone else since `_seen` was read
            duplicates += 1
        else:
            rejected.append(result.get("reason", result["status"]))
    # Logged even when nothing was found: shows how often the summarizer follows the format.
    store.log(
        "compact_capture",
        session_id=payload.get("session_id"),
        trigger=payload.get("trigger"),
        repo=project,
        found=len(lessons),
        ids=ids,
        titles=titles,
        duplicates=duplicates,
        rejected=rejected,
    )
