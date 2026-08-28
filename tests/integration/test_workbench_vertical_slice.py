from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from script_weaver.application.changeset_service import ChangeSetService
from script_weaver.application.generation_service import GenerationJobService
from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.domain.models import (
    AssetCreate,
    AssetVersionCreate,
    BindingAction,
    BindingCreate,
    ConflictError,
    EpisodeCreate,
    GenerationPrepare,
    ProjectCreate,
    SegmentCreate,
    ShotCreate,
    ShotCreateOperation,
    ShotUpdateOperation,
    TaskStart,
)
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database
from script_weaver.mcp.workbench_server import TOOL_NAMES


@pytest.fixture
def work_dir():
    path = Path(".test-tmp") / uuid4().hex
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def seed(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    workbench = WorkbenchService(db)
    project = workbench.create_project(ProjectCreate(title="雾渡"))
    episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
    segment = workbench.create_segment(
        episode["id"], SegmentCreate(code="E01-01", order_index=0)
    )
    return db, workbench, project, episode, segment


def test_four_shot_changeset_restart_and_cursor(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    task = workbench.start_task(
        TaskStart(project_id=project["id"], capability="short-drama-storyboard", intent="四镜")
    )
    changesets = ChangeSetService(db)
    change = changesets.create(task["id"], summary="四镜分镜")
    for index in range(4):
        change = changesets.append(
            change["id"],
            ShotCreateOperation(
                op="shot.create",
                payload=ShotCreate(
                    segment_id=segment["id"],
                    order_index=index,
                    image_prompt=f"frame {index}",
                    video_prompt=f"camera static {index}",
                ),
            ),
        )
    submitted = changesets.submit(change["id"])
    applied = changesets.apply(change["id"], submitted["validated_fingerprint"])
    changesets.apply(change["id"], submitted["validated_fingerprint"])
    assert applied["status"] == "APPLIED"
    assert len(workbench.get_segment(segment["id"])["shots"]) == 4
    events = workbench.list_events(project["id"])
    assert workbench.list_events(project["id"], events[-2]["id"])[0]["id"] == events[-1]["id"]
    db.close()

    restarted = Database(work_dir / "workbench.sqlite3")
    try:
        assert len(WorkbenchService(restarted).get_segment(segment["id"])["shots"]) == 4
        assert ChangeSetService(restarted).get(change["id"])["status"] == "APPLIED"
    finally:
        restarted.close()


def test_conflict_is_atomic_and_snapshot_is_immutable(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    first = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    second = workbench.create_shot(segment["id"], ShotCreate(order_index=1))
    task = workbench.start_task(
        TaskStart(project_id=project["id"], capability="short-drama-storyboard", intent="更新")
    )
    with pytest.raises(sqlite3.IntegrityError), db.write() as conn:
        conn.execute("UPDATE task_snapshots SET selection_json='{}' WHERE task_id=?", (task["id"],))
    changesets = ChangeSetService(db)
    change = changesets.create(task["id"])
    for shot in (first, second):
        change = changesets.append(
            change["id"],
            ShotUpdateOperation(
                op="shot.update",
                target_id=shot["id"],
                expected_revision=0,
                payload={"duration_seconds": 8},
            ),
        )
    submitted = changesets.submit(change["id"])
    workbench.update_shot(first["id"], 0, {"duration_seconds": 5})
    with pytest.raises(ConflictError):
        changesets.apply(change["id"], submitted["validated_fingerprint"])
    assert workbench.get_shot(first["id"])["duration_seconds"] == 5
    assert workbench.get_shot(second["id"])["duration_seconds"] == 3
    db.close()


def test_asset_propagation_sync_freeze_and_restore_versions(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    follow_shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    frozen_shot = workbench.create_shot(segment["id"], ShotCreate(order_index=1))
    asset = workbench.create_asset(
        project["id"], AssetCreate(kind="character", name="老周", content={"look": "v1"})
    )
    follow = workbench.bind_reference(
        BindingCreate(
            shot_id=follow_shot["id"], asset_version_id=asset["current_version_id"],
            usage="character", binding_mode="follow_latest",
        )
    )
    workbench.bind_reference(
        BindingCreate(
            shot_id=frozen_shot["id"], asset_version_id=asset["current_version_id"],
            usage="character", binding_mode="frozen",
        )
    )
    assert workbench.get_shot(follow_shot["id"])["revision"] == 1
    old_version = asset["current_version_id"]
    workbench.create_asset_version(
        asset["id"], AssetVersionCreate(expected_revision=0, content={"look": "v2"})
    )
    assert workbench.get_shot(follow_shot["id"])["bindings"][0]["is_stale"] == 1
    assert workbench.get_shot(frozen_shot["id"])["bindings"][0]["is_stale"] == 0
    workbench.binding_action(
        follow["id"], BindingAction(expected_shot_revision=1, action="sync")
    )
    synced = workbench.get_shot(follow_shot["id"])
    assert not synced["bindings"][0]["is_stale"] and synced["revision"] == 2
    workbench.binding_action(
        follow["id"], BindingAction(expected_shot_revision=2, action="freeze")
    )
    assert workbench.get_shot(follow_shot["id"])["bindings"][0]["binding_mode"] == "frozen"
    restored_asset = workbench.restore_asset_version(asset["id"], old_version, 1)
    assert restored_asset["version_number"] == 3 and restored_asset["content"]["look"] == "v1"
    edited = workbench.update_shot(follow_shot["id"], 3, {"duration_seconds": 9})
    restored_shot = workbench.restore_shot(follow_shot["id"], edited["revision"], 0)
    assert restored_shot["duration_seconds"] == 3 and restored_shot["revision"] == 5
    assert [item["revision"] for item in restored_shot["versions"]] == [5, 4, 3, 2, 1, 0]
    db.close()


def test_mcp_whitelist_excludes_authoritative_actions():
    assert len(TOOL_NAMES) == 21
    assert not any(word in name for name in TOOL_NAMES for word in ("apply", "confirm", "run_generation"))


def test_generation_fingerprint_confirmation_and_no_retry(work_dir, monkeypatch):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    asset = workbench.create_asset(
        project["id"], AssetCreate(kind="style", name="风格", content={"palette": "gray"})
    )
    service = GenerationJobService(db, MediaStore(work_dir / "media"))
    base = dict(
        project_id=project["id"], owner_type="shot", owner_id=shot["id"], prompt="mist",
        count=1, model="gpt-image-2", parameters={}, reference_asset_version_ids=[],
        adapter="fake", output_spec={"mime": "image/png"},
    )
    fingerprints = {service.prepare(GenerationPrepare(**base))["fingerprint"]}
    variations = [
        {"prompt": "rain"}, {"count": 2}, {"model": "gpt-image-2-preview"},
        {"parameters": {"size": "1024x1536"}},
        {"reference_asset_version_ids": [asset["current_version_id"]]},
        {"adapter": "gpt-image-2"}, {"output_spec": {"mime": "image/png", "size": "portrait"}},
    ]
    for variation in variations:
        fingerprints.add(service.prepare(GenerationPrepare(**(base | variation)))["fingerprint"])
    assert len(fingerprints) == len(variations) + 1

    monkeypatch.setattr("script_weaver.integrations.media.image.httpx.post", lambda *a, **k: pytest.fail("fake adapter accessed network"))
    job = service.prepare(GenerationPrepare(**base))
    confirmed = service.confirm(job["id"], job["fingerprint"])
    assert service.run(job["id"], confirmed["confirmation_token"])["state"] == "SUCCEEDED"
    with pytest.raises(ConflictError):
        service.run(job["id"], confirmed["confirmation_token"])

    class FailingAdapter:
        calls = 0

        def generate(self, spec, staging):
            self.calls += 1
            raise RuntimeError("provider failed")

    failing = FailingAdapter()
    service.adapters["fake"] = failing
    failed_job = service.prepare(GenerationPrepare(**base))
    token = service.confirm(failed_job["id"], failed_job["fingerprint"])["confirmation_token"]
    with pytest.raises(RuntimeError):
        service.run(failed_job["id"], token)
    with pytest.raises(ConflictError):
        service.run(failed_job["id"], token)
    assert failing.calls == 1 and service._get(failed_job["id"])["state"] == "FAILED"
    db.close()
