ALTER TABLE creative_document_versions ADD COLUMN source_changeset_id TEXT REFERENCES changesets(id);
ALTER TABLE creative_document_versions ADD COLUMN created_by TEXT NOT NULL DEFAULT 'user' CHECK(created_by IN ('user','agent','import'));
ALTER TABLE agent_runs ADD COLUMN worker_label TEXT;

CREATE TRIGGER creative_document_versions_agent_provenance_immutable
BEFORE UPDATE OF source_changeset_id,created_by ON creative_document_versions
BEGIN SELECT RAISE(ABORT, 'creative document version provenance is immutable'); END;
