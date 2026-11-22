"""Capturing lessons when Claude Code compacts the context."""

from __future__ import annotations

import io
import json

from grabli import cli, hooks
from grabli.capture import INSTRUCTIONS, MAX_LESSONS, _items, parse_lessons

SUMMARY = """\
## Summary
Работали над отчётами.

<grabli-lessons>
### mistake: YTD считали от календарного года
stack: NestJS, postgres
paths: `**/reports/**`, **/*ytd*

Отчёт суммировал от начала года. Период передавать явно.

### decision: Флаги выключены по умолчанию
Так быстрее откатывать.
</grabli-lessons>
"""


def run(event: str, payload: dict) -> tuple[int, str]:
    out = io.StringIO()
    code = hooks.run(event, stdin=io.StringIO(json.dumps(payload)), stdout=out)
    return code, out.getvalue()


def events(store, name: str) -> list[dict]:
    lines = store.log_path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == name]


def test_parse_reads_kind_title_fields_and_body():
    mistake, decision = parse_lessons(SUMMARY)
    assert mistake == {
        "kind": "mistake",
        "title": "YTD считали от календарного года",
        "stack": ["NestJS", "postgres"],
        "paths": ["**/reports/**", "**/*ytd*"],
        "body": "Отчёт суммировал от начала года. Период передавать явно.",
    }
    assert (decision["kind"], decision["paths"], decision["body"]) == (
        "decision",
        [],
        "Так быстрее откатывать.",
    )


def test_parse_tolerates_missing_block_and_missing_closing_tag():
    assert parse_lessons("Обычное summary без блока.\n### mistake: не в блоке\nтекст") == []
    (lesson,) = parse_lessons("<grabli-lessons>\n## Mistake: Обрезанный блок\nтекст в конце")
    assert (lesson["kind"], lesson["body"]) == ("mistake", "текст в конце")


def test_pre_compact_prints_plain_instructions_and_never_blocks(store):
    code, out = run("pre-compact", {"session_id": "s1", "trigger": "auto"})
    assert code == 0  # exit 2 would cancel the compaction
    assert out == INSTRUCTIONS  # plain text, not hook JSON
    assert events(store, "pre_compact")[0]["trigger"] == "auto"


def test_post_compact_queues_drafts_for_review(tmp_path, store):
    repo = tmp_path / "shop"
    (repo / ".git").mkdir(parents=True)
    payload = {"session_id": "s1", "trigger": "auto", "cwd": str(repo), "compact_summary": SUMMARY}

    assert run("post-compact", payload) == (0, "")

    drafts = {draft.kind: draft for draft in store.load(pending=True)[0]}
    assert store.load()[0] == []  # nothing enters the journal without review
    assert drafts["mistake"].stack == ["nestjs", "postgres"]
    assert drafts["mistake"].paths == ["**/reports/**", "**/*ytd*"]
    assert (drafts["mistake"].project, drafts["mistake"].source.found_by) == ("shop", "compact")
    (captured,) = events(store, "compact_capture")
    assert (captured["found"], len(captured["ids"]), captured["trigger"]) == (2, 2, "auto")
    assert {e["via"] for e in events(store, "propose")} == {"compact"}


def test_lessons_carried_into_the_next_summary_are_not_queued_twice(store):
    payload = {"session_id": "s1", "compact_summary": SUMMARY}
    run("post-compact", payload)
    run("post-compact", payload)
    assert len(store.load(pending=True)[0]) == 2
    assert events(store, "compact_capture")[-1]["duplicates"] == 2


def test_post_compact_caps_lessons_and_logs_rejections_without_failing(store):
    token = "glpat-" + "A1b2" * 6
    block = "\n".join(f"### mistake: Урок номер {n}\nтекст" for n in range(MAX_LESSONS + 2))
    summary = f"<grabli-lessons>\n### mistake: С ключом\n{token}\n{block}\n</grabli-lessons>"

    assert run("post-compact", {"session_id": "s1", "compact_summary": summary}) == (0, "")

    (captured,) = events(store, "compact_capture")
    assert len(store.load(pending=True)[0]) == MAX_LESSONS - 1  # the cap counts the rejected one
    assert len(captured["rejected"]) == 1
    assert token not in json.dumps(captured)


def test_summary_without_a_block_is_logged_as_zero_found(store):
    run("post-compact", {"session_id": "s1", "compact_summary": "Просто summary."})
    assert events(store, "compact_capture")[0]["found"] == 0
    assert store.load(pending=True) == ([], [])


def test_cli_exposes_compact_hooks(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["hook", "pre-compact"]) == 0
    assert "<grabli-lessons>" in capsys.readouterr().out


# --- review of the capture code ---------------------------------------------


def _block(*lessons: str) -> str:
    return "<grabli-lessons>\n" + "\n".join(lessons) + "\n</grabli-lessons>\n"


def test_new_lessons_are_not_crowded_out_by_carried_over_ones(store):
    old = [f"### mistake: Старый урок {n}\nтекст" for n in range(MAX_LESSONS)]
    run("post-compact", {"session_id": "s1", "compact_summary": _block(*old)})
    summary = _block(*old) + _block("### mistake: Новый урок\nтекст")

    run("post-compact", {"session_id": "s1", "compact_summary": summary})

    assert "Новый урок" in {draft.title for draft in store.load(pending=True)[0]}
    last = events(store, "compact_capture")[-1]
    assert (last["found"], last["duplicates"], len(last["ids"])) == (
        MAX_LESSONS + 1,
        MAX_LESSONS,
        1,
    )


def test_a_draft_rejected_by_the_human_does_not_come_back(store):
    payload = {"session_id": "s1", "compact_summary": SUMMARY}
    run("post-compact", payload)
    for draft in store.load(pending=True)[0]:
        store.reject(draft.id)

    run("post-compact", payload)

    assert store.load(pending=True) == ([], [])


def test_globs_with_brackets_and_braces_survive():
    assert _items("app/[id]/page.tsx, **/*.{ts,tsx}, `src/[slug]`") == [
        "app/[id]/page.tsx",
        "**/*.{ts,tsx}",
        "src/[slug]",
    ]
    assert _items("[nestjs, postgres]") == ["nestjs", "postgres"]
    assert _items("[a]/x, [b]") == ["[a]/x", "[b]"]


def test_a_mention_of_the_tag_or_the_template_example_is_not_a_lesson():
    retold = (
        "Добавили блок `<grabli-lessons>` в summary.\n\nПример:\n"
        "### mistake: короткий заголовок ошибки\nstack: nestjs\nЧто случилось"
    )
    assert parse_lessons(retold) == []
    assert parse_lessons(_block("### mistake: Короткий заголовок ошибки\nтекст")) == []


def test_field_lines_in_markdown_dress_are_read_and_prose_is_left_in_the_body():
    dressed, prose = parse_lessons(
        _block(
            "### mistake: Первый урок\n- **stack:** nestjs, postgres\n"
            "**paths:** `**/reports/**`\nтекст",
            "### mistake: Второй урок\nStack: NestJS сам не валидирует, поэтому проверяем руками.",
        )
    )
    assert (dressed["stack"], dressed["paths"], dressed["body"]) == (
        ["nestjs", "postgres"],
        ["**/reports/**"],
        "текст",
    )
    assert prose["stack"] == []
    assert prose["body"].startswith("Stack: NestJS сам не валидирует")


def test_heading_like_line_inside_a_code_fence_does_not_split_a_lesson():
    (lesson,) = parse_lessons(
        _block("### mistake: Урок с кодом\n```\n# decision: x in code\n```\nrest")
    )
    assert lesson["body"] == "```\n# decision: x in code\n```\nrest"


def test_one_failing_lesson_does_not_lose_the_others_or_the_capture_event(store, monkeypatch):
    real = store.save.__func__

    def flaky(self, entry, pending=False):
        if "Сломанный" in entry.title:
            raise OSError("no space left on device")
        return real(self, entry, pending)

    monkeypatch.setattr(type(store), "save", flaky)
    summary = _block(
        "### mistake: Первый урок\nтекст",
        "### mistake: Сломанный урок\nтекст",
        "### mistake: Третий урок\nтекст",
    )

    assert run("post-compact", {"session_id": "s1", "compact_summary": summary}) == (0, "")

    (captured,) = events(store, "compact_capture")
    assert (captured["found"], len(captured["ids"])) == (3, 2)
    assert "no space left" in captured["rejected"][0]
