"""Entry format: a markdown file with YAML frontmatter."""

from __future__ import annotations

import secrets
import string
from datetime import date
from typing import Literal

import pathspec
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Kind = Literal["mistake", "decision"]

_ID_ALPHABET = string.ascii_lowercase + string.digits
_DELIMITER = "---"


class EntryError(ValueError):
    """A file or a proposal is not a valid entry."""


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str | None = None
    commit: str | None = None
    found_by: str | None = None


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[md]-[a-z0-9]{6}$")
    kind: Kind
    title: str = Field(min_length=3, max_length=200)
    project: str | None = None
    stack: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    source: Source | None = None
    created: date
    last_verified: date
    superseded_by: str | None = None
    body: str = Field(min_length=1)

    @field_validator("stack")
    @classmethod
    def _normalize_stack(cls, value: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for tag in value:
            tag = tag.strip().lower()
            if tag:
                seen[tag] = None
        return list(seen)

    @field_validator("paths")
    @classmethod
    def _valid_globs(cls, value: list[str]) -> list[str]:
        patterns = [p.strip() for p in value if p.strip()]
        for pattern in patterns:
            try:
                pathspec.PathSpec.from_lines("gitignore", [pattern])
            except Exception as exc:  # pathspec raises its own error types per pattern kind
                raise ValueError(f"invalid glob {pattern!r}: {exc}") from exc
        return patterns

    @field_validator("title", "body", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        # Before length checks, so whitespace-only text is rejected instead of saved empty.
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _id_matches_kind(self) -> Entry:
        if self.id[0] != self.kind[0]:
            raise ValueError(f"id {self.id!r} does not match kind {self.kind!r}")
        return self


def new_id(kind: Kind) -> str:
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))
    return f"{kind[0]}-{suffix}"


def parse(text: str) -> Entry:
    """Parse the text of an entry file. Raises EntryError naming what is wrong."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        raise EntryError("file does not start with a '---' frontmatter block")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == _DELIMITER)
    except StopIteration:
        raise EntryError("frontmatter block is not closed with '---'") from None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise EntryError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise EntryError("frontmatter must be a mapping")
    return build({**meta, "body": "\n".join(lines[end + 1 :])})


def build(fields: dict) -> Entry:
    """Validate fields into an Entry. Raises EntryError with field names."""
    try:
        return Entry.model_validate(fields)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'entry'}: {err['msg']}"
            for err in exc.errors()
        )
        raise EntryError(problems) from exc


def dump(entry: Entry) -> str:
    """Serialize an entry to file text. Deterministic, so it doubles as a fingerprint source."""
    meta = entry.model_dump(exclude={"body"})
    for optional in ("project", "source"):
        if meta[optional] is None:
            del meta[optional]
    if meta.get("source"):
        meta["source"] = {k: v for k, v in meta["source"].items() if v is not None}
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=None)
    return f"{_DELIMITER}\n{front}{_DELIMITER}\n{entry.body}\n"
