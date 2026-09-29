"use client";

import React, { useState, useCallback, useRef, useEffect } from "react";
import { PanelLeftOpen } from "lucide-react";
import CardCandidates from "./components/CardCandidates";
import ChatPanel from "./components/ChatPanel";
import ArtifactPanel from "./components/ArtifactPanel";
import ProjectList, { ProjectSummary } from "./components/ProjectList";
import { HistoryPanelState, VersionSnapshot } from "./components/VersionHistory";
import { ArtifactData } from "./components/ArtifactContent";
import CardEditDrawer, {
  CardEditSession,
  CardKind,
  SaveCardResult,
} from "./components/CardEditDrawer";
import ReviewPanel, {
  ConfirmReviewResult,
  ReviewPanelSession,
  ReviewSelection,
} from "./components/ReviewPanel";

const API = "/api";

async function apiGet(path: string) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(extractError(err, `HTTP ${res.status}`));
  }
  return res.json();
}

function extractError(err: any, fallback = "请求失败"): string {
  const d = err?.detail;
  if (typeof d === "string") return d;
  if (d?.message) return d.message;
  return err?.message || fallback;
}

function pickArtifactData(fullState: any): ArtifactData {
  return {
    refined_idea: fullState.refined_idea,
    outline: fullState.outline,
    characters: fullState.characters,
    scenes: fullState.scenes,
    art_style: fullState.art_style,
    script: fullState.script,
    storyboard: fullState.storyboard,
    visual_highlights: fullState.visual_highlights,
  };
}

function projectHasArtifacts(s: any): boolean {
  return !!(
    s.refined_idea || s.outline || s.art_style || s.script || s.storyboard ||
    (s.characters && s.characters.length) || (s.scenes && s.scenes.length)
  );
}

// ── Card editing (ticket #16) ────────────────────

function findCard(
  data: ArtifactData | any,
  kind: CardKind,
  id: string,
): Record<string, any> | null {
  if (kind === "characters")
    return (data.characters || []).find((c: any) => c.id === id) ?? null;
  if (kind === "scenes")
    return (data.scenes || []).find((s: any) => s.id === id) ?? null;
  return ((data.storyboard as any)?.shots || []).find((s: any) => s.shot_id === id) ?? null;
}

const REVIEW_ARTIFACT_LABELS: Record<string, string> = {
  script: "剧本",
  storyboard: "分镜",
  visual_highlights: "影像亮点",
};

/** The persisted pending-review facts of the adopted snapshot (round 2:
 * read from data on every adoption, never a transient save-only message). */
function reviewFlagsOf(fullState: any): any[] {
  const flags = fullState?.review?.review_flags;
  return Array.isArray(flags) ? flags : [];
}

function buildReviewNoticeText(flags: any[], revision: number): string {
  const artifacts = [...new Set(flags.map((f) => REVIEW_ARTIFACT_LABELS[f.artifact] || f.artifact))];
  if (artifacts.length === 0) return "";
  return (
    `当前有待复核内容：${artifacts.join("、")}。原因：上游内容被手工修改` +
    `（保守影响范围，自 r${revision} 起生效）；是否沿用待后续确认，不会自动重新生成。`
  );
}

// Where "查看" jumps for each flagged artifact: the 影像亮点 tab renders
// both the storyboard and the visual highlights.
const TAB_FOR_REVIEW_ARTIFACT: Record<string, string> = {
  script: "script",
  storyboard: "storyboard",
  visual_highlights: "storyboard",
};

interface SessionNotice {
  epoch: number;
  text: string;
}

const HISTORY_CLOSED: HistoryPanelState = {
  open: false,
  loading: false,
  error: null,
  versions: [],
  currentRevision: 0,
  viewing: null,
  viewingLoading: false,
};

export default function HomePage() {
  const [projectId, setProjectId] = useState<string>("");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [activeTab, setActiveTab] = useState<string>("outline");
  const [artifactData, setArtifactData] = useState<ArtifactData>({});
  const [projectStatus, setProjectStatus] = useState<"idle" | "running" | "complete" | "error">("idle");
  const [leftPanelCollapsed, setLeftPanelCollapsed] = useState(false);
  const [projectListOpen, setProjectListOpen] = useState(true);
  const [sessionNotice, setSessionNotice] = useState<SessionNotice | null>(null);
  // Bumped when the user re-clicks the already-open project: the chat
  // panel re-checks run state (keeping a live observation intact).
  const [projectOpenEpoch, setProjectOpenEpoch] = useState(0);
  const [historyPanel, setHistoryPanel] = useState<HistoryPanelState>(HISTORY_CLOSED);
  // The revision of the content currently DISPLAYED — the basis every card
  // edit session opens from.
  const [projectRevision, setProjectRevision] = useState(0);
  const [editSession, setEditSession] = useState<CardEditSession | null>(null);
  // Persisted pending-review facts of the adopted snapshot (round 2): shown
  // until handled, restored from every read — not tied to one save message.
  // Project-scoped so another project's flags can never render here.
  const [reviewState, setReviewState] = useState<{
    projectId: string; flags: any[]; revision: number;
  } | null>(null);
  // The exact (project, revision) whose banner the user dismissed; a newer
  // snapshot (any save bumps the revision) makes the status visible again.
  const [reviewHiddenFor, setReviewHiddenFor] = useState<{ projectId: string; revision: number } | null>(null);
  // The open review panel's session (ticket #17): one seq per open/reload,
  // keyed remount resets the checkbox selection; the revision inside is the
  // confirm request's CAS basis.
  const [globalOpen, setGlobalOpen] = useState(false);
  const [reviewPanel, setReviewPanel] = useState<ReviewPanelSession | null>(null);

  // Late-response guard: async handlers compare against the project that is
  // open *now*, so a response for a previously selected project can never
  // overwrite the current one.
  const projectRef = useRef("");
  // Monotonic id of the current project session: every open/new-project
  // bumps it synchronously (event handler, no effect lag). The project id
  // alone is NOT a session identity — A→B→A matches again — so export and
  // review-panel results capture this value at launch and may only touch
  // the UI while it is unchanged.
  const projectSessionRef = useRef(0);
  const sessionEpochRef = useRef(0);
  // Monotonic token for history requests: every new view intent (open,
  // retry, snapshot load, close, project switch) supersedes the previous
  // one, so a late history response can only ever update the view session
  // it belongs to.
  const historyReqRef = useRef(0);
  const contentReqRef = useRef(0);
  // Set by ChatPanel; closing the stream ends the subscription only (the
  // backend owns the task).
  const closeStreamRef = useRef<() => void>(() => {});
  // Every editor open mints a fresh session id: a remounted drawer can never
  // inherit a previous session's draft, basis or in-flight save.
  const editOpenSeqRef = useRef(0);
  // Newest-wins token for card saves: a late response may only ever write
  // the UI of the save intent (and project session) it belongs to.
  const cardSaveReqRef = useRef(0);
  // Newest-wins token for conflict reloads (a remounted drawer must never be
  // replaced by an older reload response).
  const reloadReqRef = useRef(0);
  // Newest-wins tokens for the review panel's confirm POST and its conflict
  // reload: a late response may only write the UI of the panel session (and
  // project session) it belongs to.
  const reviewConfirmReqRef = useRef(0);
  const reviewReloadReqRef = useRef(0);
  // Monotonic id for review panel opens/reloads (the key remounts the panel,
  // resetting its selection).
  const reviewPanelSeqRef = useRef(0);
  // The snapshot the page currently displays: artifacts, revision and review
  // always come from THIS one adopted payload, never from mixed sources.
  const displayedSnapshotRef = useRef<{ projectId: string; revision: number }>({ projectId: "", revision: 0 });
  // Live mirror of editSession for async guards (A→B→A makes a plain project
  // check insufficient: the id matches again while the session does not).
  const editSessionRef = useRef<CardEditSession | null>(null);
  useEffect(() => {
    editSessionRef.current = editSession;
  }, [editSession]);

  useEffect(() => {
    projectRef.current = projectId;
  }, [projectId]);

  const nextNotice = useCallback((text: string) => {
    sessionEpochRef.current += 1;
    setSessionNotice({ epoch: sessionEpochRef.current, text });
  }, []);

  // Same-epoch follow-up (round 3): ChatPanel APPENDS a notice whose epoch
  // matches the one it last showed, and only a bumped epoch starts a new
  // session. Card saves (and similar in-place acknowledgements) must not
  // reset the conversation — run outcomes, errors and growth warnings stay
  // — while project switches keep using nextNotice for a real session reset.
  const appendNotice = useCallback((text: string) => {
    setSessionNotice({ epoch: sessionEpochRef.current, text });
  }, []);

  // ── Snapshot adoption (single entry, round 2) ────
  //
  // EVERY path that shows project content on this page funnels through
  // here: the displayed artifacts, revision and review flags always come
  // from one adopted snapshot, never assembled from mixed sources. Within
  // one project session a snapshot older than the displayed one is refused
  // (edits only ever move the revision forward), so a stale GET can no
  // longer overwrite a landed card save. Callers keep their own session
  // guards (epoch/openSeq) on top of this.

  const adoptSnapshot = useCallback((fullState: any): boolean => {
    const pid = fullState?.project_id;
    if (!pid || pid !== projectRef.current) return false;
    const revision = typeof fullState.revision === "number" ? fullState.revision : 0;
    const shown = displayedSnapshotRef.current;
    if (shown.projectId === pid && revision < shown.revision) return false;
    displayedSnapshotRef.current = { projectId: pid, revision };
    setProjectRevision(revision);
    setArtifactData(pickArtifactData(fullState));
    const flags = reviewFlagsOf(fullState);
    setReviewState(flags.length > 0 ? { projectId: pid, flags, revision } : null);
    return true;
  }, []);

  // Reset what the page displays when the project session changes.
  const resetDisplayedSnapshot = useCallback((pid: string) => {
    displayedSnapshotRef.current = { projectId: pid, revision: 0 };
    setProjectRevision(0);
    setReviewState(null);
    setReviewHiddenFor(null);
  }, []);

  // Parent-owned liveness checks. ChatPanel is conditionally unmounted (chat
  // collapse), so its own refs stop receiving updates the moment it unmounts;
  // these stable predicates read the always-alive page refs instead. A stale
  // async continuation from any ChatPanel instance — mounted or not — can
  // therefore only proceed while its session/project is still current.
  const isSessionActive = useCallback(
    (epoch: number) => sessionEpochRef.current === epoch,
    [],
  );
  const isProjectActive = useCallback(
    (id: string) => projectRef.current === id,
    [],
  );
  // Ownership predicate for panel-bound results (exports, review confirms):
  // true only while BOTH the project id and the open-session id are the
  // ones the result was launched under.
  const isProjectSessionActive = useCallback(
    (id: string, session: number) =>
      projectRef.current === id && projectSessionRef.current === session,
    [],
  );

  const refreshProjects = useCallback(async () => {
    try {
      setProjects(await apiGet("/projects"));
    } catch (err) {
      console.error("Failed to list projects:", err);
    }
  }, []);

  // Page opens and chat refreshes share ownership, including failed reads.
  // A superseded response returns null; only the newest read can write UI.
  // Pure reader on purpose (round 2): adoption happens in ONE place —
  // never a bare revision write ahead of the caller's decision.
  const refreshProjectContent = useCallback(async (id: string) => {
    const token = ++contentReqRef.current;
    try {
      const fullState = await apiGet(`/projects/${id}`);
      return token === contentReqRef.current ? fullState : null;
    } catch (err) {
      if (token !== contentReqRef.current) return null;
      throw err;
    }
  }, []);

  // Closing the review panel (explicitly, via 查看, or through a project
  // switch) must invalidate in-flight panel requests SYNCHRONOUSLY: bump
  // the generation token here in the event handler — never only via a
  // later-running effect. Declared before openProject, which calls it on
  // every project switch.
  const closeReviewPanel = useCallback(() => {
    reviewPanelSeqRef.current += 1;
    setReviewPanel(null);
  }, []);

  const openProject = useCallback(async (id: string) => {
    if (projectRef.current === id) {
      // R5: clicking the CURRENT project keeps a live run observation
      // (SSE + artifacts untouched); a lost view is recovered by the
      // panel's epoch-keyed re-check below. Only a real switch resets.
      setProjectOpenEpoch((e) => e + 1);
      refreshProjects();
      return;
    }
    closeStreamRef.current();
    setIsGenerating(false);
    setProjectId(id);
    projectRef.current = id;
    projectSessionRef.current += 1; // a real switch: invalidate the old session's in-flight results
    setArtifactData({});
    setActiveTab("outline");
    setProjectStatus("idle");
    setHistoryPanel(HISTORY_CLOSED);
    resetDisplayedSnapshot(id);
    setEditSession(null);
    closeReviewPanel();
    historyReqRef.current++; // in-flight history responses belong to the old project
    window.history.replaceState(null, "", `/?project=${encodeURIComponent(id)}`);
    nextNotice("📂 正在打开项目…");
    // The session epoch captured right here is this open's identity: after
    // an A→B→A round trip the project id matches again, but the FIRST open's
    // late response belongs to a dead session and must not overwrite the
    // second open's fresh content.
    const epochAtStart = sessionEpochRef.current;
    try {
      const fullState = await refreshProjectContent(id);
      if (fullState === null) return;
      if (projectRef.current !== id || !isSessionActive(epochAtStart)) return; // superseded open
      if (!adoptSnapshot(fullState)) return; // stale for this project session
      // The backend reports "running" while a generation run is active for
      // the project — the run outlives any page view (ticket #14).
      setProjectStatus(
        fullState.status === "running"
          ? "running"
          : projectHasArtifacts(fullState)
            ? "complete"
            : "idle",
      );
      nextNotice(`📂 已打开项目「${fullState.meta?.title || id}」，可继续修改或导出。`);
    } catch (err: any) {
      if (projectRef.current !== id || !isSessionActive(epochAtStart)) return;
      setProjectStatus("error");
      nextNotice(`❌ 打开项目失败: ${err.message}`);
    }
    refreshProjects();
  }, [nextNotice, refreshProjects, isSessionActive, refreshProjectContent, adoptSnapshot]);

  const startNewProject = useCallback(() => {
    closeStreamRef.current();
    setIsGenerating(false);
    setProjectId("");
    projectRef.current = "";
    projectSessionRef.current += 1;
    contentReqRef.current++;
    setArtifactData({});
    setActiveTab("outline");
    setProjectStatus("idle");
    setHistoryPanel(HISTORY_CLOSED);
    resetDisplayedSnapshot("");
    setEditSession(null);
    closeReviewPanel();
    historyReqRef.current++;
    window.history.replaceState(null, "", "/");
    nextNotice("🆕 已开始一个新项目，输入故事想法开始生成。");
  }, [nextNotice, resetDisplayedSnapshot, closeReviewPanel]);

  // A generation that just created its project keeps the current chat
  // session; only the URL and the project list change.
  const handleProjectCreated = useCallback((id: string) => {
    contentReqRef.current++;
    setProjectId(id);
    projectRef.current = id;
    projectSessionRef.current += 1;
    window.history.replaceState(null, "", `/?project=${encodeURIComponent(id)}`);
    refreshProjects();
  }, [refreshProjects]);

  const handleProjectMutated = useCallback(() => {
    refreshProjects();
  }, [refreshProjects]);

  // ChatPanel content refreshes funnel through the same adoption rule as
  // everything else (round 2): a payload carrying a project_id is a full
  // snapshot and must win only if it is the session's current content.
  const handleArtifactUpdate = useCallback((data: Record<string, any>) => {
    if (data && typeof data === "object" && data.project_id) {
      adoptSnapshot(data);
      return;
    }
    setArtifactData(data);
  }, [adoptSnapshot]);

  // Collapsing the chat unmounts ChatPanel, which closes its progress view.
  // The run itself keeps running in the backend (ticket #14) and the status
  // pill stays truthful; reopening the project resubscribes to the run.
  const collapseChat = useCallback(() => {
    setLeftPanelCollapsed(true);
  }, []);

  const handleRename = useCallback(async (id: string, title: string, expectedRevision: number) => {
    const res = await fetch(`${API}/projects/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, expected_revision: expectedRevision }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(extractError(err, `HTTP ${res.status}`));
    }
    await refreshProjects();
  }, [refreshProjects]);

  // ── Version history (read-only; separate from the live project state) ──

  const refreshHistory = useCallback(async (projectId: string) => {
    const token = ++historyReqRef.current;
    // Supersedes any pending snapshot load — end the loading state that
    // request owned, or the panel would stay stuck on "载入快照" once the
    // (now stale) response is discarded.
    setHistoryPanel((prev) => ({ ...prev, loading: true, error: null, viewingLoading: false }));
    try {
      const result = await apiGet(`/projects/${projectId}/versions`);
      if (historyReqRef.current !== token) return; // superseded view intent
      setHistoryPanel((prev) => ({
        ...prev,
        open: true,
        loading: false,
        error: null,
        versions: result.versions || [],
        currentRevision: result.current_revision ?? 0,
      }));
    } catch (err: any) {
      if (historyReqRef.current !== token) return;
      // Keep the panel open so the failure is visible and retryable.
      setHistoryPanel((prev) => ({ ...prev, open: true, loading: false, error: err.message }));
    }
  }, []);

  const handleOpenHistory = useCallback(() => {
    if (!projectId) return;
    if (historyPanel.open) {
      // Toggle: from a snapshot back to the list, from the list to closed.
      // Either way the replaced intent invalidates in-flight responses —
      // and must also end whatever loading state they owned.
      historyReqRef.current++;
      setHistoryPanel((prev) => (
        prev.viewing
          ? { ...prev, viewing: null, error: null, loading: false, viewingLoading: false }
          : HISTORY_CLOSED
      ));
      return;
    }
    setHistoryPanel({ ...HISTORY_CLOSED, open: true, loading: true });
    refreshHistory(projectId);
  }, [projectId, historyPanel.open, refreshHistory]);

  const handleSelectHistoryVersion = useCallback(async (revision: number) => {
    if (!projectId) return;
    const token = ++historyReqRef.current;
    // Supersedes any pending list load — its loading state ends here too.
    setHistoryPanel((prev) => ({ ...prev, viewingLoading: true, error: null, loading: false }));
    try {
      const snap = await apiGet(`/projects/${projectId}/versions/${revision}`);
      if (historyReqRef.current !== token) return; // superseded (closed/switched/another view)
      const viewing: VersionSnapshot = {
        revision: snap.revision,
        title: snap.meta?.title ?? "",
        source: snap.source ?? "manual",
        summary: snap.summary ?? "",
        created_at: snap.created_at ?? "",
        data: snap,
      };
      setHistoryPanel((prev) => ({ ...prev, viewing, viewingLoading: false }));
    } catch (err: any) {
      if (historyReqRef.current !== token) return;
      setHistoryPanel((prev) => ({ ...prev, viewingLoading: false, error: err.message }));
    }
  }, [projectId]);

  const handleBackToHistoryList = useCallback(() => {
    historyReqRef.current++;
    setHistoryPanel((prev) => ({ ...prev, viewing: null, error: null, loading: false, viewingLoading: false }));
  }, []);

  const handleCloseHistory = useCallback(() => {
    historyReqRef.current++;
    setHistoryPanel(HISTORY_CLOSED);
  }, []);

  // ── Card editing (ticket #16) ────────────────────

  const handleEditCard = useCallback((kind: CardKind, id: string) => {
    const pid = projectRef.current;
    if (!pid) return;
    const card = findCard(artifactData, kind, id);
    if (!card) return;
    editOpenSeqRef.current += 1;
    setEditSession({
      seq: editOpenSeqRef.current,
      projectId: pid,
      kind,
      id,
      // The draft's immutable basis: what the user sees is what they edit.
      // Opening/cancelling an editor never touches the pending-review state.
      revision: projectRevision,
      snapshot: card,
    });
  }, [artifactData, projectRevision]);

  const handleSaveCard = useCallback(
    async (
      kind: CardKind,
      id: string,
      changes: Record<string, unknown>,
      expectedRevision: number,
    ): Promise<SaveCardResult> => {
      const pid = projectRef.current;
      if (!pid) return { type: "superseded" };
      const token = ++cardSaveReqRef.current;
      try {
        const res = await fetch(
          `${API}/projects/${pid}/artifacts/${kind}/${encodeURIComponent(id)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ changes, expected_revision: expectedRevision }),
          },
        );
        // A→B→A guard: only the newest save of the CURRENT project session
        // may react to a response — and only inside that session.
        if (token !== cardSaveReqRef.current || projectRef.current !== pid) {
          return { type: "superseded" };
        }
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          const message = extractError(body, `HTTP ${res.status}`);
          const detail = body?.detail;
          if (res.status === 409) {
            return {
              type: "conflict",
              message,
              currentRevision: typeof detail?.current_revision === "number"
                ? detail.current_revision
                : null,
            };
          }
          if (res.status === 422) return { type: "invalid", message };
          return { type: "error", message };
        }
        return { type: "saved", changed: !!body.changed, payload: body };
      } catch (err: any) {
        if (token !== cardSaveReqRef.current || projectRef.current !== pid) {
          return { type: "superseded" };
        }
        return { type: "error", message: err?.message || "网络请求失败，修改未保存" };
      }
    },
    [],
  );

  const handleCardSaved = useCallback((payload: any) => {
    const pid = projectRef.current;
    if (!pid || payload?.project_id !== pid) return;
    // A landed save supersedes every read issued before it: bump the
    // content token so in-flight GETs can no longer adopt (their responses
    // would otherwise pair old artifacts with the new revision).
    contentReqRef.current++;
    if (adoptSnapshot(payload)) {
      // Acknowledge in the CURRENT chat session (append, no epoch bump) —
      // the pending-review status lives in the banner from the adopted
      // snapshot above.
      appendNotice(`✅ 已保存为 r${payload.revision}。`);
    }
    setEditSession(null);
    refreshProjects();
    if (historyPanel.open) refreshHistory(pid);
  }, [adoptSnapshot, appendNotice, refreshProjects, historyPanel.open, refreshHistory]);

  const handleReloadLatestCard = useCallback(async (kind: CardKind, id: string) => {
    const pid = projectRef.current;
    if (!pid) return;
    // Capture WHO launched this reload: project session, edit session, the
    // target card, and the reload order. Any of close / project switch /
    // editor reopen / a newer reload invalidates it — the project id alone
    // is NOT enough (A→B→A makes it match again).
    const launchedFromSeq = editSessionRef.current?.seq ?? null;
    const token = ++reloadReqRef.current;
    try {
      const fullState = await refreshProjectContent(pid);
      if (token !== reloadReqRef.current) return; // a newer reload supersedes this one
      if (projectRef.current !== pid) return; // project switched
      if ((editSessionRef.current?.seq ?? null) !== launchedFromSeq || launchedFromSeq === null) {
        return; // drawer closed, reopened, or moved to another card meanwhile
      }
      if (!fullState) return; // superseded read
      const card = findCard(fullState, kind, id);
      if (!card) {
        // The card vanished from the latest content — nothing to edit.
        setEditSession(null);
        nextNotice("该卡片在最新内容中已不存在，已关闭编辑面板。");
        return;
      }
      // The reload adopts the snapshot for the whole page (artifacts,
      // revision, review from the SAME payload) and remounts the editor on
      // that basis — an explicit user action, never an automatic draft
      // retry. The old drawer's draft dies with its session here.
      if (!adoptSnapshot(fullState)) return;
      editOpenSeqRef.current += 1;
      setEditSession({
        seq: editOpenSeqRef.current,
        projectId: pid,
        kind,
        id,
        revision: fullState.revision,
        snapshot: card,
      });
    } catch (err: any) {
      if (token !== reloadReqRef.current) return;
      if (projectRef.current !== pid) return;
      if ((editSessionRef.current?.seq ?? null) !== launchedFromSeq || launchedFromSeq === null) {
        return; // never surface an abandoned reload's failure to a new session
      }
      nextNotice(`❌ 载入最新内容失败: ${err.message}`);
    }
  }, [refreshProjectContent, nextNotice, adoptSnapshot]);

  // ── Review confirm (ticket #17) ───────────────────

  const handleOpenReviewPanel = useCallback(() => {
    const pid = projectRef.current;
    if (!pid || !reviewState || reviewState.projectId !== pid || reviewState.flags.length === 0) {
      return;
    }
    reviewPanelSeqRef.current += 1;
    setReviewPanel({
      seq: reviewPanelSeqRef.current,
      projectId: pid,
      flags: reviewState.flags,
      revision: reviewState.revision,
    });
  }, [reviewState]);

  // Live mirror of reviewPanel for the async handlers' LAUNCH read (the
  // panel is open and stable when its buttons are clicked); the guards
  // below use reviewPanelSeqRef, which is bumped synchronously on every
  // open/close/reload/switch.
  const reviewPanelRef = useRef<ReviewPanelSession | null>(null);
  useEffect(() => {
    reviewPanelRef.current = reviewPanel;
  }, [reviewPanel]);

  // The confirm POST itself. A landed confirm supersedes every read issued
  // before it (same rule as a card save) and adopts the transaction's exact
  // snapshot; failures keep the panel's selection for the user to retry or
  // re-read — never an automatic retry against a newer revision. Only the
  // confirm launched for the CURRENT project session AND the CURRENT panel
  // generation may react to a response — an abandoned POST may still land
  // server-side (it is a real transaction); the UI then recovers through
  // normal reads and CAS conflicts, never by retrying.
  const handleConfirmReview = useCallback(
    async (selections: ReviewSelection[]): Promise<ConfirmReviewResult> => {
      const session = reviewPanelRef.current;
      const pid = projectRef.current;
      if (!session || !pid || session.projectId !== pid) return { type: "superseded" };
      const atLaunch = {
        projectSession: projectSessionRef.current,
        panelSeq: reviewPanelSeqRef.current,
      };
      const token = ++reviewConfirmReqRef.current;
      const isActive = () =>
        token === reviewConfirmReqRef.current &&
        projectRef.current === pid &&
        projectSessionRef.current === atLaunch.projectSession &&
        reviewPanelSeqRef.current === atLaunch.panelSeq;
      try {
        const res = await fetch(`${API}/projects/${pid}/review/confirm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: session.revision,
            selections,
          }),
        });
        if (!isActive()) return { type: "superseded" };
        const body = await res.json().catch(() => ({}));
        // The body read is async too — re-check before ANY UI write.
        if (!isActive()) return { type: "superseded" };
        if (!res.ok) {
          const message = extractError(body, `HTTP ${res.status}`);
          const detail = body?.detail;
          if (res.status === 409) {
            return {
              type: "conflict",
              message,
              currentRevision: typeof detail?.current_revision === "number"
                ? detail.current_revision
                : null,
            };
          }
          if (res.status === 422) return { type: "invalid", message };
          return { type: "error", message };
        }
        const confirmedCount = Array.isArray(body.confirmed) ? body.confirmed.length : selections.length;
        contentReqRef.current++;
        adoptSnapshot(body);
        appendNotice(
          `✅ 已确认沿用 ${confirmedCount} 项待复核内容（r${body.revision}，不再提示该范围）。`,
        );
        closeReviewPanel();
        refreshProjects();
        if (historyPanel.open) refreshHistory(pid);
        return { type: "confirmed", payload: body, confirmedCount };
      } catch (err: any) {
        if (!isActive()) return { type: "superseded" };
        return { type: "error", message: err?.message || "网络请求失败，确认未保存" };
      }
    },
    [adoptSnapshot, appendNotice, refreshProjects, historyPanel.open, refreshHistory, closeReviewPanel],
  );

  // Conflict re-read, user-driven: adopt the fresh snapshot and re-base the
  // panel on the CURRENT flags (new seq remounts it with a clean selection).
  // Ownership is the same panel generation as the confirm's; a failure is
  // APPENDED to the current chat session (no epoch bump — the chat history
  // and the panel's selection survive), and an abandoned reload's failure
  // is ignored entirely.
  const handleReloadReviewPanel = useCallback(async () => {
    const session = reviewPanelRef.current;
    const pid = projectRef.current;
    if (!session || !pid || session.projectId !== pid) return;
    const atLaunch = {
      projectSession: projectSessionRef.current,
      panelSeq: reviewPanelSeqRef.current,
    };
    const token = ++reviewReloadReqRef.current;
    const isActive = () =>
      token === reviewReloadReqRef.current &&
      projectRef.current === pid &&
      projectSessionRef.current === atLaunch.projectSession &&
      reviewPanelSeqRef.current === atLaunch.panelSeq;
    try {
      const fullState = await refreshProjectContent(pid);
      if (!isActive()) return; // abandoned: must not rebuild the panel
      if (!fullState) return;
      if (!adoptSnapshot(fullState)) return;
      const flags = reviewFlagsOf(fullState);
      if (flags.length === 0) {
        closeReviewPanel();
        appendNotice("当前没有待复核内容。");
        return;
      }
      reviewPanelSeqRef.current += 1;
      setReviewPanel({
        seq: reviewPanelSeqRef.current,
        projectId: pid,
        flags,
        revision: fullState.revision,
      });
    } catch (err: any) {
      if (!isActive()) return; // never surface an abandoned reload's failure
      appendNotice(`❌ 重新读取待复核内容失败: ${err.message}`);
    }
  }, [refreshProjectContent, adoptSnapshot, appendNotice, closeReviewPanel]);

  // 查看 jumps to the flagged artifact's tab (the panel closes; the banner
  // stays for re-opening it).
  const handleViewReviewArtifact = useCallback((artifact: string) => {
    closeReviewPanel();
    const tab = TAB_FOR_REVIEW_ARTIFACT[artifact];
    if (tab) setActiveTab(tab);
  }, [closeReviewPanel]);

  // On mount the URL decides which project is open, so a refresh restores it.
  useEffect(() => {
    if (typeof window !== "undefined" && window.innerWidth < 1280) {
      setProjectListOpen(false);
    }
    const id = new URLSearchParams(window.location.search).get("project");
    if (id) openProject(id);
    else refreshProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div suppressHydrationWarning style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      {/* Project rail */}
      <ProjectList
        projects={projects}
        currentProjectId={projectId}
        open={projectListOpen}
        onToggle={() => setProjectListOpen((v) => !v)}
        onSelect={openProject}
        onNew={startNewProject}
        onRefresh={refreshProjects}
        onRename={handleRename}
      />

      {/* Left Panel: Chat */}
      {!leftPanelCollapsed && (
        <ChatPanel
          projectId={projectId}
          isGenerating={isGenerating}
          setIsGenerating={setIsGenerating}
          projectRevision={projectRevision}
          onGlobalRequested={() => setGlobalOpen(true)}
          projectStatus={projectStatus}
          setProjectStatus={setProjectStatus}
          onArtifactUpdate={handleArtifactUpdate}
          refreshProjectContent={refreshProjectContent}
          onTabSwitch={setActiveTab}
          onCollapse={collapseChat}
          sessionNotice={sessionNotice}
          sessionEpoch={sessionNotice?.epoch ?? 0}
          projectOpenEpoch={projectOpenEpoch}
          isSessionActive={isSessionActive}
          isProjectActive={isProjectActive}
          closeStreamRef={closeStreamRef}
          onProjectCreated={handleProjectCreated}
          onProjectMutated={handleProjectMutated}
        />
      )}

      {/* Collapse toggle */}
      {leftPanelCollapsed && (
        <button
          onClick={() => setLeftPanelCollapsed(false)}
          style={{
            position: "fixed",
            left: projectListOpen ? 248 : 56,
            top: "50%",
            transform: "translateY(-50%)",
            zIndex: 100,
            width: 36,
            height: 48,
            borderRadius: 8,
            border: "1px solid var(--border-default)",
            background: "var(--bg-sidebar)",
            color: "var(--text-secondary)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
          title="展开对话面板"
          aria-label="展开对话面板"
        >
          <PanelLeftOpen size={18} />
        </button>
      )}

      {projectId && <aside key={`${projectId}-${projectOpenEpoch}`} style={{ position: "fixed", right: 16, bottom: 16, zIndex: 110, maxWidth: "min(620px, 90vw)", maxHeight: "80vh", overflow: "auto", background: "var(--bg-sidebar)", border: "1px solid var(--border-default)", padding: 12, borderRadius: 8 }}>
        <button className="btn-secondary" aria-expanded={globalOpen} onClick={() => setGlobalOpen(v => !v)}>全局修改候选</button>
        {globalOpen && <CardCandidates session={{ projectId, seq: projectOpenEpoch, kind: "global", id: "", revision: projectRevision, snapshot: {} }}
          dirty={false} fieldLabels={{ characters: "角色", scenes: "场景", script: "剧本", storyboard: "分镜", visual_highlights: "视觉亮点" }}
          onReload={(snapshot) => { if (snapshot) adoptSnapshot(snapshot); }} onAccepted={(payload) => {
            contentReqRef.current++;
            if (adoptSnapshot(payload)) appendNotice(`✅ 已采用全局候选，保存为 r${payload.revision}。`);
            refreshProjects();
            if (historyPanel.open) refreshHistory(projectId);
          }} />}
      </aside>}

      {/* Right Panel: Structured Output */}
      <ArtifactPanel
        projectId={projectId}
        artifactData={artifactData}
        projectStatus={projectStatus}
        activeTab={activeTab}
        onTabChange={setActiveTab}
        onCollapse={collapseChat}
        onEditCard={projectId ? handleEditCard : undefined}
        reviewNotice={
          reviewState &&
          reviewState.projectId === projectId &&
          reviewState.flags.length > 0 && !(
            reviewHiddenFor?.projectId === projectId &&
            reviewHiddenFor.revision >= reviewState.revision
          )
            ? buildReviewNoticeText(reviewState.flags, reviewState.revision)
            : null
        }
        onDismissReviewNotice={() => {
          if (reviewState) {
            setReviewHiddenFor({ projectId: projectRef.current, revision: reviewState.revision });
          }
        }}
        onOpenReviewPanel={projectId ? handleOpenReviewPanel : undefined}
        projectSessionRef={projectSessionRef}
        isProjectSessionActive={isProjectSessionActive}
        historyPanel={historyPanel}
        onOpenHistory={handleOpenHistory}
        onRefreshHistory={() => projectId && refreshHistory(projectId)}
        onSelectHistoryVersion={handleSelectHistoryVersion}
        onBackToHistoryList={handleBackToHistoryList}
        onCloseHistory={handleCloseHistory}
      />

      {/* Card edit drawer — keyed per open so a new session never inherits a
          previous draft, basis or in-flight save. */}
      {editSession && editSession.projectId === projectId && (
        <CardEditDrawer
          key={editSession.seq}
          session={editSession}
          onSave={handleSaveCard}
          onSaved={handleCardSaved}
          onDiscard={() => setEditSession(null)}
          onReloadLatest={handleReloadLatestCard}
        />
      )}

      {/* Review panel (ticket #17) — keyed per open/reload so a re-based
          session starts with a clean selection. */}
      {reviewPanel && reviewPanel.projectId === projectId && (
        <ReviewPanel
          key={reviewPanel.seq}
          session={reviewPanel}
          onConfirm={handleConfirmReview}
          onReloadLatest={handleReloadReviewPanel}
          onClose={closeReviewPanel}
          onViewArtifact={handleViewReviewArtifact}
        />
      )}
    </div>
  );
}
