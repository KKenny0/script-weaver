from __future__ import annotations

import hashlib
import base64
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from script_weaver.application.generation_service import GenerationJobService
from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.daemon.server import create_app
from script_weaver.domain.models import (
    AssetCreate, BindingCreate, EpisodeCreate, GenerationPrepare, MediaCandidateAccept,
    MediaCandidateImport, ProjectCreate, ProjectMismatchError, SegmentCreate, ShotCreate,
    SurfaceContextUpsert, TaskStart, ConflictError, AssetVersionCreate,
)
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database


def seed(tmp_path):
    db = Database(tmp_path / "visual.sqlite3")
    workbench = WorkbenchService(db)
    project = workbench.create_project(ProjectCreate(title="无日期母带"))
    episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
    segment = workbench.create_segment(episode["id"], SegmentCreate(code="EP01-S01", order_index=0))
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0, image_prompt="母带特写"))
    asset = workbench.create_asset(project["id"], AssetCreate(kind="prop", name="PROP-001", content={}))
    binding = workbench.bind_reference(BindingCreate(
        shot_id=shot["id"], asset_version_id=asset["current_version_id"],
        usage="prop", binding_mode="follow_latest",
    ))
    return db, workbench, project, episode, segment, shot, asset, binding


def prepare(project_id, asset_id, count=3, parent=None):
    return GenerationPrepare(
        project_id=project_id, owner_type="asset", owner_id=asset_id,
        prompt="盒盖闭合，削弱表面划痕" if parent else "无日期母带，三种视觉方向",
        count=count, adapter="fake", parent_candidate_id=parent, target_asset_id=asset_id,
    )


def run(service, spec):
    job = service.prepare(spec)
    confirmed = service.confirm(job["id"], job["fingerprint"])
    service.run(job["id"], confirmed["confirmation_token"])


def test_three_candidates_edit_preserves_parent_and_accept_creates_formal_ref(tmp_path):
    db, workbench, project, _episode, _segment, _shot, asset, binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    run(service, GenerationPrepare(
        project_id=project["id"], owner_type="shot", owner_id=_shot["id"], prompt="冻结关键帧",
        count=1, adapter="fake", reference_asset_version_ids=[asset["current_version_id"]],
    ))
    run(service, prepare(project["id"], asset["id"]))
    candidates = service.list_candidates(project["id"], "asset", asset["id"])
    assert len(candidates) == 3
    assert all(item["candidate_status"] == "candidate" and item["is_current"] == 0 for item in candidates)

    parent = candidates[1]
    assert parent["candidate_label"] == "B"
    run(service, prepare(project["id"], asset["id"], count=1, parent=parent["id"]))
    child = next(item for item in service.list_candidates(project["id"], "asset", asset["id"])
                 if item["parent_candidate_id"] == parent["id"])
    assert child["candidate_label"] == "B1"
    assert service.get_candidate(parent["id"])["candidate_status"] == "candidate"

    accepted = service.accept_candidate(child["id"], MediaCandidateAccept(
        asset_id=asset["id"], expected_asset_revision=asset["revision"],
    ))
    assert accepted["candidate_status"] == "accepted"
    formal = workbench.get_asset(asset["id"])
    assert formal["current_version_id"] == accepted["accepted_asset_version_id"]
    assert formal["versions"][0]["content"]["media_version_id"] == child["id"]
    assert service.get_candidate(parent["id"])["candidate_status"] == "candidate"
    stale_binding = db.connection.execute(
        "SELECT is_stale FROM reference_bindings WHERE id=?", (binding["id"],)
    ).fetchone()
    assert stale_binding[0] == 1
    assert db.connection.execute("SELECT is_stale FROM shots WHERE id=?", (_shot["id"],)).fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT is_stale FROM prompt_versions WHERE owner_type='shot' AND owner_id=?", (_shot["id"],)
    ).fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT is_stale FROM media_versions WHERE owner_type='shot' AND owner_id=?", (_shot["id"],)
    ).fetchone()[0] == 1
    db.close()


def test_cross_project_accept_rolls_back_asset_and_candidate(tmp_path):
    db, workbench, project, *_rest, asset, _binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    run(service, prepare(project["id"], asset["id"], count=1))
    candidate = service.list_candidates(project["id"])[0]
    other = workbench.create_project(ProjectCreate(title="另一个项目"))
    foreign = workbench.create_asset(other["id"], AssetCreate(kind="prop", name="Foreign", content={}))
    with pytest.raises(ProjectMismatchError):
        service.accept_candidate(candidate["id"], MediaCandidateAccept(
            asset_id=foreign["id"], expected_asset_revision=foreign["revision"],
        ))
    assert workbench.get_asset(foreign["id"])["revision"] == 0
    assert service.get_candidate(candidate["id"])["candidate_status"] == "candidate"
    db.close()


def test_agent_octet_stream_import_is_agent_only_and_frozen_to_task_owner(tmp_path, monkeypatch):
    db_path, media_root = tmp_path / "api.sqlite3", tmp_path / "media"
    app = create_app(db_path, media_root, token="creator", agent_token="agent")
    workbench = app.state.workbench
    project = workbench.create_project(ProjectCreate(title="导入"))
    episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
    segment = workbench.create_segment(episode["id"], SegmentCreate(code="EP01-S01", order_index=0))
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    other_shot = workbench.create_shot(segment["id"], ShotCreate(order_index=1))
    workbench.set_surface_context(SurfaceContextUpsert(
        session_id="visual", project_id=project["id"], episode_id=episode["id"],
        segment_id=segment["id"], route="storyboard", selected_shot_ids=[shot["id"]],
    ))
    task = workbench.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-storyboard", intent="生成候选",
        surface_session_id="visual",
    ))
    run_row = workbench.claim_task(task["id"], "codex")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    digest = hashlib.sha256(png).hexdigest()
    query = (
        f"?project_id={project['id']}&task_id={task['id']}&run_id={run_row['id']}"
        f"&owner_type=shot&owner_id={shot['id']}&prompt=frame&sha256={digest}"
    )
    with TestClient(app) as client:
        creator = client.post("/api/media-candidates/import" + query, content=png, headers={
            "Authorization": "Bearer creator", "Content-Type": "application/octet-stream",
        })
        assert creator.status_code == 403
        wrong_type = client.post("/api/media-candidates/import" + query, content=png, headers={
            "Authorization": "Bearer agent", "Content-Type": "image/png",
        })
        assert wrong_type.status_code == 415
        imported = client.post("/api/media-candidates/import" + query, content=png, headers={
            "Authorization": "Bearer agent", "Content-Type": "application/octet-stream",
        })
        assert imported.status_code == 201, imported.text
        assert imported.json()["agent_run_id"] == run_row["id"]
        outside = query.replace(f"owner_id={shot['id']}", f"owner_id={other_shot['id']}")
        rejected = client.post("/api/media-candidates/import" + outside, content=png, headers={
            "Authorization": "Bearer agent", "Content-Type": "application/octet-stream",
        })
        assert rejected.status_code == 409
        bad_hash = query.replace(digest, "0" * 64)
        rejected_hash = client.post("/api/media-candidates/import" + bad_hash, content=png, headers={
            "Authorization": "Bearer agent", "Content-Type": "application/octet-stream",
        })
        assert rejected_hash.status_code == 422
    app.state.db.close()


def test_candidate_persists_across_daemon_restart(tmp_path):
    db, _workbench, project, *_rest, asset, _binding = seed(tmp_path)
    media_root = tmp_path / "media"
    service = GenerationJobService(db, MediaStore(media_root))
    run(service, prepare(project["id"], asset["id"], count=1))
    candidate_id = service.list_candidates(project["id"])[0]["id"]
    db.close()
    restarted = Database(tmp_path / "visual.sqlite3")
    try:
        assert GenerationJobService(restarted, MediaStore(media_root)).get_candidate(candidate_id)["id"] == candidate_id
    finally:
        restarted.close()


def task_for_asset(workbench, project, episode, segment, shot, asset, *, limit=3, parent=None):
    workbench.set_surface_context(SurfaceContextUpsert(
        session_id="asset-visual", project_id=project["id"], episode_id=episode["id"],
        segment_id=segment["id"], route="assets", selected_shot_ids=[shot["id"]],
    ))
    task = workbench.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-storyboard", intent="导入图片候选",
        surface_session_id="asset-visual", asset_id=asset["id"],
        media_candidate_limit=limit, parent_candidate_id=parent,
    ))
    return task, workbench.claim_task(task["id"], "codex")


def real_png():
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


def test_import_scope_is_frozen_budgeted_and_parent_must_be_pinned(tmp_path):
    db, workbench, project, episode, segment, shot, asset, _binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    png = real_png()
    task, run_row = task_for_asset(workbench, project, episode, segment, shot, asset, limit=1)
    metadata = MediaCandidateImport(
        project_id=project["id"], task_id=task["id"], run_id=run_row["id"],
        owner_type="asset", owner_id=asset["id"], target_asset_id=asset["id"],
        prompt="候选", sha256=hashlib.sha256(png).hexdigest(),
    )
    imported = service.import_candidate(metadata, png)
    assert imported["candidate_label"] == "A"
    with pytest.raises(ConflictError, match="budget"):
        service.import_candidate(metadata, png)

    # A binding added after claim cannot enlarge the frozen target asset scope.
    other = workbench.create_asset(project["id"], AssetCreate(kind="character", name="后绑定", content={}))
    workbench.bind_reference(BindingCreate(
        shot_id=shot["id"], asset_version_id=other["current_version_id"],
        usage="character", binding_mode="frozen",
    ))
    with pytest.raises(ProjectMismatchError, match="frozen task selection"):
        service.import_candidate(metadata.model_copy(update={
            "owner_id": other["id"], "target_asset_id": other["id"],
        }), png)

    next_task, next_run = task_for_asset(workbench, project, episode, segment, shot, asset)
    with pytest.raises(ProjectMismatchError, match="parent candidate"):
        service.import_candidate(metadata.model_copy(update={
            "task_id": next_task["id"], "run_id": next_run["id"],
            "parent_candidate_id": imported["id"],
        }), png)
    db.close()


def test_import_and_accept_reject_changed_frozen_revisions_and_provenance_is_immutable(tmp_path):
    db, workbench, project, episode, segment, shot, asset, _binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    run(service, prepare(project["id"], asset["id"], count=1))
    candidate = service.list_candidates(project["id"], "asset", asset["id"])[0]
    assert candidate["prompt_sha256"] == hashlib.sha256(candidate["prompt_text"].encode()).hexdigest()
    workbench.create_asset_version(asset["id"], AssetVersionCreate(
        expected_revision=0, content={"changed": True},
    ))
    with pytest.raises(ConflictError, match="changed after generation"):
        service.accept_candidate(candidate["id"], MediaCandidateAccept(
            asset_id=asset["id"], expected_asset_revision=1,
        ))

    task, run_row = task_for_asset(workbench, project, episode, segment, shot, workbench.get_asset(asset["id"]))
    png = real_png()
    metadata = MediaCandidateImport(
        project_id=project["id"], task_id=task["id"], run_id=run_row["id"],
        owner_type="asset", owner_id=asset["id"], target_asset_id=asset["id"],
        prompt="冻结", sha256=hashlib.sha256(png).hexdigest(),
    )
    workbench.create_asset_version(asset["id"], AssetVersionCreate(
        expected_revision=1, content={"changed": "again"},
    ))
    with pytest.raises(ConflictError, match="frozen candidate owner changed"):
        service.import_candidate(metadata, png)
    with pytest.raises(Exception, match="provenance is immutable"):
        db.connection.execute(
            "UPDATE media_versions SET prompt_text='tamper' WHERE id=?", (candidate["id"],)
        )
    db.close()


def png_with(width, height, decoded):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(decoded)) + chunk(b"IEND", b"")


def test_png_validator_rejects_huge_dimensions_and_bounded_decompression(tmp_path):
    db, *_ = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    with pytest.raises(ValueError, match="dimensions"):
        service._validate_png(png_with(9000, 1, b"x"))
    with pytest.raises(ValueError, match="decoded data"):
        service._validate_png(png_with(1, 1, b"x" * 1000))
    db.close()


def test_accepted_ref_cannot_be_deleted(tmp_path):
    db, _workbench, project, *_rest, asset, _binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    run(service, prepare(project["id"], asset["id"], count=1))
    candidate = service.list_candidates(project["id"])[0]
    with pytest.raises(Exception, match="provenance is immutable"):
        db.connection.execute(
            """UPDATE media_versions
               SET mime='text/html',kind='video',width=999,height=999,
                   duration_seconds=9,created_at='tampered'
               WHERE id=?""",
            (candidate["id"],),
        )
    accepted = service.accept_candidate(candidate["id"], MediaCandidateAccept(
        asset_id=asset["id"], expected_asset_revision=0,
    ))
    with pytest.raises(Exception, match="provenance is immutable"):
        db.connection.execute(
            """UPDATE media_versions
               SET mime='text/html',kind='video',width=999,height=999,
                   duration_seconds=9,created_at='tampered'
               WHERE id=?""",
            (accepted["id"],),
        )
    with pytest.raises(Exception, match="accepted media REF cannot be deleted"):
        db.connection.execute("DELETE FROM media_versions WHERE id=?", (accepted["id"],))
    db.close()


def test_shot_owned_accepted_ref_excludes_itself_from_precise_stale(tmp_path):
    db, _workbench, project, _episode, _segment, shot, asset, _binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    spec = GenerationPrepare(
        project_id=project["id"], owner_type="shot", owner_id=shot["id"],
        target_asset_id=asset["id"], prompt="道具冻结帧", count=1, adapter="fake",
        reference_asset_version_ids=[asset["current_version_id"]],
    )
    run(service, spec)
    run(service, spec)
    rows = service.list_candidates(project["id"], "shot", shot["id"])
    accepted = service.accept_candidate(rows[0]["id"], MediaCandidateAccept(
        asset_id=asset["id"], expected_asset_revision=0,
    ))
    assert accepted["is_stale"] == 0
    assert service.get_candidate(rows[1]["id"])["is_stale"] == 1
    db.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_candidate_media_integrity_is_required_before_accept(tmp_path, damage):
    db, workbench, project, *_rest, asset, _binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    run(service, prepare(project["id"], asset["id"], count=1))
    candidate = service.list_candidates(project["id"])[0]
    before = workbench.get_asset(asset["id"])
    path = Path(candidate["storage_path"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="missing|SHA-256"):
        service.accept_candidate(candidate["id"], MediaCandidateAccept(
            asset_id=asset["id"], expected_asset_revision=before["revision"],
        ))
    after = workbench.get_asset(asset["id"])
    assert (after["current_version_id"], after["revision"]) == (
        before["current_version_id"], before["revision"],
    )
    assert service.get_candidate(candidate["id"])["candidate_status"] == "candidate"
    db.close()


def test_stale_media_candidate_cannot_become_formal_ref(tmp_path):
    db, workbench, project, *_rest, asset, _binding = seed(tmp_path)
    service = GenerationJobService(db, MediaStore(tmp_path / "media"))
    run(service, prepare(project["id"], asset["id"], count=1))
    candidate = service.list_candidates(project["id"])[0]
    db.connection.execute(
        "UPDATE media_versions SET is_stale=1,stale_reason='upstream changed' WHERE id=?",
        (candidate["id"],),
    )
    before = workbench.get_asset(asset["id"])
    with pytest.raises(ConflictError, match="stale media candidate"):
        service.accept_candidate(candidate["id"], MediaCandidateAccept(
            asset_id=asset["id"], expected_asset_revision=before["revision"],
        ))
    assert workbench.get_asset(asset["id"])["current_version_id"] == before["current_version_id"]
    db.close()
