"""MCP server: two tools, `search` and `propose`."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer

from .index import Index
from .models import EntryError, Kind, build
from .store import Store, find_secret

INSTRUCTIONS = (
    "grabli — журнал ошибок и решений из прошлых проектов пользователя. "
    "Перед планом или нетривиальной правкой вызови `search` с описанием задачи: "
    "возможно, эта ошибка уже совершалась. "
    "Когда нашлась ошибка (ревью, упавший тест, откат) или принято решение с причиной, "
    "вызови `propose`. Черновик попадёт в журнал только после подтверждения человеком. "
    "Секреты и ключи в записи не клади."
)

_SEARCH_DESCRIPTION = (
    "Поиск по журналу прошлых ошибок и решений. С `query` возвращает короткие карточки, "
    "с `id` — полный текст одной записи. Отсутствие результата не ошибка: придёт "
    "`found: false` и подсказка."
)
_PROPOSE_DESCRIPTION = (
    "Предложить запись в журнал: `mistake` (что случилось, почему не поймали, как делать) "
    "или `decision` (что выбрали и почему). `paths` — glob-маски файлов, перед правкой "
    "которых запись стоит показать; `stack` — технологии. Запись ждёт подтверждения человеком."
)
_MAX_LIMIT = 20


def do_search(
    store: Store,
    query: str | None = None,
    id: str | None = None,  # noqa: A002 - public tool parameter name
    kind: Kind | None = None,
    project: str | None = None,
    stack: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    if id:
        try:
            entry = store.get(id)
        except EntryError as exc:
            return {"found": False, "hint": f"Запись не прочитана: {exc}"}
        if entry is None:
            return {"found": False, "hint": f"Записи {id} нет. Найди id через `query`."}
        return {"found": True, "entry": entry.model_dump(mode="json")}
    if not query or not query.strip():
        return {"found": False, "hint": "Передай `query` (описание задачи) или `id`."}

    with Index(store) as index:
        hits = index.search(
            query, kind=kind, project=project, stack=stack, limit=max(1, min(limit, _MAX_LIMIT))
        )
        warnings = list(index.errors)
    store.log("search", query=query, ids=[hit.id for hit in hits])

    result: dict[str, Any]
    if hits:
        result = {"found": True, "results": [hit.card() for hit in hits]}
    else:
        filtered = any((kind, project, stack))
        result = {
            "found": False,
            "hint": "Ничего не найдено. Попробуй другие слова"
            + (" или убери фильтры kind/project/stack." if filtered else "."),
        }
    if warnings:
        result["warnings"] = warnings
    return result


def do_propose(
    store: Store,
    kind: Kind,
    title: str,
    body: str,
    project: str | None = None,
    stack: list[str] | None = None,
    paths: list[str] | None = None,
    repo: str | None = None,
    commit: str | None = None,
    found_by: str | None = None,
    via: str = "mcp",
) -> dict[str, Any]:
    """`via` goes to the event log, so evals can tell agent proposals from manual ones."""
    secret = find_secret(body) or find_secret(title)
    if secret:
        return {
            "status": "rejected",
            "reason": f"В тексте похоже есть секрет: {secret}. Убери значение и предложи снова.",
        }
    metadata = {
        "project": project,
        "stack": " ".join(stack or []),
        "paths": " ".join(paths or []),
        "repo": repo,
        "commit": commit,
        "found_by": found_by,
    }
    for field, value in metadata.items():
        found = find_secret(value or "")
        if found:
            return {
                "status": "rejected",
                "reason": f"В поле `{field}` похоже есть секрет ({found.split(' on line')[0]}). "
                "Убери значение и предложи снова.",
            }
    today = date.today()
    source = {"repo": repo, "commit": commit, "found_by": found_by}
    try:
        entry = build(
            {
                "id": store.new_id(kind),
                "kind": kind,
                "title": title,
                "body": body,
                "project": project,
                "stack": stack or [],
                "paths": paths or [],
                "source": source if any(source.values()) else None,
                "created": today,
                "last_verified": today,
            }
        )
    except EntryError as exc:
        return {"status": "rejected", "reason": str(exc)}

    # Save first: looking for similar entries is a convenience and must not lose the draft.
    store.save(entry, pending=True)
    store.log("propose", ids=[entry.id], kind=kind, via=via)
    similar: list[dict[str, str]] = []
    warnings: list[str] = []
    try:
        with Index(store) as index:
            similar = [
                {"id": hit.id, "title": hit.title} for hit in index.search(entry.title, limit=3)
            ]
            warnings = list(index.errors)
    except (sqlite3.Error, OSError) as exc:
        warnings = [f"поиск похожих записей не выполнен: {exc}"]

    result: dict[str, Any] = {
        "status": "pending",
        "id": entry.id,
        "hint": "Черновик ждёт подтверждения человеком (`grabli review`). "
        "До этого в поиске и хуках его нет.",
    }
    if similar:
        result["similar"] = similar
        result["hint"] += " В журнале есть похожие записи: скажи о них пользователю."
    if warnings:
        result["warnings"] = warnings
    return result


def build_server(store: Store | None = None) -> MCPServer:
    store = store or Store()
    server = MCPServer("grabli", instructions=INSTRUCTIONS)

    @server.tool(description=_SEARCH_DESCRIPTION)
    def search(
        query: str | None = None,
        id: str | None = None,  # noqa: A002
        kind: Kind | None = None,
        project: str | None = None,
        stack: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        return do_search(store, query, id, kind, project, stack, limit)

    @server.tool(description=_PROPOSE_DESCRIPTION)
    def propose(
        kind: Kind,
        title: str,
        body: str,
        project: str | None = None,
        stack: list[str] | None = None,
        paths: list[str] | None = None,
        repo: str | None = None,
        commit: str | None = None,
        found_by: str | None = None,
    ) -> dict[str, Any]:
        return do_propose(store, kind, title, body, project, stack, paths, repo, commit, found_by)

    return server
