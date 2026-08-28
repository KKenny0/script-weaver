"""PR3: surface context containment, TaskSnapshot completeness, binding revision semantics."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.domain.models import (
    AssetCreate,
    AssetVersionCreate,
    BindingAction,
    BindingCreate,
    ConflictError,
    EpisodeCreate,
    ProjectCreate,
    ProjectMismatchError,
    SegmentCreate,
    ShotCreate,
    SurfaceContextUpsert,
    TaskStart,
)
from script_weaver.infrastructure.sqlite import Database


@pytest.fixture
def work_dir():
    path = Path(".test-tmp") / uuid4().hex
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def two_projects(work_dir):
    db = Database(work_dir / "workbench.sqlite3")
    workbench = WorkbenchService(db)
    created = {}
    for name in ("A", "B"):
        project = workbench.create_project(ProjectCreate(title=f"项目{name}"))
        episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
        segment = workbench.create_segment(episode["id"], SegmentCreate(code=f"E01-01-{name}", order_index=0))
        shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
        created[name] = {"project": project, "episode": episode, "segment": segment, "shot": shot}
    return db, workbench, created


def test_mixed_project_surface_context_is_rejected(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a, b = entities["A"], entities["B"]
    ok = SurfaceContextUpsert(session_id="s1", project_id=a["project"]["id"], episode_id=a["episode"]["id"], segment_id=a["segment"]["id"], selected_shot_ids=[a["shot"]["id"]])
    stored = workbench.set_surface_context(ok)
    assert stored["selected_shot_ids"] == [a["shot"]["id"]]

    with pytest.raises(ProjectMismatchError):
        workbench.set_surface_context(SurfaceContextUpsert(session_id="s2", project_id=a["project"]["id"], episode_id=b["episode"]["id"]))
    with pytest.raises(ProjectMismatchError):
        workbench.set_surface_context(SurfaceContextUpsert(session_id="s3", project_id=a["project"]["id"], episode_id=a["episode"]["id"], segment_id=b["segment"]["id"]))
    with pytest.raises(ProjectMismatchError):
        workbench.set_surface_context(SurfaceContextUpsert(session_id="s4", project_id=a["project"]["id"], episode_id=a["episode"]["id"], segment_id=a["segment"]["id"], selected_shot_ids=[b["shot"]["id"]]))
    with pytest.raises(ValueError):
        workbench.set_surface_context(SurfaceContextUpsert(session_id="s5", project_id=a["project"]["id"], episode_id=a["episode"]["id"], selected_shot_ids=[a["shot"]["id"]]))
    db.close()


def test_task_start_project_must_match_surface_project(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a, b = entities["A"], entities["B"]
    workbench.set_surface_context(SurfaceContextUpsert(session_id="s1", project_id=a["project"]["id"], episode_id=a["episode"]["id"], segment_id=a["segment"]["id"], selected_shot_ids=[a["shot"]["id"]]))

    with pytest.raises(ProjectMismatchError):
        workbench.start_task(TaskStart(project_id=b["project"]["id"], capability="short-drama-storyboard", intent="跨项目", surface_session_id="s1"))

    snapshot = workbench.start_task(TaskStart(project_id=a["project"]["id"], capability="short-drama-storyboard", intent="正常", surface_session_id="s1"))
    selection = snapshot["selection"]
    revisions = snapshot["expected_revisions"]
    assert selection["project_id"] == a["project"]["id"]
    assert selection["episode_id"] == a["episode"]["id"]
    assert selection["segment_id"] == a["segment"]["id"]
    assert selection["selected_shot_ids"] == [a["shot"]["id"]]
    for key in (a["project"]["id"], a["episode"]["id"], a["segment"]["id"], a["shot"]["id"]):
        assert key in revisions
    db.close()


def test_direct_bind_bumps_shot_revision_with_matching_snapshot_and_event(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a = entities["A"]
    asset = workbench.create_asset(a["project"]["id"], AssetCreate(kind="character", name="老周", content={"look": 1}))
    before = workbench.get_shot(a["shot"]["id"])

    binding = workbench.bind_reference(BindingCreate(shot_id=a["shot"]["id"], asset_version_id=asset["current_version_id"], usage="character", binding_mode="frozen"))

    after = workbench.get_shot(a["shot"]["id"])
    assert after["revision"] == before["revision"] + 1
    assert sorted(item["revision"] for item in after["versions"]) == [0, 1]
    events = [event for event in workbench.list_events(a["project"]["id"]) if event["event_type"] == "reference.bound"]
    assert events and events[-1]["revision"] == after["revision"] and events[-1]["aggregate_id"] == a["shot"]["id"]
    assert binding["asset_id"] == asset["id"]
    db.close()


def test_freeze_keeps_old_version_until_sync(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a = entities["A"]
    asset = workbench.create_asset(a["project"]["id"], AssetCreate(kind="scene", name="渡口", content={"env": 1}))
    shot = workbench.create_shot(a["segment"]["id"], ShotCreate(order_index=1))
    old_version = asset["current_version_id"]
    binding = workbench.bind_reference(BindingCreate(shot_id=shot["id"], asset_version_id=old_version, usage="scene", binding_mode="follow_latest"))
    revision_after_bind = workbench.get_shot(shot["id"])["revision"]

    workbench.create_asset_version(asset["id"], AssetVersionCreate(expected_revision=0, content={"env": 2}))
    new_version = workbench.get_asset(asset["id"])["current_version_id"]
    stale = workbench.get_shot(shot["id"])["bindings"][0]
    assert stale["is_stale"] == 1 and stale["asset_version_id"] == old_version

    workbench.binding_action(binding["id"], BindingAction(expected_shot_revision=revision_after_bind, action="freeze"))
    frozen = workbench.get_shot(shot["id"])
    assert frozen["bindings"][0]["asset_version_id"] == old_version, "freeze must not implicitly sync"
    assert frozen["bindings"][0]["binding_mode"] == "frozen"
    assert frozen["bindings"][0]["is_stale"] == 0

    workbench.binding_action(binding["id"], BindingAction(expected_shot_revision=frozen["revision"], action="sync"))
    synced = workbench.get_shot(shot["id"])
    assert synced["bindings"][0]["asset_version_id"] == new_version
    assert synced["revision"] == frozen["revision"] + 1
    db.close()


def test_restore_from_other_asset_is_rejected_without_any_change(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a = entities["A"]
    asset_one = workbench.create_asset(a["project"]["id"], AssetCreate(kind="character", name="甲", content={"v": 1}))
    asset_two = workbench.create_asset(a["project"]["id"], AssetCreate(kind="character", name="乙", content={"v": 1}))
    shot = workbench.create_shot(a["segment"]["id"], ShotCreate(order_index=1))
    binding = workbench.bind_reference(BindingCreate(shot_id=shot["id"], asset_version_id=asset_one["current_version_id"], usage="character"))
    before = workbench.get_shot(shot["id"])

    with pytest.raises(ValueError):
        workbench.binding_action(binding["id"], BindingAction(expected_shot_revision=before["revision"], action="restore", asset_version_id=asset_two["current_version_id"]))

    after = workbench.get_shot(shot["id"])
    assert after["revision"] == before["revision"]
    assert after["bindings"][0]["asset_version_id"] == asset_one["current_version_id"]
    assert [v["revision"] for v in after["versions"]] == [v["revision"] for v in before["versions"]]
    db.close()


def test_follow_latest_binding_of_historical_version_is_rejected(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a = entities["A"]
    asset = workbench.create_asset(a["project"]["id"], AssetCreate(kind="style", name="风格", content={"v": 1}))
    shot = workbench.create_shot(a["segment"]["id"], ShotCreate(order_index=1))
    old_version = asset["current_version_id"]
    workbench.create_asset_version(asset["id"], AssetVersionCreate(expected_revision=0, content={"v": 2}))
    before = workbench.get_shot(shot["id"])

    with pytest.raises(ValueError):
        workbench.bind_reference(BindingCreate(shot_id=shot["id"], asset_version_id=old_version, usage="style", binding_mode="follow_latest"))

    after = workbench.get_shot(shot["id"])
    assert after["bindings"] == []
    assert after["revision"] == before["revision"]
    # Frozen bindings may still point at a historical version.
    frozen = workbench.bind_reference(BindingCreate(shot_id=shot["id"], asset_version_id=old_version, usage="style", binding_mode="frozen"))
    assert frozen["asset_version_id"] == old_version
    db.close()


def test_binding_action_enforces_expected_shot_revision(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a = entities["A"]
    asset = workbench.create_asset(a["project"]["id"], AssetCreate(kind="scene", name="渡口", content={"env": 1}))
    shot = workbench.create_shot(a["segment"]["id"], ShotCreate(order_index=1))
    binding = workbench.bind_reference(BindingCreate(shot_id=shot["id"], asset_version_id=asset["current_version_id"], usage="scene"))
    current = workbench.get_shot(shot["id"])["revision"]

    with pytest.raises(ConflictError):
        workbench.binding_action(binding["id"], BindingAction(expected_shot_revision=current + 5, action="freeze"))

    after = workbench.get_shot(shot["id"])
    assert after["revision"] == current, "rejected action must not bump the shot"
    assert after["bindings"][0]["binding_mode"] == "frozen", "rejected action must not change the binding"
    db.close()


def test_repeated_sync_without_change_does_not_create_empty_revisions(work_dir):
    db, workbench, entities = two_projects(work_dir)
    a = entities["A"]
    asset = workbench.create_asset(a["project"]["id"], AssetCreate(kind="scene", name="渡口", content={"env": 1}))
    shot = workbench.create_shot(a["segment"]["id"], ShotCreate(order_index=1))
    binding = workbench.bind_reference(BindingCreate(shot_id=shot["id"], asset_version_id=asset["current_version_id"], usage="scene", binding_mode="follow_latest"))
    revision = workbench.get_shot(shot["id"])["revision"]

    result = workbench.binding_action(binding["id"], BindingAction(expected_shot_revision=revision, action="sync"))
    assert result["asset_version_id"] == asset["current_version_id"]
    assert workbench.get_shot(shot["id"])["revision"] == revision, "no-op sync must not bump the shot"
    db.close()
