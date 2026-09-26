"""Offline checks for the SQLite project store (ticket #13)."""

import sqlite3

import pytest

from script_weaver.core.project_store import (
    DataDirLock,
    DataDirLockError,
    ProjectStore,
    ProjectStoreError,
    RevisionConflictError,
    UnsupportedDatabaseVersionError,
)
from script_weaver.core.types import BasicInfo, Outline, ProjectState


@pytest.fixture
def store(tmp_path):
    s = ProjectStore(tmp_path / "projects.sqlite3")
    yield s
    s.close()


def _with_outline(state: ProjectState, logline: str) -> ProjectState:
    state.outline = Outline(basic_info=BasicInfo(logline=logline))
    return state


# ── Creation & identity ────────────────────────────────────


def test_create_pins_service_id_to_meta_id(store):
    record = store.create_project(user_input="一个悬疑故事", title="悬疑A")
    assert record.project_id == record.state.meta.id
    assert record.revision == 1
    assert record.title == "悬疑A"

    loaded = store.get(record.project_id)
    assert loaded is not None
    assert loaded.state == record.state  # valid ProjectState round-trip
    assert loaded.state.meta.id == record.project_id


def test_get_missing_project_returns_none(store):
    assert store.get("nope") is None
    with pytest.raises(ProjectStoreError):
        store.get_required("nope")


# ── Multi-project isolation ────────────────────────────────


def test_two_projects_do_not_cross_write(store):
    a = store.create_project(user_input="A 的想法")
    b = store.create_project(user_input="B 的想法")

    updated = _with_outline(store.get(a.project_id).state, "A 的大纲")
    saved = store.save_state(a.project_id, updated, 1, source="manual", summary="改A")
    assert saved.revision == 2

    rb = store.get(b.project_id)
    assert rb.revision == 1
    assert rb.state.outline is None
    assert rb.state.user_input == "B 的想法"
    # B's updated_at is untouched by A's save.
    assert rb.updated_at == b.updated_at

    ra = store.get(a.project_id)
    assert ra.revision == 2
    assert ra.state.outline.basic_info.logline == "A 的大纲"


def test_list_orders_by_real_updated_time(store):
    a = store.create_project(user_input="第一个")
    b = store.create_project(user_input="第二个")
    assert [p["project_id"] for p in store.list_projects()] == [b.project_id, a.project_id]

    updated = _with_outline(store.get(a.project_id).state, "更新A")
    store.save_state(a.project_id, updated, 1, source="manual", summary="x")
    listed = store.list_projects()
    assert listed[0]["project_id"] == a.project_id
    assert listed[0]["updated_at"] >= listed[1]["updated_at"]
    assert set(listed[0]) >= {"project_id", "title", "revision", "stage",
                              "created_at", "updated_at"}


# ── Rename ─────────────────────────────────────────────────


def test_rename_advances_revision_and_appends_version(store):
    record = store.create_project(user_input="想改名的项目")
    renamed = store.rename_project(record.project_id, "新名字", 1)
    assert renamed.revision == 2
    assert renamed.title == "新名字"
    assert renamed.state.meta.title == "新名字"
    assert renamed.state.meta.id == record.project_id
    versions = store.list_versions(record.project_id)
    assert [v.revision for v in versions] == [2, 1]
    assert versions[0].source == "manual"
    assert "命名" in versions[0].summary


def test_rename_rejects_empty_title(store):
    record = store.create_project(user_input="x")
    with pytest.raises(ProjectStoreError):
        store.rename_project(record.project_id, "   ", 1)


# ── Revision conflicts & atomicity ─────────────────────────


def test_stale_revision_rejected_without_partial_write(store):
    record = store.create_project(user_input="并发测试")
    first = _with_outline(store.get(record.project_id).state, "第一次修改")
    store.save_state(record.project_id, first, 1, source="manual", summary="第一次")

    # A second writer still holds revision 1.
    stale = _with_outline(store.get(record.project_id).state, "过期的修改")
    with pytest.raises(RevisionConflictError) as exc:
        store.save_state(record.project_id, stale, 1, source="manual", summary="过期")
    assert exc.value.current_revision == 2

    current = store.get(record.project_id)
    assert current.revision == 2
    assert current.state.outline.basic_info.logline == "第一次修改"
    assert [v.revision for v in store.list_versions(record.project_id)] == [2, 1]


def test_failed_write_leaves_no_partial_rows(store):
    """A failure between UPDATE and INSERT must roll back completely."""
    record = store.create_project(user_input="失败原子性")

    # Sabotage: pre-insert a row for the revision the next save will try to
    # write, so the version INSERT fails after the projects UPDATE ran.
    store._conn.execute(
        "INSERT INTO project_versions (project_id, revision, title, state_json,"
        " source, summary, created_at) VALUES (?, 2, '占位', '{}', 'manual', '', 'now')",
        (record.project_id,),
    )

    state = _with_outline(store.get(record.project_id).state, "将要失败")
    with pytest.raises(sqlite3.IntegrityError):
        store.save_state(record.project_id, state, 1, source="manual", summary="失败")

    current = store.get(record.project_id)
    assert current.revision == 1  # UPDATE rolled back too
    assert current.state.outline is None
    assert current.title == record.title


class _CommitFailProxy:
    """Delegate to a real sqlite3 connection but fail COMMIT N times.

    sqlite3.Connection forbids attribute assignment, so failures are injected
    by swapping the store's connection for this proxy.
    """

    def __init__(self, conn, failures=1):
        self._conn = conn
        self._failures = failures

    def execute(self, sql, params=()):
        if sql == "COMMIT" and self._failures > 0:
            self._failures -= 1
            raise sqlite3.OperationalError(
                "disk I/O error during COMMIT (simulated)"
            )
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_failed_commit_rolls_back_and_keeps_store_usable(tmp_path, monkeypatch):
    """A rejected COMMIT must roll back, keep the original exception, and
    leave the same store instance usable once the fault clears."""
    db = tmp_path / "projects.sqlite3"
    store = ProjectStore(db)
    record = store.create_project(user_input="提交失败原子性")

    proxy = _CommitFailProxy(store._conn)
    monkeypatch.setattr(store, "_conn", proxy)
    state = _with_outline(store.get(record.project_id).state, "COMMIT 将失败")
    with pytest.raises(sqlite3.OperationalError, match="COMMIT"):
        store.save_state(record.project_id, state, 1, source="manual", summary="x")
    monkeypatch.undo()  # fault cleared

    assert not store._conn.in_transaction, "failed COMMIT left a transaction open"

    current = store.get(record.project_id)
    assert current.revision == 1, "uncommitted revision became visible"
    assert current.state.outline is None
    assert [v.revision for v in store.list_versions(record.project_id)] == [1]

    # The same instance saves successfully after the fault clears.
    ok = store.save_state(
        record.project_id,
        _with_outline(current.state, "恢复后保存"),
        1, source="manual", summary="y",
    )
    assert ok.revision == 2
    store.close()

    reopened = ProjectStore(db)
    r = reopened.get(record.project_id)
    assert r.revision == 2
    assert r.state.outline.basic_info.logline == "恢复后保存"
    assert len(reopened.list_versions(record.project_id)) == 2
    reopened.close()


def test_failed_commit_during_migration_rolls_back(tmp_path, monkeypatch):
    """The migration transaction must handle a rejected COMMIT the same way."""
    import script_weaver.core.project_store as ps

    db = tmp_path / "projects.sqlite3"
    store = ProjectStore(db)  # creates schema at user_version 1
    store.close()

    # Reset the version so the next open runs the migration again (the DDL
    # is idempotent), with COMMIT failing through a connection proxy.
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version=0")
    conn.commit()
    conn.close()

    real_connect = sqlite3.connect
    proxies = []

    def proxied_connect(*args, **kwargs):
        proxies.append(_CommitFailProxy(real_connect(*args, **kwargs)))
        return proxies[-1]

    monkeypatch.setattr(sqlite3, "connect", proxied_connect)
    with pytest.raises(sqlite3.OperationalError, match="COMMIT"):
        ProjectStore(db)
    monkeypatch.undo()

    assert not proxies[0]._conn.in_transaction, "migration left a transaction open"
    proxies[0]._conn.close()

    # Reopening after the fault migrates cleanly; nothing half-applied.
    reopened = ProjectStore(db)
    assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == ps.SCHEMA_VERSION
    reopened.close()


def test_replace_state_tolerates_rename_during_generation(store):
    record = store.create_project(user_input="生成期间被改名")
    base_json = store.get(record.project_id).state_json

    renamed = store.rename_project(record.project_id, "生成期间改名", 1)

    generated = _with_outline(store.get(record.project_id).state, "生成结果")
    result = store.replace_state(
        record.project_id, generated,
        base_revision=1, base_state_json=base_json,
        source="pipeline", summary="完整生成",
    )
    assert result.revision == renamed.revision + 1
    assert result.title == "生成期间改名"  # rename survives
    assert result.state.outline.basic_info.logline == "生成结果"


def test_replace_state_rejects_content_change_during_generation(store):
    record = store.create_project(user_input="生成期间被编辑")
    base_json = store.get(record.project_id).state_json

    edited = _with_outline(store.get(record.project_id).state, "用户手工编辑")
    store.save_state(record.project_id, edited, 1, source="manual", summary="并发编辑")

    generated = _with_outline(store.get(record.project_id).state, "生成结果")
    with pytest.raises(RevisionConflictError):
        store.replace_state(
            record.project_id, generated,
            base_revision=1, base_state_json=base_json,
            source="pipeline", summary="完整生成",
        )
    current = store.get(record.project_id)
    assert current.state.outline.basic_info.logline == "用户手工编辑"


# ── History ────────────────────────────────────────────────


def test_versions_are_immutable_snapshots(store):
    record = store.create_project(user_input="历史快照")
    v1 = store.get_version(record.project_id, 1)
    assert v1 is not None

    # Mutating a returned historical copy must not affect stored history.
    v1.state.user_input = "被篡改的历史"
    again = store.get_version(record.project_id, 1)
    assert again.state.user_input == "历史快照"

    updated = _with_outline(store.get(record.project_id).state, "第二版内容")
    store.save_state(record.project_id, updated, 1, source="manual", summary="第二版")
    v2 = store.get_version(record.project_id, 2)
    assert v2.state.outline is not None
    assert v1.state.outline is None  # old revision still shows old content
    assert store.get_version(record.project_id, 99) is None


def test_delete_cascades_versions(store):
    record = store.create_project(user_input="将被删除")
    updated = _with_outline(store.get(record.project_id).state, "内容")
    store.save_state(record.project_id, updated, 1, source="manual", summary="x")

    assert store.delete_project(record.project_id) is True
    assert store.get(record.project_id) is None
    assert store.list_versions(record.project_id) == []
    assert store.delete_project(record.project_id) is False


# ── Persistence across reopen ──────────────────────────────


def test_store_reopen_finds_projects(tmp_path):
    db = tmp_path / "projects.sqlite3"
    store = ProjectStore(db)
    record = store.create_project(user_input="重启后仍在")
    updated = _with_outline(store.get(record.project_id).state, "重启前保存")
    store.save_state(record.project_id, updated, 1, source="manual", summary="x")
    store.close()

    reopened = ProjectStore(db)
    loaded = reopened.get(record.project_id)
    assert loaded is not None
    assert loaded.revision == 2
    assert loaded.state.outline.basic_info.logline == "重启前保存"
    assert len(reopened.list_versions(record.project_id)) == 2
    reopened.close()


# ── Schema versioning ──────────────────────────────────────


def test_future_schema_version_refused(tmp_path):
    db = tmp_path / "projects.sqlite3"
    store = ProjectStore(db)
    store.close()
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version=99")
    conn.close()

    with pytest.raises(UnsupportedDatabaseVersionError):
        ProjectStore(db)


# ── Single-instance data dir lock ──────────────────────────


def test_data_dir_lock_rejects_second_holder(tmp_path):
    lock = DataDirLock(tmp_path)
    lock.acquire()
    try:
        with pytest.raises(DataDirLockError):
            DataDirLock(tmp_path).acquire()
    finally:
        lock.release()
    # After a clean release the next instance may start.
    second = DataDirLock(tmp_path)
    second.acquire()
    second.release()


# ── Generation runs (ticket #14) ───────────────────────────


def _seed_run(store: ProjectStore, project_id: str, **overrides):
    record = store.get_required(project_id)
    defaults = {
        "kind": "generate",
        "request_key": "k1",
        "request": {"user_input": "run story", "auto_approve": True},
        "base_revision": record.revision,
        "base_state_json": record.state_json,
        "checkpoint_json": record.state_json,
    }
    defaults.update(overrides)
    return store.create_generation_run(project_id, **defaults)


def test_run_lifecycle_create_query_update(store):
    project = store.create_project(user_input="run story")
    run = _seed_run(store, project.project_id)

    assert run.status == "running"
    assert run.request_hash  # fingerprint stored
    fetched = store.get_generation_run(run.run_id)
    assert fetched.request == {"user_input": "run story", "auto_approve": True}
    assert store.get_generation_run("nope") is None
    with pytest.raises(ProjectStoreError):
        store.get_generation_run_required("nope")

    # Stage progress persists step by step.
    updated = store.update_generation_run(
        run.run_id,
        status="running",
        completed_steps=["idea_refiner"],
        last_progress={"stage": "idea_refiner", "message": "阶段完成：概念精炼"},
    )
    assert updated.completed_steps == ["idea_refiner"]
    assert updated.last_progress["stage"] == "idea_refiner"

    terminal = store.update_generation_run(
        run.run_id, status="succeeded", error=None,
        result_summary={"title": "t", "details": ["大纲: x"]},
    )
    assert terminal.status == "succeeded"
    assert terminal.result_summary["details"] == ["大纲: x"]
    # Unknown fields are a programming error, not silent data loss.
    with pytest.raises(TypeError):
        store.update_generation_run(run.run_id, nope=1)


def test_active_and_latest_run_selection(store):
    project = store.create_project(user_input="multi run")
    finished = _seed_run(store, project.project_id)
    store.update_generation_run(finished.run_id, status="succeeded")

    active = _seed_run(store, project.project_id, request_key="k2",
                       request={"user_input": "second", "auto_approve": True})
    assert store.active_generation_run(project.project_id).run_id == active.run_id
    assert store.latest_generation_run(project.project_id).run_id == active.run_id

    stopping = _seed_run(store, project.project_id, request_key="k3",
                         request={"user_input": "third", "auto_approve": True})
    store.update_generation_run(stopping.run_id, status="stopping")
    assert store.active_generation_run(project.project_id).run_id == stopping.run_id

    store.update_generation_run(active.run_id, status="cancelled")
    assert store.active_generation_run(project.project_id).run_id == stopping.run_id
    # Per-project isolation: another project's runs never leak in.
    other = store.create_project(user_input="other")
    assert store.active_generation_run(other.project_id) is None
    assert store.latest_generation_run(other.project_id) is None


def test_interrupt_stale_runs_marks_only_active(store):
    project = store.create_project(user_input="stale")
    live = _seed_run(store, project.project_id)
    stopping = _seed_run(store, project.project_id, request_key="k2",
                         request={"user_input": "b", "auto_approve": True})
    store.update_generation_run(stopping.run_id, status="stopping")
    done = _seed_run(store, project.project_id, request_key="k3",
                     request={"user_input": "c", "auto_approve": True})
    store.update_generation_run(done.run_id, status="succeeded")

    assert store.interrupt_stale_generation_runs() == 2
    assert store.get_generation_run(live.run_id).status == "interrupted"
    assert store.get_generation_run(stopping.run_id).status == "interrupted"
    assert store.get_generation_run(done.run_id).status == "succeeded"
    # Re-sweep is a no-op and keeps a pre-existing error message intact.
    store.update_generation_run(live.run_id, status="running")
    store.get_generation_run(live.run_id)  # still readable
    assert store.interrupt_stale_generation_runs() == 1


def test_delete_project_cascades_runs(store):
    project = store.create_project(user_input="doomed")
    run = _seed_run(store, project.project_id)
    assert store.delete_project(project.project_id) is True
    assert store.get_generation_run(run.run_id) is None


def test_version_one_database_migrates_to_runs_schema(tmp_path):
    """A pre-#14 database (user_version=1) gains the runs table in place."""
    db = tmp_path / "projects.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version=1")
    conn.close()

    store = ProjectStore(db)
    try:
        assert store.interrupt_stale_generation_runs() == 0  # table usable
        version = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2
    finally:
        store.close()
