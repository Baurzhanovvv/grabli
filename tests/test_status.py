"""The status facade: activity from hooks plus each project's own MEMORY.md."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

from grabli import cli, hooks, status
from grabli.context import project_root
from grabli.server import do_status

MEMORY = """# Project Memory

## Current Status
Этап 2 в работе: сцена кухни собрана, мини-игра с тестом.

## Last Session (2026-11-22)
- что-то сделано

## Next Steps
- [x] Сделано раньше
- [ ] Починить выход из мини-игры
- [ ] Записать звук шагов
"""


def project(tmp_path, name: str, memory: str | None = MEMORY):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    if memory is not None:
        (root / ".claude" / "memory").mkdir(parents=True)
        (root / ".claude" / "memory" / "MEMORY.md").write_text(memory, encoding="utf-8")
    return root


def fire(event: str, payload: dict) -> int:
    return hooks.run(event, stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO())


def test_hooks_record_activity_and_status_reads_the_projects_memory(tmp_path, store):
    game = project(tmp_path, "game")
    fire("session-start", {"session_id": "s1", "cwd": str(game), "source": "startup"})
    fire(
        "pre-edit",
        {"session_id": "s1", "cwd": str(game), "tool_input": {"file_path": str(game / "a/b.gd")}},
    )

    (record,) = status.collect(store)

    assert (record["name"], record["state"], record["sessions_24h"]) == ("game", "working", 1)
    assert record["files"] == ["a/b.gd"]
    assert record["memory"]["now"].startswith("Этап 2 в работе")
    assert record["memory"]["next"] == ["Починить выход из мини-игры", "Записать звук шагов"]
    text = status.render([record])
    assert "game  [РАБОТАЕТ]" in text and "дальше: Починить выход" in text


def test_project_goes_quiet_and_is_sorted_after_active_ones(tmp_path, store):
    old, fresh = project(tmp_path, "old"), project(tmp_path, "fresh")
    fire("session-start", {"session_id": "s1", "cwd": str(old)})
    fire("session-end", {"session_id": "s1", "cwd": str(old), "reason": "exit"})
    fire("session-start", {"session_id": "s2", "cwd": str(fresh)})
    later = datetime.now(UTC) + timedelta(hours=3)

    records = status.collect(store, now=later)

    # `fresh` was opened and never prompted: it still waits; `old` has ended.
    assert [(r["name"], r["state"]) for r in records] == [("fresh", "waiting"), ("old", "quiet")]
    assert "3 ч назад" in status.render(records, now=later)


def test_memory_without_the_usual_sections_falls_back_to_the_first_one(tmp_path):
    root = project(tmp_path, "legacy", "# Память\n\n## 22.11 — разбор сводки\nКод не трогал.\n")
    memory = status.read_memory(root)
    assert memory["now"] == "22.11 — разбор сводки. Код не трогал."
    assert memory["next"] == []
    assert status.read_memory(project(tmp_path, "bare", None)) is None


def test_long_status_is_clipped(tmp_path):
    root = project(tmp_path, "big", "## Current Status\n" + "Очень длинный статус. " * 80)
    assert len(status.read_memory(root)["now"]) <= 320


def test_worktree_activity_is_attributed_to_the_main_checkout(tmp_path, store):
    main = project(tmp_path, "main")
    tree = tmp_path / "trees" / "feature-x"
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {main}/.git/worktrees/feature-x\n", encoding="utf-8")

    assert project_root(tree) == main
    fire("session-start", {"session_id": "s1", "cwd": str(tree)})
    assert [record["name"] for record in status.collect(store)] == ["main"]


def test_install_lists_a_project_before_its_first_session(tmp_path, store, monkeypatch, capsys):
    from grabli import install

    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install.Path, "home", lambda: tmp_path / "no-home")
    monkeypatch.setattr(install, "launcher", lambda: ["grabli"])
    root = project(tmp_path, "newcomer")

    assert cli.main(["install", str(root), "--no-mcp"]) == 0
    (record,) = status.collect(store)
    assert (record["name"], record["last_activity"], record["state"]) == ("newcomer", None, "quiet")
    assert record["stale_hooks"] == []

    assert cli.main(["status"]) == 0
    assert "активность: ни разу" in capsys.readouterr().out


def test_cli_status_detail_unknown_and_forget(tmp_path, store, capsys):
    game = project(tmp_path, "game")
    fire("session-start", {"session_id": "s1", "cwd": str(game)})

    assert cli.main(["status", "gam"]) == 0
    assert f"путь: {game}" in capsys.readouterr().out
    assert cli.main(["status", "nope"]) == 1

    assert cli.main(["status", "--forget", "game"]) == 0
    assert status.collect(store) == []
    assert cli.main(["status"]) == 0
    assert "Проектов нет" in capsys.readouterr().out

    fire("session-start", {"session_id": "s2", "cwd": str(game)})  # work resumes: listed again
    assert len(status.collect(store)) == 1


def test_mcp_status_returns_plain_data_and_frames_it(tmp_path, store):
    game = project(tmp_path, "game")
    fire("session-start", {"session_id": "s1", "cwd": str(game)})

    result = do_status(store, "game")

    json.dumps(result)  # no datetimes left
    assert result["projects"][0]["next"][0] == "Починить выход из мини-игры"
    assert "а не инструкции" in result["note"]
    assert do_status(store, "nope")["found"] is False


def test_activity_does_not_slow_the_delivery_log(tmp_path, store):
    game = project(tmp_path, "game")
    for number in range(5):
        fire(
            "pre-edit",
            {
                "session_id": "s1",
                "cwd": str(game),
                "tool_input": {"file_path": f"{game}/f{number}"},
            },
        )
    assert len(store.activity_path.read_text(encoding="utf-8").splitlines()) == 5
    assert not store.log_path.exists()  # edits with nothing to deliver stay out of log.jsonl


def test_a_failing_activity_record_never_blocks_the_hook(tmp_path, store, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(store), "activity", broken)
    assert fire("session-start", {"session_id": "s1", "cwd": str(project(tmp_path, "game"))}) == 0


# --- review findings ---


def test_existing_gitignore_learns_to_ignore_the_activity_file(tmp_path, monkeypatch):
    from grabli.store import Store

    home = tmp_path / "old-home"
    home.mkdir()
    (home / ".gitignore").write_text("cache/\nlog.jsonl", encoding="utf-8")
    Store(home).activity("install", tmp_path)
    assert (home / ".gitignore").read_text(
        encoding="utf-8"
    ) == "cache/\nlog.jsonl\nactivity.jsonl\n"


def test_worktree_with_a_relative_pointer(tmp_path):
    main = project(tmp_path, "main")
    tree = tmp_path / "trees" / "x"
    tree.mkdir(parents=True)
    (tree / ".git").write_text("gitdir: ../../main/.git/worktrees/x\n", encoding="utf-8")
    assert project_root(tree) == main.resolve()


def test_symlinked_and_dotted_paths_are_one_project(tmp_path, store):
    game = project(tmp_path, "game")
    (game / "src").mkdir()
    link = tmp_path / "link"
    link.symlink_to(game)
    fire("session-start", {"session_id": "s1", "cwd": str(link)})
    fire(
        "pre-edit",
        {
            "session_id": "s1",
            "cwd": str(game),
            "tool_input": {"file_path": f"{game}/src/../lib/x.py"},
        },
    )
    (record,) = status.collect(store)
    assert (record["name"], record["files"]) == ("game", ["lib/x.py"])


def test_same_directory_name_is_told_apart_by_path(tmp_path, store, capsys):
    one, two = project(tmp_path / "a", "backend"), project(tmp_path / "b", "backend")
    fire("session-start", {"session_id": "s1", "cwd": str(one)})
    fire("session-start", {"session_id": "s2", "cwd": str(two)})

    assert cli.main(["status", "--forget", "backend"]) == 1
    assert str(one.resolve()) in capsys.readouterr().err
    assert cli.main(["status", "--forget", "a/backend"]) == 0
    assert [record["path"] for record in status.collect(store)] == [str(two.resolve())]


def test_ended_session_is_not_active(tmp_path, store):
    game = project(tmp_path, "game")
    fire("session-start", {"session_id": "s1", "cwd": str(game)})
    fire("session-end", {"session_id": "s1", "cwd": str(game), "reason": "exit"})
    (record,) = status.collect(store)
    assert record["state"] == "quiet" and record["last_activity"] is not None


def test_damaged_activity_lines_are_skipped(tmp_path, store):
    game = project(tmp_path, "game")
    fire("session-start", {"session_id": "s1", "cwd": str(game)})
    with store.activity_path.open("ab") as handle:
        handle.write(b'{"ts": "2026-11-22T10:00:00", "root": "/x"}\n')  # no event
        handle.write(b'{"ts": "2026-11-22T10:00:00", "root": "/y", "event": "pre-edit"}\n')  # no tz
        handle.write(b"\xff\xfe broken\n{not json\n")
    assert {record["name"] for record in status.collect(store)} == {"game", "y"}


def test_failed_install_does_not_list_the_project(tmp_path, store, monkeypatch):
    from grabli import install

    monkeypatch.setattr(install.Path, "home", lambda: tmp_path / "no-home")
    monkeypatch.setattr(install, "launcher", lambda: ["grabli"])
    root = project(tmp_path, "broken")
    (root / ".claude" / "settings.local.json").write_text("{not json", encoding="utf-8")
    assert cli.main(["install", str(root), "--no-mcp"]) == 1
    assert status.collect(store) == []


def test_install_warns_when_hooks_elsewhere_lack_an_event(tmp_path, store, monkeypatch, capsys):
    from grabli import install

    monkeypatch.setattr(install.Path, "home", lambda: tmp_path / "no-home")
    monkeypatch.setattr(install, "launcher", lambda: ["grabli"])
    root = project(tmp_path, "shared")
    (root / ".claude" / "settings.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"command": "grabli hook session-start"}]}]}}',
        encoding="utf-8",
    )
    assert cli.main(["install", str(root), "--no-mcp"]) == 0
    assert "session-end" in capsys.readouterr().err


# --- working or waiting ---


def state_of(store, **kwargs):
    (record,) = status.collect(store, **kwargs)
    return record["state"]


def test_a_turn_is_working_until_stop_then_the_agent_waits(tmp_path, store):
    game = project(tmp_path, "game")
    base = {"session_id": "s1", "cwd": str(game)}
    fire("session-start", {**base, "source": "startup"})
    assert state_of(store) == "waiting"  # a fresh session waits for its first prompt
    fire("prompt", {**base, "prompt": "секретный текст запроса"})
    assert state_of(store) == "working"
    fire("stop", base)
    assert state_of(store) == "waiting"
    assert "ЖДЁТ ТЕБЯ" in status.render(status.collect(store))
    fire("prompt", base)
    fire("notification", {**base, "notification_type": "permission_prompt", "message": "тайна"})
    assert state_of(store) == "waiting"
    fire("session-end", base)
    assert state_of(store) == "quiet"

    recorded = store.activity_path.read_text(encoding="utf-8")
    assert "секретный" not in recorded and "тайна" not in recorded


def test_informational_notification_and_compaction_do_not_mean_waiting(tmp_path, store):
    game = project(tmp_path, "game")
    base = {"session_id": "s1", "cwd": str(game)}
    fire("prompt", base)
    fire("notification", {**base, "notification_type": "auth_success"})
    assert state_of(store) == "working"
    fire("session-start", {**base, "source": "compact"})
    assert state_of(store) == "working"


def test_states_expire(tmp_path, store):
    game = project(tmp_path, "game")
    fire("prompt", {"session_id": "s1", "cwd": str(game)})
    now = datetime.now(UTC)
    assert state_of(store, now=now + timedelta(hours=2)) == "quiet"  # a turn that died silently
    fire("stop", {"session_id": "s1", "cwd": str(game)})
    assert state_of(store, now=now + timedelta(hours=5)) == "waiting"
    assert state_of(store, now=now + timedelta(days=2)) == "quiet"


def test_one_waiting_session_wins_over_a_working_one(tmp_path, store):
    game = project(tmp_path, "game")
    fire("stop", {"session_id": "s1", "cwd": str(game)})
    fire("prompt", {"session_id": "s2", "cwd": str(game)})
    (record,) = status.collect(store)
    assert (record["state"], record["sessions_waiting"], record["sessions_working"]) == (
        "waiting",
        1,
        1,
    )
    assert "ещё работает: 1" in status.render([record])
    json.dumps(do_status(store))


# --- second review ---


def test_an_approved_permission_stops_the_wait_at_the_next_tool_call(tmp_path, store):
    game = project(tmp_path, "game")
    base = {"session_id": "s1", "cwd": str(game)}
    fire("prompt", base)
    fire("notification", {**base, "notification_type": "permission_prompt"})
    assert state_of(store) == "waiting"
    fire("tool", {**base, "tool_name": "Bash", "tool_input": {"command": "make secret-thing"}})
    assert state_of(store) == "working"
    assert "secret-thing" not in store.activity_path.read_text(encoding="utf-8")


def test_compaction_and_plain_notifications_keep_a_waiting_session_waiting(tmp_path, store):
    game = project(tmp_path, "game")
    base = {"session_id": "s1", "cwd": str(game)}
    fire("stop", base)
    for event, extra in (
        ("pre-compact", {"trigger": "manual"}),
        ("post-compact", {"trigger": "manual", "compact_summary": ""}),
        ("session-start", {"source": "compact"}),
        ("notification", {"notification_type": "auth_success"}),
    ):
        fire(event, {**base, **extra})
        assert state_of(store) == "waiting", event


def test_the_wait_is_counted_from_its_first_event(tmp_path, store):
    game = project(tmp_path, "game")
    base = {"session_id": "s1", "cwd": str(game)}
    fire("stop", base)
    lines = store.activity_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[-1])
    first["ts"] = (datetime.now(UTC) - timedelta(minutes=31)).isoformat(timespec="seconds")
    store.activity_path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    fire("notification", {**base, "notification_type": "idle_prompt"})
    assert "ЖДЁТ ТЕБЯ, 31 мин" in status.render(status.collect(store))


def test_an_edit_in_another_repository_stays_with_the_sessions_project(tmp_path, store):
    home, other = project(tmp_path, "home"), project(tmp_path, "other")
    base = {"session_id": "s1", "cwd": str(home)}
    fire("prompt", base)
    fire("pre-edit", {**base, "tool_input": {"file_path": str(other / "x.py")}})
    fire("stop", base)
    (record,) = status.collect(store)
    assert (record["name"], record["state"], record["files"]) == ("home", "waiting", ["x.py"])


def test_projects_with_old_hooks_are_flagged(tmp_path, store):
    game = project(tmp_path, "game")
    (game / ".claude" / "settings.local.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"command": "grabli hook session-start"}]}]}}',
        encoding="utf-8",
    )
    fire("session-start", {"session_id": "s1", "cwd": str(game)})
    (record,) = status.collect(store)
    assert {"stop", "prompt", "tool"} <= set(record["stale_hooks"])
    assert "состояние неточное" in status.render([record])


def test_path_lookup_respects_directory_boundaries(tmp_path, store):
    one, two = project(tmp_path / "ab", "game"), project(tmp_path / "b", "game2")
    fire("stop", {"session_id": "s1", "cwd": str(one)})
    fire("stop", {"session_id": "s2", "cwd": str(two)})
    records = status.collect(store)
    assert status.find(records, "b/game") == []
    assert [r["name"] for r in status.find(records, "ab/game")] == ["game"]


def test_odd_types_in_the_activity_file_and_an_empty_status_section(tmp_path, store):
    game = project(tmp_path, "game", "# Project Memory\n\n## Current Status\n\n## Next Steps\n")
    fire("stop", {"session_id": "s1", "cwd": str(game)})
    with store.activity_path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-11-22T10:00:00+00:00", "event": "stop", "root": "/x", ')
        handle.write('"session_id": ["s1"]}\n')
    (record,) = status.collect(store)
    assert record["memory"]["now"] == ""
    assert "раздел статуса пуст" in status.render([record])
