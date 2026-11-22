"""File store: entries and pending drafts as markdown files under GRABLI_HOME."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from .models import Entry, EntryError, Kind, dump, new_id, parse, title_key

_ID_RE = re.compile(r"^[md]-[a-z0-9]{6}$")
_GITIGNORE = "cache/\nlog.jsonl\nactivity.jsonl\n"
_SHOWN_EVENTS = {"session_start", "pre_edit"}
_CONTEXT_LOST = {"compact", "clear"}  # SessionStart sources after which earlier context is gone

# Names only: a match is reported by pattern name and line, never by value.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("credentials in URL", re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{3,}@")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}")),
    (
        "assigned secret",
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*['\"]?"
            r"(?=[A-Za-z0-9+/_-]*\d)[A-Za-z0-9+/_-]{12,}"
        ),
    ),
]


def home() -> Path:
    return Path(os.environ.get("GRABLI_HOME") or "~/.grabli").expanduser()


MODES = ("auto", "review")


def mode() -> str:
    """`auto`: proposals go straight into the journal. `review`: they wait in the queue.

    `GRABLI_MODE` wins; otherwise the `mode` file in the home directory. The file is what
    reaches hooks and the MCP server, which Claude Code starts in its own environment.
    """
    chosen = os.environ.get("GRABLI_MODE", "").strip().lower()
    if chosen not in MODES:
        try:
            chosen = (home() / "mode").read_text(encoding="utf-8").strip().lower()
        except (OSError, UnicodeDecodeError):
            chosen = ""
    return chosen if chosen in MODES else "auto"


def find_secret(text: str) -> str | None:
    """Describe the first thing in text that looks like a credential, or None."""
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                return f"{name} on line {number}"
    return None


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or home()
        self.entries_dir = self.root / "entries"
        self.pending_dir = self.root / "pending"
        self.cache_dir = self.root / "cache"
        self.retracted_dir = self.root / "retracted"
        self.log_path = self.root / "log.jsonl"
        self.activity_path = self.root / "activity.jsonl"

    def _ensure(self) -> None:
        for directory in (self.entries_dir, self.pending_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE, encoding="utf-8")
            return
        # A journal created by an older version: add what it does not ignore yet.
        try:
            text = gitignore.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        present = {line.strip() for line in text.splitlines()}
        missing = [line for line in _GITIGNORE.splitlines() if line not in present]
        if missing:
            joint = "" if text.endswith("\n") or not text else "\n"
            gitignore.write_text(text + joint + "\n".join(missing) + "\n", encoding="utf-8")

    def _dir(self, pending: bool) -> Path:
        return self.pending_dir if pending else self.entries_dir

    def _path(self, entry_id: str, pending: bool) -> Path:
        if not _ID_RE.match(entry_id):
            raise EntryError(f"invalid id {entry_id!r}")
        return self._dir(pending) / f"{entry_id}.md"

    def load(self, pending: bool = False) -> tuple[list[Entry], list[str]]:
        """All readable entries plus one message per file that could not be read."""
        entries: list[Entry] = []
        errors: list[str] = []
        directory = self._dir(pending)
        if not directory.is_dir():
            return entries, errors
        for path in sorted(directory.glob("*.md")):
            try:
                entry = parse(path.read_text(encoding="utf-8"))
                if entry.id != path.stem:
                    raise EntryError(f"id {entry.id!r} does not match the file name")
            except (EntryError, OSError, UnicodeDecodeError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            entries.append(entry)
        return entries, errors

    def get(self, entry_id: str, pending: bool = False) -> Entry | None:
        path = self._path(entry_id, pending)
        if not path.is_file():
            return None
        entry = parse(path.read_text(encoding="utf-8"))
        if entry.id != entry_id:
            raise EntryError(f"file {path.name} holds id {entry.id!r}")
        return entry

    def new_id(self, kind: Kind) -> str:
        while True:
            candidate = new_id(kind)
            taken = any(self._path(candidate, p).exists() for p in (False, True))
            if not taken and not (self.retracted_dir / f"{candidate}.md").exists():
                return candidate

    def save(self, entry: Entry, pending: bool = False) -> Path:
        """Write through a temp file and rename, so a reader never sees half a file."""
        self._ensure()
        path = self._path(entry.id, pending)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{entry.id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(dump(entry))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def accept(self, entry_id: str) -> Entry:
        """Move a pending draft into the journal, stamped as verified today."""
        draft = self.get(entry_id, pending=True)
        if draft is None:
            unreviewed = self.get(entry_id)
            if unreviewed is None or unreviewed.reviewed:
                raise EntryError(f"no pending draft {entry_id!r}")
            # Written in auto mode: accepting it means a human has now read it.
            entry = unreviewed.model_copy(update={"reviewed": True, "last_verified": date.today()})
            self.save(entry)
            self.log("accept", ids=[entry_id])
            return entry
        if self._path(entry_id, pending=False).exists():
            raise EntryError(f"journal already has an entry {entry_id!r}; not overwriting it")
        entry = draft.model_copy(update={"reviewed": True, "last_verified": date.today()})
        self.save(entry)
        self._path(entry_id, pending=True).unlink()
        self.log("accept", ids=[entry_id])
        return entry

    def reject(self, entry_id: str) -> None:
        path = self._path(entry_id, pending=True)
        if not path.is_file():
            raise EntryError(f"no pending draft {entry_id!r}")
        path.unlink()
        self.log("reject", ids=[entry_id])

    def set_mode(self, chosen: str) -> None:
        if chosen not in MODES:
            raise EntryError(f"unknown mode {chosen!r}")
        self._ensure()
        (self.root / "mode").write_text(chosen + "\n", encoding="utf-8")

    def retract(self, entry_id: str, reason: str, via: str = "cli") -> Path:
        """Take an entry out of the journal. The file moves to `retracted/`, it is not deleted."""
        path = self._path(entry_id, pending=False)
        if not path.is_file():
            raise EntryError(f"no entry {entry_id!r} in the journal")
        self.retracted_dir.mkdir(parents=True, exist_ok=True)
        target = self.retracted_dir / path.name
        os.replace(path, target)
        self.log("retract", ids=[entry_id], reason=reason, via=via)
        return target

    def find_by_title(self, title: str, kind: Kind) -> Entry | None:
        """The active journal entry of this kind with the same title, if any."""
        key = title_key(title)
        for entry in self.load()[0]:
            same = entry.kind == kind and title_key(entry.title) == key
            if same and entry.superseded_by is None:
                return entry
        return None

    def log(self, event: str, **fields: object) -> None:
        """Append one usage event. The log is what the evals read."""
        self._ensure()
        record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def activity(self, event: str, root: Path | str, **fields: object) -> None:
        """Append one project activity event. Read by `grabli status`.

        Kept apart from `log.jsonl`: this file gets a line per edit, and the log is read
        in full by `shown_ids` on every edit.
        """
        self._ensure()
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            "root": str(Path(root).resolve()),
            **{key: value for key, value in fields.items() if value is not None},
        }
        with self.activity_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def shown_ids(self, session_id: str) -> set[str]:
        """Entries a hook has already delivered in this session."""
        shown: set[str] = set()
        if not session_id or not self.log_path.is_file():
            return shown
        with self.log_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("session_id") != session_id:
                    continue
                if record.get("event") not in _SHOWN_EVENTS:
                    continue
                if record.get("source") in _CONTEXT_LOST:
                    shown.clear()  # the agent no longer has what was delivered before this
                shown.update(record.get("ids") or [])
        return shown
