"""Command line: the MCP server, the hooks, and the human side of the review queue."""

from __future__ import annotations

import argparse
import sys

from . import hooks
from .index import Index
from .models import EntryError, dump
from .store import Store


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


def _pending(_: argparse.Namespace) -> int:
    drafts, errors = Store().load(pending=True)
    _warn(errors)
    if not drafts:
        print("Очередь пуста.")
    for draft in drafts:
        print(f"{draft.id}  {draft.kind:<8}  {draft.title}")
    return 0


def _review(_: argparse.Namespace) -> int:
    store = Store()
    drafts, errors = store.load(pending=True)
    _warn(errors)
    if not drafts:
        print("Очередь пуста.")
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
                store.reject(draft.id)
                print(f"Отклонено: {draft.id}")
            elif answer == "q":
                return 0
            elif answer != "s":
                continue
            break
    return 0


def _accept(args: argparse.Namespace) -> int:
    entry = Store().accept(args.id)
    print(f"Принято: {entry.id}  {entry.title}")
    return 0


def _reject(args: argparse.Namespace) -> int:
    Store().reject(args.id)
    print(f"Отклонено: {args.id}")
    return 0


def _hook(args: argparse.Namespace) -> int:
    return hooks.run(args.event, block=args.block)


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

    commands.add_parser("pending", help="показать очередь черновиков").set_defaults(run=_pending)
    commands.add_parser("review", help="разобрать очередь по одному").set_defaults(run=_review)

    for name, handler, text in (
        ("accept", _accept, "принять черновик в журнал"),
        ("reject", _reject, "удалить черновик"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("id")
        command.set_defaults(run=handler)

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
    except EntryError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
