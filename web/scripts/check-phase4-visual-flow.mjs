import assert from "node:assert/strict";
import { candidateDisplayName, selectAfterGeneration, shotStaleLabel } from "../src/workbench/visual-decision-state.mjs";

const candidates = [
  { id: "b1", generation_job_id: "edit-job", candidate_label: "B1", candidate_status: "candidate" },
  { id: "b", generation_job_id: "root-job", candidate_label: "B", candidate_status: "candidate" },
  { id: "a", generation_job_id: "root-job", candidate_label: "A", candidate_status: "candidate" },
];
assert.equal(selectAfterGeneration(candidates, "edit-job", 1, "b"), "b1");
assert.equal(candidateDisplayName(candidates[0]), "候选 B1 · 未接受");
assert.equal(candidateDisplayName({ ...candidates[0], candidate_status: "accepted", accepted_asset_version_id: "version-123456" }), "正式 REF · version-");
assert.equal(shotStaleLabel({ is_stale: 1, stale_reason: "asset version v2 is current", bindings: [] }), "REF 已替换");
console.log("phase 4 visual decision flow contract: ok");
