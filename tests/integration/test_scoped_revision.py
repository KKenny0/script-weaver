import pytest

from script_weaver.application.changeset_service import ChangeSetService
from script_weaver.domain.models import (
    ConflictError, DocumentDecision, DocumentVersionCreateOperation,
    DocumentVersionCreatePayload, DraftSave, ProposalSubmit, TaskStart,
)
from tests.integration.test_phase3_projections import create_developed_project, screenplay


def setup_revision(tmp_path):
    db, service, project, development = create_developed_project(tmp_path, ["来信", "守约"])
    document = next(d for d in project["documents"] if d["kind"] == "screenplay")
    original = screenplay(1, "来信").replace("沈乔推门进来。", "沈乔握着信😀推门进来。")
    document = service.save_document_draft(document["id"], DraftSave(expected_revision=0, content=original))
    start = original.index("你还在等我？")
    task = service.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-write", intent="对白更克制",
        document_id=document["id"], document_version_id=development["current_version_id"],
        edit_scope={"draft_revision": document["draft"]["revision"], "start": start, "end": start + len("你还在等我？")},
    ))
    run = service.claim_task(task["id"], "test-worker")
    return db, service, ChangeSetService(db), document, task, run, original


def proposal(document, run, content):
    return ProposalSubmit(run_id=run["id"], summary="对白修订", operations=[DocumentVersionCreateOperation(
        op="document.version.create", target_id=document["id"], expected_revision=document["revision"],
        payload=DocumentVersionCreatePayload(content=content),
    )])


def test_scoped_request_rejects_invalid_range_revision_and_target(tmp_path):
    db, service, _changes, document, task, _run, original = setup_revision(tmp_path)
    try:
        base = dict(project_id=document["project_id"], capability="short-drama-write", intent="重试",
                    document_id=document["id"], document_version_id=task["selection"]["document_version_id"])
        for start, end in [(3, 3), (4, 2), (0, len(original) + 1)]:
            with pytest.raises(ValueError, match="范围"):
                service.start_task(TaskStart(**base, edit_scope={"draft_revision": 1, "start": start, "end": end}))
        with pytest.raises(ConflictError, match="草稿"):
            service.start_task(TaskStart(**base, edit_scope={"draft_revision": 0, "start": 0, "end": 1}))
        with pytest.raises(ValueError):
            TaskStart(**{**base, "document_id": None}, edit_scope={"draft_revision": 1, "start": 0, "end": 1})
        assert len(service.list_tasks(document["project_id"])) == 1
    finally:
        db.close()


def test_scoped_revision_preserves_outside_text_and_accepts(tmp_path):
    db, service, changes, document, task, run, original = setup_revision(tmp_path)
    try:
        context = service.get_task_context(task["id"])
        assert context["target"]["draft_content"] == original
        assert context["selection"]["edit_scope"]["start"] == original.index("你还在等我？")
        with pytest.raises(ValueError, match="选区以外"):
            changes.submit_proposal(task["id"], proposal(document, run, original.replace("夜", "晨")))
        with pytest.raises(ValueError, match="选区以外"):
            changes.submit_proposal(task["id"], proposal(document, run, "全部重写"))
        double = proposal(document, run, original)
        double.operations.append(double.operations[0])
        with pytest.raises(ValueError, match="一个候选"):
            changes.submit_proposal(task["id"], double)
        assert service.get_document(document["id"])["versions"] == []
        content = original.replace("你还在等我？", "还没走。")
        change = changes.submit_proposal(task["id"], proposal(document, run, content))
        changes.apply(change["id"], change["validated_fingerprint"])
        candidate = service.get_document(document["id"])
        assert candidate["draft"]["content"] == original
        assert candidate["current_version_id"] is None
        accepted = service.decide_document_version(document["id"], candidate["versions"][0]["id"], DocumentDecision(expected_document_revision=candidate["revision"], action="accept"))
        assert accepted["current_version_id"] == candidate["versions"][0]["id"]
        assert accepted["draft"]["content"] == original
    finally:
        db.close()


@pytest.mark.parametrize("stage", ["submit", "apply", "accept"])
def test_scoped_revision_rejects_concurrent_draft_edit_at_each_gate(tmp_path, stage):
    db, service, changes, document, task, run, original = setup_revision(tmp_path)
    try:
        change = None
        if stage != "submit":
            change = changes.submit_proposal(task["id"], proposal(document, run, original.replace("你还在等我？", "还没走。")))
        if stage == "accept":
            changes.apply(change["id"], change["validated_fingerprint"])
        service.save_document_draft(document["id"], DraftSave(expected_revision=document["draft"]["revision"], content=original + "新的结尾。"))
        with pytest.raises(ConflictError, match="草稿"):
            if stage == "submit":
                changes.submit_proposal(task["id"], proposal(document, run, original))
            elif stage == "apply":
                changes.apply(change["id"], change["validated_fingerprint"])
            else:
                current = service.get_document(document["id"])
                service.decide_document_version(document["id"], current["versions"][0]["id"], DocumentDecision(expected_document_revision=current["revision"], action="accept"))
        current = service.get_document(document["id"])
        assert current["current_version_id"] is None
        assert current["draft"]["content"].endswith("新的结尾。")
        assert service.get_task_context(task["id"])["target"]["draft_content"] == original
        if stage == "accept":
            rejected = service.decide_document_version(document["id"], current["versions"][0]["id"], DocumentDecision(expected_document_revision=current["revision"], action="reject", feedback="按新稿重试"))
            assert rejected["versions"][0]["status"] == "REJECTED"
    finally:
        db.close()
