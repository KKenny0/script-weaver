export function selectAfterGeneration(candidates, completedJobId, count, currentId) {
  if (count === 1) return candidates.find((item) => item.generation_job_id === completedJobId)?.id || currentId || null;
  return currentId && candidates.some((item) => item.id === currentId) ? currentId : candidates[0]?.id || null;
}

export function candidateDisplayName(candidate) {
  return candidate.candidate_status === "accepted"
    ? `正式 REF · ${candidate.accepted_asset_version_id?.slice(0, 8) || "已登记"}`
    : `候选 ${candidate.candidate_label || candidate.id.slice(0, 4)} · 未接受`;
}

export function shotStaleLabel(shot) {
  const refChanged = shot.stale_reason?.includes("asset version")
    || shot.bindings.some((item) => item.stale_reason?.includes("asset version"));
  return refChanged ? "REF 已替换" : shot.is_stale ? "剧本已更新" : "上游已换图";
}
