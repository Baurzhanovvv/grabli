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
- No silent trust. An entry written without a human is marked as such, in the
  file and at delivery.

## 3. Terms

| Term | Meaning |
|---|---|
| Entry | One record in the journal. |
| Draft | A proposed record waiting in the queue (review mode only). Invisible to search and hooks. |
| Mode | `auto` (default): proposals are written to the journal at once. `review`: they wait in the queue. Stored in the `mode` file in the home directory (`grabli mode review`); the `GRABLI_MODE` variable overrides it. The file is what reaches hooks and the MCP server, which Claude Code starts in its own environment. |
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
| `reviewed` | bool, default true | False for an entry written without a human. Serialized only when false. |
| body | Markdown | At least 1 character after trimming. |

Serialization is deterministic and uses no YAML anchors, because the files
are meant to be read, edited by hand, and kept in git.

## 5. Storage

```
~/.grabli/
  entries/<id>.md      the journal, source of truth
  pending/<id>.md      the review queue (review mode)
  retracted/<id>.md    entries taken out of the journal; kept, never loaded
  mode                 `auto` or `review`; absent means `auto`
  activity.jsonl       one line per hook call, read by `grabli status`
  cache/index.sqlite   search index, rebuilt from the files
  log.jsonl            append-only event log, read by the evals
  .gitignore           ignores cache/, log.jsonl and activity.jsonl; missing lines are added
```

- Files are written atomically (temporary file, then rename).
- A file that fails to parse, or whose `id` differs from its name, is skipped
  and reported. It never hides the other entries.
- `accept` refuses when the draft's `id` does not match its file name, and
  when the journal already holds an entry with that id.

## 6. Lifecycle

```
auto mode (default):
  propose (agent | human | compaction) -> entries/ -> delivery
                                             \-> retract / remove -> retracted/

review mode (`grabli mode review`):
  propose -> pending/ -> grabli review -> entries/ -> delivery
```

In auto mode the agent fills its memory on its own. Each write is one atomic
file rename into `entries/`, so an entry is either fully there or not at all,
and it persists as a plain file. Entries from the agent or from compaction
carry `reviewed: false`; an entry typed by the human is reviewed from the start.

A proposal whose kind and title match an active entry (title compared
ignoring case, `ё`/`е` and spacing) does not create a second entry. The call
returns `status: exists` with the existing title and the start of its body,
so the caller can tell a repeat from a different lesson under the same
name. An unreviewed entry gets today's `last_verified`; the date of an
entry a human reviewed is not moved by an agent.

`reviewed: true` records a human's decision, so the CLI commands that set
or undo it (`accept`, `reject`, `remove`, `review`, `mode <value>`) refuse
to run without a terminal on stdout, and `grabli propose` run without one
writes an unreviewed entry with `found_by: agent`. An agent calling grabli
through its shell tool has a pipe there. This is a speed bump against
accidents, not a security boundary: an agent with a shell can edit the
files directly.

Human review is optional: `grabli recent` lists what was written,
`grabli review` walks unreviewed entries, `grabli accept <id>` marks one as
reviewed, `grabli remove <id>` takes one out.

There are three sources of proposals.

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

Ranks active entries sharing at least one stack tag with the repo, most
overlap first, then most recently verified. The first 3 are delivered in
full; the next 12 are named by id and title only, so the agent knows they
exist. Listed entries do not count as shown: `pre-edit` can still deliver
them. Output: hook JSON with `additionalContext`.

Entries named by title only carry the same "not reviewed" label as full ones.

### 7.2.1 Digest (both delivery hooks)

A delivered body has a budget of 1100 characters, in `session-start` and in
`pre-edit` alike. A longer body is rearranged: the part that says what to do
comes first and is kept whole up to 900 characters; the rest follows and is
what gets cut. Entries are written story first, so a plain cut from the top
would drop the action.

- The action part is the last section whose heading starts with «Как
  делать», «Как применять», «Что делать», "What to do" or "How to
  do/apply/fix/avoid". It includes its own subsections and ends at a heading
  of the same or a higher level. Headings inside code fences do not count.
- "How to reproduce" and «Как делать не надо» are not action sections.
- For an entry written as prose, a sentence or paragraph opening with
  «Как делать:» / «Как применять:» / «Что делать:» marks the action part,
  which then runs to the end. The instructions given to the agent and to the
  summarizer ask for these markers.
- A body with no such part is cut from the end. A cut never leaves a code
  fence open.

Delivered text is framed as reference from past sessions, not instructions,
and tells the agent not to run commands found in entries. A reviewed entry
is shown with the date it was verified; an unreviewed one is labelled as
written by the agent and not checked.

### 7.3 Hook `pre-edit` (PreToolUse, matcher `Edit|Write|MultiEdit`)

Selects up to 3 active entries whose `paths` match the edited file. Bodies
are delivered through the digest (7.2.1).

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

2. `post-compact` reads `compact_summary`, parses the block, and proposes up to
   3 entries with `found_by: compact`. In auto mode they go into the journal
   with `reviewed: false`; in review mode, into the queue.

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

### 7.5 Observing hooks

`session-end` (SessionEnd), `prompt` (UserPromptSubmit), `stop` (Stop),
`notification` (Notification) and `tool` (PostToolUse, asynchronous, so it
never holds the agent up) deliver nothing. They exist so `grabli status`
can tell a working agent from a waiting one. The prompt text and the
notification text are never recorded; only the notification kind is.

### 7.6 Activity record

Every hook call appends one line to `activity.jsonl`: the event, the project
root, the session id, and for `pre-edit` the edited file relative to the
repository. A git worktree is attributed to its main checkout. This file is
separate from `log.jsonl` because it gets a line per edit, and `log.jsonl`
is read in full on every edit. A failure to record activity never affects
the delivery.

### 7.7 Failure policy

A failure inside any hook exits 0 and writes a `hook_error` event. Broken
memory must not stop the agent's work or cancel a compaction. The only
non-zero exit is the deliberate one-time refusal of `pre-edit --block`.

Hooks other than `post-compact` do not use the search index and do not
import the MCP SDK; they read the entry files directly to stay fast.

## 8. Interfaces

### 8.1 MCP tools (stdio server, `grabli serve`)

`search(query?, id?, kind?, project?, stack?, limit=5)`

- With `id`: the full entry.
- With `query`: cards (`id`, `kind`, `title`, `project`, `stack`, `score`, `snippet`,
  `reviewed`).
- A miss is not an error: `{"found": false, "hint": ...}`.
- `limit` is clamped to 1..20. Unreadable files are reported in `warnings`.

`propose(kind, title, body, project?, stack?, paths?, repo?, commit?, found_by?)`

- Returns `status`: `saved` (auto mode), `exists` (same title, date refreshed),
  `pending` (review mode) or `rejected` (with `reason`). Also `id`, `hint`,
  and optionally `similar` and `warnings`.
- The entry is saved before the search for similar entries, so a busy index
  cannot lose it.

`retract(id, reason, superseded_by?)`

- Takes a wrong or outdated entry out of the journal. `reason` is required
  and is logged.
- Without `superseded_by` the file moves to `retracted/`. With it, the entry
  stays and is marked superseded by an existing, active entry. An entry that
  is itself superseded cannot be a replacement, so no cycles.
- Only unreviewed entries. For an entry a human has reviewed the tool
  refuses and tells the agent to report it to the user. An agent misled by
  injected text must not be able to empty the reviewed part of the journal.
- Returns `status`: `retracted`, `superseded`, `refused` or `not_found`.

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
| `grabli propose <title> [--body] [--kind] [--stack] [--paths] ...` | Add an entry by hand (reviewed). Body from `--body` or stdin. |
| `grabli recent [--limit] [--unreviewed]` | Latest journal entries, newest first, with who wrote them. |
| `grabli review` | Walk the queue and the unreviewed entries: accept, reject, skip, quit. |
| `grabli accept <id>` | Accept a draft, or mark a journal entry as reviewed. |
| `grabli remove <id> [--reason]` | Take an entry out of the journal. The file moves to `retracted/`. |
| `grabli mode [auto\|review]` | Show or set the mode. |
| `grabli pending` / `reject <id>` | Queue operations (review mode). |
| `grabli search <query> [--kind] [--project] [--stack] [--limit]` | Search the journal. |
| `grabli status [project] [--forget P]` | What is going on in the connected projects. See 8.5. |
| `grabli hook <event> [--block]` | Hook handlers; read hook JSON from stdin. |

### 8.5 Status facade

`grabli status [project]` and the MCP tool `status(project?)` show every
connected project in one place. It is read-only: grabli observes, it starts
no agents and assigns no work.

A project is listed once `grabli install` has run in it or a hook has fired
there. For each project:

| Field | Source |
|---|---|
| state | `working`, `waiting` or `quiet`, see below. |
| last activity, sessions in 24 h, recently edited files | `activity.jsonl` |
| now, next | The project's own `.claude/memory/MEMORY.md`, read at display time. `now` is the "Current Status" section, or the first section when there is none; `next` is up to 3 unchecked items of "Next Steps". |
| lessons | Journal entries whose `project` equals the directory name. |

grabli keeps no copy of a project's status, so there is nothing to drift.
The price is that "now" and "next" are only as fresh as the project's last
write to its memory file; the age of that file is always shown. What an
agent plans inside a running session is not visible.

Text returned by the MCP tool comes from other projects' files and is
labelled as data, not instructions.

`grabli status --forget <project>` hides a project until a hook fires there
again. A project is named by its directory name, or by a path
when two share one.

Each session has a state, moved by its events in order:

| Event | State after it |
|---|---|
| `session-end` | quiet |
| `stop`; a `notification` of kind `permission_prompt`, `idle_prompt`, `agent_needs_input`, `elicitation_dialog` or `elicitation_url_dialog`; `session-start` other than after a compaction | waiting: the agent needs the user |
| `prompt`, `pre-edit`, `tool` | working |
| a compaction, any other notification | unchanged |

A waiting session counts its wait from the first event that put it there.
A working session with no event for 1 hour, and any session with none for
24 hours, is shown as quiet: a session killed without a SessionEnd would
otherwise keep its last state forever.

A project is `waiting` if any of its sessions is, else `working` if any is,
else `quiet`. A session belongs to the project it runs in: an edit of a file
in another repository is counted for the session's own project. Subagents
are not seen separately.

When a project's settings lack some of the observing hooks (it was connected
by an older version), the view says so instead of showing a state it cannot
know. `activity.jsonl` gets a line per tool call, is not rotated, and is
read in full on every call.

### 8.4 `grabli install`

Writes the nine hooks into `.claude/settings.local.json` and registers the
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
| `session_start` | `session_id`, `source`, `repo`, `stack`, `ids` (delivered in full), `listed` (titles only) |
| `pre_edit` | `session_id`, `repo`, `file`, `mode`, `ids` |
| `search` | `query`, `ids` |
| `propose` | `ids`, `kind`, `via`, `mode`, `duplicate`? |
| `accept` / `reject` | `ids` |
| `retract` | `ids`, `reason`, `via` (`mcp`, `cli`, `review`), `superseded_by`? |
| `pre_compact` | `session_id`, `trigger` |
| `compact_capture` | `session_id`, `trigger`, `repo`, `found`, `ids`, `titles`, `duplicates`, `rejected` |
| `hook_error` | `hook`, `error` |

`compact_capture` is written even when no block was found, so the log shows
how often the summarizer follows the instructions.

## 11. Invariants

1. Every entry written without a human carries `reviewed: false` until a human accepts it.
2. Through the MCP tools an agent cannot remove, supersede or re-date an entry a human has reviewed.
3. Nothing is deleted: a removed entry moves to `retracted/`.
4. A draft in the queue is never visible to search or hooks.
5. A hook failure never fails the session.
6. The files are the source of truth; the index and the log can be deleted.
7. An entry is delivered at most once per context.
8. A secret-looking value is never echoed back or logged.

## 12. Measurements

No result in this repository should be read as measured unless it is listed
here with its run count.

### 12.1 Eval A, retrieval (run once, 2026-11-22)

`uv run --group eval python -m evals.retrieval`. A snapshot of the author's
real journal, 57 entries from three projects, mostly Russian. 53 queries: 45
with one to three relevant entries, 8 with none. Intervals are 95% bootstrap
over queries.

| Engine | hit@3 | MRR@10 | ms/query |
|---|---|---|---|
| FTS5 + Snowball stems (shipped) | 0.96 [0.89, 1.00] | 0.89 [0.81, 0.96] | 0.3 |
| FTS5 + lemmas (pymorphy3) | 0.96 [0.89, 1.00] | 0.91 [0.84, 0.97] | 0.2 |
| Embeddings, paraphrase-multilingual-MiniLM-L12-v2 | 0.80 [0.69, 0.91] | 0.70 [0.59, 0.80] | 3 |
| Embeddings, multilingual-e5-large | 0.98 [0.93, 1.00] | 0.96 [0.91, 1.00] | 22 |
| Hybrid (RRF), shipped + e5-large | 1.00 [1.00, 1.00] | 0.96 [0.91, 1.00] | 21 |

Paired difference in MRR against the shipped engine: lemmas +0.02
[+0.00, +0.06]; MiniLM −0.19 [−0.29, −0.10]; hybrid with e5-large +0.07
[+0.03, +0.12], better on 8 queries and worse on none.

Decisions:

- **No embeddings for now.** The only variant with a measurable gain needs a
  2.2 GB model in every MCP server process and is about 70 times slower, for
  two more queries out of 45 in the top 3.
- **No lemmatization.** Its interval touches zero.
- **A "no answer" threshold is the next change.** On all 8 queries with no
  relevant entry every engine returned something. The top score separates
  "has an answer" from "has none" with AUC 0.91 for the shipped engine.

Limits of this run:

- The queries are synthetic. They were written by a separate model that read
  the entries, was not told how the search works, was forbidden to run it,
  and for the paraphrase type could not reuse a content word from the title.
  They are still not what agents actually type; the event log held two real
  queries when this was run.
- 45 queries is few: the intervals are wide and the shipped engine is near
  the ceiling, so this set cannot rank the good variants against each other.
- 57 entries is a small journal. Lexical search has it easy.
- The journal and the queries are private and are not in this repository;
  only the harness and these aggregates are.

### 12.2 Eval B, behavior (not run)

Tasks with a known trap taken from real past mistakes.
Conditions: no memory, memory on request (MCP only), hooks in context mode,
hooks in block mode, memory with irrelevant entries, and a journal filled in
auto mode against one filled through review. Several headless runs
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

- **Auto mode trades review for coverage.** A wrong conclusion written by the
  agent is delivered to every matching project until someone retracts it.
- **Memory is an injection surface.** Text from a repository or a web page
  can talk the agent into writing an "entry" that then reaches future
  sessions in other projects. The framing at delivery, the `reviewed` flag
  and the secret scan reduce this; they do not prevent it. `grabli mode review`
  is the mode for untrusted code.
- Duplicate detection compares kind and title only. The same lesson under a
  different title becomes a second entry.
- The `reviewed` flag is honest only as long as the agent does not write
  the files itself; see section 6.

- Lexical search: a query with no shared word stems finds nothing. Snowball
  does not handle fleeting vowels, so «ошибка» does not find «ошибок».
- `log.jsonl` is never rotated and is read in full on every edit.
- Compaction capture depends on the summarizer following instructions. Not
  yet observed on automatic compaction or alongside another plugin's
  PreCompact hook.
- `MultiEdit` in the hook matcher is unconfirmed.
- The installed-package form of `grabli install` (hooks calling plain
  `grabli`) is untested.
