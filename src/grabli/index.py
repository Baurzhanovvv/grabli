"""Lexical search over entries: SQLite FTS5 with BM25.

The index is a disposable cache. Every open re-syncs it against the entry
files by content fingerprint, so manual edits and `git pull` are picked up.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass

import snowballstemmer

from .models import Entry, dump
from .store import Store

SCHEMA_VERSION = 2
_MAX_TOKENS = 24
_PREFIX_MIN = 4
_SNIPPET_CHARS = 180
_CYRILLIC = re.compile(r"[а-я]")
_RUSSIAN = snowballstemmer.stemmer("russian")

_SCHEMA = """
CREATE TABLE meta (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    project TEXT, stack TEXT NOT NULL, superseded INTEGER NOT NULL, body TEXT NOT NULL,
    reviewed INTEGER NOT NULL
);
CREATE VIRTUAL TABLE fts USING fts5(
    id UNINDEXED, title, tags, body, tokenize = 'unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True)
class Hit:
    id: str
    kind: str
    title: str
    project: str | None
    stack: list[str]
    score: float
    snippet: str
    reviewed: bool = True  # False: written without a human, see Entry.reviewed

    def card(self) -> dict:
        return asdict(self)


def normalize(text: str) -> str:
    """Fold case and ё/е, which unicode61 keeps apart."""
    return text.lower().replace("ё", "е")


def tokens(query: str) -> list[str]:
    """Query terms. Russian words are reduced to stems so inflected forms match by prefix."""
    found = []
    for token in re.findall(r"\w+", normalize(query)):
        if _CYRILLIC.search(token):
            token = _RUSSIAN.stemWord(token)
        if len(token) >= 2:
            found.append(token)
    return list(dict.fromkeys(found))[:_MAX_TOKENS]


def _match_expression(terms: list[str]) -> str:
    # Stems always match as prefixes. Latin terms do so only when long enough,
    # which keeps short identifiers like "ytd" exact.
    return " OR ".join(
        f'"{t}"*' if _CYRILLIC.search(t) or len(t) >= _PREFIX_MIN else f'"{t}"' for t in terms
    )


def _snippet(body: str, terms: list[str]) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    prose = [line for line in lines if not line.startswith("#")] or lines
    chosen = next((line for line in prose if any(t in normalize(line) for t in terms)), None)
    chosen = chosen or (prose[0] if prose else "")
    return chosen if len(chosen) <= _SNIPPET_CHARS else chosen[: _SNIPPET_CHARS - 1] + "…"


def _fingerprint(entry: Entry) -> str:
    return hashlib.sha256(dump(entry).encode("utf-8")).hexdigest()


class Index:
    busy_timeout = 5.0  # seconds to wait for another process's write lock

    def __init__(self, store: Store) -> None:
        self.store = store
        self.path = store.cache_dir / "index.sqlite"
        self.errors: list[str] = []
        try:
            self._open()
            self._sync_unless_busy()
        except sqlite3.OperationalError:
            raise  # busy or unusable, but not proof of damage: never delete on this
        except sqlite3.DatabaseError:
            # A damaged cache is rebuilt from the files, which are the source of truth.
            self.close()
            for suffix in ("", "-journal", "-wal", "-shm"):
                self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
            self._open()
            self._sync_unless_busy()

    def _sync_unless_busy(self) -> None:
        """If another process holds the write lock it is syncing the same files: search as is."""
        try:
            self.sync()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            self.errors.append("index is being updated by another process; results may be stale")

    def _open(self) -> None:
        self.store.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None)
        if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self.db.executescript(
                "DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS fts;"
                + _SCHEMA
                + f"PRAGMA user_version = {SCHEMA_VERSION};"
            )

    def close(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            db.close()
            self.db = None

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def sync(self) -> None:
        entries, self.errors = self.store.load()
        current = {entry.id: (_fingerprint(entry), entry) for entry in entries}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            indexed = dict(self.db.execute("SELECT id, fingerprint FROM meta").fetchall())
            stale = [i for i, fp in indexed.items() if i not in current or current[i][0] != fp]
            for entry_id in stale:
                self.db.execute("DELETE FROM meta WHERE id = ?", (entry_id,))
                self.db.execute("DELETE FROM fts WHERE id = ?", (entry_id,))
            for entry_id, (fingerprint, entry) in current.items():
                if indexed.get(entry_id) == fingerprint:
                    continue
                self._insert(entry, fingerprint)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _insert(self, entry: Entry, fingerprint: str) -> None:
        self.db.execute(
            "INSERT INTO meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                fingerprint,
                entry.kind,
                entry.title,
                entry.project,
                json.dumps(entry.stack),
                int(entry.superseded_by is not None),
                entry.body,
                int(entry.reviewed),
            ),
        )
        tags = " ".join([entry.kind, entry.project or "", *entry.stack])
        self.db.execute(
            "INSERT INTO fts VALUES (?, ?, ?, ?)",
            (entry.id, normalize(entry.title), normalize(tags), normalize(entry.body)),
        )

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        project: str | None = None,
        stack: list[str] | None = None,
        limit: int = 5,
    ) -> list[Hit]:
        terms = tokens(query)
        if not terms:
            return []
        sql = (
            "SELECT m.id, m.kind, m.title, m.project, m.stack, m.body, m.reviewed, "
            "bm25(fts, 0.0, 3.0, 2.0, 1.0) AS rank "
            "FROM fts JOIN meta m ON m.id = fts.id WHERE fts MATCH ? AND m.superseded = 0"
        )
        params: list[object] = [_match_expression(terms)]
        if kind:
            sql += " AND m.kind = ?"
            params.append(kind)
        if project:
            sql += " AND m.project = ?"
            params.append(project)
        wanted = sorted({tag.strip().lower() for tag in stack or [] if tag.strip()})
        if wanted:
            # Filter before LIMIT, or a rare stack is cut off by better-ranked foreign entries.
            marks = ", ".join("?" for _ in wanted)
            sql += f" AND EXISTS (SELECT 1 FROM json_each(m.stack) WHERE value IN ({marks}))"
            params.extend(wanted)
        sql += " ORDER BY rank LIMIT ?"
        params.append(max(1, limit))

        hits: list[Hit] = []
        for (
            entry_id,
            entry_kind,
            title,
            entry_project,
            stack_json,
            body,
            reviewed,
            rank,
        ) in self.db.execute(sql, params):
            entry_stack = json.loads(stack_json)
            hits.append(
                Hit(
                    id=entry_id,
                    kind=entry_kind,
                    title=title,
                    project=entry_project,
                    stack=entry_stack,
                    score=-rank,
                    snippet=_snippet(body, terms),
                    reviewed=bool(reviewed),
                )
            )
        return hits
