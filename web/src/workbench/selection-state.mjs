export function resolveSelection(previous, project, loadedEpisode) {
  const activeEpisodes = project.episodes.filter((item) => item.status !== "removed");
  const episodeId = previous.projectId === project.id && activeEpisodes.some((item) => item.id === previous.episodeId)
    ? previous.episodeId
    : activeEpisodes[0]?.id || null;
  if (!loadedEpisode || loadedEpisode.id !== episodeId) {
    return { projectId: project.id, episodeId, segmentId: null, shotId: null };
  }
  const segments = loadedEpisode.segments || [];
  const segmentId = previous.projectId === project.id && segments.some((item) => item.id === previous.segmentId)
    ? previous.segmentId
    : segments[0]?.id || null;
  return {
    projectId: project.id,
    episodeId,
    segmentId,
    shotId: segmentId === previous.segmentId ? previous.shotId : null,
  };
}

export function visibleSegmentCode(code) {
  return code.replace(/@r\d+$/, "");
}

export function initialDocumentId(documents, selectedEpisodeId) {
  return documents.find((item) => item.kind === "screenplay" && item.episode_id === selectedEpisodeId)?.id
    || documents.find((item) => item.draft)?.id
    || documents[0]?.id
    || "";
}

export function episodeIdForScreenplay(document) {
  return document?.kind === "screenplay" ? document.episode_id || null : null;
}

export async function loadLatestEpisodeSelection({ loadEpisode, loadSegment, isCurrent, commit }) {
  const loaded = await loadEpisode();
  if (!isCurrent(loaded.id)) return false;
  const segment = await loadSegment(loaded);
  if (!isCurrent(loaded.id)) return false;
  commit(loaded, segment);
  return true;
}

export function episodeDocumentState(episode, document) {
  if (episode.status === "removed") return { label: "已移出分集地图", kind: "removed" };
  const current = document?.versions.find((version) => version.id === document.current_version_id);
  if (document?.is_stale && current) return { label: "已接受 · stale", kind: "stale" };
  if (document?.is_stale) return { label: "草稿需更新", kind: "stale" };
  if (current?.projection_revision) return { label: `已投影 r${current.projection_revision}`, kind: "projected" };
  if (current) return { label: "已接受 · 待投影", kind: "accepted" };
  if (document?.versions.some((version) => version.status === "SUBMITTED")) return { label: "待审批", kind: "submitted" };
  return { label: "草稿", kind: "draft" };
}
