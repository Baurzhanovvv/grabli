"""Claude Code hooks: deliver relevant entries without waiting for the agent to ask.

By default both hooks only add context and never set a permission decision,
so the edit goes through the user's normal permission flow. Claude Code
delivers that context alongside the tool result, i.e. after the first edit.

`pre-edit --block` instead refuses the edit once, with the entries as the
reason, so the agent reads them before it writes. The retry passes because
an entry is delivered at most once per session.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .context import detect_stack, for_file, for_session, render, repo_root
from .store import Store


def session_start(payload: dict, store: Store) -> str | None:
    root = repo_root(Path(payload["cwd"]))
    stack = detect_stack(root)
    entries, _ = store.load()
    chosen = for_session(entries, stack)
    store.log(
        "session_start",
        session_id=payload.get("session_id"),
        source=payload.get("source"),
        repo=root.name,
        stack=sorted(stack),
        ids=[entry.id for entry in chosen],
    )
    if not chosen:
        return None
    return render(chosen, f"подобранные по стеку этого репозитория ({', '.join(sorted(stack))})")


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


_HANDLERS: dict[str, tuple[str, Callable[..., str | None]]] = {
    "session-start": ("SessionStart", session_start),
    "pre-edit": ("PreToolUse", pre_edit),
}

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
    if text:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": hook_event, "additionalContext": text}},
            stdout,
            ensure_ascii=False,
        )
    return 0
