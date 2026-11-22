"""Claude Code hooks: deliver relevant entries without waiting for the agent to ask.

By default both hooks only add context and never set a permission decision,
so the edit goes through the user's normal permission flow. Claude Code
delivers that context alongside the tool result, i.e. after the first edit.

`pre-edit --block` instead refuses the edit once, with the entries as the
reason, so the agent reads them before it writes. The retry passes because
an entry is delivered at most once per session.

`pre-compact` and `post-compact` go the other way: they capture lessons
from a session whose context is about to be summarized (see `capture`).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .capture import post_compact, pre_compact
from .context import (
    SESSION_FULL,
    SESSION_LISTED,
    detect_stack,
    for_file,
    for_session,
    project_root,
    render,
    repo_root,
)
from .store import Store


def session_start(payload: dict, store: Store) -> str | None:
    root = repo_root(Path(payload["cwd"]))
    stack = detect_stack(root)
    entries, _ = store.load()
    relevant = for_session(entries, stack, limit=None)
    chosen = relevant[:SESSION_FULL]
    listed = relevant[SESSION_FULL : SESSION_FULL + SESSION_LISTED]
    store.log(
        "session_start",
        session_id=payload.get("session_id"),
        source=payload.get("source"),
        repo=root.name,
        stack=sorted(stack),
        ids=[entry.id for entry in chosen],
        # Named by title only: not "shown", so pre-edit can still deliver them in full.
        listed=[entry.id for entry in listed],
    )
    if not chosen:
        return None
    return render(
        chosen, f"подобранные по стеку этого репозитория ({', '.join(sorted(stack))})", listed
    )


def pre_edit(payload: dict, store: Store, block: bool = False) -> str | None:
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return None
    path = Path(file_path)
    root = repo_root(path)
    if not (root / ".git").exists() and payload.get("cwd"):
        root = Path(payload["cwd"])  # not a git repo: globs are relative to the session directory
    entries, _ = store.load()
    session_id = payload.get("session_id") or ""
    already = store.shown_ids(session_id)
    chosen = [e for e in for_file(entries, root, path, detect_stack(root)) if e.id not in already]
    if not chosen:
        return None
    store.log(
        "pre_edit",
        session_id=session_id,
        repo=root.name,
        file=path.name,
        mode="block" if block else "context",
        ids=[entry.id for entry in chosen],
    )
    if block:
        return render(
            chosen,
            f"относящиеся к файлу {path.name}. Правка отклонена один раз, чтобы ты прочитал их "
            "до изменения файла. Учти их и повтори правку",
        )
    return render(chosen, f"относящиеся к файлу {path.name}. Учти их в этой правке")


def session_end(payload: dict, store: Store) -> None:
    """Nothing to deliver: the event exists for the activity record written in `run`."""


def _record_activity(event: str, payload: dict, store: Store) -> None:
    """One line per hook call for `grabli status`: which project, which session, which file."""
    file_path = (payload.get("tool_input") or {}).get("file_path")
    start = Path(file_path) if file_path else Path(payload.get("cwd") or "")
    if not start.is_absolute():
        return
    start = start.resolve()  # one project, one key: no symlinked or `..` spellings of it
    root = project_root(start)
    if file_path and not (root / ".git").exists() and payload.get("cwd"):
        root = project_root(Path(payload["cwd"]).resolve())
    relative = None
    if file_path:
        try:
            relative = start.relative_to(repo_root(start))
        except ValueError:
            relative = Path(Path(file_path).name)
    store.activity(
        event,
        root,
        session_id=payload.get("session_id"),
        file=relative.as_posix() if relative else None,
        source=payload.get("source"),
        reason=payload.get("reason"),
        trigger=payload.get("trigger"),
    )


_HANDLERS: dict[str, tuple[str, Callable[..., str | None]]] = {
    "session-start": ("SessionStart", session_start),
    "session-end": ("SessionEnd", session_end),
    "pre-edit": ("PreToolUse", pre_edit),
    "pre-compact": ("PreCompact", pre_compact),
    "post-compact": ("PostCompact", post_compact),
}
# Claude Code reads PreCompact's plain stdout as instructions for the summary.
_PLAIN_STDOUT = {"pre-compact"}

EVENTS = tuple(_HANDLERS)
BLOCK_EXIT_CODE = 2  # Claude Code: the tool call is refused and stderr goes to the model


def run(
    event: str,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    block: bool = False,
) -> int:
    """Hook entry point.

    A failure inside grabli always exits 0: broken memory must not stop the
    agent's work. The only non-zero exit is the deliberate one-time refusal
    of `pre-edit --block`.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    hook_event, handler = _HANDLERS[event]
    block = block and event == "pre-edit"
    store = Store()
    try:
        payload = json.load(stdin)
        try:
            _record_activity(event, payload, store)
        except Exception as exc:  # noqa: BLE001 - the status view must never cost a delivery
            store.log("hook_error", hook=event, error=f"activity: {type(exc).__name__}: {exc}")
        # Without a session id "shown once" cannot be tracked, and blocking would never stop.
        block = block and bool(payload.get("session_id"))
        text = handler(payload, store, block=True) if block else handler(payload, store)
    except Exception as exc:  # noqa: BLE001 - recorded below, see docstring
        try:
            store.log("hook_error", hook=event, error=f"{type(exc).__name__}: {exc}")
        except OSError:
            print(f"grabli hook {event}: {exc}", file=sys.stderr)
        return 0
    if text and block:
        print(text, file=stderr)
        return BLOCK_EXIT_CODE
    if text and event in _PLAIN_STDOUT:
        stdout.write(text)
    elif text:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": hook_event, "additionalContext": text}},
            stdout,
            ensure_ascii=False,
        )
    return 0
