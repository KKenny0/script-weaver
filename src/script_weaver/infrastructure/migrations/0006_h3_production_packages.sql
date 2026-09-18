ALTER TABLE generation_jobs ADD COLUMN external_job_id TEXT;
ALTER TABLE generation_jobs ADD COLUMN external_status TEXT;
ALTER TABLE generation_jobs ADD COLUMN submitted_fingerprint TEXT;
ALTER TABLE generation_jobs ADD COLUMN submitted_at TEXT;
ALTER TABLE generation_jobs ADD COLUMN completed_at TEXT;
ALTER TABLE media_versions ADD COLUMN accepted_shot_revision INTEGER;

DROP TRIGGER media_candidate_accept_transition;
CREATE TRIGGER media_candidate_accept_transition
BEFORE UPDATE OF candidate_status,accepted_asset_version_id,accepted_shot_revision,accepted_at
ON media_versions
WHEN NOT (
  OLD.candidate_status='candidate' AND NEW.candidate_status='accepted'
  AND OLD.accepted_asset_version_id IS NULL AND OLD.accepted_shot_revision IS NULL
  AND NEW.accepted_at IS NOT NULL AND NEW.is_current=1
  AND (
    (OLD.kind='image' AND NEW.accepted_asset_version_id IS NOT NULL
      AND NEW.accepted_shot_revision IS NULL)
    OR
    (OLD.kind='video' AND OLD.owner_type='shot'
      AND NEW.accepted_asset_version_id IS NULL AND NEW.accepted_shot_revision IS NOT NULL)
  )
)
BEGIN SELECT RAISE(ABORT, 'invalid media candidate status transition'); END;

CREATE UNIQUE INDEX generation_jobs_external_id_idx
  ON generation_jobs(adapter,external_job_id) WHERE external_job_id IS NOT NULL;

CREATE TABLE production_packages(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  version_number INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
  storage_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id,version_number),
  UNIQUE(project_id,fingerprint)
);

CREATE TRIGGER production_packages_immutable_update
BEFORE UPDATE ON production_packages
BEGIN SELECT RAISE(ABORT, 'production package is immutable'); END;

CREATE TRIGGER production_packages_immutable_delete
BEFORE DELETE ON production_packages
BEGIN SELECT RAISE(ABORT, 'production package is immutable'); END;
