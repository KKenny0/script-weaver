ALTER TABLE creative_document_versions ADD COLUMN projection_revision INTEGER;
ALTER TABLE creative_document_versions ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE creative_document_versions ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));

ALTER TABLE creative_documents ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE creative_documents ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));
ALTER TABLE creative_documents ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE creative_documents ADD COLUMN stale_reason TEXT;

ALTER TABLE episodes ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE episodes ADD COLUMN development_fingerprint TEXT;
ALTER TABLE episodes ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));
ALTER TABLE episodes ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE episodes ADD COLUMN stale_reason TEXT;
ALTER TABLE episodes ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','removed'));

ALTER TABLE script_scenes ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE script_scenes ADD COLUMN source_projection_revision INTEGER;
ALTER TABLE script_scenes ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));
ALTER TABLE script_scenes ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired'));

ALTER TABLE segments ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE segments ADD COLUMN source_projection_revision INTEGER;
ALTER TABLE segments ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));
ALTER TABLE segments ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired'));

ALTER TABLE shots ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE shots ADD COLUMN source_projection_revision INTEGER;
ALTER TABLE shots ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));
ALTER TABLE shots ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shots ADD COLUMN stale_reason TEXT;

ALTER TABLE prompt_versions ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE prompt_versions ADD COLUMN source_projection_revision INTEGER;
ALTER TABLE prompt_versions ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));
ALTER TABLE prompt_versions ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE prompt_versions ADD COLUMN stale_reason TEXT;

ALTER TABLE media_versions ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE media_versions ADD COLUMN source_projection_revision INTEGER;
ALTER TABLE media_versions ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));
ALTER TABLE media_versions ADD COLUMN stale_reason TEXT;

ALTER TABLE reference_bindings ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id);
ALTER TABLE reference_bindings ADD COLUMN source_projection_revision INTEGER;
ALTER TABLE reference_bindings ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json));

ALTER TABLE tasks ADD COLUMN batch_key TEXT;

CREATE TABLE source_episode_spans(
  id TEXT PRIMARY KEY,
  source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
  episode_id TEXT NOT NULL REFERENCES episodes(id),
  episode_number INTEGER NOT NULL,
  byte_start INTEGER NOT NULL CHECK(byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK(byte_end > byte_start),
  sha256 TEXT NOT NULL,
  confirmed_development_version_id TEXT NOT NULL REFERENCES creative_document_versions(id),
  created_at TEXT NOT NULL,
  UNIQUE(source_snapshot_id, episode_number, confirmed_development_version_id)
);

CREATE INDEX source_episode_spans_snapshot_idx ON source_episode_spans(source_snapshot_id, episode_number);
CREATE INDEX creative_documents_episode_idx ON creative_documents(episode_id, kind);
CREATE INDEX script_scenes_projection_idx ON script_scenes(episode_id, source_projection_revision, status);
CREATE INDEX segments_projection_idx ON segments(episode_id, source_projection_revision, status);

CREATE TRIGGER creative_document_versions_lineage_immutable
BEFORE UPDATE OF source_document_version_id,derived_from_ids_json ON creative_document_versions
BEGIN SELECT RAISE(ABORT, 'creative document version lineage is immutable'); END;
