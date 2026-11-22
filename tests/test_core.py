from __future__ import annotations

from datetime import date

import pytest

from grabli.index import Index
from grabli.models import EntryError, dump, parse
from grabli.store import find_secret

VALID = """---
id: m-abc123
kind: mistake
title: YTD без привязки к отчётному периоду
stack: [NestJS, postgres, nestjs]
paths: ["**/reports/**"]
created: 2026-11-22
last_verified: 2026-11-22
superseded_by: null
---
## Что случилось
Отчёт суммировал от начала года.
"""


# --- entry format ---------------------------------------------------------


def test_parse_dump_roundtrip():
    entry = parse(VALID)
    assert entry.stack == ["nestjs", "postgres"]  # lowercased, deduplicated
    assert entry.created == date(2026, 11, 22)
    assert parse(dump(entry)) == entry


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no frontmatter here", "does not start"),
        ("---\nid: m-abc123\n", "not closed"),
        (VALID.replace("kind: mistake", "kind: mistake\nseverity: high"), "severity"),
        (VALID.replace("kind: mistake", "kind: decision"), "does not match kind"),
        (VALID.replace("id: m-abc123", "id: ../../etc"), "id"),
    ],
)
def test_parse_names_the_problem(text, expected):
    with pytest.raises(EntryError, match=expected):
        parse(text)


# --- store ----------------------------------------------------------------


def test_save_then_load_and_get(store, make_entry):
    entry = make_entry()
    entries, errors = store.load()
    assert entries == [entry]
    assert errors == []
    assert store.get(entry.id) == entry


def test_load_reports_broken_file_and_keeps_the_rest(store, make_entry):
    good = make_entry()
    (store.entries_dir / "m-broken.md").write_text("not an entry", encoding="utf-8")
    renamed = make_entry()
    (store.entries_dir / f"{renamed.id}.md").rename(store.entries_dir / "m-zzzzzz.md")

    entries, errors = store.load()

    assert entries == [good]
    assert len(errors) == 2
    assert any("m-broken.md" in e for e in errors)
    assert any("does not match the file name" in e for e in errors)


def test_get_rejects_path_like_id(store):
    with pytest.raises(EntryError, match="invalid id"):
        store.get("../secrets")


def test_accept_moves_draft_and_stamps_today(store, make_entry):
    draft = make_entry(pending=True)
    assert store.load()[0] == []

    accepted = store.accept(draft.id)

    assert accepted.last_verified == date.today()
    assert store.get(draft.id) == accepted
    assert store.load(pending=True)[0] == []


def test_reject_removes_draft(store, make_entry):
    draft = make_entry(pending=True)
    store.reject(draft.id)
    assert store.load(pending=True)[0] == []
    assert store.load()[0] == []


def test_accept_and_reject_unknown_draft_fail(store):
    with pytest.raises(EntryError, match="no pending draft"):
        store.accept("m-bbbbbb")
    with pytest.raises(EntryError, match="no pending draft"):
        store.reject("m-aaaaaa")


def test_shown_ids_are_per_session(store):
    store.log("pre_edit", session_id="s1", ids=["m-aaaaaa"])
    store.log("session_start", session_id="s1", ids=["m-bbbbbb"])
    store.log("pre_edit", session_id="s2", ids=["m-cccccc"])
    store.log("search", session_id="s1", ids=["m-dddddd"])
    assert store.shown_ids("s1") == {"m-aaaaaa", "m-bbbbbb"}
    assert store.shown_ids("") == set()


# --- secret guard ---------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "sk-" + "a1B2" * 8,
        "AKIA" + "ABCDEFGH12345678",
        "ghp_" + "x9" * 20,
        "-----BEGIN RSA PRIVATE KEY-----",
        "API_" + "KEY=" + "abcd1234" * 3,  # split so secret scanners do not flag this fixture
        'password: "' + "Zx9" * 6 + '"',
    ],
)
def test_find_secret_reports_line_not_value(line):
    found = find_secret(f"первая строка\n{line}")
    assert found is not None
    assert found.endswith("on line 2")
    assert line not in found


@pytest.mark.parametrize(
    "text",
    [
        'token = request.headers.get("authorization")',
        "Токен хранить в переменной окружения, а не в коде.",
        "password: передавать через secret manager",
        "sk-short",
    ],
)
def test_find_secret_ignores_prose_and_code(text):
    assert find_secret(text) is None


# --- index ----------------------------------------------------------------


def test_search_folds_yo_and_matches_inflected_forms(store, make_entry):
    entry = make_entry()
    with Index(store) as index:
        assert [h.id for h in index.search("отчет")] == [entry.id]  # е vs ё
        assert [h.id for h in index.search("календарный год")] == [entry.id]  # prefix match
        assert index.search("kubernetes") == []
        assert index.search("  ") == []


def test_title_match_outranks_body_match(store, make_entry):
    in_body = make_entry(title="Пагинация через декоратор", body="Не забыть про миграции схемы.")
    in_title = make_entry(title="Миграции без отката", body="Писать down для каждой.")
    with Index(store) as index:
        assert [h.id for h in index.search("миграции")] == [in_title.id, in_body.id]


def test_filters_and_superseded(store, make_entry):
    mistake = make_entry()
    decision = make_entry(kind="decision", title="Отчёты считаем в UTC", stack=["fastapi"])
    make_entry(title="Отчёт старый подход", superseded_by=mistake.id)
    with Index(store) as index:
        assert {h.id for h in index.search("отчет")} == {mistake.id, decision.id}
        assert [h.id for h in index.search("отчет", kind="decision")] == [decision.id]
        assert [h.id for h in index.search("отчет", stack=["NestJS"])] == [mistake.id]
        assert index.search("отчет", project="other") == []


def test_hit_card_has_snippet_from_matching_line(store, make_entry):
    make_entry()
    with Index(store) as index:
        card = index.search("календарного")[0].card()
    assert card["snippet"] == "Отчёт суммировал от начала календарного года."
    assert set(card) == {"id", "kind", "title", "project", "stack", "score", "snippet", "reviewed"}
    assert card["score"] > 0


def test_index_follows_manual_edits_and_deletions(store, make_entry):
    entry = make_entry()
    with Index(store) as index:
        assert index.search("суммировал")
    path = store.entries_dir / f"{entry.id}.md"

    edited = path.read_text(encoding="utf-8").replace("суммировал", "складывал")
    path.write_text(edited, encoding="utf-8")
    with Index(store) as index:
        assert index.search("суммировал") == []
        assert [h.id for h in index.search("складывал")] == [entry.id]

    path.unlink()
    with Index(store) as index:
        assert index.search("складывал") == []


def test_damaged_cache_is_rebuilt_from_files(store, make_entry):
    entry = make_entry()
    with Index(store):
        pass
    (store.cache_dir / "index.sqlite").write_bytes(b"this is not a database")
    with Index(store) as index:
        assert [h.id for h in index.search("отчет")] == [entry.id]


def test_index_surfaces_unreadable_files(store, make_entry):
    make_entry()
    (store.entries_dir / "m-broken.md").write_text("garbage", encoding="utf-8")
    with Index(store) as index:
        assert len(index.search("отчет")) == 1
        assert index.errors and "m-broken.md" in index.errors[0]


def test_dump_writes_equal_dates_without_yaml_anchors(make_entry):
    today = date.today()  # one shared object is what makes PyYAML emit an anchor
    text = dump(make_entry(created=today, last_verified=today))
    assert "&id" not in text and "*id" not in text
    assert parse(text).created == parse(text).last_verified
