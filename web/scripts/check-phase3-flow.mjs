import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { episodeDocumentState, episodeIdForScreenplay, initialDocumentId, loadLatestEpisodeSelection, resolveSelection, visibleSegmentCode } from "../src/workbench/selection-state.mjs";
import { shotStaleLabel } from "../src/workbench/visual-decision-state.mjs";

const documents = readFileSync(new URL("../src/workbench/DocumentWorkspace.tsx", import.meta.url), "utf8");
const shell = readFileSync(new URL("../src/workbench/WorkbenchShell.tsx", import.meta.url), "utf8");

assert.match(documents, /aria-label="多集导航"/);
const episodes = [
  { id: "ep1", status: "active" },
  { id: "ep4", status: "active" },
  { id: "removed", status: "removed" },
];
const project = { id: "p1", episodes };
const loadedEpisode = { id: "ep4", segments: [{ id: "seg4" }, { id: "seg5" }] };
const preserved = resolveSelection({ projectId: "p1", episodeId: "ep4", segmentId: "seg4", shotId: "shot4" }, project, loadedEpisode);
assert.deepEqual(preserved, { projectId: "p1", episodeId: "ep4", segmentId: "seg4", shotId: "shot4" });
assert.equal(resolveSelection({ projectId: "p1", episodeId: "removed", segmentId: null, shotId: null }, project, null).episodeId, "ep1");
assert.equal(resolveSelection({ projectId: "p1", episodeId: "ep4", segmentId: "missing", shotId: null }, project, loadedEpisode).segmentId, "seg4");
assert.equal(visibleSegmentCode("EP04-S03@r7"), "EP04-S03");
const documentsForSelection = [
  { id: "development", kind: "development", draft: {} },
  { id: "screenplay-2", kind: "screenplay", episode_id: "ep2", draft: {} },
];
assert.equal(initialDocumentId(documentsForSelection, "ep2"), "screenplay-2");
assert.equal(episodeIdForScreenplay(documentsForSelection[1]), "ep2");
assert.equal(episodeIdForScreenplay(documentsForSelection[0]), null);

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
let currentEpisodeId = "ep3";
const episode3 = deferred(), episode4 = deferred(), segment3 = deferred(), segment4 = deferred();
const commits = [];
const load = (episodePromise, segmentPromise) => loadLatestEpisodeSelection({
  loadEpisode: () => episodePromise.promise,
  loadSegment: () => segmentPromise.promise,
  isCurrent: (id) => currentEpisodeId === id,
  commit: (episode, segment) => commits.push([episode.id, segment?.id]),
});
const slowEpisode3 = load(episode3, segment3);
currentEpisodeId = "ep4";
const fastEpisode4 = load(episode4, segment4);
episode4.resolve({ id: "ep4" });
await Promise.resolve();
segment4.resolve({ id: "seg4" });
assert.equal(await fastEpisode4, true);
episode3.resolve({ id: "ep3" });
segment3.resolve({ id: "seg3" });
assert.equal(await slowEpisode3, false);
assert.deepEqual(commits, [["ep4", "seg4"]]);
assert.deepEqual(episodeDocumentState({ status: "removed" }, null), { label: "已移出分集地图", kind: "removed" });
assert.deepEqual(episodeDocumentState({ status: "active" }, { is_stale: 1, current_version_id: null, versions: [] }), { label: "草稿需更新", kind: "stale" });
assert.deepEqual(episodeDocumentState({ status: "active" }, { is_stale: 1, current_version_id: "v1", versions: [{ id: "v1" }] }), { label: "已接受 · stale", kind: "stale" });

assert.match(documents, /接受并投影/);
assert.match(documents, /重试投影/);
assert.match(documents, /createTask\("short-drama-write"\)/);
assert.match(documents, /按反馈重新交给 Codex/);
assert.match(documents, /让 Codex 发展故事/);
assert.match(documents, /<DevelopmentEditor/);
assert.match(documents, /validateDevelopment\(saveState.current.content\)/);
assert.match(documents, /development_episode_map_/);
assert.match(documents, /恢复为草稿，请修改后提交新版本/);
assert.match(shell, /aria-label="分镜集数"/);
assert.equal(shotStaleLabel({ is_stale: 1, stale_reason: "screenplay projection replaced", bindings: [] }), "剧本已更新");
assert.equal(shotStaleLabel({ is_stale: 1, stale_reason: "asset version replaced", bindings: [] }), "REF 已替换");
assert.match(shell, /返回当前集剧本并投影/);
assert.doesNotMatch(shell, /阶段 3 开放/);

console.log("phase 3 multi-episode flow contract: ok");
