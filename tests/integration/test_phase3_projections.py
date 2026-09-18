from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from script_weaver.application.changeset_service import ChangeSetService
from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.application.creative_projection import (
    DevelopmentMapError,
    parse_episode_map,
    parse_screenplay,
    verify_episode_spans,
)
from script_weaver.domain.models import (
    AssetCreate,
    BindingCreate,
    ConflictError,
    DocumentDecision,
    DocumentRestore,
    DocumentCreateOperation,
    DocumentCreatePayload,
    DocumentSubmit,
    DraftSave,
    ProjectionRetry,
    ProjectIntake,
    ProjectInputCreate,
    ProjectMismatchError,
    ProposalSubmit,
    SceneCreate,
    SegmentCreate,
    ShotCreate,
    SurfaceContextUpsert,
    TaskStart,
)
from script_weaver.infrastructure.sqlite import Database, SchemaError
from script_weaver.daemon.server import create_app


def episode_map(
    titles: list[str], spans: list[dict] | None = None, stories: list[str] | None = None
) -> str:
    lines = []
    for index, title in enumerate(titles, 1):
        item = {"episode": index, "title": title, "story": (stories or titles)[index - 1]}
        if spans:
            item["source_span"] = spans[index - 1]
        lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    return "# 开发稿\n\n```script-weaver-episode-map\n" + "\n".join(lines) + "\n```\n"


def submit_and_decide(
    service: WorkbenchService, document: dict, content: str, action: str = "accept", feedback: str | None = None
) -> dict:
    saved = service.save_document_draft(
        document["id"], DraftSave(expected_revision=document["draft"]["revision"], content=content)
    )
    submitted = service.submit_document(
        document["id"],
        DocumentSubmit(
            expected_document_revision=saved["revision"],
            expected_draft_revision=saved["draft"]["revision"],
        ),
    )
    candidate = next(version for version in submitted["versions"] if version["status"] == "SUBMITTED")
    return service.decide_document_version(
        document["id"], candidate["id"],
        DocumentDecision(
            expected_document_revision=submitted["revision"], action=action, feedback=feedback
        ),
    )


def screenplay(number: int, title: str, location: str = "家") -> str:
    return (
        f"# EP{number:03d} {title}\n"
        f"## EP{number:03d}-SC001 内 · {location} · 夜\n"
        "沈乔推门进来。\n沈乔：你还在等我？\n"
    )


def create_developed_project(tmp_path, titles: list[str], stories: list[str] | None = None):
    db = Database(tmp_path / "project.sqlite3")
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title="系列", intake_kind="idea", content="一个跨集故事")
    )
    development = next(doc for doc in project["documents"] if doc["kind"] == "development")
    accepted = submit_and_decide(service, development, episode_map(titles, stories=stories))
    return db, service, service.get_project(project["id"]), accepted


def _downgrade_to_unreleased_v4(path, *, extra_missing=False):
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER creative_document_versions_lineage_immutable")
    connection.execute("ALTER TABLE creative_document_versions DROP COLUMN source_document_version_id")
    connection.execute("ALTER TABLE creative_document_versions DROP COLUMN derived_from_ids_json")
    connection.execute("ALTER TABLE episodes DROP COLUMN status")
    if extra_missing:
        connection.execute("ALTER TABLE episodes DROP COLUMN stale_reason")
    connection.commit()
    connection.close()


def test_unreleased_v4_repair_requires_exact_known_shape(tmp_path):
    repairable = tmp_path / "repairable.sqlite3"
    initial = Database(repairable)
    service = WorkbenchService(initial)
    project = service.create_from_intake(
        ProjectIntake(title="迁移语义", intake_kind="idea", content="来源")
    )
    development = next(item for item in project["documents"] if item["kind"] == "development")
    first = submit_and_decide(service, development, episode_map(["一"]))
    second = submit_and_decide(service, service.get_document(development["id"]), episode_map(["一"], stories=["剧情更新"]))
    episode_id = service.get_project(project["id"])["episodes"][0]["id"]
    aggregate = service.get_document(development["id"])
    expected_source = aggregate["source_document_version_id"]
    expected_derived = aggregate["derived_from_ids"]
    with initial.write() as connection:
        connection.execute(
            "UPDATE episodes SET is_stale=1,stale_reason='removed from accepted development map' WHERE id=?",
            (episode_id,),
        )
    initial.close()
    _downgrade_to_unreleased_v4(repairable)
    repaired = Database(repairable)
    assert {row[1] for row in repaired.connection.execute("PRAGMA table_info(creative_document_versions)")} >= {
        "source_document_version_id", "derived_from_ids_json"
    }
    assert "status" in {row[1] for row in repaired.connection.execute("PRAGMA table_info(episodes)")}
    assert repaired.connection.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()[0] == "removed"
    current = repaired.connection.execute(
        "SELECT source_document_version_id,derived_from_ids_json FROM creative_document_versions WHERE id=?",
        (second["current_version_id"],),
    ).fetchone()
    assert current[0] == expected_source
    assert json.loads(current[1]) == expected_derived
    historical = repaired.connection.execute(
        "SELECT source_document_version_id,derived_from_ids_json FROM creative_document_versions WHERE id=?",
        (first["current_version_id"],),
    ).fetchone()
    assert historical[0] is None
    assert json.loads(historical[1]) == []
    repaired.close()

    malformed = tmp_path / "malformed.sqlite3"
    Database(malformed).close()
    _downgrade_to_unreleased_v4(malformed, extra_missing=True)
    with pytest.raises(SchemaError):
        Database(malformed)
    unchanged = sqlite3.connect(malformed)
    assert "source_document_version_id" not in {row[1] for row in unchanged.execute("PRAGMA table_info(creative_document_versions)")}
    assert "status" not in {row[1] for row in unchanged.execute("PRAGMA table_info(episodes)")}
    unchanged.close()


def test_development_accept_is_atomic_and_creates_episode_skeletons(tmp_path):
    path = tmp_path / "development.sqlite3"
    db = Database(path)
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title="系列", intake_kind="idea", content="想法")
    )
    development = next(doc for doc in project["documents"] if doc["kind"] == "development")
    service.save_document_draft(
        development["id"], DraftSave(expected_revision=0, content="# invalid")
    )
    submitted = service.submit_document(
        development["id"], DocumentSubmit(expected_document_revision=0, expected_draft_revision=1)
    )
    candidate = submitted["versions"][0]
    with pytest.raises(DevelopmentMapError, match="缺少分集地图") as error:
        service.decide_document_version(
            development["id"], candidate["id"],
            DocumentDecision(expected_document_revision=1, action="accept"),
        )
    assert error.value.code == "development_episode_map_missing"
    assert service.get_project(project["id"])["episodes"] == []
    assert service.get_document(development["id"])["current_version_id"] is None
    assert service.get_document(development["id"])["versions"][0]["status"] == "SUBMITTED"
    db.close()

    restarted = Database(path)
    assert WorkbenchService(restarted).get_project(project["id"])["episodes"] == []
    restarted.close()


@pytest.mark.parametrize(
    ("content", "code", "message"),
    [
        ("# 发展稿", "development_episode_map_missing", "缺少分集地图"),
        (
            episode_map(["一"]) + episode_map(["二"]),
            "development_episode_map_multiple",
            "多个分集地图",
        ),
        (
            "```script-weaver-episode-map\n{not json}\n```",
            "development_episode_map_jsonl_invalid",
            "不是有效 JSON",
        ),
        (
            "```script-weaver-episode-map\n"
            '{"episode":1,"title":"一","story":"一"}\n'
            '{"episode":3,"title":"三","story":"三"}\n```',
            "development_episode_map_noncontinuous",
            "连续且不重复",
        ),
    ],
)
def test_development_map_errors_have_stable_distinct_chinese_contract(content, code, message):
    with pytest.raises(DevelopmentMapError) as error:
        parse_episode_map(content)
    assert error.value.code == code
    assert message in str(error.value)


def test_development_map_api_returns_stable_public_code_without_raw_parser_error(tmp_path):
    path = tmp_path / "api-development.sqlite3"
    seeded = Database(path)
    service = WorkbenchService(seeded)
    project = service.create_from_intake(
        ProjectIntake(title="API 错误", intake_kind="idea", content="想法")
    )
    development = next(item for item in project["documents"] if item["kind"] == "development")
    saved = service.save_document_draft(
        development["id"], DraftSave(expected_revision=0, content="# 尚无地图")
    )
    submitted = service.submit_document(
        development["id"],
        DocumentSubmit(
            expected_document_revision=saved["revision"],
            expected_draft_revision=saved["draft"]["revision"],
        ),
    )
    candidate = next(item for item in submitted["versions"] if item["status"] == "SUBMITTED")
    seeded.close()

    app = create_app(
        database_path=path,
        media_root=tmp_path / "media",
        token="creator-token",
        agent_token="agent-token",
    )
    response = TestClient(app).post(
        f"/api/documents/{development['id']}/versions/{candidate['id']}/decision",
        headers={"Authorization": "Bearer creator-token"},
        json={"expected_document_revision": submitted["revision"], "action": "accept"},
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "开发稿缺少分集地图。请插入一个 script-weaver-episode-map 代码块。",
        "code": "development_episode_map_missing",
    }
    assert WorkbenchService(app.state.db).get_project(project["id"])["episodes"] == []
    app.state.db.close()


def test_fresh_creator_can_repair_invalid_development_and_accept_new_version(tmp_path):
    db = Database(tmp_path / "fresh-repair.sqlite3")
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title="第一次创作", intake_kind="idea", content="一个陌生人归还旧钥匙的故事")
    )
    development = next(item for item in project["documents"] if item["kind"] == "development")
    saved = service.save_document_draft(
        development["id"], DraftSave(expected_revision=0, content="# 发展稿\n\n先写故事，再补分集。")
    )
    submitted = service.submit_document(
        development["id"],
        DocumentSubmit(
            expected_document_revision=saved["revision"],
            expected_draft_revision=saved["draft"]["revision"],
        ),
    )
    candidate = next(item for item in submitted["versions"] if item["status"] == "SUBMITTED")
    with pytest.raises(DevelopmentMapError):
        service.decide_document_version(
            development["id"], candidate["id"],
            DocumentDecision(expected_document_revision=submitted["revision"], action="accept"),
        )
    assert service.get_project(project["id"])["episodes"] == []

    rejected = service.decide_document_version(
        development["id"], candidate["id"],
        DocumentDecision(
            expected_document_revision=submitted["revision"],
            action="reject",
            feedback="缺少合法分集地图",
        ),
    )
    restored = service.restore_document_draft(
        development["id"],
        DocumentRestore(
            expected_draft_revision=rejected["draft"]["revision"],
            source_version_id=candidate["id"],
        ),
    )
    repaired = service.save_document_draft(
        development["id"],
        DraftSave(
            expected_revision=restored["draft"]["revision"],
            content=episode_map(["归还钥匙"]),
        ),
    )
    resubmitted = service.submit_document(
        development["id"],
        DocumentSubmit(
            expected_document_revision=repaired["revision"],
            expected_draft_revision=repaired["draft"]["revision"],
        ),
    )
    new_candidate = next(item for item in resubmitted["versions"] if item["status"] == "SUBMITTED")
    accepted = service.decide_document_version(
        development["id"], new_candidate["id"],
        DocumentDecision(expected_document_revision=resubmitted["revision"], action="accept"),
    )
    assert accepted["current_version_id"] == new_candidate["id"]
    assert [item["title"] for item in service.get_project(project["id"])["episodes"]] == ["归还钥匙"]
    db.close()


def test_multi_script_spans_are_verified_before_episode_documents_exist(tmp_path):
    source = screenplay(1, "一") + screenplay(2, "二")
    encoded = source.encode()
    boundary = len(screenplay(1, "一").encode())
    chunks = [encoded[:boundary], encoded[boundary:]]
    spans = []
    start = 0
    for chunk in chunks:
        spans.append({"start": start, "end": start + len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
        start += len(chunk)

    db = Database(tmp_path / "multi.sqlite3")
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title="整稿", intake_kind="multi_script", content=source)
    )
    assert project["episodes"] == []
    development = next(doc for doc in project["documents"] if doc["kind"] == "development")
    accepted = submit_and_decide(service, development, episode_map(["一", "二"], spans))
    refreshed = service.get_project(project["id"])
    assert accepted["current_version_id"]
    assert [episode["episode_number"] for episode in refreshed["episodes"]] == [1, 2]
    screenplays = [doc for doc in refreshed["documents"] if doc["kind"] == "screenplay"]
    assert [doc["draft"]["content"] for doc in screenplays] == [chunk.decode() for chunk in chunks]
    rows = list(db.connection.execute("SELECT byte_start,byte_end,sha256 FROM source_episode_spans ORDER BY episode_number"))
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (span["start"], span["end"], span["sha256"]) for span in spans
    ]
    db.close()

    restarted = Database(tmp_path / "multi.sqlite3")
    persisted = WorkbenchService(restarted).get_project(project["id"])
    assert [item["draft"]["content"] for item in persisted["documents"] if item["kind"] == "screenplay"] == [
        chunk.decode() for chunk in chunks
    ]
    restarted.close()


def test_bad_multi_script_hash_rolls_back_decision_and_all_episodes(tmp_path):
    source = screenplay(1, "一")
    span = {"start": 0, "end": len(source.encode()), "sha256": "0" * 64}
    db = Database(tmp_path / "bad-span.sqlite3")
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title="整稿", intake_kind="multi_script", content=source)
    )
    development = next(doc for doc in project["documents"] if doc["kind"] == "development")
    with pytest.raises(ValueError, match="hash mismatch"):
        submit_and_decide(service, development, episode_map(["一"], [span]))
    assert service.get_project(project["id"])["episodes"] == []
    assert db.connection.execute("SELECT COUNT(*) FROM source_episode_spans").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize(
    "entries, source, message",
    [
        ([{"episode": 1, "title": "一", "story": "一", "source_span": {"start": 0, "end": 4, "sha256": hashlib.sha256(b"abcd").hexdigest()}}, {"episode": 2, "title": "二", "story": "二", "source_span": {"start": 3, "end": 6, "sha256": hashlib.sha256(b"def").hexdigest()}}], "abcdef", "non-overlapping"),
        ([{"episode": 1, "title": "一", "story": "一", "source_span": {"start": 0, "end": 7, "sha256": "0" * 64}}], "abcdef", "outside"),
    ],
)
def test_multi_script_span_contract_rejects_overlap_and_bounds(entries, source, message):
    with pytest.raises(ValueError, match=message):
        verify_episode_spans(entries, source)


def test_projection_parsers_reject_numbering_duplicates_and_pre_scene_prose():
    duplicate = "```script-weaver-episode-map\n{\"episode\":1,\"title\":\"一\",\"story\":\"一\"}\n{\"episode\":1,\"title\":\"又一\",\"story\":\"又一\"}\n```"
    with pytest.raises(DevelopmentMapError, match="连续且不重复"):
        parse_episode_map(duplicate)
    with pytest.raises(ValueError, match="before the first scene"):
        parse_screenplay("# EP001 一\n这段不能被静默丢弃\n## EP001-SC001 内 · 家 · 夜\n动作。", 1)


def test_agent_document_create_cannot_escape_frozen_episode(tmp_path):
    db, service, project, development = create_developed_project(tmp_path, ["一", "二"])
    documents = {doc["episode_id"]: doc for doc in project["documents"] if doc["kind"] == "screenplay"}
    first, second = project["episodes"]
    task = service.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-write", intent="审查第一集",
        document_id=documents[first["id"]]["id"], document_version_id=development["current_version_id"],
        skill_manifest=[{"name": "short-drama-write", "version": "3ab6b855"}],
    ))
    run = service.claim_task(task["id"], "codex")
    with pytest.raises(ProjectMismatchError, match="task snapshot"):
        ChangeSetService(db).submit_proposal(task["id"], ProposalSubmit(
            run_id=run["id"], summary="越界审查",
            operations=[DocumentCreateOperation(
                op="document.create",
                payload=DocumentCreatePayload(
                    kind="review", title="EP02 审查", content="第二集审查",
                    episode_id=second["id"],
                ),
            )],
        ))
    db.close()


def test_screenplay_projection_rolls_back_invalid_and_preserves_history(tmp_path):
    db, service, project, _ = create_developed_project(tmp_path, ["第一集"])
    episode = project["episodes"][0]
    document = next(doc for doc in project["documents"] if doc["episode_id"] == episode["id"])
    first = submit_and_decide(service, document, screenplay(1, "第一集", "旧屋"))
    assert first["drives_downstream"] is True
    old_segment = service.get_episode(episode["id"])["segments"][0]

    saved = service.save_document_draft(
        document["id"], DraftSave(expected_revision=1, content="# EP001 坏稿\n没有场景")
    )
    submitted = service.submit_document(
        document["id"],
        DocumentSubmit(expected_document_revision=first["revision"], expected_draft_revision=saved["draft"]["revision"]),
    )
    invalid = next(version for version in submitted["versions"] if version["status"] == "SUBMITTED")
    with pytest.raises(ValueError, match="at least one scene"):
        service.decide_document_version(
            document["id"], invalid["id"],
            DocumentDecision(expected_document_revision=submitted["revision"], action="accept"),
        )
    after_failure = service.get_document(document["id"])
    assert after_failure["current_version_id"] == first["current_version_id"]
    assert service.get_episode(episode["id"])["segments"][0]["id"] == old_segment["id"]
    after_reject = service.decide_document_version(
        document["id"], invalid["id"],
        DocumentDecision(
            expected_document_revision=after_failure["revision"], action="reject",
            feedback="格式无法投影",
        ),
    )
    second = submit_and_decide(service, after_reject, screenplay(1, "第一集", "新屋"))
    assert second["versions"][0]["projection_revision"] == 2
    history = service.get_episode(episode["id"])["projection_history"]
    assert {(item["source_projection_revision"], item["status"]) for item in history} == {(1, "retired"), (2, "active")}
    db.close()


def test_creator_can_retry_legacy_accepted_screenplay_projection(tmp_path):
    db, service, project, _ = create_developed_project(tmp_path, ["第一集"])
    episode = project["episodes"][0]
    document = next(doc for doc in project["documents"] if doc.get("episode_id") == episode["id"])
    accepted = submit_and_decide(service, document, screenplay(1, "第一集"))
    with db.write() as conn:
        conn.execute("UPDATE creative_document_versions SET projection_revision=NULL WHERE id=?", (accepted["current_version_id"],))
        conn.execute("UPDATE script_scenes SET status='retired' WHERE episode_id=?", (episode["id"],))
        conn.execute("UPDATE segments SET status='retired' WHERE episode_id=?", (episode["id"],))
    legacy = service.get_document(document["id"])
    assert legacy["drives_downstream"] is False
    projected = service.retry_screenplay_projection(
        document["id"], ProjectionRetry(expected_document_revision=legacy["revision"])
    )
    assert projected["drives_downstream"] is True
    assert service.get_episode(episode["id"])["segments"][0]["source_projection_revision"] == 2
    db.close()


def test_new_projection_stales_only_old_episode_downstream(tmp_path):
    db, service, project, _ = create_developed_project(tmp_path, ["一", "二"])
    episodes = project["episodes"]
    docs = {doc["episode_id"]: doc for doc in project["documents"] if doc["kind"] == "screenplay"}
    accepted_one = submit_and_decide(service, docs[episodes[0]["id"]], screenplay(1, "一"))
    accepted_two = submit_and_decide(service, docs[episodes[1]["id"]], screenplay(2, "二"))
    segment_one = service.get_episode(episodes[0]["id"])["segments"][0]
    segment_two = service.get_episode(episodes[1]["id"])["segments"][0]
    shot_one = service.create_shot(segment_one["id"], ShotCreate(order_index=0, image_prompt="old prompt"))
    shot_two = service.create_shot(segment_two["id"], ShotCreate(order_index=0, image_prompt="keep prompt"))
    asset = service.create_asset(project["id"], AssetCreate(kind="character", name="沈乔"))
    binding = service.bind_reference(BindingCreate(
        shot_id=shot_one["id"], asset_version_id=asset["current_version_id"], usage="character"
    ))
    with db.write() as conn:
        conn.execute(
            """INSERT INTO media_versions(
            id,owner_type,owner_id,kind,storage_path,sha256,mime,is_current,is_stale,created_at,
            source_document_version_id,source_projection_revision,derived_from_ids_json
            ) SELECT 'media-old','shot',id,'image','objects/x',?,'image/png',1,0,'now',
            source_document_version_id,source_projection_revision,derived_from_ids_json FROM shots WHERE id=?""",
            ("a" * 64, shot_one["id"]),
        )

    latest_one = service.get_document(docs[episodes[0]["id"]]["id"])
    submit_and_decide(service, latest_one, screenplay(1, "一", "新地点"))
    stale_shot = service.get_shot(shot_one["id"])
    assert stale_shot["is_stale"] == 1
    assert stale_shot["prompts"][0]["is_stale"] == 1
    assert next(item for item in stale_shot["bindings"] if item["id"] == binding["id"])["is_stale"] == 1
    assert stale_shot["media"][0]["is_stale"] == 1
    assert service.get_shot(shot_two["id"])["is_stale"] == 0
    assert service.get_document(docs[episodes[1]["id"]]["id"])["current_version_id"] == accepted_two["current_version_id"]
    assert accepted_one["current_version_id"] != service.get_document(docs[episodes[0]["id"]]["id"])["current_version_id"]
    db.close()


def test_reject_feedback_v2_precise_development_stale_and_batch_key(tmp_path):
    db, service, project, development = create_developed_project(tmp_path, ["一", "二", "三", "四"])
    docs = {episode["episode_number"]: next(
        doc for doc in project["documents"] if doc.get("episode_id") == episode["id"] and doc["kind"] == "screenplay"
    ) for episode in project["episodes"]}
    rejected = submit_and_decide(service, docs[3], screenplay(3, "三"), "reject", "主角动机不成立")
    task = service.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-write", intent="按反馈重写 EP03",
        document_id=docs[3]["id"], document_version_id=development["current_version_id"],
        batch_key="season-pass-1",
    ))
    assert "主角动机不成立" in service.get_task_context(task["id"])["target"]["rejected_feedback"]
    ep3_v2 = submit_and_decide(service, rejected, screenplay(3, "三（修订）"))
    assert ep3_v2["current_version_id"]
    assert next(item for item in ep3_v2["versions"] if item["id"] == ep3_v2["current_version_id"])["version_number"] == 2
    sibling = service.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-write", intent="写 EP04",
        document_id=docs[4]["id"], document_version_id=development["current_version_id"],
        batch_key="season-pass-1",
    ))
    assert task["id"] != sibling["id"] and task["batch_key"] == sibling["batch_key"]

    accepted_four = submit_and_decide(service, docs[4], screenplay(4, "四"))
    current_development = service.get_document(development["id"])
    revised_map = episode_map(["一", "二", "三", "四（新方向）"])
    submit_and_decide(service, current_development, revised_map)
    refreshed = service.get_project(project["id"])
    stale = {doc["episode_id"]: doc["is_stale"] for doc in refreshed["documents"] if doc["kind"] == "screenplay"}
    assert [stale[episode["id"]] for episode in refreshed["episodes"]] == [0, 0, 0, 1]
    ep4 = service.get_document(docs[4]["id"])
    assert ep4["current_version_id"] == accepted_four["current_version_id"] and ep4["is_stale"] == 1
    assert rejected["versions"][0]["status"] == "REJECTED"
    db.close()


def test_development_uses_exact_latest_source_snapshot_and_persists_lineage(tmp_path):
    original = screenplay(1, "旧稿")
    revised = screenplay(1, "新稿", "新屋")
    db = Database(tmp_path / "multi-source.sqlite3")
    service = WorkbenchService(db)
    project = service.create_from_intake(
        ProjectIntake(title="多源", intake_kind="multi_script", content=original)
    )
    latest = service.add_project_input(
        project["id"],
        ProjectInputCreate(intake_kind="revision", title="完整修订整稿", content=revised),
    )["document"]
    development = next(doc for doc in service.get_project(project["id"])["documents"] if doc["kind"] == "development")
    encoded = revised.encode()
    span = {"start": 0, "end": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    accepted = submit_and_decide(
        service, development, episode_map(["新稿"], [span], ["主角进入新屋并改变决定。"])
    )
    current = next(item for item in accepted["versions"] if item["id"] == accepted["current_version_id"])
    assert current["source_document_version_id"] == latest["current_version_id"]
    assert accepted["source_document_version_id"] == latest["current_version_id"]
    episode = service.get_project(project["id"])["episodes"][0]
    screenplay_doc = next(doc for doc in service.get_project(project["id"])["documents"] if doc["kind"] == "screenplay")
    assert screenplay_doc["draft"]["content"] == revised
    row = db.connection.execute(
        """SELECT s.content FROM source_episode_spans p
        JOIN source_snapshots s ON s.id=p.source_snapshot_id WHERE p.episode_id=?""",
        (episode["id"],),
    ).fetchone()
    assert row[0] == revised
    db.close()


def test_same_title_story_change_stales_only_changed_episode(tmp_path):
    db, service, project, development = create_developed_project(
        tmp_path, ["同名", "同名"], ["第一集保持原剧情。", "第二集原剧情。"]
    )
    docs = {episode["episode_number"]: next(
        doc for doc in project["documents"] if doc.get("episode_id") == episode["id"] and doc["kind"] == "screenplay"
    ) for episode in project["episodes"]}
    submit_and_decide(service, docs[1], screenplay(1, "同名"))
    submit_and_decide(service, docs[2], screenplay(2, "同名"))
    submit_and_decide(
        service, service.get_document(development["id"]),
        episode_map(["同名", "同名"], stories=["第一集保持原剧情。", "第二集主角背叛盟友，剧情完全改变。"]),
    )
    refreshed = service.get_project(project["id"])
    states = {doc["episode_id"]: doc["is_stale"] for doc in refreshed["documents"] if doc["kind"] == "screenplay"}
    assert [states[item["id"]] for item in refreshed["episodes"]] == [0, 1]
    db.close()


def test_removed_episode_rejects_old_candidate_task_accept_and_retry(tmp_path):
    db, service, project, development = create_developed_project(tmp_path, ["一", "二"])
    episode_two = project["episodes"][1]
    document = next(doc for doc in project["documents"] if doc.get("episode_id") == episode_two["id"])
    accepted = submit_and_decide(service, document, screenplay(2, "二"))
    document = service.get_document(document["id"])
    saved = service.save_document_draft(
        document["id"], DraftSave(expected_revision=document["draft"]["revision"], content=screenplay(2, "二修订"))
    )
    submitted = service.submit_document(
        document["id"], DocumentSubmit(expected_document_revision=saved["revision"], expected_draft_revision=saved["draft"]["revision"])
    )
    candidate = next(item for item in submitted["versions"] if item["status"] == "SUBMITTED")
    submit_and_decide(service, service.get_document(development["id"]), episode_map(["一"]))
    removed = service.get_project(project["id"])["episodes"][1]
    assert removed["status"] == "removed"
    latest_doc = service.get_document(document["id"])
    with pytest.raises(ConflictError, match="removed"):
        service.decide_document_version(
            document["id"], candidate["id"],
            DocumentDecision(expected_document_revision=latest_doc["revision"], action="accept"),
        )
    with pytest.raises(ConflictError, match="removed"):
        service.start_task(TaskStart(
            project_id=project["id"], capability="short-drama-write", intent="复活第二集",
            document_id=document["id"], document_version_id=development["current_version_id"],
            skill_manifest=[{"name": "short-drama-write", "version": "3ab6b855"}],
        ))
    with pytest.raises(ConflictError, match="removed"):
        service.retry_screenplay_projection(
            document["id"], ProjectionRetry(expected_document_revision=latest_doc["revision"])
        )

    development = service.get_document(development["id"])
    reintroduced = submit_and_decide(service, development, episode_map(["一", "二"]))
    active = service.get_project(project["id"])["episodes"][1]
    assert active["status"] == "active"
    task = service.start_task(TaskStart(
        project_id=project["id"], capability="short-drama-write", intent="重新写第二集",
        document_id=document["id"], document_version_id=reintroduced["current_version_id"],
        skill_manifest=[{"name": "short-drama-write", "version": "3ab6b855"}],
    ))
    assert task["status"] == "QUEUED"
    assert accepted["current_version_id"] is not None
    db.close()


def test_removed_episode_document_write_routes_are_read_only(tmp_path):
    app = create_app(
        database_path=tmp_path / "api.sqlite3", media_root=tmp_path / "media",
        token="creator-token", agent_token="agent-token",
    )
    service = app.state.workbench
    project = service.create_from_intake(
        ProjectIntake(title="只读移除集", intake_kind="idea", content="两集故事")
    )
    development = next(item for item in project["documents"] if item["kind"] == "development")
    submit_and_decide(service, development, episode_map(["一", "二"]))
    project = service.get_project(project["id"])
    episode_two = project["episodes"][1]
    document = next(
        item for item in project["documents"]
        if item.get("episode_id") == episode_two["id"] and item["kind"] == "screenplay"
    )
    accepted = submit_and_decide(service, document, screenplay(2, "二"))
    submit_and_decide(
        service, service.get_document(development["id"]), episode_map(["一"])
    )
    before = service.get_document(document["id"])
    headers = {"Authorization": "Bearer creator-token"}
    with TestClient(app) as client:
        save = client.put(
            f"/api/documents/{document['id']}/draft", headers=headers,
            json={"expected_revision": before["draft"]["revision"], "content": "不能写"},
        )
        submit = client.post(
            f"/api/documents/{document['id']}/submit", headers=headers,
            json={
                "expected_document_revision": before["revision"],
                "expected_draft_revision": before["draft"]["revision"],
            },
        )
        restore = client.post(
            f"/api/documents/{document['id']}/draft/restore", headers=headers,
            json={
                "expected_draft_revision": before["draft"]["revision"],
                "source_version_id": accepted["current_version_id"],
            },
        )
    assert [save.status_code, submit.status_code, restore.status_code] == [409, 409, 409]
    after = service.get_document(document["id"])
    assert after["draft"] == before["draft"]
    assert after["versions"] == before["versions"]
    app.state.db.close()


def test_projection_retires_owned_orphan_but_keeps_manual_facts(tmp_path):
    db, service, project, _ = create_developed_project(tmp_path, ["一"])
    episode = project["episodes"][0]
    document = next(doc for doc in project["documents"] if doc.get("episode_id") == episode["id"])
    accepted = submit_and_decide(service, document, screenplay(1, "一"))
    manual_scene = service.create_scene(
        episode["id"], SceneCreate(order_index=90, scene_number="manual", heading={}, blocks=[])
    )
    manual_segment = service.create_segment(
        episode["id"], SegmentCreate(code="MANUAL", order_index=90, title="人工段")
    )
    manual_shot = service.create_shot(manual_segment["id"], ShotCreate(order_index=0))
    with db.write() as conn:
        conn.execute(
            """INSERT INTO script_scenes(
            id,episode_id,order_index,scene_number,heading_json,blocks_json,revision,
            source_document_version_id,source_projection_revision,derived_from_ids_json,status
            ) VALUES('owned-orphan',?,91,'orphan','{}','[]',0,?,1,?,'active')""",
            (episode["id"], accepted["current_version_id"], json.dumps([accepted["current_version_id"]])),
        )
    submit_and_decide(service, service.get_document(document["id"]), screenplay(1, "一", "新屋"))
    assert db.connection.execute("SELECT status FROM script_scenes WHERE id='owned-orphan'").fetchone()[0] == "retired"
    assert db.connection.execute("SELECT status FROM script_scenes WHERE id=?", (manual_scene["id"],)).fetchone()[0] == "active"
    assert db.connection.execute("SELECT status FROM segments WHERE id=?", (manual_segment["id"],)).fetchone()[0] == "active"
    assert service.get_shot(manual_shot["id"])["is_stale"] == 0
    db.close()


def test_surface_episode_cannot_override_target_document_episode(tmp_path):
    db, service, project, development = create_developed_project(tmp_path, ["一", "二"])
    first, second = project["episodes"]
    document = next(doc for doc in project["documents"] if doc.get("episode_id") == first["id"])
    service.set_surface_context(SurfaceContextUpsert(
        session_id="surface", project_id=project["id"], episode_id=second["id"], route="documents"
    ))
    with pytest.raises(ProjectMismatchError, match="surface context episode"):
        service.start_task(TaskStart(
            project_id=project["id"], capability="short-drama-write", intent="写第一集",
            document_id=document["id"], document_version_id=development["current_version_id"],
            surface_session_id="surface",
            skill_manifest=[{"name": "short-drama-write", "version": "3ab6b855"}],
        ))
    db.close()
