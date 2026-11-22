"""The default mode: proposals go straight into the journal, no human in the loop."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest

from grabli import cli, hooks
from grabli.context import render
from grabli.index import Index
from grabli.models import dump, parse
from grabli.server import do_propose, do_retract
from grabli.store import mode


@pytest.fixture(autouse=True)
def auto(store, monkeypatch):
    monkeypatch.delenv("GRABLI_MODE")


def events(store, name: str) -> list[dict]:
    lines = store.log_path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == name]


def test_auto_is_the_default_and_review_is_opt_in(monkeypatch):
    assert mode() == "auto"
    monkeypatch.setenv("GRABLI_MODE", "Review")
    assert mode() == "review"


def test_agent_proposal_is_written_to_the_journal_and_marked_unreviewed(store):
    result = do_propose(store, "mistake", "Отчёт без периода", "Период передавать явно.")

    assert result["status"] == "saved"
    (entry,) = store.load()[0]
    assert (entry.id, entry.reviewed) == (result["id"], False)
    assert store.load(pending=True) == ([], [])
    assert "reviewed: false" in (store.entries_dir / f"{entry.id}.md").read_text(encoding="utf-8")
    assert events(store, "propose")[0]["mode"] == "auto"
    with Index(store) as index:
        assert [hit.id for hit in index.search("отчет")] == [entry.id]  # visible at once


def test_reviewed_entries_keep_their_file_format(make_entry):
    text = dump(make_entry())
    assert "reviewed" not in text
    assert parse(text).reviewed is True


def test_same_title_refreshes_the_entry_instead_of_adding_a_copy(store, make_entry):
    old = make_entry(title="Отчёт без периода", last_verified=date.today() - timedelta(days=40))

    result = do_propose(store, "mistake", "  отчет   без ПЕРИОДА ", "другой текст")

    assert (result["status"], result["id"]) == ("exists", old.id)
    (entry,) = store.load()[0]
    assert result["existing"]["body"] == old.body
    assert events(store, "propose")[0]["duplicate"] is True
    # A human-reviewed entry keeps its date; an agent's own entry is refreshed.
    assert store.load()[0][0].last_verified == date.today() - timedelta(days=40)
    make_entry(
        title="Запись агента", reviewed=False, last_verified=date.today() - timedelta(days=40)
    )
    assert do_propose(store, "mistake", "запись агента", "текст")["status"] == "exists"
    assert store.find_by_title("Запись агента", "mistake").last_verified == date.today()


def test_same_title_of_another_kind_is_a_separate_entry(store, make_entry):
    make_entry(title="Забыли миграцию")
    result = do_propose(store, "decision", "Забыли миграцию", "Теперь проверяем в CI.")
    assert result["status"] == "saved"
    assert len(store.load()[0]) == 2


def test_secrets_and_invalid_entries_are_still_refused(store):
    token = "glpat-" + "A1b2" * 6
    assert do_propose(store, "mistake", "Заголовок записи", token)["status"] == "rejected"
    assert do_propose(store, "mistake", "ab", "текст")["status"] == "rejected"
    assert store.load() == ([], [])


def test_agent_can_retract_its_own_entry_and_the_file_is_kept(store):
    entry_id = do_propose(store, "mistake", "Неверный вывод", "текст")["id"]

    assert do_retract(store, entry_id, "вывод не подтвердился")["status"] == "retracted"

    assert store.load() == ([], [])
    assert (store.retracted_dir / f"{entry_id}.md").is_file()
    assert events(store, "retract")[0]["reason"] == "вывод не подтвердился"


def test_agent_cannot_retract_what_a_human_reviewed(store, make_entry):
    reviewed = make_entry()
    result = do_retract(store, reviewed.id, "мне кажется, это неверно")
    assert result["status"] == "refused"
    assert f"grabli remove {reviewed.id}" in result["reason"]
    assert store.get(reviewed.id) == reviewed


def test_retract_needs_a_reason_an_existing_entry_and_a_real_replacement(store):
    entry_id = do_propose(store, "mistake", "Старая запись", "текст")["id"]
    newer = do_propose(store, "mistake", "Новая запись", "текст")["id"]

    assert do_retract(store, entry_id, "  ")["status"] == "refused"
    assert do_retract(store, "m-zzzzzz", "нет такой")["status"] == "not_found"
    assert do_retract(store, "../etc", "путь")["status"] == "refused"
    assert do_retract(store, entry_id, "замена", superseded_by="m-zzzzzz")["status"] == "refused"
    assert do_retract(store, entry_id, "замена", superseded_by=entry_id)["status"] == "refused"

    assert do_retract(store, entry_id, "замена", superseded_by=newer)["status"] == "superseded"
    assert store.get(entry_id).superseded_by == newer
    # No cycles: an entry that is itself superseded cannot be the replacement.
    assert do_retract(store, newer, "обратно", superseded_by=entry_id)["status"] == "refused"
    assert [event["via"] for event in events(store, "retract")] == ["mcp"]
    with Index(store) as index:
        assert [hit.id for hit in index.search("запись")] == [newer]


def test_delivery_frames_entries_as_reference_and_marks_unreviewed_ones(store, make_entry):
    text = render([make_entry(), make_entry(reviewed=False)], "по стеку")
    assert text.count("записано агентом") == 1
    assert text.count(", проверено ") == 1  # only the reviewed one is called verified
    assert "а не инструкции" in text


def test_compaction_capture_writes_to_the_journal(store):
    summary = "<grabli-lessons>\n### mistake: Урок из сжатия\nтекст\n</grabli-lessons>"
    payload = {"session_id": "s1", "compact_summary": summary}
    for _ in range(2):  # the second summary carries the same block
        hooks.run("post-compact", stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO())

    (entry,) = store.load()[0]
    assert (entry.reviewed, entry.source.found_by) == (False, "compact")
    assert [len(event["ids"]) for event in events(store, "compact_capture")] == [1, 0]


def test_cli_propose_by_a_human_is_reviewed_and_reports_duplicates(store, capsys):
    args = ["propose", "Отчёт без периода", "--body", "текст"]
    assert cli.main(args) == 0
    assert "Записано" in capsys.readouterr().out
    assert store.load()[0][0].reviewed is True

    assert cli.main(args) == 0
    assert "Уже есть" in capsys.readouterr().out
    assert len(store.load()[0]) == 1


def test_cli_recent_accept_and_remove(store, make_entry, capsys):
    reviewed = make_entry(title="Проверенная запись")
    auto_id = do_propose(store, "mistake", "Запись агента", "текст", found_by="code-review")["id"]

    assert cli.main(["recent"]) == 0
    out = capsys.readouterr().out
    assert out.index(auto_id) < out.index(reviewed.id)  # newest first
    assert "авто" in out and "code-review" in out
    assert cli.main(["recent", "--unreviewed"]) == 0
    assert reviewed.id not in capsys.readouterr().out

    assert cli.main(["accept", auto_id]) == 0
    assert store.get(auto_id).reviewed is True

    assert cli.main(["remove", reviewed.id, "--reason", "устарело"]) == 0
    assert store.get(reviewed.id) is None
    assert (store.retracted_dir / f"{reviewed.id}.md").is_file()
    assert cli.main(["remove", reviewed.id]) == 1


def test_cli_review_walks_unreviewed_journal_entries(store, monkeypatch, capsys):
    keep = do_propose(store, "mistake", "Оставить эту запись", "текст")["id"]
    drop = do_propose(store, "mistake", "Убрать эту запись", "текст")["id"]
    answers = {keep: "a", drop: "r"}
    order = iter(answers[entry.id] for entry in store.load()[0])
    monkeypatch.setattr("builtins.input", lambda _: next(order))

    assert cli.main(["review"]) == 0

    (entry,) = store.load()[0]
    assert (entry.id, entry.reviewed) == (keep, True)
    assert (store.retracted_dir / f"{drop}.md").is_file()


# --- review of the autonomous mode ------------------------------------------


def test_cli_from_an_agent_shell_cannot_claim_human_review(store, make_entry, monkeypatch, capsys):
    monkeypatch.setattr("grabli.cli._human", lambda: False)  # stdout is a pipe
    reviewed = make_entry()

    assert cli.main(["propose", "Запись через Bash", "--body", "текст"]) == 0
    piped = store.find_by_title("Запись через Bash", "mistake")
    assert (piped.reviewed, piped.source.found_by) == (False, "agent")

    for command in (["accept", piped.id], ["remove", reviewed.id], ["review"], ["mode", "review"]):
        assert cli.main(command) == 1
    assert "only in a terminal" in capsys.readouterr().err
    assert store.get(piped.id).reviewed is False
    assert store.get(reviewed.id) == reviewed


def test_search_cards_say_who_wrote_the_entry(store, make_entry):
    make_entry(title="Отчёт проверенный")
    do_propose(store, "mistake", "Отчёт от агента", "текст")
    with Index(store) as index:
        cards = {hit.title: hit.card()["reviewed"] for hit in index.search("отчет")}
    assert cards == {"Отчёт проверенный": True, "Отчёт от агента": False}


def test_mode_is_stored_in_the_home_directory_and_env_wins(store, monkeypatch, capsys):
    assert cli.main(["mode", "review"]) == 0
    assert mode() == "review"  # no env: this is what hooks and the MCP server see
    assert do_propose(store, "mistake", "В очередь", "текст")["status"] == "pending"

    monkeypatch.setenv("GRABLI_MODE", "auto")
    assert mode() == "auto"
    assert cli.main(["mode"]) == 0
    assert "Режим: auto" in capsys.readouterr().out


def test_server_instructions_follow_the_mode(store, monkeypatch):
    from grabli.server import instructions

    assert "сохраняется в журнал сразу" in instructions("auto")
    assert "после подтверждения человеком" in instructions("review")


def test_compaction_counts_a_concurrent_duplicate_instead_of_failing(store, monkeypatch):
    monkeypatch.setattr("grabli.capture._seen", lambda _store: set())  # a stale view
    do_propose(store, "mistake", "Урок из сжатия", "текст")
    summary = (
        "<grabli-lessons>\n### mistake: Урок из сжатия\nтекст\n"
        "### mistake: Второй урок\nтекст\n</grabli-lessons>"
    )
    hooks.run(
        "post-compact",
        stdin=io.StringIO(json.dumps({"session_id": "s1", "compact_summary": summary})),
        stdout=io.StringIO(),
    )
    (captured,) = events(store, "compact_capture")
    assert (captured["duplicates"], len(captured["ids"])) == (1, 1)
    assert events(store, "hook_error") == []


def test_similar_list_is_not_shortened_by_the_new_entry_itself(store, make_entry):
    for number in range(3):
        make_entry(title=f"Миграции без отката {number}")
    result = do_propose(store, "mistake", "Миграции без отката", "текст")
    assert len(result["similar"]) == 3
    assert result["id"] not in {item["id"] for item in result["similar"]}
