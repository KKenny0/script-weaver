from __future__ import annotations

import json
import sqlite3
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from script_weaver.application.generation_service import GenerationJobService
from script_weaver.application.h3_service import H3VideoService, validate_h3_url
from script_weaver.application.production_package_service import ProductionPackageService
from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.daemon.server import create_app
from script_weaver.domain.models import (
    AssetCreate,
    BindingCreate,
    EpisodeCreate,
    GenerationPrepare,
    H3VideoPrepare,
    MediaCandidateAccept,
    ProjectCreate,
    SegmentCreate,
    ShotCreate,
    ShotUpdateFields,
)
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database, SchemaError


def mp4() -> bytes:
    def box(kind, body=b""):
        return struct.pack(">I4s", len(body) + 8, kind) + body
    return box(b"ftyp", b"isom") + box(b"mdat", b"data") + box(b"moov")


class FakeH3:
    def __init__(
        self, *, content=None, content_type="video/mp4", redirect=False,
        model_id="MiniMaxAI/MiniMax-H3", models_status=200,
    ):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != "/v1/videos":
                    return self._json(404, {})
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                expected_keys = {
                    "task", "model", "conditions", "target", "seconds", "prompt",
                    "num_outputs_per_prompt", "num_inference_steps", "flow_shift",
                    "audio_flow_shift", "seed",
                }
                valid = set(payload) == expected_keys
                valid = valid and payload.get("task") == "fl2va"
                valid = valid and payload.get("model") == "MiniMaxAI/MiniMax-H3"
                seconds = payload.get("seconds")
                valid = valid and isinstance(seconds, int) and 4 <= seconds <= 15
                valid = valid and payload.get("target") == {
                    "short_edge": 768, "aspect_ratio": "auto",
                    "duration_seconds": seconds,
                }
                valid = valid and {
                    "num_outputs_per_prompt": payload.get("num_outputs_per_prompt"),
                    "num_inference_steps": payload.get("num_inference_steps"),
                    "flow_shift": payload.get("flow_shift"),
                    "audio_flow_shift": payload.get("audio_flow_shift"),
                    "seed": payload.get("seed"),
                } == {
                    "num_outputs_per_prompt": 1, "num_inference_steps": 50,
                    "flow_shift": 12.0, "audio_flow_shift": 3.0,
                    "seed": payload.get("seed"),
                }
                valid = valid and isinstance(payload.get("seed"), int)
                conditions = payload.get("conditions")
                valid = valid and isinstance(conditions, list) and 1 <= len(conditions) <= 2
                if valid:
                    for index, condition in enumerate(conditions):
                        valid = valid and set(condition) == {
                            "type", "uri", "role", "frame_index",
                        }
                        valid = valid and condition.get("type") == "image"
                        valid = valid and condition.get("role") == "keyframe"
                        valid = valid and str(condition.get("uri", "")).startswith("file://")
                        valid = valid and condition.get("frame_index") == (0 if index == 0 else -1)
                if not valid:
                    return self._json(422, {"error": "invalid FL2VA payload"})
                state.requests.append(payload)
                state.idempotency.append(self.headers.get("Idempotency-Key"))
                self._json(200, {"id": f"h3-job-{len(state.requests)}", "status": "queued"})

            def do_GET(self):
                if self.path == "/v1/models":
                    return self._json(state.models_status, {"data": [{"id": state.model_id}]})
                if self.path.startswith("/v1/videos/h3-job-") and not self.path.endswith("/content"):
                    return self._json(200, {"id": self.path.rsplit("/", 1)[-1], "status": state.status})
                if self.path.startswith("/v1/videos/h3-job-") and self.path.endswith("/content"):
                    if state.redirect:
                        self.send_response(302)
                        self.send_header("Location", "http://example.com/evil.mp4")
                        return self.end_headers()
                    body = state.content
                    self.send_response(200)
                    self.send_header("Content-Type", state.content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                return self._json(404, {})

        self.requests = []
        self.idempotency = []
        self.status = "completed"
        self.content = mp4() if content is None else content
        self.content_type = content_type
        self.redirect = redirect
        self.model_id = model_id
        self.models_status = models_status
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def accepted_keyframe(tmp_path):
    db = Database(tmp_path / "h3.sqlite3")
    media_store = MediaStore(tmp_path / "media")
    workbench = WorkbenchService(db)
    generation = GenerationJobService(db, media_store)
    project = workbench.create_project(ProjectCreate(title="H3 工作台"))
    episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
    segment = workbench.create_segment(
        episode["id"], SegmentCreate(code="EP01-S01", order_index=0)
    )
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    asset = workbench.create_asset(
        project["id"], AssetCreate(kind="prop", name="冻结关键帧", content={})
    )
    image_job = generation.prepare(GenerationPrepare(
        project_id=project["id"], owner_type="asset", owner_id=asset["id"],
        target_asset_id=asset["id"], prompt="冻结关键帧", adapter="fake", count=1,
    ))
    confirmed = generation.confirm(image_job["id"], image_job["fingerprint"])
    generation.run(image_job["id"], confirmed["confirmation_token"])
    candidate = generation.list_candidates(project["id"], "asset", asset["id"])[0]
    accepted = generation.accept_candidate(candidate["id"], MediaCandidateAccept(
        asset_id=asset["id"], expected_asset_revision=0,
    ))
    binding = workbench.bind_reference(BindingCreate(
        shot_id=shot["id"], asset_version_id=accepted["accepted_asset_version_id"],
        usage="prop", binding_mode="frozen",
    ))
    accepted = {**accepted, "_binding_id": binding["id"]}
    return db, media_store, workbench, generation, project, episode, segment, shot, asset, accepted, binding


def h3_spec(project, shot, asset, accepted):
    return H3VideoPrepare(
        project_id=project["id"], shot_id=shot["id"], target_asset_id=asset["id"],
        prompt="演员抬眼，镜头缓慢推近", keyframes=[{
            "media_id": accepted["id"], "binding_id": accepted["_binding_id"],
            "frame_index": 0,
        }],
        duration_seconds=6,
    )


def add_keyframe(workbench, generation, project, shot, name):
    asset = workbench.create_asset(
        project["id"], AssetCreate(kind="prop", name=name, content={})
    )
    job = generation.prepare(GenerationPrepare(
        project_id=project["id"], owner_type="asset", owner_id=asset["id"],
        target_asset_id=asset["id"], prompt=name, adapter="fake", count=1,
    ))
    confirmed = generation.confirm(job["id"], job["fingerprint"])
    generation.run(job["id"], confirmed["confirmation_token"])
    candidate = generation.list_candidates(project["id"], "asset", asset["id"])[0]
    accepted = generation.accept_candidate(candidate["id"], MediaCandidateAccept(
        asset_id=asset["id"], expected_asset_revision=0,
    ))
    binding = workbench.bind_reference(BindingCreate(
        shot_id=shot["id"], asset_version_id=accepted["accepted_asset_version_id"],
        usage="style", binding_mode="frozen",
    ))
    return asset, {**accepted, "_binding_id": binding["id"]}


def replace_asset_ref(generation, project, asset, prompt):
    revision = generation.db.connection.execute(
        "SELECT revision FROM assets WHERE id=?", (asset["id"],)
    ).fetchone()[0]
    job = generation.prepare(GenerationPrepare(
        project_id=project["id"], owner_type="asset", owner_id=asset["id"],
        target_asset_id=asset["id"], prompt=prompt, adapter="fake", count=1,
    ))
    confirmation = generation.confirm(job["id"], job["fingerprint"])
    generation.run(job["id"], confirmation["confirmation_token"])
    candidate = generation.list_candidates(project["id"], "asset", asset["id"])[0]
    return generation.accept_candidate(candidate["id"], MediaCandidateAccept(
        asset_id=asset["id"], expected_asset_revision=revision,
    ))


def add_accepted_screenplay(db, project, episode):
    document_id = f"screenplay-{episode['id']}"
    version_id = f"{document_id}-v1"
    timestamp = "2026-08-30T00:00:00+00:00"
    with db.write() as conn:
        conn.execute(
            """INSERT INTO creative_documents(
            id,project_id,episode_id,kind,title,current_version_id,revision,created_at,updated_at
            ) VALUES(?,?,?,'screenplay','正式剧本',?,0,?,?)""",
            (document_id, project["id"], episode["id"], version_id, timestamp, timestamp),
        )
        conn.execute(
            """INSERT INTO creative_document_versions(
            id,document_id,version_number,content,status,projection_status,created_at,decided_at
            ) VALUES(?,?,1,'正式正文','ACCEPTED','not_projected',?,?)""",
            (version_id, document_id, timestamp, timestamp),
        )


def accept_h3_video(tmp_path, db, store, generation, project, shot, asset, accepted, fake):
    h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
    job = h3.prepare(h3_spec(project, shot, asset, accepted))
    confirmation = generation.confirm(job["id"], job["fingerprint"])
    h3.submit(job["id"], confirmation["confirmation_token"])
    h3.poll(job["id"])
    candidate = generation.list_candidates(project["id"], "shot", shot["id"])[0]
    revision = db.connection.execute(
        "SELECT revision FROM shots WHERE id=?", (shot["id"],)
    ).fetchone()[0]
    return generation.accept_candidate(candidate["id"], MediaCandidateAccept(
        expected_shot_revision=revision,
    ))


@pytest.mark.parametrize("url", [
    "http://localhost:3000", "http://10.0.0.2:3000", "http://user@127.0.0.1:3000",
    "http://127.0.0.1:3000?x=1", "http://127.0.0.1:3000/base",
])
def test_h3_url_is_literal_loopback_without_ambient_components(url):
    with pytest.raises(ValueError):
        validate_h3_url(url)


def test_h3_submit_restart_poll_ingest_accept_and_doctor(tmp_path):
    fake = FakeH3()
    try:
        db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        shared = tmp_path / "shared"
        h3 = H3VideoService(db, store, generation, fake.url, shared)
        doctor = h3.doctor()
        assert doctor["reachable"] and doctor["identity"] == "verified"
        assert doctor["ready"] and doctor["requested_task"] == "fl2va"
        assert doctor["requested_short_edge"] == 768
        job = h3.prepare(h3_spec(project, shot, asset, accepted))
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        submitted = h3.submit(job["id"], confirmation["confirmation_token"])
        assert submitted["external_job_id"] == "h3-job-1"
        assert fake.idempotency == [job["id"]]
        request = fake.requests[0]
        assert request["task"] == "fl2va" and request["model"] == "MiniMaxAI/MiniMax-H3"
        assert request["seconds"] == 6
        assert request["target"] == {
            "short_edge": 768, "aspect_ratio": "auto", "duration_seconds": 6,
        }
        sampling_keys = (
            "num_outputs_per_prompt", "num_inference_steps", "flow_shift",
            "audio_flow_shift", "seed",
        )
        assert {key: request[key] for key in sampling_keys} == {
            key: job["spec"][key] for key in sampling_keys
        }
        condition = request["conditions"][0]
        assert condition.keys() == {"type", "uri", "role", "frame_index"}
        assert condition["type"] == "image" and condition["role"] == "keyframe"
        frame = Path(condition["uri"].removeprefix("file://"))
        assert shared.resolve() in frame.resolve().parents and condition["frame_index"] == 0
        db.close()

        restarted = Database(tmp_path / "h3.sqlite3")
        restarted_generation = GenerationJobService(restarted, store)
        restarted_h3 = H3VideoService(restarted, store, restarted_generation, fake.url, shared)
        completed = restarted_h3.poll(job["id"])
        assert completed["state"] == "SUCCEEDED" and len(fake.requests) == 1
        candidate = restarted_generation.list_candidates(project["id"], "shot", shot["id"])[0]
        assert candidate["mime"] == "video/mp4" and candidate["candidate_status"] == "candidate"
        assert restarted_h3.poll(job["id"])["state"] == "SUCCEEDED"
        asset_before = restarted.connection.execute(
            "SELECT current_version_id,revision FROM assets WHERE id=?", (asset["id"],)
        ).fetchone()
        shot_revision = restarted.connection.execute(
            "SELECT revision FROM shots WHERE id=?", (shot["id"],)
        ).fetchone()[0]
        formal = restarted_generation.accept_candidate(candidate["id"], MediaCandidateAccept(
            expected_shot_revision=shot_revision,
        ))
        assert formal["candidate_status"] == "accepted" and formal["is_current"] == 1
        assert formal["accepted_asset_version_id"] is None
        assert formal["accepted_shot_revision"] == shot_revision
        assert restarted.connection.execute(
            "SELECT current_version_id,revision FROM assets WHERE id=?", (asset["id"],)
        ).fetchone() == asset_before
        restarted.close()
    finally:
        fake.close()


@pytest.mark.parametrize(("server", "identity"), [
    (lambda: FakeH3(model_id="wrong/model"), "mismatch"),
    (lambda: FakeH3(models_status=404), "degraded_models_endpoint_unsupported"),
])
def test_h3_doctor_never_reports_unverified_identity_ready(tmp_path, server, identity):
    fake = server()
    try:
        db = Database(tmp_path / "doctor.sqlite3")
        store = MediaStore(tmp_path / "media")
        doctor = H3VideoService(
            db, store, GenerationJobService(db, store), fake.url, tmp_path / "shared"
        ).doctor()
        assert doctor["reachable"] and doctor["identity"] == identity
        assert doctor["ready"] is False
        db.close()
    finally:
        fake.close()


def test_h3_preserves_explicit_start_and_end_keyframe_order(tmp_path):
    fake = FakeH3()
    try:
        db, store, workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        end_asset, end = add_keyframe(workbench, generation, project, shot, "结束帧")
        h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
        spec = H3VideoPrepare.model_validate({
            **h3_spec(project, shot, asset, accepted).model_dump(mode="json"),
            "target_asset_id": end_asset["id"],
            "keyframes": [
                {"media_id": end["id"], "binding_id": end["_binding_id"], "frame_index": 0},
                {"media_id": accepted["id"], "binding_id": accepted["_binding_id"], "frame_index": -1},
            ],
        })
        job = h3.prepare(spec)
        assert [(item["media_id"], item["role"], item["frame_index"])
                for item in job["input_hashes"]["conditions"]] == [
            (end["id"], "start", 0), (accepted["id"], "end", -1),
        ]
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        h3.submit(job["id"], confirmation["confirmation_token"])
        assert [item["frame_index"] for item in fake.requests[0]["conditions"]] == [0, -1]
        with pytest.raises(ValueError, match="start keyframe"):
            h3.prepare(spec.model_copy(update={"target_asset_id": asset["id"]}))
        db.close()
    finally:
        fake.close()


def test_h3_freezes_seed_per_job_and_new_round_gets_a_new_seed(tmp_path, monkeypatch):
    seeds = iter((101, 202))
    monkeypatch.setattr(
        "script_weaver.application.h3_service.secrets.randbelow", lambda _limit: next(seeds)
    )
    db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
    service = H3VideoService(
        db, store, generation, "http://127.0.0.1:1", tmp_path / "shared"
    )
    first = service.prepare(h3_spec(project, shot, asset, accepted))
    second = service.prepare(h3_spec(project, shot, asset, accepted))
    assert first["spec"]["seed"] == first["input_hashes"]["sampling"]["seed"] == 101
    assert second["spec"]["seed"] == 202
    first_id = first["id"]
    db.close()
    restarted = Database(tmp_path / "h3.sqlite3")
    restarted_generation = GenerationJobService(restarted, store)
    assert restarted_generation._get(first_id)["spec"]["seed"] == 101
    restarted.close()


def test_h3_accepts_frozen_historical_ref_but_rejects_stale_follow_latest(tmp_path):
    db, store, workbench, generation, project, _episode, segment, frozen_shot, asset, historical, _binding = accepted_keyframe(tmp_path)
    follow_shot = workbench.create_shot(segment["id"], ShotCreate(order_index=1))
    workbench.bind_reference(BindingCreate(
        shot_id=follow_shot["id"], asset_version_id=historical["accepted_asset_version_id"],
        usage="prop", binding_mode="follow_latest",
    ))
    latest = replace_asset_ref(generation, project, asset, "更新后的正式 REF")
    assert latest["accepted_asset_version_id"] != historical["accepted_asset_version_id"]
    assert generation.get_candidate(historical["id"])["is_current"] == 0
    h3 = H3VideoService(
        db, store, generation, "http://127.0.0.1:1", tmp_path / "shared"
    )
    frozen = h3.prepare(h3_spec(project, frozen_shot, asset, historical))
    assert frozen["input_hashes"]["conditions"][0]["asset_version_id"] == historical[
        "accepted_asset_version_id"
    ]
    with pytest.raises(ValueError, match="non-stale|binding is stale"):
        h3.prepare(h3_spec(project, follow_shot, asset, historical))
    db.close()


def test_h3_keyframe_uses_exact_binding_when_same_asset_has_multiple_versions(tmp_path):
    db, store, workbench, generation, project, _episode, _segment, shot, asset, v1, binding_v1 = accepted_keyframe(tmp_path)
    v2 = replace_asset_ref(generation, project, asset, "同一道具第二版正式 REF")
    binding_v2 = workbench.bind_reference(BindingCreate(
        shot_id=shot["id"], asset_version_id=v2["accepted_asset_version_id"],
        usage="character", binding_mode="frozen",
    ))
    service = H3VideoService(
        db, store, generation, "http://127.0.0.1:1", tmp_path / "shared"
    )
    old = h3_spec(project, shot, asset, {
        **v1, "_binding_id": binding_v1["id"],
    })
    new = h3_spec(project, shot, asset, {
        **v2, "_binding_id": binding_v2["id"],
    })
    assert service.prepare(old)["input_hashes"]["conditions"][0]["asset_version_id"] == v1[
        "accepted_asset_version_id"
    ]
    assert service.prepare(new)["input_hashes"]["conditions"][0]["asset_version_id"] == v2[
        "accepted_asset_version_id"
    ]
    mismatched = old.model_copy(deep=True)
    mismatched.keyframes[0].binding_id = binding_v2["id"]
    with pytest.raises(ValueError, match="frozen"):
        service.prepare(mismatched)
    unrelated = workbench.create_asset(
        project["id"], AssetCreate(kind="prop", name="未绑定目标", content={})
    )
    wrong_target = old.model_copy(update={"target_asset_id": unrelated["id"]})
    with pytest.raises(ValueError, match="target asset"):
        service.prepare(wrong_target)
    db.close()


def test_accepted_videos_are_current_per_shot_without_replacing_shared_asset_ref(tmp_path):
    fake = FakeH3()
    try:
        db, store, workbench, generation, project, episode, segment, first_shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        second_shot = workbench.create_shot(segment["id"], ShotCreate(order_index=1))
        second_binding = workbench.bind_reference(BindingCreate(
            shot_id=second_shot["id"], asset_version_id=accepted["accepted_asset_version_id"],
            usage="prop", binding_mode="frozen",
        ))
        h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
        asset_before = tuple(db.connection.execute(
            "SELECT current_version_id,revision FROM assets WHERE id=?", (asset["id"],)
        ).fetchone())
        formal_ids = []
        for shot in (first_shot, second_shot):
            exact = accepted if shot["id"] == first_shot["id"] else {
                **accepted, "_binding_id": second_binding["id"],
            }
            job = h3.prepare(h3_spec(project, shot, asset, exact))
            confirmation = generation.confirm(job["id"], job["fingerprint"])
            h3.submit(job["id"], confirmation["confirmation_token"])
            h3.poll(job["id"])
            candidate = generation.list_candidates(project["id"], "shot", shot["id"])[0]
            shot_revision = db.connection.execute(
                "SELECT revision FROM shots WHERE id=?", (shot["id"],)
            ).fetchone()[0]
            formal = generation.accept_candidate(candidate["id"], MediaCandidateAccept(
                expected_shot_revision=shot_revision,
            ))
            formal_ids.append(formal["id"])
        assert tuple(db.connection.execute(
            "SELECT current_version_id,revision FROM assets WHERE id=?", (asset["id"],)
        ).fetchone()) == asset_before
        rows = db.connection.execute(
            """SELECT owner_id,id,is_current,accepted_asset_version_id,accepted_shot_revision
            FROM media_versions WHERE id IN (?,?) ORDER BY owner_id""", formal_ids,
        ).fetchall()
        assert {row[0] for row in rows} == {first_shot["id"], second_shot["id"]}
        assert all(row[2] == 1 and row[3] is None and row[4] == 1 for row in rows)
        db.close()
    finally:
        fake.close()


def test_exact_prerelease_v6_is_backed_up_and_repairs_video_slot_semantics(tmp_path):
    fake = FakeH3()
    database_path = tmp_path / "prerelease-v6.sqlite3"
    try:
        db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        # Move the fixture to the explicit path used by this repair test.
        db.close()
        (tmp_path / "h3.sqlite3").replace(database_path)
        db = Database(database_path)
        generation = GenerationJobService(db, store)
        h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
        job = h3.prepare(h3_spec(project, shot, asset, accepted))
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        h3.submit(job["id"], confirmation["confirmation_token"])
        h3.poll(job["id"])
        video = generation.list_candidates(project["id"], "shot", shot["id"])[0]
        shot_revision = db.connection.execute(
            "SELECT revision FROM shots WHERE id=?", (shot["id"],)
        ).fetchone()[0]
        generation.accept_candidate(video["id"], MediaCandidateAccept(
            expected_shot_revision=shot_revision,
        ))
        image_version = db.connection.execute(
            "SELECT current_version_id FROM assets WHERE id=?", (asset["id"],)
        ).fetchone()[0]
        db.close()

        legacy_version = "legacy-video-asset-version"
        raw = sqlite3.connect(database_path)
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("DROP TRIGGER media_candidate_accept_transition")
        raw.execute(
            """INSERT INTO asset_versions(
            id,asset_id,version_number,content_json,supersedes_id,created_at
            ) VALUES(?,?,3,?,?,?)""",
            (legacy_version, asset["id"], json.dumps({"media_version_id": video["id"]}),
             image_version, "2026-08-30T00:00:00+00:00"),
        )
        raw.execute(
            "UPDATE assets SET current_version_id=?,revision=revision+1 WHERE id=?",
            (legacy_version, asset["id"]),
        )
        raw.execute("UPDATE media_versions SET is_current=0 WHERE id=?", (accepted["id"],))
        raw.execute(
            "UPDATE media_versions SET accepted_asset_version_id=?,accepted_shot_revision=NULL WHERE id=?",
            (legacy_version, video["id"]),
        )
        raw.execute("ALTER TABLE media_versions DROP COLUMN accepted_shot_revision")
        raw.commit()
        raw.close()
        backups_before = set(tmp_path.glob("prerelease-v6.sqlite3.backup-*"))

        repaired = Database(database_path)
        assert set(tmp_path.glob("prerelease-v6.sqlite3.backup-*")) > backups_before
        repaired_video = repaired.connection.execute(
            """SELECT accepted_asset_version_id,accepted_shot_revision,is_current
            FROM media_versions WHERE id=?""", (video["id"],),
        ).fetchone()
        assert tuple(repaired_video) == (None, shot_revision, 1)
        assert repaired.connection.execute(
            "SELECT current_version_id FROM assets WHERE id=?", (asset["id"],)
        ).fetchone()[0] == image_version
        assert repaired.connection.execute(
            "SELECT is_current FROM media_versions WHERE id=?", (accepted["id"],)
        ).fetchone()[0] == 1
        repaired.close()
    finally:
        fake.close()


def test_malformed_prerelease_v6_is_rejected_without_writes(tmp_path):
    database_path = tmp_path / "malformed-v6.sqlite3"
    Database(database_path).close()
    raw = sqlite3.connect(database_path)
    raw.execute("DROP TRIGGER media_candidate_accept_transition")
    raw.execute("ALTER TABLE media_versions DROP COLUMN accepted_shot_revision")
    raw.execute("ALTER TABLE production_packages DROP COLUMN sha256")
    raw.commit()
    raw.close()
    backups_before = set(tmp_path.glob("malformed-v6.sqlite3.backup-*"))
    raw = sqlite3.connect(database_path)
    before = raw.execute("SELECT sql FROM sqlite_master ORDER BY type,name").fetchall()
    raw.close()
    with pytest.raises(SchemaError):
        Database(database_path)
    raw = sqlite3.connect(database_path)
    after = raw.execute("SELECT sql FROM sqlite_master ORDER BY type,name").fetchall()
    raw.close()
    assert after == before
    assert set(tmp_path.glob("malformed-v6.sqlite3.backup-*")) == backups_before


def test_restart_upgrades_unique_legacy_submitted_h3_job_and_poll_completes(tmp_path):
    fake = FakeH3()
    try:
        db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
        job = h3.prepare(h3_spec(project, shot, asset, accepted))
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        h3.submit(job["id"], confirmation["confirmation_token"])
        spec = dict(job["spec"])
        spec["keyframe_media_ids"] = [item["media_id"] for item in spec.pop("keyframes")]
        for key in (
            "num_outputs_per_prompt", "num_inference_steps", "flow_shift",
            "audio_flow_shift", "seed",
        ):
            spec.pop(key)
        inputs = dict(job["input_hashes"])
        inputs.pop("sampling")
        with db.write() as conn:
            conn.execute(
                """UPDATE generation_jobs SET spec_json=?,input_hashes_json=?,
                fingerprint='legacy',submitted_fingerprint='legacy' WHERE id=?""",
                (json.dumps(spec), json.dumps(inputs), job["id"]),
            )
        db.close()

        restarted = Database(tmp_path / "h3.sqlite3")
        restarted_generation = GenerationJobService(restarted, store)
        repaired = restarted_generation._get(job["id"])
        assert repaired["state"] == "H3_SUBMITTED"
        assert repaired["spec"]["keyframes"][0] == {
            "media_id": accepted["id"], "binding_id": accepted["_binding_id"],
            "frame_index": 0,
        }
        assert repaired["spec"]["seed"] == repaired["input_hashes"]["sampling"]["seed"] == 42
        completed = H3VideoService(
            restarted, store, restarted_generation, fake.url, tmp_path / "shared"
        ).poll(job["id"])
        assert completed["state"] == "SUCCEEDED"
        restarted.close()
    finally:
        fake.close()


def test_h3_changed_inputs_and_bad_content_never_change_facts(tmp_path):
    fake = FakeH3(content=b"not an mp4")
    try:
        db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
        job = h3.prepare(h3_spec(project, shot, asset, accepted))
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        db.connection.execute("UPDATE shots SET revision=revision+1 WHERE id=?", (shot["id"],))
        with pytest.raises(Exception, match="inputs changed"):
            h3.submit(job["id"], confirmation["confirmation_token"])
        assert not fake.requests

        fresh_shot = dict(shot)
        fresh_shot["revision"] += 1
        job = h3.prepare(h3_spec(project, fresh_shot, asset, accepted))
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        h3.submit(job["id"], confirmation["confirmation_token"])
        with pytest.raises(ValueError, match="MP4"):
            h3.poll(job["id"])
        assert not generation.list_candidates(project["id"], "shot", shot["id"])
        assert generation._get(job["id"])["state"] == "FAILED"
        db.close()
    finally:
        fake.close()


def test_h3_completion_after_revision_change_is_stale_candidate(tmp_path):
    fake = FakeH3()
    try:
        db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
        job = h3.prepare(h3_spec(project, shot, asset, accepted))
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        h3.submit(job["id"], confirmation["confirmation_token"])
        db.connection.execute("UPDATE shots SET revision=revision+1 WHERE id=?", (shot["id"],))
        h3.poll(job["id"])
        candidate = generation.list_candidates(project["id"], "shot", shot["id"])[0]
        assert candidate["is_stale"] == 1 and candidate["candidate_status"] == "candidate"
        with pytest.raises(Exception, match="stale media"):
            generation.accept_candidate(candidate["id"], MediaCandidateAccept(
                expected_shot_revision=shot["revision"] + 1,
            ))
        db.close()
    finally:
        fake.close()


@pytest.mark.parametrize("fake", [
    pytest.param(lambda: FakeH3(redirect=True), id="redirect"),
    pytest.param(lambda: FakeH3(content=mp4(), content_type="text/html"), id="mime"),
])
def test_h3_content_redirect_and_wrong_mime_are_rejected(tmp_path, fake):
    server = fake()
    try:
        db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        h3 = H3VideoService(db, store, generation, server.url, tmp_path / "shared")
        job = h3.prepare(h3_spec(project, shot, asset, accepted))
        confirmation = generation.confirm(job["id"], job["fingerprint"])
        h3.submit(job["id"], confirmation["confirmation_token"])
        with pytest.raises(ValueError):
            h3.poll(job["id"])
        assert not generation.list_candidates(project["id"], "shot", shot["id"])
        db.close()
    finally:
        server.close()


def test_shared_root_symlink_and_cross_project_keyframe_are_rejected(tmp_path):
    real = tmp_path / "real-shared"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    db, store, _workbench, generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        H3VideoService(db, store, generation, "http://127.0.0.1:1", alias)
    other = _workbench.create_project(ProjectCreate(title="别的项目"))
    service = H3VideoService(db, store, generation, "http://127.0.0.1:1", real)
    with pytest.raises(Exception):
        service.prepare(h3_spec(other, shot, asset, accepted))
    foreign_asset = _workbench.create_asset(
        other["id"], AssetCreate(kind="prop", name="外部关键帧", content={})
    )
    foreign_job = generation.prepare(GenerationPrepare(
        project_id=other["id"], owner_type="asset", owner_id=foreign_asset["id"],
        target_asset_id=foreign_asset["id"], prompt="foreign", adapter="fake", count=1,
    ))
    confirmation = generation.confirm(foreign_job["id"], foreign_job["fingerprint"])
    generation.run(foreign_job["id"], confirmation["confirmation_token"])
    foreign_candidate = generation.list_candidates(other["id"], "asset", foreign_asset["id"])[0]
    foreign_accepted = generation.accept_candidate(foreign_candidate["id"], MediaCandidateAccept(
        asset_id=foreign_asset["id"], expected_asset_revision=0,
    ))
    with pytest.raises(ValueError, match="another project"):
            service.prepare(h3_spec(project, shot, asset, {
                **foreign_accepted, "_binding_id": accepted["_binding_id"],
            }))
    with pytest.raises(ValueError, match="distinct"):
        H3VideoPrepare.model_validate({
            **h3_spec(project, shot, asset, accepted).model_dump(mode="json"),
            "keyframes": [
            {"media_id": accepted["id"], "binding_id": accepted["_binding_id"], "frame_index": 0},
            {"media_id": accepted["id"], "binding_id": accepted["_binding_id"], "frame_index": -1},
            ],
        })
    db.close()


def test_h3_and_package_routes_are_creator_only(tmp_path):
    fake = FakeH3()
    try:
        db, _store, _workbench, _generation, project, _episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        db.close()
        app = create_app(
            tmp_path / "h3.sqlite3", tmp_path / "media", token="creator", agent_token="agent",
            h3_url=fake.url, h3_shared_root=tmp_path / "shared",
            production_package_root=tmp_path / "packages",
        )
        body = h3_spec(project, shot, asset, accepted).model_dump(mode="json")
        creator = {"Authorization": "Bearer creator"}
        agent = {"Authorization": "Bearer agent"}
        with TestClient(app) as client:
            assert client.post("/api/h3/video-jobs", json=body, headers=agent).status_code == 403
            prepared = client.post("/api/h3/video-jobs", json=body, headers=creator)
            assert prepared.status_code == 201, prepared.text
            job = prepared.json()
            latest = client.get(
                f"/api/projects/{project['id']}/shots/{shot['id']}/h3-video-job",
                headers=agent,
            )
            assert latest.status_code == 200 and latest.json()["id"] == job["id"]
            confirmation = client.post(
                f"/api/generation-jobs/{job['id']}/confirm",
                json={"fingerprint": job["fingerprint"]}, headers=creator,
            ).json()
            route = f"/api/h3/video-jobs/{job['id']}/submit"
            assert client.post(route, json={"confirmation_token": confirmation["confirmation_token"]}, headers=agent).status_code == 403
            assert client.post(route, json={"confirmation_token": confirmation["confirmation_token"]}, headers=creator).status_code == 200
            package_route = f"/api/projects/{project['id']}/production-packages"
            assert client.post(package_route, json={"expected_project_revision": project["revision"]}, headers=agent).status_code == 403
            assert client.post(package_route, json={"expected_project_revision": project["revision"]}, headers=creator).status_code == 201
        app.state.db.close()
    finally:
        fake.close()


def test_production_package_is_deterministic_versioned_and_atomic(tmp_path, monkeypatch):
    db, store, _workbench, _generation, project, _episode, _segment, _shot, _asset, _accepted, _binding = accepted_keyframe(tmp_path)
    service = ProductionPackageService(db, tmp_path / "packages", store)
    first = service.build(project["id"], project["revision"])
    same = service.build(project["id"], project["revision"])
    assert same["id"] == first["id"] and same["sha256"] == first["sha256"]
    manifest = first["manifest"]
    assert manifest["format"] == "script-weaver-production-package-v1"
    assert manifest["missing"] and manifest["accepted"]["assets"] and not manifest["ready"]
    assert "weights" not in json.dumps(manifest).lower()

    db.connection.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (project["id"],))
    original_replace = __import__("script_weaver.application.production_package_service", fromlist=["os"]).os.replace

    def fail_replace(_source, _target):
        raise OSError("publish failed")

    monkeypatch.setattr("script_weaver.application.production_package_service.os.replace", fail_replace)
    with pytest.raises(OSError, match="publish failed"):
        service.build(project["id"], project["revision"] + 1)
    monkeypatch.setattr("script_weaver.application.production_package_service.os.replace", original_replace)
    assert len(service.list(project["id"])) == 1
    assert not list((tmp_path / "packages" / project["id"]).glob(".v*.tmp"))
    db.close()


def test_production_package_rejects_corrupt_accepted_media(tmp_path):
    db, store, _workbench, _generation, project, _episode, _segment, _shot, _asset, accepted, _binding = accepted_keyframe(tmp_path)
    path = Path(db.connection.execute(
        "SELECT storage_path FROM media_versions WHERE id=?", (accepted["id"],)
    ).fetchone()[0])
    path.write_bytes(b"corrupt")
    service = ProductionPackageService(db, tmp_path / "packages", store)
    with pytest.raises(ValueError, match="corrupt"):
        service.build(project["id"], project["revision"])
    assert not service.list(project["id"])
    db.close()


def test_ready_package_links_verified_formal_video_and_ignores_resolved_history(tmp_path):
    fake = FakeH3()
    try:
        db, store, _workbench, generation, project, episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        add_accepted_screenplay(db, project, episode)
        old = "2020-01-01T00:00:00+00:00"
        with db.write() as conn:
            conn.execute(
                """INSERT INTO generation_jobs(
                id,project_id,owner_type,owner_id,kind,state,spec_json,fingerprint,
                input_hashes_json,adapter,error_json,created_at,updated_at,completed_at
                ) VALUES('failed-old',?,'shot',?,'video','FAILED','{}','old','{}',
                'h3-fl2va','{"message":"old failure"}',?,?,?)""",
                (project["id"], shot["id"], old, old, old),
            )
        formal = accept_h3_video(
            tmp_path, db, store, generation, project, shot, asset, accepted, fake
        )
        # A stale, unaccepted exploration is history, not a production fact.
        h3 = H3VideoService(db, store, generation, fake.url, tmp_path / "shared")
        obsolete = h3.prepare(h3_spec(project, shot, asset, accepted))
        confirmation = generation.confirm(obsolete["id"], obsolete["fingerprint"])
        h3.submit(obsolete["id"], confirmation["confirmation_token"])
        h3.poll(obsolete["id"])
        stale_id = next(
            item["id"] for item in generation.list_candidates(
                project["id"], "shot", shot["id"]
            ) if item["candidate_status"] == "candidate"
        )
        db.connection.execute(
            "UPDATE media_versions SET is_stale=1 WHERE id=?", (stale_id,)
        )
        assert any(
            item["candidate_status"] == "candidate" and item["is_stale"]
            for item in generation.list_candidates(project["id"], "shot", shot["id"])
        )
        package = ProductionPackageService(db, tmp_path / "packages", store).build(
            project["id"], project["revision"]
        )["manifest"]
        packaged_shot = package["accepted"]["shots"][0]
        assert package["ready"] is True
        assert package["failed"] == [] and package["stale"]["media"] == []
        assert packaged_shot["video_media_version_id"] == formal["id"]
        assert packaged_shot["video_sha256"] == formal["sha256"]
        assert packaged_shot["video_accepted_shot_revision"] == db.connection.execute(
            "SELECT revision FROM shots WHERE id=?", (shot["id"],)
        ).fetchone()[0]
        db.close()
    finally:
        fake.close()


def test_package_rejects_video_from_old_shot_revision_until_regenerated(tmp_path):
    fake = FakeH3()
    try:
        db, store, workbench, generation, project, episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        add_accepted_screenplay(db, project, episode)
        old_video = accept_h3_video(
            tmp_path, db, store, generation, project, shot, asset, accepted, fake
        )
        current_revision = db.connection.execute(
            "SELECT revision FROM shots WHERE id=?", (shot["id"],)
        ).fetchone()[0]
        updated = workbench.update_shot(
            shot["id"], current_revision, ShotUpdateFields(dialogue="第二版对白")
        )
        service = ProductionPackageService(db, tmp_path / "packages", store)
        stale = service.build(project["id"], project["revision"])["manifest"]
        assert stale["ready"] is False
        assert old_video["id"] in stale["stale"]["media"]
        assert {item["type"] for item in stale["missing"]} == {"accepted_video"}
        fresh_video = accept_h3_video(
            tmp_path, db, store, generation, project, updated, asset, accepted, fake
        )
        ready = service.build(project["id"], project["revision"])["manifest"]
        assert ready["ready"] is True and ready["stale"]["media"] == []
        assert ready["accepted"]["shots"][0]["video_media_version_id"] == fresh_video["id"]
        assert ready["accepted"]["shots"][0]["video_accepted_shot_revision"] == updated[
            "revision"
        ]
        db.close()
    finally:
        fake.close()


def test_stale_active_screenplay_blocks_package_but_removed_history_does_not(tmp_path):
    fake = FakeH3()
    try:
        db, store, _workbench, generation, project, episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        add_accepted_screenplay(db, project, episode)
        accept_h3_video(tmp_path, db, store, generation, project, shot, asset, accepted, fake)
        document_id = f"screenplay-{episode['id']}"
        db.connection.execute(
            "UPDATE creative_documents SET is_stale=1,stale_reason='needs revision' WHERE id=?",
            (document_id,),
        )
        service = ProductionPackageService(db, tmp_path / "packages", store)
        stale = service.build(project["id"], project["revision"])["manifest"]
        assert stale["ready"] is False and stale["stale"]["documents"] == [document_id]
        # Accepting a replacement clears these aggregate flags; package consumes that current fact.
        db.connection.execute(
            "UPDATE creative_documents SET is_stale=0,stale_reason=NULL WHERE id=?", (document_id,)
        )
        ready = service.build(project["id"], project["revision"])["manifest"]
        assert ready["ready"] is True and ready["stale"]["documents"] == []
        db.connection.execute("UPDATE episodes SET status='removed' WHERE id=?", (episode["id"],))
        db.connection.execute(
            "UPDATE creative_documents SET is_stale=1,stale_reason='removed history' WHERE id=?",
            (document_id,),
        )
        removed = service.build(project["id"], project["revision"])["manifest"]
        assert removed["stale"]["documents"] == []
        db.close()
    finally:
        fake.close()


def test_retired_history_media_and_bindings_do_not_block_current_package(tmp_path):
    fake = FakeH3()
    try:
        db, store, _workbench, generation, project, episode, segment, shot, asset, accepted, binding = accepted_keyframe(tmp_path)
        add_accepted_screenplay(db, project, episode)
        formal = accept_h3_video(
            tmp_path, db, store, generation, project, shot, asset, accepted, fake
        )
        Path(db.connection.execute(
            "SELECT storage_path FROM media_versions WHERE id=?", (formal["id"],)
        ).fetchone()[0]).write_bytes(b"corrupt retired history")
        with db.write() as conn:
            conn.execute("UPDATE shots SET status='retired' WHERE id=?", (shot["id"],))
            conn.execute("UPDATE reference_bindings SET is_stale=1 WHERE id=?", (binding["id"],))
            conn.execute("UPDATE media_versions SET is_stale=1 WHERE id=?", (formal["id"],))
        manifest = ProductionPackageService(db, tmp_path / "packages", store).build(
            project["id"], project["revision"]
        )["manifest"]
        assert manifest["ready"] is True
        assert manifest["accepted"]["shots"] == []
        assert manifest["stale"] == {
            "documents": [], "shots": [], "bindings": [], "media": [],
        }
        db.close()
    finally:
        fake.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_production_package_rejects_missing_or_corrupt_accepted_video(tmp_path, damage):
    fake = FakeH3()
    try:
        db, store, _workbench, generation, project, episode, _segment, shot, asset, accepted, _binding = accepted_keyframe(tmp_path)
        add_accepted_screenplay(db, project, episode)
        formal = accept_h3_video(
            tmp_path, db, store, generation, project, shot, asset, accepted, fake
        )
        path = Path(db.connection.execute(
            "SELECT storage_path FROM media_versions WHERE id=?", (formal["id"],)
        ).fetchone()[0])
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt")
        service = ProductionPackageService(db, tmp_path / "packages", store)
        with pytest.raises(ValueError, match=damage):
            service.build(project["id"], project["revision"])
        assert not service.list(project["id"])
        db.close()
    finally:
        fake.close()


def test_production_package_excludes_removed_episode_documents_and_retired_segments(tmp_path):
    db, store, _workbench, _generation, project, episode, segment, _shot, _asset, _accepted, _binding = accepted_keyframe(tmp_path)
    document_id, version_id = "removed-screenplay", "removed-screenplay-v1"
    with db.write() as conn:
        conn.execute(
            """INSERT INTO creative_documents(
            id,project_id,episode_id,kind,title,current_version_id,revision,created_at,updated_at
            ) VALUES(?,?,?,'screenplay','已移出剧本',?,0,?,?)""",
            (document_id, project["id"], episode["id"], version_id,
             "2026-08-30T00:00:00+00:00", "2026-08-30T00:00:00+00:00"),
        )
        conn.execute(
            """INSERT INTO creative_document_versions(
            id,document_id,version_number,content,status,projection_status,created_at,decided_at
            ) VALUES(?,?,1,'正文','ACCEPTED','not_applicable',?,?)""",
            (version_id, document_id, "2026-08-30T00:00:00+00:00",
             "2026-08-30T00:00:00+00:00"),
        )
        conn.execute("UPDATE episodes SET status='removed' WHERE id=?", (episode["id"],))
        conn.execute("UPDATE segments SET status='retired' WHERE id=?", (segment["id"],))
    manifest = ProductionPackageService(db, tmp_path / "packages", store).build(
        project["id"], project["revision"]
    )["manifest"]
    assert document_id not in {item["id"] for item in manifest["accepted"]["documents"]}
    assert manifest["accepted"]["shots"] == []
    assert {item["episode_id"] for item in manifest["excluded"]} == {episode["id"]}
    db.close()
