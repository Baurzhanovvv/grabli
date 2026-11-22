from __future__ import annotations

import asyncio
import io
import json
from datetime import date

import pytest

from grabli import cli, hooks
from grabli.context import detect_stack, for_file, for_session, repo_root
from grabli.server import build_server, do_propose, do_search


@pytest.fixture
def repo(tmp_path):
    """A NestJS backend + Next.js frontend monorepo."""
    root = tmp_path / "shop"
    (root / ".git").mkdir(parents=True)
    (root / "backend" / "src" / "reports").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "backend" / "package.json").write_text(
        json.dumps({"dependencies": {"@nestjs/core": "11", "pg": "8"}}), encoding="utf-8"
    )
    (root / "frontend" / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16"}, "devDependencies": {"typescript": "6"}}),
        encoding="utf-8",
    )
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return root


def run_hook(event: str, payload: object) -> tuple[int, str]:
    out = io.StringIO()
    code = hooks.run(event, stdin=io.StringIO(json.dumps(payload)), stdout=out)
    return code, out.getvalue()


def log_events(store) -> list[dict]:
    if not store.log_path.exists():
        return []
    return [json.loads(line) for line in store.log_path.read_text(encoding="utf-8").splitlines()]


# --- context --------------------------------------------------------------


def test_detect_stack_reads_root_and_child_manifests(repo, tmp_path):
    assert detect_stack(repo) == {"nestjs", "postgres", "nextjs", "typescript", "docker"}
    (repo / "pyproject.toml").write_text('dependencies = ["fastapi"]\n', encoding="utf-8")
    assert {"python", "fastapi"} <= detect_stack(repo)
    assert detect_stack(tmp_path / "missing") == set()


def test_detect_stack_survives_broken_package_json(repo):
    (repo / "backend" / "package.json").write_text("{not json", encoding="utf-8")
    assert "nextjs" in detect_stack(repo)


def test_repo_root_walks_up_to_git(repo):
    nested = repo / "backend" / "src" / "reports"
    assert repo_root(nested) == repo
    assert repo_root(nested / "new-file.ts") == repo  # the file does not exist yet


def test_for_file_matches_globs_relative_to_repo(repo, store, make_entry):
    match = make_entry()
    make_entry(paths=["**/auth/**"])
    make_entry(paths=[])
    make_entry(stack=["django"])  # same glob, foreign stack
    make_entry(title="Устаревшее", superseded_by=match.id)
    stack = detect_stack(repo)
    target = repo / "backend" / "src" / "reports" / "ytd.service.ts"

    all_entries = store.load()[0]
    assert len(all_entries) == 5
    assert for_file(all_entries, repo, target, stack) == [match]
    assert for_file(all_entries, repo, repo / "backend" / "src" / "main.ts", stack) == []
    assert for_file(all_entries, repo, repo.parent / "elsewhere" / "reports" / "a.ts", stack) == []


def test_for_session_ranks_by_overlap_then_recency(make_entry):
    one_tag_new = make_entry(stack=["nestjs"], last_verified=date(2026, 11, 20))
    two_tags = make_entry(stack=["nestjs", "postgres"])
    one_tag_old = make_entry(stack=["postgres"], last_verified=date(2026, 1, 1))
    foreign = make_entry(stack=["django"])
    replaced = make_entry(stack=["nestjs"], title="Заменено", superseded_by=two_tags.id)
    entries = [one_tag_old, foreign, one_tag_new, replaced, two_tags]

    stack = {"nestjs", "postgres"}
    assert for_session(entries, stack) == [two_tags, one_tag_new, one_tag_old]
    assert for_session(entries, stack, limit=1) == [two_tags]
    assert for_session(entries, set()) == []


# --- hooks ----------------------------------------------------------------


def test_session_start_injects_entries_for_repo_stack(repo, store, make_entry):
    entry = make_entry()
    make_entry(stack=["django"], title="Чужой стек")

    code, out = run_hook(
        "session-start", {"session_id": "s1", "cwd": str(repo / "backend"), "source": "startup"}
    )

    assert code == 0
    output = json.loads(out)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert entry.title in output["additionalContext"]
    assert "Чужой стек" not in output["additionalContext"]
    assert log_events(store)[-1]["ids"] == [entry.id]


def test_session_start_with_nothing_relevant_is_silent_but_logged(repo, store):
    code, out = run_hook("session-start", {"session_id": "s1", "cwd": str(repo)})
    assert (code, out) == (0, "")
    assert log_events(store)[-1]["event"] == "session_start"
    assert log_events(store)[-1]["ids"] == []


def test_pre_edit_adds_context_once_per_session_without_deciding_permission(
    repo, store, make_entry
):
    entry = make_entry()
    payload = {
        "session_id": "s1",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "backend" / "src" / "reports" / "ytd.service.ts")},
    }

    code, out = run_hook("pre-edit", payload)
    output = json.loads(out)["hookSpecificOutput"]
    assert code == 0
    assert output["hookEventName"] == "PreToolUse"
    assert entry.title in output["additionalContext"]
    assert "permissionDecision" not in output

    assert run_hook("pre-edit", payload) == (0, "")  # already shown in this session
    assert json.loads(run_hook("pre-edit", {**payload, "session_id": "s2"})[1])


def test_pre_edit_is_silent_for_unrelated_file(repo, store, make_entry):
    make_entry()
    payload = {"session_id": "s1", "tool_input": {"file_path": str(repo / "frontend" / "app.tsx")}}
    assert run_hook("pre-edit", payload) == (0, "")
    assert log_events(store) == []


@pytest.mark.parametrize("event", hooks.EVENTS)
def test_hook_never_fails_the_session_and_records_the_error(store, event):
    out = io.StringIO()
    code = hooks.run(event, stdin=io.StringIO("{broken json"), stdout=out)
    assert (code, out.getvalue()) == (0, "")
    assert log_events(store)[-1]["event"] == "hook_error"
    assert log_events(store)[-1]["hook"] == event


# --- MCP tools ------------------------------------------------------------


def test_server_exposes_exactly_four_tools(store):
    tools = asyncio.run(build_server(store).list_tools())
    assert {tool.name for tool in tools} == {"search", "propose", "retract", "status"}
    assert all(tool.description for tool in tools)


def test_search_miss_is_not_an_error(store, make_entry):
    make_entry()
    assert do_search(store) == {
        "found": False,
        "hint": "Передай `query` (описание задачи) или `id`.",
    }
    miss = do_search(store, query="kubernetes", kind="mistake")
    assert miss["found"] is False
    assert "фильтры" in miss["hint"]
    assert do_search(store, id="m-aaaaaa")["found"] is False
    assert do_search(store, id="../x")["found"] is False


def test_search_returns_cards_then_full_entry_by_id(store, make_entry):
    entry = make_entry()
    found = do_search(store, query="отчет за период", limit=99)
    assert found["found"] is True
    assert [card["id"] for card in found["results"]] == [entry.id]
    assert "body" not in found["results"][0]

    full = do_search(store, id=entry.id)
    assert full["entry"]["body"] == entry.body
    assert full["entry"]["created"] == "2026-11-01"
    assert log_events(store)[-1] | {"ts": ""} == {
        "ts": "",
        "event": "search",
        "query": "отчет за период",
        "ids": [entry.id],
    }


def test_propose_goes_to_queue_and_is_invisible_until_accepted(store, make_entry):
    existing = make_entry()
    result = do_propose(
        store,
        "mistake",
        "Отчёт за период считали без таймзоны",
        "## Что случилось\nГраница суток съехала.",
        stack=["NestJS"],
        paths=["**/reports/**"],
        repo="shop",
        found_by="code-review",
    )

    assert result["status"] == "pending"
    assert result["similar"] == [{"id": existing.id, "title": existing.title}]
    draft = store.get(result["id"], pending=True)
    assert draft.stack == ["nestjs"]
    assert draft.source.repo == "shop"
    assert draft.source.commit is None
    assert do_search(store, query="таймзоны")["found"] is False

    store.accept(result["id"])
    assert do_search(store, query="таймзоны")["found"] is True


def test_propose_refuses_secrets_and_invalid_entries(store):
    leaked = do_propose(store, "mistake", "Ключ в логах", "Вот он:\nsk-" + "a1B2" * 8)
    assert leaked["status"] == "rejected"
    assert "line 2" in leaked["reason"]
    assert "a1B2" not in leaked["reason"]

    invalid = do_propose(store, "decision", "ab", "текст")
    assert invalid["status"] == "rejected"
    assert "title" in invalid["reason"]

    assert store.load(pending=True) == ([], [])


# --- CLI ------------------------------------------------------------------


def test_cli_search_pending_accept_reject(store, make_entry, capsys):
    entry = make_entry()
    first = make_entry(pending=True, title="Черновик один")
    second = make_entry(pending=True, title="Черновик два")

    assert cli.main(["search", "отчет", "--stack", "nestjs"]) == 0
    assert entry.id in capsys.readouterr().out
    assert cli.main(["search", "kubernetes"]) == 0
    assert "Ничего не найдено" in capsys.readouterr().out

    assert cli.main(["pending"]) == 0
    listing = capsys.readouterr().out
    assert first.id in listing and second.id in listing

    assert cli.main(["accept", first.id]) == 0
    assert cli.main(["reject", second.id]) == 0
    assert store.get(first.id) is not None
    assert store.load(pending=True)[0] == []

    assert cli.main(["accept", first.id]) == 1
    assert "no pending draft" in capsys.readouterr().err


def test_cli_review_walks_the_queue(store, make_entry, monkeypatch, capsys):
    drafts = sorted(
        (make_entry(pending=True, title=f"Черновик {n}") for n in range(3)), key=lambda e: e.id
    )
    answers = iter(["x", "a", "r", "s"])  # unknown key re-prompts
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert cli.main(["review"]) == 0

    assert store.get(drafts[0].id) is not None
    assert [d.id for d in store.load(pending=True)[0]] == [drafts[2].id]
    assert "[1/3]" in capsys.readouterr().out


def test_cli_review_stops_on_quit_or_eof(store, make_entry, monkeypatch):
    make_entry(pending=True)

    def closed(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert cli.main(["review"]) == 0
    assert len(store.load(pending=True)[0]) == 1


def test_godot_project_without_git_uses_session_directory(tmp_path, store, make_entry):
    project = tmp_path / "game"
    (project / "game" / "scripts").mkdir(parents=True)
    (project / "game" / "project.godot").write_text("", encoding="utf-8")
    assert detect_stack(project) == {"godot"}

    entry = make_entry(stack=["godot"], paths=["**/scripts/**"])
    payload = {
        "session_id": "s1",
        "cwd": str(project),
        "tool_input": {"file_path": str(project / "game" / "scripts" / "player.gd")},
    }
    assert entry.title in run_hook("pre-edit", payload)[1]


def test_pre_edit_block_refuses_once_then_lets_the_retry_through(repo, store, make_entry):
    entry = make_entry()
    payload = {
        "session_id": "s1",
        "tool_input": {"file_path": str(repo / "backend" / "src" / "reports" / "ytd.service.ts")},
    }

    def run_blocking() -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run(
            "pre-edit", stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err, block=True
        )
        return code, out.getvalue(), err.getvalue()

    code, out, err = run_blocking()
    assert code == hooks.BLOCK_EXIT_CODE
    assert out == ""
    assert entry.title in err
    assert "повтори правку" in err
    assert log_events(store)[-1]["mode"] == "block"

    assert run_blocking() == (0, "", "")  # the retry is not refused again


def test_block_flag_never_blocks_on_failure_or_on_session_start(repo, store, make_entry):
    make_entry()
    out, err = io.StringIO(), io.StringIO()
    broken = hooks.run("pre-edit", stdin=io.StringIO("{nope"), stdout=out, stderr=err, block=True)
    assert (broken, out.getvalue(), err.getvalue()) == (0, "", "")

    start = hooks.run(
        "session-start",
        stdin=io.StringIO(json.dumps({"session_id": "s1", "cwd": str(repo)})),
        stdout=out,
        stderr=err,
        block=True,
    )
    assert start == 0
    assert "additionalContext" in out.getvalue()


def test_cli_passes_block_flag(repo, store, make_entry, monkeypatch, capsys):
    make_entry()
    payload = {
        "session_id": "s1",
        "tool_input": {"file_path": str(repo / "backend" / "src" / "reports" / "a.ts")},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "pre-edit", "--block"]) == hooks.BLOCK_EXIT_CODE
    assert "[grabli]" in capsys.readouterr().err


def test_cli_propose_queues_a_draft_from_flags(store, make_entry, capsys):
    existing = make_entry()
    code = cli.main(
        [
            "propose",
            "Отчёт за период без таймзоны",
            "--body",
            "Граница суток съехала.",
            "--stack",
            "NestJS",
            "--stack",
            "postgres",
            "--paths",
            "**/reports/**",
            "--project",
            "shop",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    (draft,) = store.load(pending=True)[0]
    assert draft.id in out
    assert existing.id in out  # similar entries are shown
    assert (draft.kind, draft.stack, draft.paths) == (
        "mistake",
        ["nestjs", "postgres"],
        ["**/reports/**"],
    )
    assert draft.source.found_by == "human"
    assert store.load()[0] == [existing]  # still needs `accept`


def test_cli_propose_reads_body_from_stdin(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("## Почему\nТак быстрее откатывать.\n"))
    assert cli.main(["propose", "Флаги выключены по умолчанию", "--kind", "decision"]) == 0
    (draft,) = store.load(pending=True)[0]
    assert draft.kind == "decision"
    assert draft.body == "## Почему\nТак быстрее откатывать."


def test_cli_propose_reports_rejection(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    assert cli.main(["propose", "Заголовок записи"]) == 1
    assert "Отклонено" in capsys.readouterr().err
    assert store.load(pending=True) == ([], [])


# --- what a delivered entry contains ----------------------------------------

LONG = (
    "## Что случилось\n" + "Долгий рассказ об ошибке. " * 30 + "\n\n"
    "## Почему не поймали\n" + "Подробности про тесты. " * 20 + "\n\n"
    "## Как делать\nПериод передавать явно и проверять на данных по обе стороны границы."
)


def test_long_entry_is_delivered_with_its_action_section_first_and_whole():
    from grabli.context import digest

    text = digest(LONG)
    assert len(LONG) > 1100 >= len(text)
    assert text.startswith("## Как делать\nПериод передавать явно")
    assert "по обе стороны границы." in text
    assert "## Что случилось" in text and text.endswith("…")  # the story is what gets cut


def test_digest_leaves_short_entries_alone_and_cuts_unstructured_ones():
    from grabli.context import digest

    assert digest("## Как делать\nКоротко.") == "## Как делать\nКоротко."
    plain = digest("слово " * 400)
    assert len(plain) <= 1100 and plain.endswith("…")
    decision = "## Что выбрали\n" + "Текст. " * 200 + "\n## Как применять\nВключать за флагом."
    assert digest(decision).startswith("## Как применять\nВключать за флагом.")


def test_session_start_lists_further_entries_by_title_and_leaves_them_for_pre_edit(
    repo, store, make_entry
):
    for number in range(5):
        make_entry(title=f"Запись номер {number}", body=LONG)
    code, out = run_hook("session-start", {"session_id": "s1", "cwd": str(repo)})
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    event = log_events(store)[-1]

    assert code == 0 and len(text) < 10_000  # Claude Code's limit for hook context
    assert text.count("## Как делать") == 3
    assert "только заголовки" in text
    assert (len(event["ids"]), len(event["listed"])) == (3, 2)
    assert all(entry_id in text for entry_id in event["listed"])
    assert store.shown_ids("s1") == set(event["ids"])  # listed ones are not "shown"


# --- review of the digest ---------------------------------------------------

STORY = "## Что случилось\n" + "Долгий рассказ об ошибке. " * 50 + "\n\n"


def _digest(body: str) -> str:
    from grabli.context import digest

    text = digest(body)
    assert len(text) <= 1100 + len("\n```")  # the budget, plus a fence closed after a cut
    return text


def test_digest_ignores_headings_inside_code_fences_and_never_leaves_a_fence_open():
    action = (
        "## Как делать\nСначала миграция:\n```bash\n# сначала миграция\nyarn m:run\n```\n"
        "Потом деплой."
    )
    text = _digest(STORY + action)
    assert text.startswith(action)

    fenced_story = "## Что случилось\n```bash\n# how to run\n" + "echo шаг\n" * 200 + "```\n\n"
    text = _digest(fenced_story + "## Как делать\nНе запускать под root.")
    assert text.startswith("## Как делать\nНе запускать под root.")
    assert sum(line.startswith("```") for line in text.split("\n")) % 2 == 0


def test_digest_keeps_subsections_inside_the_action_section():
    action = "## Как делать\n### Backend\nПериод явно.\n### Frontend\nПередавать даты."
    assert _digest(STORY + action + "\n\n## Источник\nревью").startswith(action)


@pytest.mark.parametrize(
    "decoy", ["## How to reproduce", "## Как делать не надо", "## Show tooltip"]
)
def test_digest_does_not_take_lookalike_headings_for_the_action(decoy):
    body = f"{decoy}\n" + "Шаг воспроизведения. " * 70 + "\n\n## Как делать\nЧинить так."
    assert _digest(body).startswith("## Как делать\nЧинить так.")
    assert _digest(f"{decoy}\n" + "Текст без вывода. " * 90).startswith(decoy)  # plain cut


def test_digest_finds_the_action_in_prose_entries():
    prose = (
        "Что случилось: " + "рассказ. " * 160 + "\nКак делать правильно: период передавать явно."
    )
    assert _digest(prose).startswith("Как делать правильно: период передавать явно.")
    inline = "Рассказ. " * 160 + "Как применять: включать за флагом."
    assert _digest(inline).startswith("Как применять: включать за флагом.")


def test_digest_of_an_action_only_entry_uses_the_whole_budget():
    text = _digest("## Как делать\n" + "Шаг процедуры. " * 120)
    assert 1050 < len(text) <= 1100 and text.endswith("…")


def test_listed_titles_carry_the_unreviewed_mark(make_entry):
    from grabli.context import render

    text = render([make_entry()], "по стеку", [make_entry(), make_entry(reviewed=False)])
    listing = text.split("только заголовки")[1]
    assert listing.count("не проверено") == 1
