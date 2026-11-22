"""Command line: the MCP server, the hooks, and the human side of the review queue."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import hooks, install, status
from .context import project_root, repo_root
from .index import Index
from .models import EntryError, dump
from .store import MODES, Store, mode


def _human() -> bool:
    """A terminal on stdout. An agent running grabli through its shell tool has a pipe there.

    This is a speed bump, not a security boundary: an agent with a shell can also edit the
    files directly. It keeps an agent from marking entries as human-reviewed by accident.
    """
    return sys.stdout.isatty()


def _humans_only(action: str) -> None:
    if not _human():
        raise EntryError(
            f"`grabli {action}` works only in a terminal: it records a human's decision. "
            "An agent should use the MCP tools `propose` and `retract`."
        )


def _warn(messages: list[str]) -> None:
    for message in messages:
        print(f"пропущен файл: {message}", file=sys.stderr)


def _serve(_: argparse.Namespace) -> int:
    from .server import build_server  # the MCP SDK is slow to import; hooks must not pay for it

    build_server().run()
    return 0


def _search(args: argparse.Namespace) -> int:
    with Index(Store()) as index:
        hits = index.search(
            args.query,
            kind=args.kind,
            project=args.project,
            stack=args.stack,
            limit=max(1, args.limit),
        )
        _warn(index.errors)
    if not hits:
        print("Ничего не найдено.")
        return 0
    for hit in hits:
        print(f"{hit.id}  {hit.kind:<8}  {hit.score:>9.3g}  {hit.title}")
        print(f"    {hit.snippet}")
    return 0


def _propose(args: argparse.Namespace) -> int:
    from .server import do_propose  # same rules as the MCP tool; the SDK import is slow

    body = args.body
    typed = body is None
    if typed:
        if sys.stdin.isatty():
            print("Текст записи (закончить: Ctrl-D):", file=sys.stderr)
        body = sys.stdin.read()
    # `--stack nestjs,postgres` means two tags, not one that never matches a repo.
    stack = [tag for value in args.stack or [] for tag in re.split(r"[,\s]+", value) if tag]
    result = do_propose(
        Store(),
        args.kind,
        args.title,
        body,
        project=args.project,
        stack=stack,
        paths=args.paths,
        repo=args.repo,
        commit=args.commit,
        found_by=args.found_by or ("human" if _human() else "agent"),
        via="cli",
        reviewed=_human(),  # typed by the human themselves, not piped by an agent
    )
    if result["status"] == "rejected":
        print(f"Отклонено: {result['reason']}", file=sys.stderr)
        if typed and body.strip():
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", prefix="grabli-draft-", suffix=".md", delete=False
            ) as kept:
                kept.write(body)
            print(f"Набранный текст сохранён: {kept.name}", file=sys.stderr)
        return 1
    _warn(result.get("warnings", []))
    if result["status"] == "exists":
        print(f"Уже есть: {result['id']}, обновил дату. Новая запись не создана.")
        return 0
    print(f"{'Черновик' if result['status'] == 'pending' else 'Записано'}: {result['id']}")
    for similar in result.get("similar", []):
        print(f"  похожая запись: {similar['id']}  {similar['title']}")
    if result["status"] == "pending":
        print(f"Принять: grabli accept {result['id']}")
    return 0


def _pending(_: argparse.Namespace) -> int:
    drafts, errors = Store().load(pending=True)
    _warn(errors)
    if not drafts:
        print("Очередь пуста.")
    for draft in drafts:
        print(f"{draft.id}  {draft.kind:<8}  {draft.title}")
    return 0


def _recent(args: argparse.Namespace) -> int:
    entries, errors = Store().load()
    _warn(errors)
    entries.sort(key=lambda entry: (entry.created, entry.id), reverse=True)
    if args.unreviewed:
        entries = [entry for entry in entries if not entry.reviewed]
    if not entries:
        print("Записей нет.")
    for entry in entries[: max(1, args.limit)]:
        origin = (entry.source.found_by if entry.source else None) or "?"
        mark = "проверено" if entry.reviewed else "авто"
        print(f"{entry.id}  {entry.created}  {mark:<9}  {origin:<12}  {entry.title}")
    return 0


def _mode(args: argparse.Namespace) -> int:
    if args.mode:
        _humans_only("mode")
        Store().set_mode(args.mode)
    print(f"Режим: {mode()}")
    if os.environ.get("GRABLI_MODE", "").strip().lower() in MODES:
        print("Задан переменной GRABLI_MODE, она главнее сохранённого.", file=sys.stderr)
    return 0


def _status(args: argparse.Namespace) -> int:
    store = Store()
    projects = status.collect(store)
    if args.forget:
        _humans_only("status --forget")
        matches = status.find(projects, args.forget)
        if not matches:
            raise EntryError(f"проекта {args.forget!r} в списке нет")
        if len(matches) > 1:
            paths = ", ".join(match["path"] for match in matches)
            raise EntryError(f"под {args.forget!r} подходят несколько, укажи путь: {paths}")
        store.activity("forget", matches[0]["path"])
        print(f"Убран из списка: {matches[0]['name']}")
        return 0
    if args.project:
        projects = status.find(projects, args.project)
        if not projects:
            print(f"Проекта {args.project!r} в списке нет.", file=sys.stderr)
            return 1
    print(status.render(projects, detail=bool(args.project)))
    return 0


def _remove(args: argparse.Namespace) -> int:
    _humans_only("remove")
    target = Store().retract(args.id, reason=args.reason or "removed by human")
    print(f"Убрано из журнала: {args.id} (файл сохранён: {target})")
    return 0


def _review(_: argparse.Namespace) -> int:
    _humans_only("review")
    store = Store()
    queued, errors = store.load(pending=True)
    _warn(errors)
    written, errors = store.load()
    _warn(errors)
    # The queue first, then what was written without a human.
    drafts = [*queued, *(entry for entry in written if not entry.reviewed)]
    in_queue = {draft.id for draft in queued}
    if not drafts:
        print("Проверять нечего.")
        return 0
    for number, draft in enumerate(drafts, start=1):
        print(f"\n[{number}/{len(drafts)}]\n{dump(draft)}")
        while True:
            try:
                answer = input("[a] принять  [r] отклонить  [s] пропустить  [q] выйти > ")
            except EOFError:
                return 0
            answer = answer.strip().lower()
            if answer == "a":
                store.accept(draft.id)
                print(f"Принято: {draft.id}")
            elif answer == "r":
                if draft.id in in_queue:
                    store.reject(draft.id)
                else:
                    store.retract(draft.id, reason="rejected in review", via="review")
                print(f"Отклонено: {draft.id}")
            elif answer == "q":
                return 0
            elif answer != "s":
                continue
            break
    return 0


def _accept(args: argparse.Namespace) -> int:
    _humans_only("accept")
    entry = Store().accept(args.id)
    print(f"Принято: {entry.id}  {entry.title}")
    return 0


def _reject(args: argparse.Namespace) -> int:
    _humans_only("reject")
    Store().reject(args.id)
    print(f"Отклонено: {args.id}")
    return 0


def _hook(args: argparse.Namespace) -> int:
    return hooks.run(args.event, block=args.block)


def _install(args: argparse.Namespace) -> int:
    project = Path(args.path).expanduser().resolve()
    if not project.is_dir():
        raise install.InstallError(f"{project} is not a directory")
    root = repo_root(project)
    if root != project:
        # Claude Code reads project settings from the repo root, not from a subdirectory.
        print(f"{project} внутри репозитория {root}: ставлю в корень.")
        project = root
    command = install.launcher()
    elsewhere = install.hooked_elsewhere(project)
    if elsewhere:
        print(f"Хуки уже подключены в {elsewhere}, второй раз не ставлю.")
        missing = install.missing_hooks(elsewhere)
        if missing:
            print(
                f"Внимание: в {elsewhere} нет хуков {', '.join(missing)}. Добавь их по образцу "
                "соседних, иначе эти события не сработают.",
                file=sys.stderr,
            )
    else:
        settings = install.write_hooks(project, command, block=args.block)
        print(f"Хуки: {settings}")
        if install.tracked_by_git(project, settings):
            print(f"Внимание: {settings.name} не в .gitignore и попадёт в git.", file=sys.stderr)
    # Only after the hooks are in place: a failed install must not list the project.
    Store().activity("install", project_root(project))
    if args.no_mcp:
        return 0
    status = install.add_mcp(project, command)
    if status == "manual":
        print("MCP: команда `claude` не найдена. Выполни в проекте:", file=sys.stderr)
        print(f"  {shlex.join(install.mcp_command(command))}", file=sys.stderr)
    else:
        print("MCP: подключён" if status == "added" else "MCP: был подключён, обновил")
    print("Открой новую сессию Claude Code в проекте: текущие сессии хуки не подхватят.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grabli", description="Журнал ошибок и решений агента.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="запустить MCP-сервер (stdio)").set_defaults(run=_serve)

    search = commands.add_parser("search", help="поиск по журналу")
    search.add_argument("query")
    search.add_argument("--kind", choices=["mistake", "decision"])
    search.add_argument("--project")
    search.add_argument("--stack", action="append")
    search.add_argument("--limit", type=int, default=5)
    search.set_defaults(run=_search)

    propose = commands.add_parser(
        "propose", help="предложить запись (текст из --body или из stdin)"
    )
    propose.add_argument("title")
    propose.add_argument("--kind", choices=["mistake", "decision"], default="mistake")
    propose.add_argument(
        "--body",
        help="текст записи; без него читается stdin. Текст с дефиса пишется как --body=-текст",
    )
    propose.add_argument("--project")
    propose.add_argument("--stack", action="append", help="технология; можно повторять")
    propose.add_argument("--paths", action="append", help="glob-маска файлов; можно повторять")
    propose.add_argument("--repo")
    propose.add_argument("--commit")
    propose.add_argument("--found-by", dest="found_by")
    propose.set_defaults(run=_propose)

    commands.add_parser("pending", help="показать очередь черновиков").set_defaults(run=_pending)
    commands.add_parser(
        "review", help="разобрать по одному: очередь и записи, сделанные без человека"
    ).set_defaults(run=_review)

    state = commands.add_parser(
        "status", help="что происходит в подключённых проектах: активность, сейчас, дальше"
    )
    state.add_argument("project", nargs="?", help="подробно по одному проекту")
    state.add_argument("--forget", metavar="PROJECT", help="убрать проект из списка")
    state.set_defaults(run=_status)

    recent = commands.add_parser("recent", help="последние записи журнала")
    recent.add_argument("--limit", type=int, default=10)
    recent.add_argument("--unreviewed", action="store_true", help="только записанные без человека")
    recent.set_defaults(run=_recent)

    switch = commands.add_parser("mode", help="показать или сменить режим записи")
    switch.add_argument("mode", nargs="?", choices=MODES)
    switch.set_defaults(run=_mode)

    remove = commands.add_parser("remove", help="убрать запись из журнала (файл сохраняется)")
    remove.add_argument("id")
    remove.add_argument("--reason")
    remove.set_defaults(run=_remove)

    for name, handler, text in (
        ("accept", _accept, "принять черновик или отметить запись проверенной"),
        ("reject", _reject, "удалить черновик"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("id")
        command.set_defaults(run=handler)

    setup = commands.add_parser(
        "install", help="подключить проект: хуки в settings.local.json и MCP-сервер"
    )
    setup.add_argument(
        "path", nargs="?", default=".", help="каталог проекта (по умолчанию текущий)"
    )
    setup.add_argument(
        "--block", action="store_true", help="хук перед правкой в режиме однократного отклонения"
    )
    setup.add_argument("--no-mcp", action="store_true", help="только хуки, MCP не трогать")
    setup.set_defaults(run=_install)

    hook = commands.add_parser("hook", help="обработчик хука Claude Code (читает JSON из stdin)")
    hook.add_argument("event", choices=hooks.EVENTS)
    hook.add_argument(
        "--block",
        action="store_true",
        help="pre-edit: один раз отклонить правку и отдать записи как причину",
    )
    hook.set_defaults(run=_hook)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except (EntryError, install.InstallError, sqlite3.Error, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
