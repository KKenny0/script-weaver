from __future__ import annotations

import os

import pytest

from script_weaver.application.changeset_service import ChangeSetService
from script_weaver.application.workbench_service import WorkbenchService, skill_tree_hash
from script_weaver.domain.models import (
    ConflictError,
    DraftSave,
    DocumentDecision,
    DocumentCreateOperation,
    DocumentCreatePayload,
    DocumentVersionCreateOperation,
    DocumentVersionCreatePayload,
    ProjectIntake,
    ProjectInputCreate,
    ProjectMismatchError,
    ProposalSubmit,
    TaskFail,
    TaskStart,
)
from script_weaver.infrastructure.sqlite import Database


PINNED = [{"name": "short-drama-develop", "version": "3ab6b855"}]


def idea_task(service: WorkbenchService):
    project = service.create_from_intake(
        ProjectIntake(title="未通过的好友申请", intake_kind="idea", content="一个陌生人每天申请好友")
    )
    source = next(item for item in project["documents"] if item["kind"] == "source")
    target = next(item for item in project["documents"] if item["kind"] == "development")
    task = service.start_task(
        TaskStart(
            project_id=project["id"], capability="short-drama-develop", intent="发展完整故事",
            document_id=target["id"], document_version_id=source["current_version_id"],
            skill_manifest=PINNED,
        )
    )
    return project, source, target, task


def proposal(run_id: str, target: dict, content: str = """# 故事开发稿
他最终发现申请来自未来。

```script-weaver-episode-map
{"episode":1,"title":"未来好友","story":"主角发现申请来自未来，并决定回应。"}
```
"""):
    return ProposalSubmit(
        run_id=run_id, summary="发展故事",
        operations=[DocumentVersionCreateOperation(
            op="document.version.create", target_id=target["id"],
            expected_revision=target["revision"],
            payload=DocumentVersionCreatePayload(content=content),
        )],
    )


def test_real_idea_develop_claim_context_proposal_apply_then_creator_accept(tmp_path):
    path = tmp_path / "collaboration.sqlite3"
    db = Database(path)
    workbench, changesets = WorkbenchService(db), ChangeSetService(db)
    project, source, target, task = idea_task(workbench)
    assert task["status"] == "QUEUED"

    run = workbench.claim_task(task["id"], "codex")
    with pytest.raises(ConflictError):
        workbench.claim_task(task["id"], "another-worker")
    context = workbench.get_task_context(task["id"])
    assert context["source"]["version_id"] == source["current_version_id"]
    assert context["source"]["content"].startswith("一个陌生人")
    assert context["target"]["id"] == target["id"]
    assert context["skill_manifest"][0]["hash"] == run["skill_hash"]
    frozen_target = context["target"]
    workbench.save_document_draft(
        target["id"],
        DraftSave(expected_revision=target["draft"]["revision"], content="later local edit"),
    )
    assert workbench.get_task_context(task["id"])["target"] == frozen_target

    change = changesets.submit_proposal(task["id"], proposal(run["id"], target))
    assert change["status"] == "SUBMITTED"
    assert workbench.get_task_snapshot(task["id"])["status"] == "SUBMITTED"
    applied = changesets.apply(change["id"], change["validated_fingerprint"])
    assert applied["status"] == "APPLIED"
    document_event = next(
        event for event in reversed(workbench.list_events(project["id"]))
        if event["event_type"] == "document.version.create"
    )
    assert "content" not in document_event["payload"]
    assert len(document_event["payload"]["content_sha256"]) == 64
    candidate = workbench.get_document(target["id"])
    assert candidate["current_version_id"] is None
    assert candidate["versions"][0]["status"] == "SUBMITTED"
    assert candidate["versions"][0]["created_by"] == "agent"
    assert candidate["versions"][0]["source_changeset_id"] == change["id"]
    assert candidate["versions"][0]["source_document_version_id"] == source["current_version_id"]
    with pytest.raises(ConflictError):
        changesets.submit_proposal(task["id"], proposal(run["id"], target))

    accepted = workbench.decide_document_version(
        target["id"], candidate["versions"][0]["id"],
        DocumentDecision(expected_document_revision=candidate["revision"], action="accept"),
    )
    assert accepted["current_version_id"] == candidate["versions"][0]["id"]
    assert accepted["source_document_version_id"] == source["current_version_id"]
    db.close()

    restarted = Database(path)
    try:
        persisted = WorkbenchService(restarted).get_document(target["id"])
        assert persisted["current_version_id"] == accepted["current_version_id"]
    finally:
        restarted.close()


def test_agent_candidate_and_document_lineage_advance_to_frozen_new_input(tmp_path):
    db = Database(tmp_path / "agent-lineage.sqlite3")
    workbench, changesets = WorkbenchService(db), ChangeSetService(db)
    project, _source, target, _task = idea_task(workbench)
    new_source = workbench.add_project_input(
        project["id"],
        ProjectInputCreate(intake_kind="revision", title="新来源", content="完整新来源"),
    )["document"]
    target = workbench.get_document(target["id"])
    task = workbench.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-develop", intent="按新来源重做",
        document_id=target["id"], document_version_id=new_source["current_version_id"],
        skill_manifest=PINNED,
    ))
    run = workbench.claim_task(task["id"], "codex")
    change = changesets.submit_proposal(task["id"], proposal(run["id"], target))
    changesets.apply(change["id"], change["validated_fingerprint"])
    candidate = workbench.get_document(target["id"])
    version = next(item for item in candidate["versions"] if item["status"] == "SUBMITTED")
    assert version["source_document_version_id"] == new_source["current_version_id"]
    accepted = workbench.decide_document_version(
        target["id"], version["id"],
        DocumentDecision(expected_document_revision=candidate["revision"], action="accept"),
    )
    assert accepted["source_document_version_id"] == new_source["current_version_id"]
    db.close()

def test_fail_cross_task_cross_project_and_revision_boundaries(tmp_path):
    db = Database(tmp_path / "boundaries.sqlite3")
    workbench, changesets = WorkbenchService(db), ChangeSetService(db)
    first, _, first_target, first_task = idea_task(workbench)
    second, _, second_target, second_task = idea_task(workbench)
    first_run = workbench.claim_task(first_task["id"], "codex-1")
    second_run = workbench.claim_task(second_task["id"], "codex-2")

    with pytest.raises(ProjectMismatchError):
        changesets.submit_proposal(first_task["id"], proposal(second_run["id"], first_target))
    with pytest.raises(ProjectMismatchError):
        changesets.submit_proposal(first_task["id"], proposal(first_run["id"], second_target))
    with pytest.raises(ProjectMismatchError):
        workbench.fail_task(first_task["id"], TaskFail(run_id=second_run["id"], message="wrong"))

    failed = workbench.fail_task(
        first_task["id"], TaskFail(run_id=first_run["id"], message="Codex unavailable")
    )
    assert failed["status"] == "FAILED"
    assert workbench.get_project(first["id"])["documents"]  # manual workbench remains available

    # The second task froze the project revision; archiving changes it before submission.
    workbench.set_project_archived(second["id"], second["revision"], True)
    stale = changesets.submit_proposal(
        second_task["id"], proposal(second_run["id"], second_target)
    )
    with pytest.raises(ConflictError):
        changesets.apply(stale["id"], stale["validated_fingerprint"])
    db.close()


def test_document_create_apply_creates_candidate_not_current(tmp_path):
    db = Database(tmp_path / "create-document.sqlite3")
    workbench, changesets = WorkbenchService(db), ChangeSetService(db)
    project, source, _target, _ = idea_task(workbench)
    task = workbench.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-write", intent="写第一集",
        document_version_id=source["current_version_id"],
        skill_manifest=[{"name": "short-drama-write", "version": "3ab6b855"}],
    ))
    run = workbench.claim_task(task["id"], "codex")
    change = changesets.submit_proposal(task["id"], ProposalSubmit(
        run_id=run["id"], summary="创建第一集剧本",
        operations=[DocumentCreateOperation(
            op="document.create",
            payload=DocumentCreatePayload(
                kind="screenplay", title="EP01 剧本", content="# EP001\n## EP001-SC001 内 · 家 · 夜"
            ),
        )],
    ))
    changesets.apply(change["id"], change["validated_fingerprint"])
    screenplay = next(item for item in workbench.get_project(project["id"])["documents"] if item["kind"] == "screenplay")
    assert screenplay["current_version_id"] is None
    assert screenplay["versions"][0]["status"] == "SUBMITTED"
    db.close()


def test_document_version_scope_rejects_same_project_other_and_source(tmp_path):
    db = Database(tmp_path / "document-scope.sqlite3")
    workbench, changesets = WorkbenchService(db), ChangeSetService(db)
    project, source, target, task = idea_task(workbench)
    other = workbench.add_project_input(
        project["id"],
        ProjectInputCreate(intake_kind="supplement", title="other", content="source"),
    )["document"]
    run = workbench.claim_task(task["id"], "codex")
    for document in (other, source):
        with pytest.raises((ProjectMismatchError, ValueError)):
            changesets.submit_proposal(
                task["id"],
                ProposalSubmit(
                    run_id=run["id"], summary="escape",
                    operations=[DocumentVersionCreateOperation(
                        op="document.version.create", target_id=document["id"],
                        expected_revision=document["revision"],
                        payload=DocumentVersionCreatePayload(content="forbidden"),
                    )],
                ),
            )
    source_task = workbench.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-develop", intent="bad source edit",
        document_id=source["id"], document_version_id=source["current_version_id"],
        skill_manifest=PINNED,
    ))
    source_run = workbench.claim_task(source_task["id"], "codex")
    with pytest.raises(ValueError, match="source"):
        changesets.submit_proposal(
            source_task["id"],
            ProposalSubmit(
                run_id=source_run["id"], summary="source edit",
                operations=[DocumentVersionCreateOperation(
                    op="document.version.create", target_id=source["id"],
                    expected_revision=source["revision"],
                    payload=DocumentVersionCreatePayload(content="forbidden"),
                )],
            ),
        )
    db.close()


def test_failed_run_and_succeeded_run_cannot_replay(tmp_path):
    db = Database(tmp_path / "replay.sqlite3")
    workbench, changesets = WorkbenchService(db), ChangeSetService(db)
    _project, _source, target, task = idea_task(workbench)
    run = workbench.claim_task(task["id"], "codex")
    workbench.fail_task(task["id"], TaskFail(run_id=run["id"], message="failed"))
    with pytest.raises(ConflictError):
        changesets.submit_proposal(task["id"], proposal(run["id"], target))

    _project, _source, target, task = idea_task(workbench)
    run = workbench.claim_task(task["id"], "codex")
    changesets.submit_proposal(task["id"], proposal(run["id"], target))
    with pytest.raises(ConflictError):
        changesets.submit_proposal(task["id"], proposal(run["id"], target))
    db.close()


def test_skill_hash_rejects_escape_symlinks_and_frames_paths(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    with pytest.raises(ValueError):
        skill_tree_hash("../escape", root)

    skill = root / "linked"
    skill.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, skill / "file")
    with pytest.raises(ValueError):
        skill_tree_hash("linked", root)

    first = root / "first"
    second = root / "second"
    first.mkdir()
    second.mkdir()
    (first / "a").write_text("bc")
    (second / "ab").write_text("c")
    assert skill_tree_hash("first", root) != skill_tree_hash("second", root)


def test_proposal_rechecks_pinned_skill_hash(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skill = skills / "short-drama-develop"
    skill.mkdir(parents=True)
    marker = skill / "SKILL.md"
    marker.write_text("v1")
    monkeypatch.setenv("SCRIPT_WEAVER_SKILLS_DIR", str(skills))
    db = Database(tmp_path / "skill-change.sqlite3")
    workbench, changesets = WorkbenchService(db), ChangeSetService(db)
    _project, _source, target, task = idea_task(workbench)
    run = workbench.claim_task(task["id"], "codex")
    marker.write_text("v2")
    with pytest.raises(ConflictError, match="skill tree changed"):
        changesets.submit_proposal(task["id"], proposal(run["id"], target))
    db.close()
