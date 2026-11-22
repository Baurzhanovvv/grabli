"""Regressions for defects found in the first code review."""

from __future__ import annotations

import io
import json
import sqlite3

import pytest

from grabli import cli, hooks
from grabli.context import for_file
from grabli.index import Index
from grabli.models import EntryError
from grabli.server import do_propose, do_search
from grabli.store import find_secret


@pytest.mark.parametrize(("title", "body"), [("good title", "  \n "), ("   ", "real body")])
def test_whitespace_only_text_is_rejected_not_saved_unreadable(store, title, body):
    assert do_propose(store, "mistake", title, body)["status"] == "rejected"
    assert store.load(pending=True) == ([], [])


@pytest.mark.parametrize("pattern", ["!", "\\"])
def test_invalid_glob_is_rejected_at_propose(store, pattern):
    result = do_propose(store, "mistake", "Заголовок записи", "текст", paths=[pattern])
    assert result["status"] == "rejected"
    assert "invalid glob" in result["reason"]


def test_hand_edited_bad_glob_does_not_silence_other_entries(tmp_path, store, make_entry):
    good = make_entry(stack=[], paths=["**/*.ts"])
    broken = make_entry(stack=[], paths=["**/*.ts"])
    path = store.entries_dir / f"{broken.id}.md"
    path.write_text(path.read_text(encoding="utf-8").replace("'**/*.ts'", "'!'"), encoding="utf-8")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    payload = {"session_id": "s1", "tool_input": {"file_path": str(tmp_path / "repo" / "a.ts")}}

    out = io.StringIO()
    assert hooks.run("pre-edit", stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0

    assert good.id in out.getvalue()
    assert any(broken.id in error for error in store.load()[1])


@pytest.mark.parametrize("tags", [["go"], ["postgresql", "nest"]])
def test_undetectable_stack_tags_do_not_hide_a_path_match(tmp_path, make_entry, tags):
    entry = make_entry(stack=tags, paths=["**/*.go"])
    foreign = make_entry(stack=["django"], paths=["**/*.go"])
    root = tmp_path / "svc"
    root.mkdir()
    assert for_file([entry, foreign], root, root / "main.go", {"docker"}) == [entry]


def test_locked_index_is_searched_stale_and_never_deleted(store, make_entry, monkeypatch):
    first = make_entry()
    with Index(store):
        pass
    make_entry(title="Вторая запись про отчёт")
    monkeypatch.setattr(Index, "busy_timeout", 0.05)
    holder = sqlite3.connect(store.cache_dir / "index.sqlite", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with Index(store) as index:
            assert [h.id for h in index.search("отчет")] == [first.id]  # the pre-lock state
            assert any("stale" in error for error in index.errors)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    with Index(store) as index:
        assert len(index.search("отчет")) == 2


def test_block_mode_without_session_id_falls_back_to_context(repo_file, store, make_entry):
    make_entry()
    payload = {"session_id": "", "tool_input": {"file_path": repo_file}}
    for _ in range(3):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run(
            "pre-edit", stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err, block=True
        )
        assert code == 0
        assert err.getvalue() == ""
        assert "additionalContext" in out.getvalue()


def test_entries_are_delivered_again_after_compaction(repo_file, store, make_entry):
    entry = make_entry()
    payload = {"session_id": "s1", "tool_input": {"file_path": repo_file}}

    def deliver() -> str:
        out = io.StringIO()
        hooks.run("pre-edit", stdin=io.StringIO(json.dumps(payload)), stdout=out)
        return out.getvalue()

    assert entry.id in deliver()
    assert deliver() == ""
    store.log("session_start", session_id="s1", source="resume", ids=[])
    assert deliver() == ""  # a resumed session still has its context
    store.log("session_start", session_id="s1", source="compact", ids=[])
    assert entry.id in deliver()


def test_secrets_in_metadata_fields_are_rejected(store):
    token = "glpat-" + "A1b2" * 6
    result = do_propose(
        store, "mistake", "Заголовок записи", "текст", repo=f"https://oauth2:{token}@gitlab.com/x/y"
    )
    assert result["status"] == "rejected"
    assert "`repo`" in result["reason"]
    assert token not in result["reason"]
    assert store.load(pending=True) == ([], [])


@pytest.mark.parametrize(
    "text",
    [
        "DATABASE_URL=postgres://u:" + "Sup3rSecret" + "@h/db",
        "Authorization: Bearer " + "abcdef1234567890" * 2,
        'password = "' + "hunter2hunter2" + '"',
    ],
)
def test_secret_patterns_cover_urls_bearer_and_short_passwords(text):
    assert find_secret(text) is not None


def test_secret_patterns_leave_plain_urls_alone():
    assert find_secret("см. https://example.com:8080/docs и git@github.com:org/repo.git") is None


def test_accept_refuses_mismatched_id_and_existing_entry(store, make_entry):
    existing = make_entry(body="оригинал")
    draft = make_entry(pending=True)
    draft_path = store.pending_dir / f"{draft.id}.md"
    draft_path.write_text(
        draft_path.read_text(encoding="utf-8").replace(draft.id, existing.id), encoding="utf-8"
    )
    with pytest.raises(EntryError, match="holds id"):
        store.accept(draft.id)

    clash = make_entry(pending=True, id=existing.id, body="подмена")
    with pytest.raises(EntryError, match="not overwriting"):
        store.accept(clash.id)
    assert store.get(existing.id).body == "оригинал"


def test_stack_filter_is_applied_before_the_limit(store, make_entry):
    for number in range(12):
        make_entry(title=f"Миграции миграции {number}", stack=["typeorm"], body="Миграции.")
    rare = make_entry(
        title="Схема базы", stack=["prisma"], body="Длинный текст, где миграции мельком."
    )
    assert [
        c["id"] for c in do_search(store, query="миграции", stack=["prisma"], limit=2)["results"]
    ] == [rare.id]


def test_cli_search_clamps_nonpositive_limit(store, make_entry, capsys):
    for number in range(3):
        make_entry(title=f"Отчёт номер {number}")
    assert cli.main(["search", "отчет", "--limit", "-1"]) == 0
    assert capsys.readouterr().out.count("mistake") == 1


# --- second review: the `grabli propose` command ----------------------------


def _events(store) -> list[dict]:
    return [json.loads(line) for line in store.log_path.read_text(encoding="utf-8").splitlines()]


def test_propose_keeps_the_draft_when_similar_search_fails(store, monkeypatch):
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("grabli.server.Index", locked)
    result = do_propose(store, "mistake", "Заголовок записи", "текст")

    assert result["status"] == "pending"
    assert "database is locked" in result["warnings"][0]
    assert [draft.id for draft in store.load(pending=True)[0]] == [result["id"]]


def test_event_log_tells_cli_proposals_from_agent_ones(store, monkeypatch):
    do_propose(store, "mistake", "Заголовок от агента", "текст")
    monkeypatch.setattr("sys.stdin", io.StringIO("текст"))
    assert cli.main(["propose", "Заголовок от человека"]) == 0
    assert [event["via"] for event in _events(store) if event["event"] == "propose"] == [
        "mcp",
        "cli",
    ]


def test_cli_stack_values_are_split_into_tags(store, capsys):
    args = ["propose", "Заголовок записи", "--body", "текст"]
    assert cli.main([*args, "--stack", "nestjs,postgres", "--stack", "TypeORM redis"]) == 0
    (draft,) = store.load(pending=True)[0]
    assert draft.stack == ["nestjs", "postgres", "typeorm", "redis"]


def test_cli_keeps_typed_text_when_the_draft_is_rejected(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("Три абзаца, которые жалко потерять."))

    assert cli.main(["propose", "ab"]) == 1  # title too short

    err = capsys.readouterr().err
    (kept,) = tmp_path.glob("grabli-draft-*.md")
    assert str(kept) in err
    assert kept.read_text(encoding="utf-8") == "Три абзаца, которые жалко потерять."


def test_cli_reports_storage_errors_without_a_traceback(store, monkeypatch, capsys):
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("grabli.cli.Index", locked)
    assert cli.main(["search", "отчет"]) == 1
    assert "database is locked" in capsys.readouterr().err
