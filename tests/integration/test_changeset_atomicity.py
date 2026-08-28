"""PR2: ChangeSet atomicity, per-thread SQLite isolation and project containment."""

from __future__ import annotations

import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from script_weaver.application.changeset_service import ChangeSetService
from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.domain.models import (
    AssetCreate,
    AssetVersionCreateOperation,
    ConflictError,
    EpisodeCreate,
    NotFoundError,
    ProjectCreate,
    ProjectMismatchError,
    PromptVersionCreateOperation,
    ReferenceBindOperation,
    SegmentCreate,
    SegmentCreateOperation,
    ShotCreate,
    ShotCreateOperation,
    ShotRetireOperation,
    ShotReorderOperation,
    ShotUpdateOperation,
    SurfaceContextUpsert,
    TaskStart,
)
from script_weaver.infrastructure.sqlite import Database

CAPABILITY = "short-drama-storyboard"


@pytest.fixture
def work_dir():
    path = Path(".test-tmp") / uuid4().hex
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def seed(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    workbench = WorkbenchService(db)
    project = workbench.create_project(ProjectCreate(title="项目A"))
    episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
    segment = workbench.create_segment(episode["id"], SegmentCreate(code="E01-01", order_index=0))
    return db, workbench, project, episode, segment


def seed_two_projects(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    workbench = WorkbenchService(db)
    created = {}
    for name in ("A", "B"):
        project = workbench.create_project(ProjectCreate(title=f"项目{name}"))
        episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
        segment = workbench.create_segment(episode["id"], SegmentCreate(code=f"E01-01-{name}", order_index=0))
        shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
        asset = workbench.create_asset(project["id"], AssetCreate(kind="character", name=f"角色{name}", content={"v": 1}))
        created[name] = {"project": project, "episode": episode, "segment": segment, "shot": shot, "asset": asset}
    return db, workbench, created


def build_changeset(workbench, changesets, project_id, operations):
    task = workbench.start_task(TaskStart(project_id=project_id, capability=CAPABILITY, intent="测试"))
    change = changesets.create(task["id"])
    for operation in operations:
        change = changesets.append(change["id"], operation)
    return change


def with_barrier_write(db, parties: int):
    barrier = threading.Barrier(parties, timeout=10)
    original_write = db.write

    @contextmanager
    def barriered_write():
        barrier.wait()
        with original_write() as conn:
            yield conn

    db.write = barriered_write
    return barrier, original_write


def run_together(targets: list):
    """Run callables in threads; return list of ('ok', result) / ('error', exc)."""
    outcomes: list = [None] * len(targets)

    def runner(index, fn):
        try:
            outcomes[index] = ("ok", fn())
        except Exception as error:  # noqa: BLE001
            outcomes[index] = ("error", error)

    threads = [threading.Thread(target=runner, args=(index, fn)) for index, fn in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "test deadlocked"
    return outcomes


def test_concurrent_apply_is_exactly_once(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    changesets = ChangeSetService(db)
    operations = [
        ShotCreateOperation(op="shot.create", payload=ShotCreate(segment_id=segment["id"], order_index=index))
        for index in range(4)
    ]
    change = build_changeset(workbench, changesets, project["id"], operations)
    submitted = changesets.submit(change["id"])
    fingerprint = submitted["validated_fingerprint"]

    with_barrier_write(db, 2)
    outcomes = run_together([
        lambda: changesets.apply(change["id"], fingerprint),
        lambda: changesets.apply(change["id"], fingerprint),
    ])
    db.write = Database.write.__get__(db)

    statuses = [result["status"] for kind, result in outcomes if kind == "ok"]
    assert len(statuses) == 2 and statuses == ["APPLIED", "APPLIED"]
    assert len(workbench.get_segment(segment["id"])["shots"]) == 4
    events = [event for event in workbench.list_events(project["id"]) if event["causation_id"] == change["id"]]
    assert len(events) == 4
    for shot in workbench.get_segment(segment["id"])["shots"]:
        assert shot["revision"] == 0
    db.close()


def test_apply_and_direct_patch_never_silently_overwrite(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    changesets = ChangeSetService(db)
    change = build_changeset(workbench, changesets, project["id"], [
        ShotUpdateOperation(op="shot.update", target_id=shot["id"], expected_revision=0, payload={"duration_seconds": 8}),
    ])
    submitted = changesets.submit(change["id"])

    with_barrier_write(db, 2)
    outcomes = run_together([
        lambda: changesets.apply(change["id"], submitted["validated_fingerprint"]),
        lambda: workbench.update_shot(shot["id"], 0, {"duration_seconds": 9}),
    ])
    db.write = Database.write.__get__(db)

    (apply_kind, apply_result), (patch_kind, patch_result) = outcomes
    assert not (apply_kind == "ok" and patch_kind == "ok"), "both writers succeeded: silent overwrite"
    final = workbench.get_shot(shot["id"])
    assert final["revision"] == 1
    if apply_kind == "ok":
        assert final["duration_seconds"] == 8
        assert patch_kind == "error" and isinstance(patch_result, ConflictError)
        assert changesets.get(change["id"])["status"] == "APPLIED"
    else:
        assert final["duration_seconds"] == 9
        assert isinstance(apply_result, ConflictError)
        assert changesets.get(change["id"])["status"] == "CONFLICTED"
    db.close()


def test_apply_and_reject_produce_single_terminal_state(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    changesets = ChangeSetService(db)
    change = build_changeset(workbench, changesets, project["id"], [
        ShotUpdateOperation(op="shot.update", target_id=shot["id"], expected_revision=0, payload={"duration_seconds": 8}),
    ])
    submitted = changesets.submit(change["id"])

    with_barrier_write(db, 2)
    outcomes = run_together([
        lambda: changesets.apply(change["id"], submitted["validated_fingerprint"]),
        lambda: changesets.reject(change["id"]),
    ])
    db.write = Database.write.__get__(db)

    (apply_kind, _), (reject_kind, _) = outcomes
    status = changesets.get(change["id"])["status"]
    final = workbench.get_shot(shot["id"])
    if reject_kind == "ok":
        assert status == "REJECTED"
        assert final["duration_seconds"] == 3, "facts written after REJECTED"
        assert apply_kind == "error"
    else:
        assert status == "APPLIED"
        assert final["duration_seconds"] == 8
        assert reject_kind == "error" and isinstance(outcomes[1][1], ConflictError)
    db.close()


def test_idempotent_apply_still_requires_the_applied_fingerprint(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    changesets = ChangeSetService(db)
    change = build_changeset(workbench, changesets, project["id"], [
        ShotUpdateOperation(op="shot.update", target_id=shot["id"], expected_revision=0, payload={"duration_seconds": 8}),
    ])
    submitted = changesets.submit(change["id"])
    assert changesets.apply(change["id"], submitted["validated_fingerprint"])["status"] == "APPLIED"

    with pytest.raises(ConflictError):
        changesets.apply(change["id"], "f" * 64)
    db.close()


def test_changeset_create_rejects_unknown_run_id(work_dir):
    db, workbench, project, _episode, _segment = seed(work_dir)
    changesets = ChangeSetService(db)
    task = workbench.start_task(TaskStart(project_id=project["id"], capability=CAPABILITY, intent="run 校验"))
    with pytest.raises(NotFoundError):
        changesets.create(task["id"], run_id="missing-run")
    db.close()


def test_uncommitted_facts_and_events_are_invisible_to_readers(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    baseline_shots = db.connection.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    baseline_events = len(workbench.list_events(project["id"]))
    reader_ready, started, checked = threading.Event(), threading.Event(), threading.Event()
    failures: list[Exception] = []

    def writer():
        try:
            with db.write() as conn:
                conn.execute(
                    "INSERT INTO shots VALUES('ghost-shot',?,999,'active',0,3,'medium','eye_level','static',NULL,NULL,'{}','{}','[]')",
                    (segment["id"],),
                )
                conn.execute(
                    "INSERT INTO domain_events(project_id,aggregate_type,aggregate_id,revision,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (project["id"], "shot", "ghost-shot", 0, "shot.created", "{}", "now"),
                )
                started.set()
                assert checked.wait(10)
                raise RuntimeError("rollback now")
        except RuntimeError as error:
            if str(error) != "rollback now":
                failures.append(error)

    def reader():
        # Create the reader's per-thread connection before the writer holds the lock.
        db.connection.execute("SELECT 1").fetchone()
        reader_ready.set()
        assert started.wait(10)
        seen_shots = db.connection.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
        seen_events = len(workbench.list_events(project["id"]))
        if seen_shots != baseline_shots or seen_events != baseline_events:
            failures.append(AssertionError(f"reader saw uncommitted data: shots={seen_shots}/{baseline_shots} events={seen_events}/{baseline_events}"))
        checked.set()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert reader_ready.wait(10)
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    writer_thread.join(timeout=20)
    reader_thread.join(timeout=20)
    assert failures == []
    assert not writer_thread.is_alive() and not reader_thread.is_alive()
    assert db.connection.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == baseline_shots
    db.close()


def test_stale_snapshot_base_revision_marks_conflicted_with_zero_writes(work_dir):
    db, workbench, project, episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    other = workbench.create_shot(segment["id"], ShotCreate(order_index=1))
    workbench.set_surface_context(SurfaceContextUpsert(
        session_id="s-stale", project_id=project["id"], episode_id=episode["id"],
        segment_id=segment["id"], route="storyboard", selected_shot_ids=[shot["id"], other["id"]],
    ))
    changesets = ChangeSetService(db)
    task = workbench.start_task(TaskStart(project_id=project["id"], capability=CAPABILITY, intent="过期基础", surface_session_id="s-stale"))
    base_revisions = task["expected_revisions"]
    assert base_revisions[other["id"]] == 0
    change = changesets.create(task["id"])
    change = changesets.append(change["id"], ShotUpdateOperation(op="shot.update", target_id=shot["id"], expected_revision=0, payload={"duration_seconds": 8}))
    submitted = changesets.submit(change["id"])
    # Move a snapshot-selected entity the operation never touches; the frozen base is now stale.
    workbench.update_shot(other["id"], 0, {"duration_seconds": 7})

    with pytest.raises(ConflictError):
        changesets.apply(change["id"], submitted["validated_fingerprint"])
    assert changesets.get(change["id"])["status"] == "CONFLICTED"
    assert workbench.get_shot(shot["id"])["duration_seconds"] == 3
    assert workbench.get_shot(other["id"])["duration_seconds"] == 7
    events = [event for event in workbench.list_events(project["id"]) if event["causation_id"] == change["id"]]
    assert events == []
    db.close()


def test_every_cross_project_target_and_parent_is_rejected(work_dir):
    db, workbench, entities = seed_two_projects(work_dir)
    changesets = ChangeSetService(db)
    a, b = entities["A"], entities["B"]

    cases = [
        SegmentCreateOperation(op="segment.create", payload=SegmentCreate(code="X1", order_index=0, episode_id=b["episode"]["id"])),
        ShotCreateOperation(op="shot.create", payload=ShotCreate(segment_id=b["segment"]["id"], order_index=0)),
        ShotUpdateOperation(op="shot.update", target_id=b["shot"]["id"], expected_revision=0, payload={"duration_seconds": 8}),
        ShotRetireOperation(op="shot.retire", target_id=b["shot"]["id"], expected_revision=0),
        ShotReorderOperation(op="shot.reorder", target_id=b["segment"]["id"], expected_revision=0, payload={"shots": [{"id": b["shot"]["id"], "order_index": 3}]}),
        AssetVersionCreateOperation(op="asset.version.create", target_id=b["asset"]["id"], expected_revision=0, payload={"content": {"v": 2}}),
        ReferenceBindOperation(op="reference.bind", target_id=a["shot"]["id"], expected_revision=0, payload={"usage": "character", "asset_version_id": b["asset"]["current_version_id"]}),
        PromptVersionCreateOperation(op="prompt.version.create", target_type="shot", target_id=b["shot"]["id"], expected_revision=0, payload={"kind": "image", "content": "prompt"}),
    ]
    for operation in cases:
        change = build_changeset(workbench, changesets, a["project"]["id"], [operation])
        with pytest.raises((ConflictError, ProjectMismatchError, NotFoundError, ValueError)):
            changesets.submit(change["id"])
        assert changesets.get(change["id"])["status"] == "DRAFT", f"changeset for {operation.op} must not be submitted"
    db.close()


def test_reorder_with_foreign_shot_leaves_it_untouched(work_dir):
    db, workbench, entities = seed_two_projects(work_dir)
    changesets = ChangeSetService(db)
    a, b = entities["A"], entities["B"]
    foreign_before = workbench.get_shot(b["shot"]["id"])
    foreign_events = len(workbench.list_events(b["project"]["id"]))

    change = build_changeset(workbench, changesets, a["project"]["id"], [
        ShotReorderOperation(op="shot.reorder", target_id=a["segment"]["id"], expected_revision=0, payload={
            "shots": [
                {"id": a["shot"]["id"], "order_index": 4},
                {"id": b["shot"]["id"], "order_index": 5},
            ]
        }),
    ])
    with pytest.raises(ProjectMismatchError):
        changesets.submit(change["id"])
    foreign_after = workbench.get_shot(b["shot"]["id"])
    assert foreign_after["order_index"] == foreign_before["order_index"]
    assert foreign_after["revision"] == foreign_before["revision"]
    assert [v["revision"] for v in foreign_after["versions"]] == [v["revision"] for v in foreign_before["versions"]]
    assert len(workbench.list_events(b["project"]["id"])) == foreign_events

    # Removing the foreign item lets the same reorder apply cleanly.
    task = workbench.start_task(TaskStart(project_id=a["project"]["id"], capability=CAPABILITY, intent="重排"))
    clean = changesets.create(task["id"])
    clean = changesets.append(clean["id"], ShotReorderOperation(op="shot.reorder", target_id=a["segment"]["id"], expected_revision=0, payload={"shots": [{"id": a["shot"]["id"], "order_index": 4}]}))
    applied = changesets.apply(clean["id"], changesets.submit(clean["id"])["validated_fingerprint"])
    assert applied["status"] == "APPLIED"
    assert workbench.get_shot(a["shot"]["id"])["order_index"] == 4
    assert workbench.get_shot(a["shot"]["id"])["revision"] == 1
    db.close()


def test_malformed_payloads_are_rejected_before_submit(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    changesets = ChangeSetService(db)

    with pytest.raises(ValidationError):
        build_changeset(workbench, changesets, project["id"], [
            ShotUpdateOperation(op="shot.update", target_id=shot["id"], expected_revision=0, payload={"duration_seconds": -5}),
        ])
    with pytest.raises(ValidationError):
        build_changeset(workbench, changesets, project["id"], [
            ShotUpdateOperation(op="shot.update", target_id=shot["id"], expected_revision=0, payload={"duration_seconds": 5, "bogus_field": 1}),
        ])
    with pytest.raises(ValidationError):
        build_changeset(workbench, changesets, project["id"], [
            ShotUpdateOperation(op="shot.update", target_id=shot["id"], expected_revision=-1, payload={"duration_seconds": 5}),
        ])

    # A payload that bypassed append validation must fail submit, never a false-green SUBMITTED.
    task = workbench.start_task(TaskStart(project_id=project["id"], capability=CAPABILITY, intent="坏数据"))
    change = changesets.create(task["id"])
    conn = db.connection
    conn.execute(
        "INSERT INTO changeset_operations VALUES(?,0,'shot.update','shot',?,0,?)",
        (change["id"], shot["id"], '{"duration_seconds": -5}'),
    )
    with pytest.raises(ValueError):
        changesets.submit(change["id"])
    assert changesets.get(change["id"])["status"] == "DRAFT"
    db.close()
