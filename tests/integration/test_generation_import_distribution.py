"""PR4: generation invalidation/recovery, media atomicity, legacy import, migrations, skills."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from script_weaver.application.generation_service import GenerationJobService
from script_weaver.application.workbench_service import WorkbenchService, skill_tree_hash
from script_weaver.core.types import (
    Character,
    ProjectMeta,
    ProjectState,
    Script,
    ScriptScene,
    ScriptSceneHeading,
    Shot as LegacyShot,
    Storyboard,
)
from script_weaver.domain.models import (
    AssetCreate,
    ConflictError,
    EpisodeCreate,
    GenerationPrepare,
    NotFoundError,
    ProjectCreate,
    ProjectMismatchError,
    SegmentCreate,
    ShotCreate,
    TaskStart,
)
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database, SchemaError


@pytest.fixture
def work_dir():
    path = Path(".test-tmp") / uuid4().hex
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def seed(work_dir):
    db = Database(work_dir / "workbench.sqlite3")
    workbench = WorkbenchService(db)
    project = workbench.create_project(ProjectCreate(title="雾渡"))
    episode = workbench.create_episode(project["id"], EpisodeCreate(episode_number=1))
    segment = workbench.create_segment(episode["id"], SegmentCreate(code="E01-01", order_index=0))
    return db, workbench, project, episode, segment


def generation_service(db, work_dir):
    return GenerationJobService(db, MediaStore(work_dir / "media"))


def base_prepare(project_id, shot_id, **overrides):
    payload = dict(
        project_id=project_id, owner_type="shot", owner_id=shot_id, prompt="mist",
        count=1, model="gpt-image-2", parameters={}, reference_asset_version_ids=[],
        adapter="fake", output_spec={"mime": "image/png"},
    )
    payload.update(overrides)
    return GenerationPrepare(**payload)


def test_prepare_rejects_missing_and_cross_project_inputs(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    other_project = workbench.create_project(ProjectCreate(title="同库另一个项目"))
    foreign_asset = workbench.create_asset(other_project["id"], AssetCreate(kind="prop", name="别家道具", content={}))
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    service = generation_service(db, work_dir)

    with pytest.raises(NotFoundError):
        service.prepare(base_prepare("missing-project", shot["id"]))
    with pytest.raises(ProjectMismatchError):
        service.prepare(base_prepare(project["id"], foreign_asset["id"]).model_copy(update={"owner_type": "asset"}))
    with pytest.raises(NotFoundError):
        service.prepare(base_prepare(project["id"], "missing-shot"))
    with pytest.raises(ProjectMismatchError):
        service.prepare(base_prepare(project["id"], shot["id"], reference_asset_version_ids=[foreign_asset["current_version_id"]]))
    with pytest.raises(NotFoundError):
        service.prepare(base_prepare(project["id"], shot["id"], reference_asset_version_ids=["missing-version"]))
    assert db.connection.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 0
    db.close()


def test_changed_owner_revision_makes_confirmed_run_stale_without_adapter_call(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    service = generation_service(db, work_dir)

    class Guard:
        calls = 0

        def generate(self, spec, staging):
            self.calls += 1
            return []

    guard = Guard()
    service.adapters["fake"] = guard
    job = service.prepare(base_prepare(project["id"], shot["id"]))
    confirmed = service.confirm(job["id"], job["fingerprint"])
    workbench.update_shot(shot["id"], 0, {"duration_seconds": 4})

    with pytest.raises(ConflictError):
        service.run(job["id"], confirmed["confirmation_token"])
    assert guard.calls == 0
    state = service._get(job["id"])
    assert state["state"] == "STALE"
    # The token is consumed: retrying the stale job can never run either.
    with pytest.raises(ConflictError):
        service.run(job["id"], confirmed["confirmation_token"])
    assert guard.calls == 0
    db.close()


def test_running_jobs_fail_on_daemon_restart_without_retry(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    service = generation_service(db, work_dir)

    class Guard:
        calls = 0

        def generate(self, spec, staging):
            self.calls += 1
            return []

    guard = Guard()
    service.adapters["fake"] = guard
    job = service.prepare(base_prepare(project["id"], shot["id"]))
    confirmed = service.confirm(job["id"], job["fingerprint"])
    db.connection.execute("UPDATE generation_jobs SET state='RUNNING',confirmation_used=1 WHERE id=?", (job["id"],))

    recovered = GenerationJobService(db, MediaStore(work_dir / "media"))
    state = recovered._get(job["id"])
    assert state["state"] == "FAILED"
    error = state["error"]
    assert error["code"] == "daemon_restart_during_generation"
    assert "未自动重试" in error["message"]
    assert guard.calls == 0
    with pytest.raises(ConflictError):
        recovered.run(job["id"], confirmed["confirmation_token"])
    db.close()


def test_multi_output_job_ingests_all_as_unaccepted_candidates_and_cleans_staging(work_dir):
    db, workbench, project, _episode, segment = seed(work_dir)
    shot = workbench.create_shot(segment["id"], ShotCreate(order_index=0))
    service = generation_service(db, work_dir)
    job = service.prepare(base_prepare(project["id"], shot["id"], count=2))
    confirmed = service.confirm(job["id"], job["fingerprint"])
    finished = service.run(job["id"], confirmed["confirmation_token"])

    assert finished["state"] == "SUCCEEDED"
    rows = db.connection.execute(
        "SELECT is_current,candidate_status FROM media_versions WHERE owner_type='shot' AND owner_id=? AND kind='image' ORDER BY created_at",
        (shot["id"],),
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(0, "candidate"), (0, "candidate")]
    assert not (Path(work_dir) / "media" / "staging" / job["id"]).exists()
    db.close()


def test_interrupted_object_publication_leaves_no_partial_object(work_dir, monkeypatch):
    store = MediaStore(work_dir / "media")
    staged = store.staging / "job-1"
    staged.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\n" + b"payload" * 100
    source = staged / "0.png"
    source.write_bytes(payload)

    real_replace = __import__("os").replace

    def broken_replace(temp, target):
        raise OSError("simulated crash during publish")

    monkeypatch.setattr("script_weaver.infrastructure.media_store.os.replace", broken_replace)
    with pytest.raises(OSError):
        store.ingest(source)
    monkeypatch.setattr("script_weaver.infrastructure.media_store.os.replace", real_replace)

    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    target = store.objects / digest[:2] / digest
    assert not target.exists(), "failed publish must not leave a partial object"
    stored, stored_digest = store.ingest(source)
    assert stored == str(target) and stored_digest == digest
    assert target.read_bytes() == payload

    # A corrupted existing object is detected instead of trusted.
    target.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="corrupt"):
        store.ingest(source)
    db_close_guard = None
    del db_close_guard


def test_same_digest_concurrent_publish_yields_one_complete_object(work_dir):
    store = MediaStore(work_dir / "media")
    staged = store.staging / "job-2"
    staged.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\n" + b"twin" * 50
    sources = []
    for index in range(2):
        source = staged / f"{index}.png"
        source.write_bytes(payload)
        sources.append(source)

    import threading

    results: list[tuple[str, str]] = []
    errors: list[Exception] = []

    def worker(source):
        try:
            results.append(store.ingest(source))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(source,)) for source in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    assert len({path for path, _ in results}) == 1
    assert Path(results[0][0]).read_bytes() == payload


def legacy_state(scene_number="1", characters=None, shots=None):
    return ProjectState(
        meta=ProjectMeta(id="legacy-1", title="旧项目"),
        user_input="雾中渡口的故事",
        script=Script(title="EP01", scenes=[ScriptScene(heading=ScriptSceneHeading(scene_number=scene_number, location="渡口"), estimated_duration_seconds=12.0)]),
        characters=characters or [],
        storyboard=Storyboard(shots=shots or []),
    )


def test_legacy_import_failure_at_any_stage_writes_nothing(work_dir):
    db, workbench, _project, _episode, _segment = seed(work_dir)
    service = WorkbenchService(db)
    baseline = db.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    long_name = "x" * 300

    first_stage = legacy_state(scene_number="1" * 60)  # fails building the first SceneCreate
    middle_stage = legacy_state(characters=[Character(name="老周"), Character(name=long_name), Character(name="阿妹")])  # fails mid assets
    last_stage = legacy_state(shots=[LegacyShot(scene_id="sc_a", duration_seconds=2.5), LegacyShot(scene_id="sc_a", duration_seconds=2.5, image_prompt="p" * 60000)])  # fails in shot plans

    for index, state in enumerate((first_stage, middle_stage, last_stage)):
        with pytest.raises(ValidationError):
            service.import_legacy(state.model_dump(mode="json"))
        assert db.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == baseline, f"stage {index} left a project behind"
    db.close()


def test_legacy_import_is_idempotent_by_legacy_id(work_dir):
    db, workbench, _project, _episode, _segment = seed(work_dir)
    service = WorkbenchService(db)
    state = legacy_state(characters=[Character(name="老周", key_props=["船桨"])])
    result = service.import_legacy(state.model_dump(mode="json"))
    project = result["project"]
    assert project["title"] == "旧项目"
    assert {asset["kind"] for asset in project["assets"]} >= {"character", "prop"}
    metadata = project["metadata"]
    assert metadata["legacy_project_id"] == "legacy-1"
    with pytest.raises(ConflictError):
        service.import_legacy(state.model_dump(mode="json"))
    assert db.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2  # seed project + one import
    db.close()


def test_incompatible_schema_never_records_migration_success(work_dir, monkeypatch):
    migrations_dir = work_dir / "migrations"
    migrations_dir.mkdir()
    shutil.copyfile(
        Path("src/script_weaver/infrastructure/migrations/0001_initial.sql"),
        migrations_dir / "0001_initial.sql",
    )
    (migrations_dir / "0002_broken.sql").write_text(
        "CREATE TABLE pending_table(id TEXT PRIMARY KEY);\nINSERT INTO definitely_missing VALUES(1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("script_weaver.infrastructure.sqlite.MIGRATIONS_DIR", migrations_dir)

    db_path = work_dir / "db.sqlite3"
    with pytest.raises(SchemaError):
        Database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        applied = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
        pending = conn.execute("SELECT 1 FROM sqlite_master WHERE name='pending_table'").fetchone()
    finally:
        conn.close()
    assert applied == [1], "the broken migration must not be recorded as applied"
    assert pending is None, "the failed migration must roll back completely"


def test_new_migration_version_is_verified_against_its_own_shape(work_dir, monkeypatch):
    """A future 0002 must not brick startup: shape checks follow the latest applied version."""
    migrations_dir = work_dir / "migrations"
    migrations_dir.mkdir()
    shutil.copyfile(
        Path("src/script_weaver/infrastructure/migrations/0001_initial.sql"),
        migrations_dir / "0001_initial.sql",
    )
    (migrations_dir / "0002_extension.sql").write_text(
        "CREATE TABLE plugin_state(id TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
        "ALTER TABLE projects ADD COLUMN color_tag TEXT;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("script_weaver.infrastructure.sqlite.MIGRATIONS_DIR", migrations_dir)

    db = Database(work_dir / "db.sqlite3")
    try:
        applied = [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations")]
        assert applied == [1, 2]
        columns = {row[1] for row in db.connection.execute("PRAGMA table_info(projects)")}
        assert "color_tag" in columns
        db.connection.execute("INSERT INTO plugin_state VALUES('k','v')")
    finally:
        db.close()

    # Reopening verifies against the latest applied version, not hardcoded v1.
    reopened = Database(work_dir / "db.sqlite3")
    reopened.close()


def test_base_exception_rolls_back_and_releases_the_writer(work_dir):
    db = Database(work_dir / "workbench.sqlite3")
    workbench = WorkbenchService(db)
    project = workbench.create_project(ProjectCreate(title="雾渡"))

    with pytest.raises(KeyboardInterrupt), db.write() as conn:
        conn.execute("INSERT INTO projects(id,title,format,aspect_ratio,prompt_language,metadata_json,revision,created_at,updated_at) VALUES('ghost','','','','','{}',0,'t','t')")
        raise KeyboardInterrupt

    assert db.connection.execute("SELECT COUNT(*) FROM projects WHERE id='ghost'").fetchone()[0] == 0
    # The same thread must be able to open the next write transaction.
    assert workbench.get_project(project["id"])["title"] == "雾渡"
    db.close()


def test_skill_tree_hash_is_order_and_separator_stable(work_dir):
    first = work_dir / "skills-a" / "demo"
    second = work_dir / "skills-b" / "demo"
    for base in (first, second):
        (base / "references").mkdir(parents=True)
        (base / "SKILL.md").write_text("main", encoding="utf-8")
        (base / "references" / "a.md").write_text("alpha", encoding="utf-8")
        (base / "references" / "b.md").write_text("beta", encoding="utf-8")

    assert skill_tree_hash("demo", first.parent) == skill_tree_hash("demo", second.parent)

    (first / "references" / "a.md").write_text("changed", encoding="utf-8")
    assert skill_tree_hash("demo", first.parent) != skill_tree_hash("demo", second.parent)

    with pytest.raises(NotFoundError):
        skill_tree_hash("missing-skill", second.parent)


def test_task_start_recomputes_skill_hashes_and_rejects_unknown_skills(work_dir):
    db, workbench, project, _episode, _segment = seed(work_dir)
    snapshot = workbench.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-storyboard", intent="校验",
        skill_manifest=[{"name": "short-drama-storyboard", "version": "1.0", "hash": "0" * 64}],
    ))
    manifest = snapshot["skill_manifest"]
    assert manifest[0]["hash"] == skill_tree_hash("short-drama-storyboard")
    assert manifest[0]["hash"] != "0" * 64, "client-submitted hashes must never be trusted"

    with pytest.raises(NotFoundError):
        workbench.start_task(TaskStart(
            project_id=project["id"], capability="not-a-real-skill", intent="未知能力",
            skill_manifest=[{"name": "not-a-real-skill", "version": "1"}],
        ))
    db.close()


def test_skills_no_longer_reference_missing_scripts():
    skills = Path(".agents/skills")
    checked = 0
    for markdown in skills.rglob("*.md"):
        text = markdown.read_text(encoding="utf-8")
        assert "../scripts/" not in text, f"{markdown} still links to an external script"
        assert "python3 scripts/" not in text, f"{markdown} still instructs running an external script"
        checked += 1
    assert checked >= 8
