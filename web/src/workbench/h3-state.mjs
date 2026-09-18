export function h3Readiness(binding, keyframe, prompt, duration) {
  if (!binding) return { ready: false, reason: "先绑定视觉资产" };
  if (binding.binding_mode !== "frozen") return { ready: false, reason: "先冻结当前 REF" };
  if (!keyframe || keyframe.candidate_status !== "accepted" || keyframe.mime !== "image/png") return { ready: false, reason: "需要已接受的 PNG 冻结关键帧" };
  if (!prompt.trim()) return { ready: false, reason: "需要视频提示词" };
  if (!Number.isInteger(duration) || duration < 4 || duration > 15) return { ready: false, reason: "时长必须是 4–15 秒整数" };
  return { ready: true, reason: "可提交本地 H3" };
}

export function continuationActions(packageReady, hasNextEpisode) {
  if (!packageReady) return [];
  return [...(hasNextEpisode ? ["下一集"] : []), "下一季", "新作品", "项目库"];
}

export function h3JobMode(state) {
  if (!state) return "editable";
  if (state === "AWAITING_CONFIRMATION") return "confirm";
  if (state === "H3_SUBMITTED" || state === "H3_RUNNING") return "poll-only";
  if (state === "SUCCEEDED") return "review-candidate";
  return "reprepare";
}

export function packageIsReady(manifest) {
  return Boolean(manifest?.ready)
    && manifest.missing.length === 0
    && manifest.failed.length === 0
    && Object.values(manifest.stale).every((items) => items.length === 0);
}

export function canStartNewH3Round(state) {
  return state === "SUCCEEDED";
}

export function resolveKeyframeChoice(choices, currentBindingId, frozenJobBindingId) {
  const wanted = currentBindingId || frozenJobBindingId;
  if (wanted) return choices.find((item) => item.binding.id === wanted) || null;
  return choices.length === 1 ? choices[0] : null;
}
