ALTER TABLE projects ADD COLUMN archived_at TEXT;

CREATE TABLE creative_documents(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  episode_id TEXT REFERENCES episodes(id),
  kind TEXT NOT NULL CHECK(kind IN ('source','development','screenplay','review')),
  title TEXT NOT NULL,
  current_version_id TEXT REFERENCES creative_document_versions(id) DEFERRABLE INITIALLY DEFERRED,
  revision INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE source_snapshots(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  document_id TEXT NOT NULL REFERENCES creative_documents(id),
  intake_kind TEXT NOT NULL CHECK(intake_kind IN ('idea','novel','single_script','multi_script','supplement','revision')),
  content TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE creative_document_versions(
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES creative_documents(id),
  version_number INTEGER NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('SUBMITTED','ACCEPTED','REJECTED')),
  source_snapshot_id TEXT REFERENCES source_snapshots(id),
  decision_feedback TEXT,
  projection_status TEXT NOT NULL DEFAULT 'not_applicable' CHECK(projection_status IN ('not_applicable','not_projected')),
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(document_id, version_number)
);

CREATE TABLE document_drafts(
  document_id TEXT PRIMARY KEY REFERENCES creative_documents(id),
  content TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  base_version_id TEXT REFERENCES creative_document_versions(id),
  updated_at TEXT NOT NULL
);

CREATE INDEX creative_documents_project_idx ON creative_documents(project_id, updated_at);
CREATE INDEX creative_document_versions_document_idx ON creative_document_versions(document_id, version_number);
CREATE INDEX source_snapshots_project_idx ON source_snapshots(project_id, created_at);

CREATE TRIGGER creative_document_versions_immutable_update
BEFORE UPDATE OF document_id,version_number,content,source_snapshot_id,created_at ON creative_document_versions
BEGIN SELECT RAISE(ABORT, 'creative document version content is immutable'); END;

CREATE TRIGGER creative_document_versions_immutable_delete
BEFORE DELETE ON creative_document_versions
BEGIN SELECT RAISE(ABORT, 'creative document versions are immutable'); END;

CREATE TRIGGER source_snapshots_immutable_update
BEFORE UPDATE ON source_snapshots
BEGIN SELECT RAISE(ABORT, 'source snapshots are immutable'); END;

CREATE TRIGGER source_snapshots_immutable_delete
BEFORE DELETE ON source_snapshots
BEGIN SELECT RAISE(ABORT, 'source snapshots are immutable'); END;
