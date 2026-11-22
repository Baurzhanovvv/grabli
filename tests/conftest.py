from __future__ import annotations

from datetime import date

import pytest

from grabli.models import Entry
from grabli.store import Store


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """No test may touch the real ~/.grabli, whether or not it asks for `store`."""
    monkeypatch.setenv("GRABLI_HOME", str(tmp_path / "home"))


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    # The queue is the opt-in mode; tests of the default (auto) mode live in test_auto.py.
    monkeypatch.setenv("GRABLI_MODE", "review")
    monkeypatch.setattr("grabli.cli._human", lambda: True)  # tests act as a person at a terminal
    return Store()


@pytest.fixture
def make_entry(store):
    def _make(pending: bool = False, **overrides) -> Entry:
        kind = overrides.get("kind", "mistake")
        fields = {
            "id": store.new_id(kind),
            "kind": kind,
            "title": "YTD без привязки к отчётному периоду",
            "project": "finance-app",
            "stack": ["nestjs", "postgres"],
            "paths": ["**/reports/**"],
            "created": date(2026, 11, 1),
            "last_verified": date(2026, 11, 1),
            "body": "## Что случилось\nОтчёт суммировал от начала календарного года.",
            **overrides,
        }
        entry = Entry.model_validate(fields)
        store.save(entry, pending=pending)
        return entry

    return _make


@pytest.fixture
def repo_file(tmp_path) -> str:
    """Path of a file inside a NestJS repo that the default entry's globs match."""
    root = tmp_path / "svc"
    (root / ".git").mkdir(parents=True)
    (root / "package.json").write_text('{"dependencies": {"@nestjs/core": "11"}}', encoding="utf-8")
    return str(root / "src" / "reports" / "ytd.service.ts")
