ALTER TABLE generation_jobs ADD COLUMN parent_candidate_id TEXT REFERENCES media_versions(id);

ALTER TABLE media_versions ADD COLUMN project_id TEXT REFERENCES projects(id);
ALTER TABLE media_versions ADD COLUMN candidate_status TEXT NOT NULL DEFAULT 'formal'
  CHECK(candidate_status IN ('formal','candidate','accepted','rejected'));
ALTER TABLE media_versions ADD COLUMN generation_job_id TEXT REFERENCES generation_jobs(id);
ALTER TABLE media_versions ADD COLUMN parent_candidate_id TEXT REFERENCES media_versions(id);
ALTER TABLE media_versions ADD COLUMN prompt_text TEXT;
ALTER TABLE media_versions ADD COLUMN prompt_version_id TEXT REFERENCES prompt_versions(id);
ALTER TABLE media_versions ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'legacy'
  CHECK(source_kind IN ('legacy','generation','edit','agent_import'));
ALTER TABLE media_versions ADD COLUMN task_id TEXT REFERENCES tasks(id);
ALTER TABLE media_versions ADD COLUMN agent_run_id TEXT REFERENCES agent_runs(id);
ALTER TABLE media_versions ADD COLUMN accepted_asset_version_id TEXT REFERENCES asset_versions(id);
ALTER TABLE media_versions ADD COLUMN accepted_at TEXT;
ALTER TABLE media_versions ADD COLUMN candidate_label TEXT;
ALTER TABLE media_versions ADD COLUMN owner_revision INTEGER;
ALTER TABLE media_versions ADD COLUMN source_fingerprint TEXT;
ALTER TABLE media_versions ADD COLUMN prompt_sha256 TEXT;
ALTER TABLE media_versions ADD COLUMN target_asset_id TEXT REFERENCES assets(id);
ALTER TABLE media_versions ADD COLUMN target_asset_revision INTEGER;

CREATE INDEX media_candidates_project_owner_idx
  ON media_versions(project_id,owner_type,owner_id,candidate_status,created_at);
CREATE INDEX media_candidates_task_run_idx ON media_versions(task_id,agent_run_id);

CREATE TRIGGER media_candidate_provenance_immutable
BEFORE UPDATE OF project_id,owner_type,owner_id,kind,storage_path,sha256,mime,width,height,
  duration_seconds,created_at,generation_job_id,
  parent_candidate_id,prompt_text,prompt_version_id,source_kind,task_id,agent_run_id,
  candidate_label,owner_revision,source_fingerprint,prompt_sha256,target_asset_id,target_asset_revision,
  source_document_version_id,source_projection_revision,derived_from_ids_json
ON media_versions
WHEN OLD.candidate_status<>'formal'
BEGIN SELECT RAISE(ABORT, 'media candidate provenance is immutable'); END;

CREATE TRIGGER media_candidate_accept_transition
BEFORE UPDATE OF candidate_status,accepted_asset_version_id,accepted_at ON media_versions
WHEN NOT (
  OLD.candidate_status='candidate' AND NEW.candidate_status='accepted'
  AND OLD.accepted_asset_version_id IS NULL AND NEW.accepted_asset_version_id IS NOT NULL
  AND OLD.accepted_at IS NULL AND NEW.accepted_at IS NOT NULL AND NEW.is_current=1
)
BEGIN SELECT RAISE(ABORT, 'invalid media candidate status transition'); END;

CREATE TRIGGER accepted_media_ref_delete_protected
BEFORE DELETE ON media_versions
WHEN OLD.candidate_status='accepted' OR OLD.accepted_asset_version_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'accepted media REF cannot be deleted'); END;
