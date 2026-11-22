# grabli specification

Status: living document. It describes what the code does today, not what is
planned. Any change to behavior updates this file in the same commit.
Planned work lives in section 12.

## 1. Purpose

grabli is a cross-project journal of an AI coding agent's mistakes and
decisions, delivered back to the agent by Claude Code hooks.

The project exists to answer one question with numbers: **does this kind of
memory reduce an agent's repeat mistakes?** The server and the hooks are the
instrument; the measurements (section 12) are the result.

## 2. Non-goals

- Not a general knowledge base or note-taking tool. Two entry kinds only.
- Not a secrets vault. Entries must not contain secrets; see section 9.
- No embeddings or vector search until a measurement shows they pay off.
- No agent other than Claude Code. The MCP tools work anywhere, the hooks do not.
- No write path that skips the human. Nothing reaches the journal unreviewed.

## 3. Terms

| Term | Meaning |
|---|---|
| Entry | One reviewed record in the journal. |
| Draft | A proposed record waiting in the queue. Invisible to search and hooks. |
| Journal | The set of entries: `entries/` under the home directory. |
| Queue | The set of drafts: `pending/`. |
| Home | `GRABLI_HOME`, default `~/.grabli`. |
| Stack | Lowercase technology tags, e.g. `nestjs`, `postgres`. |

## 4. Data model

An entry is a Markdown file with YAML frontmatter. Unknown frontmatter keys
are rejected.

| Field | Type | Rules |
|---|---|---|
| `id` | string | `^[md]-[a-z0-9]{6}$`. The first letter equals the first letter of `kind`. Equals the file name without `.md`. |
| `kind` | `mistake` \| `decision` | |
| `title` | string | 3 to 200 characters after trimming. |
| `project` | string, optional | Where it happened. |
| `stack` | list of strings | Lowercased, trimmed, deduplicated. |
| `paths` | list of globs | gitignore syntax, relative to the repo root. Each must compile. |
| `source` | object, optional | `repo`, `commit`, `found_by`. |
| `created` | date | ISO 8601. |
| `last_verified` | date | Set to today when a draft is accepted. |
| `superseded_by` | id, optional | A superseded entry is never delivered or found. |
| body | Markdown | At least 1 character after trimming. |

Serialization is deterministic and uses no YAML anchors, because the files
are meant to be read, edited by hand, and kept in git.

## 5. Storage

```
~/.grabli/
  entries/<id>.md      the journal, source of truth
  pending/<id>.md      the review queue
  cache/index.sqlite   search index, rebuilt from the files
  log.jsonl            append-only event log, read by the evals
  .gitignore           ignores cache/ and log.jsonl
```

- Files are written atomically (temporary file, then rename).
- A file that fails to parse, or whose `id` differs from its name, is skipped
  and reported. It never hides the other entries.
- `accept` refuses when the draft's `id` does not match its file name, and
  when the journal already holds an entry with that id.

## 6. Lifecycle

```
propose (agent | human | compaction) -> pending/ -> review -> entries/ -> delivery
                                                      \-> rejected (file deleted)
```

There are three ways to create a draft and one way to accept it.

| Source | Entry point | `via` in the log |
|---|---|---|
| Agent | MCP tool `propose` | `mcp` |
| Human | `grabli propose` | `cli` |
| Context compaction | hooks `pre-compact` + `post-compact` | `compact` |

Every source goes through the same function and the same checks: secret
scan over all fields, field validation, glob compilation.

## 7. Delivery

### 7.1 Stack detection

The repo root is the nearest ancestor with `.git`, else the starting
directory. The detector reads the root and its direct children:

- `package.json` dependencies, mapped to tags: `nestjs`, `nextjs`, `react`,
  `react-native`, `mui`, `typeorm`, `prisma`, `postgres`, `bullmq`, `redis`, and others;
- `pyproject.toml` / `requirements.txt`: `python`, plus `fastapi`, `django`,
  `sqlalchemy`, `celery`, `pydantic` when named;
- Docker files: `docker`; `project.godot`: `godot`.

### 7.2 Hook `session-start` (SessionStart)

Selects up to 3 active entries sharing at least one stack tag with the repo,
most overlap first, then most recently verified. Output: hook JSON with
`additionalContext`. Entry bodies are cut at 700 characters.

### 7.3 Hook `pre-edit` (PreToolUse, matcher `Edit|Write|MultiEdit`)

Selects up to 3 active entries whose `paths` match the edited file.

- An entry's stack tags can exclude it only if the detector is able to
  recognise them. Tags it cannot detect (`go`, `postgresql`) never hide an entry.
- An entry is delivered at most once per session. The record of what was
  shown is reset by a `SessionStart` with source `compact` or `clear`, since
  the agent no longer has that context.
- Outside a git repo, globs are relative to the session directory.

Two modes:

| Mode | Behavior |
|---|---|
| context (default) | Adds `additionalContext`, makes no permission decision. Claude Code delivers it with the tool result, that is after the first edit. |
| `--block` | Refuses the edit once with exit code 2 and the entries on stderr, so the agent reads them before writing. The retry passes. Never blocks without a `session_id`. |

### 7.4 Hooks `pre-compact` and `post-compact`

The model gets no turn at compaction time, so it cannot call `propose`.

1. `pre-compact` prints plain-text instructions. Claude Code uses a
   PreCompact hook's stdout as instructions for the summary. They ask for a
   block at the end of the summary:

   ```
   <grabli-lessons>
   ### mistake: short title
   stack: nestjs, postgres
   paths: **/reports/**
   What happened, why it was not caught, how to do it right.
   </grabli-lessons>
   ```

2. `post-compact` reads `compact_summary`, parses the block, and queues up to
   3 drafts with `found_by: compact`.

Parsing rules:

- The tags count only when alone on a line. Only the last block may lack its
  closing tag.
- A heading is `#` to `######`, then `mistake:` or `decision:`, then the
  title. Headings inside a code fence are ignored.
- Optional `stack:` and `paths:` lines directly after the heading, with or
  without list or bold markers. A line that reads as prose stays in the body.
- Commas inside `{}` and brackets inside a glob are preserved.
- The example titles from the instructions are discarded.
- A title already in the journal, in the queue, or queued by an earlier
  compaction is skipped. This covers drafts the human rejected. Duplicates
  are removed before the limit of 3 is applied.

### 7.5 Failure policy

A failure inside any hook exits 0 and writes a `hook_error` event. Broken
memory must not stop the agent's work or cancel a compaction. The only
non-zero exit is the deliberate one-time refusal of `pre-edit --block`.

Hooks other than `post-compact` do not use the search index and do not
import the MCP SDK; they read the entry files directly to stay fast.

## 8. Interfaces

### 8.1 MCP tools (stdio server, `grabli serve`)

`search(query?, id?, kind?, project?, stack?, limit=5)`

- With `id`: the full entry.
- With `query`: cards (`id`, `kind`, `title`, `project`, `stack`, `score`, `snippet`).
- A miss is not an error: `{"found": false, "hint": ...}`.
- `limit` is clamped to 1..20. Unreadable files are reported in `warnings`.

`propose(kind, title, body, project?, stack?, paths?, repo?, commit?, found_by?)`

- Returns `{"status": "pending", "id", "hint", "similar"?, "warnings"?}` or
  `{"status": "rejected", "reason"}`.
- The draft is saved before the search for similar entries, so a busy index
  cannot lose it.

### 8.2 Search

SQLite FTS5, tokenizer `unicode61 remove_diacritics 2`, BM25 with weights
title 3, tags 2, body 1. Text is lowercased and `ё` is folded to `е`.
Cyrillic query words are stemmed with Snowball and prefix-matched; Latin
words of 4 or more characters are prefix-matched. Filters (`kind`,
`project`, `stack`) apply before the limit. Superseded entries are excluded.

The index is a cache keyed by a hash of each entry's serialized form. It
follows manual edits and deletions. A corrupt file is rebuilt; a locked one
is searched as is, with a warning, and is never deleted.

### 8.3 CLI

| Command | Purpose |
|---|---|
| `grabli install [path] [--block] [--no-mcp]` | Connect a project. See 8.4. |
| `grabli serve` | Run the MCP server. |
| `grabli propose <title> [--body] [--kind] [--stack] [--paths] ...` | Queue a draft by hand. Body from `--body` or stdin. |
| `grabli review` | Walk the queue: accept, reject, skip, quit. |
| `grabli pending` / `accept <id>` / `reject <id>` | Queue operations. |
| `grabli search <query> [--kind] [--project] [--stack] [--limit]` | Search the journal. |
| `grabli hook <event> [--block]` | Hook handlers; read hook JSON from stdin. |

### 8.4 `grabli install`

Writes the four hooks into `.claude/settings.local.json` and registers the
MCP server in Claude Code's local scope (`claude mcp add -s local`). Both
are private to the user.

- A path inside a git repository is lifted to the repository root.
- Other content of the settings file is kept. Only hooks that are a single
  command ending in `grabli hook <event>` are replaced; a compound command
  that contains one is left alone.
- The file is not touched when it is not valid UTF-8 JSON, or when `hooks`
  or an event under it has the wrong type.
- A symlinked settings file is written through the link; the file mode is kept.
- If the project's `.claude/settings.json` or `~/.claude/settings.json`
  already runs grabli's hooks, hooks are not installed a second time.
- An existing MCP registration is removed and added again, since it may
  point at an old checkout.
- From a checkout the hooks call `uv run --directory <checkout> grabli`;
  from an installed package they call the executable by its full path.
- A warning is printed when the settings file is not ignored by git.

## 9. Secrets

`propose` scans every field and rejects a draft that looks like it holds a
secret: private key headers, AWS access keys, `sk-` keys, GitHub and GitLab
tokens, JWTs, credentials in URLs, bearer tokens, and `password = ...`
style assignments. A rejection names the pattern and the line, never the
value.

This is a list of regular expressions, not a guarantee.

## 10. Event log

`log.jsonl`, one JSON object per line: `ts` (UTC, ISO 8601), `event`, and
event-specific fields.

| Event | Fields |
|---|---|
| `session_start` | `session_id`, `source`, `repo`, `stack`, `ids` |
| `pre_edit` | `session_id`, `repo`, `file`, `mode`, `ids` |
| `search` | `query`, `ids` |
| `propose` | `ids`, `kind`, `via` |
| `pre_compact` | `session_id`, `trigger` |
| `compact_capture` | `session_id`, `trigger`, `repo`, `found`, `ids`, `titles`, `duplicates`, `rejected` |
| `hook_error` | `hook`, `error` |

`compact_capture` is written even when no block was found, so the log shows
how often the summarizer follows the instructions.

## 11. Invariants

1. Nothing enters `entries/` without a human accepting it.
2. A draft is never visible to search or hooks.
3. A hook failure never fails the session.
4. The files are the source of truth; the index and the log can be deleted.
5. An entry is delivered at most once per context.
6. A secret-looking value is never echoed back or logged.

## 12. Planned measurements

Not done yet. No result in this repository should be read as measured
unless it is listed here with its run count.

**Eval A, retrieval.** 40 to 50 labeled queries against a real journal.
Metrics: recall@3 and MRR. Variants: FTS5 as is, embeddings, hybrid,
stack and path filters. Embeddings are added only if the gain is measurable.

**Eval B, behavior.** Tasks with a known trap taken from real past mistakes.
Conditions: no memory, memory on request (MCP only), hooks in context mode,
hooks in block mode, memory with irrelevant entries. Several headless runs
per condition, in isolation:

```
claude -p "<task>" --setting-sources project --strict-mcp-config \
  --permission-mode acceptEdits --output-format stream-json --verbose
```

Observations so far, one run each, not results:

- In context mode the entry reaches the agent after its first edit. The
  agent wrote the trap version, then corrected it.
- In block mode the trap version never reached disk.
- After a manual `/compact`, the summarizer produced the lessons block in
  the requested format and two drafts were queued.

## 13. Known limitations

- Lexical search: a query with no shared word stems finds nothing. Snowball
  does not handle fleeting vowels, so «ошибка» does not find «ошибок».
- `log.jsonl` is never rotated and is read in full on every edit.
- Compaction capture depends on the summarizer following instructions. Not
  yet observed on automatic compaction or alongside another plugin's
  PreCompact hook.
- `MultiEdit` in the hook matcher is unconfirmed.
- The installed-package form of `grabli install` (hooks calling plain
  `grabli`) is untested.
