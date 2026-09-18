from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.domain.models import (
    ConflictError,
    DocumentDecision,
    DocumentRestore,
    DocumentSubmit,
    DraftSave,
    ProjectInputCreate,
    ProjectIntake,
    ProjectMismatchError,
)
from script_weaver.infrastructure.sqlite import Database


def test_v1_database_gets_additive_document_migration(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        (Path(__file__).parents[2] / "src/script_weaver/infrastructure/migrations/0001_initial.sql").read_text()
    )
    connection.close()
    db = Database(path)
    try:
        assert [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations")] == [1, 2, 3, 4, 5, 6]
        assert db.connection.execute("SELECT 1 FROM sqlite_master WHERE name='creative_documents'").fetchone()
        assert "archived_at" in {row[1] for row in db.connection.execute("PRAGMA table_info(projects)")}
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["idea", "novel", "single_script", "multi_script"])
def test_all_intakes_persist_source_outside_project_metadata(tmp_path, kind):
    db_path = tmp_path / f"{kind}.sqlite3"
    db = Database(db_path)
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title=kind, intake_kind=kind, content=f"{kind} 原文")
    )
    assert "原文" not in str(project["metadata"])
    source = next(document for document in project["documents"] if document["kind"] == "source")
    working = next(document for document in project["documents"] if document["kind"] != "source")
    assert source["versions"][0]["content"] == f"{kind} 原文"
    assert working["draft"]["content"] == f"{kind} 原文"
    assert working["kind"] == ("screenplay" if kind == "single_script" else "development")
    assert len(project["episodes"]) == (1 if kind == "single_script" else 0)
    listed = service.list_projects()[0]
    assert listed["document_count"] == 2
    assert listed["episode_count"] == (1 if kind == "single_script" else 0)
    db.close()

    restarted = Database(db_path)
    try:
        assert WorkbenchService(restarted).get_project(project["id"])["documents"]
    finally:
        restarted.close()


def test_season_continuation_requires_existing_source_and_persists_relationship(tmp_path):
    db_path = tmp_path / "season.sqlite3"
    db = Database(db_path)
    service = WorkbenchService(db)
    first = service.create_from_intake(
        ProjectIntake(title="雾渡", intake_kind="idea", content="第一季想法")
    )
    second = service.create_from_intake(ProjectIntake(
        title="雾渡 · 第二季", intake_kind="idea", content="第二季的新冲突",
        source_project_id=first["id"], continuation_kind="season",
    ))
    assert second["metadata"] == {
        "source_project_id": first["id"], "continuation_kind": "season",
    }
    with pytest.raises(Exception, match="source project"):
        service.create_from_intake(ProjectIntake(
            title="孤立续季", intake_kind="idea", content="无法关联",
            source_project_id="missing-project", continuation_kind="season",
        ))
    db.close()
    restarted = Database(db_path)
    try:
        assert WorkbenchService(restarted).get_project(second["id"])["metadata"][
            "source_project_id"
        ] == first["id"]
    finally:
        restarted.close()


def test_draft_conflict_submit_immutable_decisions_and_restore(tmp_path):
    db = Database(tmp_path / "documents.sqlite3")
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title="故事", intake_kind="idea", content="第一稿")
    )
    document = next(item for item in project["documents"] if item["kind"] == "development")
    service.save_document_draft(
        document["id"], DraftSave(expected_revision=0, content="第二稿")
    )
    with pytest.raises(ConflictError):
        service.save_document_draft(
            document["id"], DraftSave(expected_revision=0, content="覆盖别人的草稿")
        )
    submitted = service.submit_document(
        document["id"],
        DocumentSubmit(expected_document_revision=0, expected_draft_revision=1),
    )
    version = submitted["versions"][0]
    with pytest.raises(sqlite3.IntegrityError), db.write() as conn:
        conn.execute(
            "UPDATE creative_document_versions SET content='mutated' WHERE id=?", (version["id"],)
        )
    with pytest.raises(ValidationError):
        DocumentDecision(expected_document_revision=1, action="reject")
    rejected = service.decide_document_version(
        document["id"], version["id"],
        DocumentDecision(expected_document_revision=1, action="reject", feedback="重写结尾"),
    )
    assert rejected["versions"][0]["decision_feedback"] == "重写结尾"
    restored = service.restore_document_draft(
        document["id"],
        DocumentRestore(expected_draft_revision=1, source_version_id=version["id"]),
    )
    assert restored["draft"]["revision"] == 2
    assert restored["draft"]["content"] == "第二稿"
    db.close()


def test_accept_and_direct_single_script_are_honest_about_projection(tmp_path):
    path = tmp_path / "screenplay.sqlite3"
    db = Database(path)
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(
            title="单集", intake_kind="single_script",
            content="# EP001 单集\n## EP001-SC001 内 · 家 · 夜\n演员进入",
        )
    )
    document = next(item for item in project["documents"] if item["kind"] == "screenplay")
    adopted = service.adopt_single_script(
        document["id"],
        DocumentSubmit(expected_document_revision=0, expected_draft_revision=0),
    )
    assert adopted["versions"][0]["status"] == "ACCEPTED"
    assert adopted["versions"][0]["projection_status"] == "projected"
    assert adopted["drives_downstream"] is True
    db.close()
    restarted = Database(path)
    try:
        persisted = WorkbenchService(restarted).get_document(document["id"])
        assert persisted["current_version_id"] == adopted["current_version_id"]
        assert persisted["versions"][0]["status"] == "ACCEPTED"
    finally:
        restarted.close()


def test_archive_restore_new_input_and_cross_project_version_boundary(tmp_path):
    db = Database(tmp_path / "projects.sqlite3")
    service = WorkbenchService(db)
    first = service.create_from_intake(ProjectIntake(title="一", intake_kind="idea", content="A"))
    second = service.create_from_intake(ProjectIntake(title="二", intake_kind="idea", content="B"))
    added = service.add_project_input(
        first["id"], ProjectInputCreate(intake_kind="supplement", title="补充", content="C")
    )
    assert added["document"]["versions"][0]["content"] == "C"
    archived = service.set_project_archived(first["id"], added["project_revision"], True)
    assert archived["archived_at"]
    assert {item["id"] for item in service.list_projects()} == {second["id"]}
    assert {item["id"] for item in service.list_projects(archived=True)} == {first["id"]}
    restored = service.set_project_archived(first["id"], archived["revision"], False)
    assert restored["archived_at"] is None

    first_doc = next(item for item in restored["documents"] if item["kind"] == "development")
    second_version = next(
        item for item in service.get_project(second["id"])["documents"] if item["kind"] == "source"
    )["versions"][0]
    with pytest.raises(ProjectMismatchError):
        service.restore_document_draft(
            first_doc["id"],
            DocumentRestore(expected_draft_revision=0, source_version_id=second_version["id"]),
        )
    db.close()
