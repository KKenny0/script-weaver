"use client";

import React, { useState, useCallback, useRef, useEffect } from "react";
import { PanelLeftOpen } from "lucide-react";
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

function buildReviewNotice(payload: any): string | null {
  const flags: any[] = payload?.review?.review_flags || [];
  const artifacts = [...new Set(flags.map((f) => REVIEW_ARTIFACT_LABELS[f.artifact] || f.artifact))];
  const base = `已保存为 r${payload.revision}。`;
  if (artifacts.length === 0) return base;
  return (
    `${base}因本次手工编辑，以下已有内容被保守标记为「待复核」：${artifacts.join("、")}` +
    `（原因：上游内容被直接修改；是否沿用待后续确认，不自动重生成）。`
  );
}

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
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);

  // Late-response guard: async handlers compare against the project that is
  // open *now*, so a response for a previously selected project can never
  // overwrite the current one.
  const projectRef = useRef("");
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

  useEffect(() => {
    projectRef.current = projectId;
  }, [projectId]);

  const nextNotice = useCallback((text: string) => {
    sessionEpochRef.current += 1;
    setSessionNotice({ epoch: sessionEpochRef.current, text });
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

  const refreshProjects = useCallback(async () => {
    try {
      setProjects(await apiGet("/projects"));
    } catch (err) {
      console.error("Failed to list projects:", err);
    }
  }, []);

  // Page opens and chat refreshes share ownership, including failed reads.
  // A superseded response returns null; only the newest read can write UI.
  const refreshProjectContent = useCallback(async (id: string) => {
    const token = ++contentReqRef.current;
    try {
      const fullState = await apiGet(`/projects/${id}`);
      if (token !== contentReqRef.current) return null;
      // Whatever renders from this read is exactly revision N's content —
      // card edits opened on it use this as their CAS basis.
      setProjectRevision(fullState.revision);
      return fullState;
    } catch (err) {
      if (token !== contentReqRef.current) return null;
      throw err;
    }
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
    setArtifactData({});
    setActiveTab("outline");
    setProjectStatus("idle");
    setHistoryPanel(HISTORY_CLOSED);
    setProjectRevision(0);
    setEditSession(null);
    setReviewNotice(null);
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
      setArtifactData(pickArtifactData(fullState));
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
  }, [nextNotice, refreshProjects, isSessionActive, refreshProjectContent]);

  const startNewProject = useCallback(() => {
    closeStreamRef.current();
    setIsGenerating(false);
    setProjectId("");
    projectRef.current = "";
    contentReqRef.current++;
    setArtifactData({});
    setActiveTab("outline");
    setProjectStatus("idle");
    setHistoryPanel(HISTORY_CLOSED);
    setProjectRevision(0);
    setEditSession(null);
    setReviewNotice(null);
    historyReqRef.current++;
    window.history.replaceState(null, "", "/");
    nextNotice("🆕 已开始一个新项目，输入故事想法开始生成。");
  }, [nextNotice]);

  // A generation that just created its project keeps the current chat
  // session; only the URL and the project list change.
  const handleProjectCreated = useCallback((id: string) => {
    contentReqRef.current++;
    setProjectId(id);
    projectRef.current = id;
    window.history.replaceState(null, "", `/?project=${encodeURIComponent(id)}`);
    refreshProjects();
  }, [refreshProjects]);

  const handleProjectMutated = useCallback(() => {
    refreshProjects();
  }, [refreshProjects]);

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
    setReviewNotice(null);
    setEditSession({
      seq: editOpenSeqRef.current,
      projectId: pid,
      kind,
      id,
      // The draft's immutable basis: what the user sees is what they edit.
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
    // The server snapshot is the truth: adopt it wholesale and move the
    // displayed revision so the next edit opens on the saved basis.
    setArtifactData(pickArtifactData(payload));
    setProjectRevision(payload.revision);
    setReviewNotice(buildReviewNotice(payload));
    setEditSession(null);
    refreshProjects();
    if (historyPanel.open) refreshHistory(pid);
  }, [refreshProjects, historyPanel.open, refreshHistory]);

  const handleReloadLatestCard = useCallback(async (kind: CardKind, id: string) => {
    const pid = projectRef.current;
    if (!pid) return;
    try {
      const fullState = await refreshProjectContent(pid);
      if (!fullState || projectRef.current !== pid) return;
      const card = findCard(fullState, kind, id);
      if (!card) {
        // The card vanished from the latest content — nothing to edit.
        setEditSession(null);
        nextNotice("该卡片在最新内容中已不存在，已关闭编辑面板。");
        return;
      }
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
      nextNotice(`❌ 载入最新内容失败: ${err.message}`);
    }
  }, [refreshProjectContent, nextNotice]);

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
          projectStatus={projectStatus}
          setProjectStatus={setProjectStatus}
          onArtifactUpdate={setArtifactData}
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

      {/* Right Panel: Structured Output */}
      <ArtifactPanel
        projectId={projectId}
        artifactData={artifactData}
        projectStatus={projectStatus}
        activeTab={activeTab}
        onTabChange={setActiveTab}
        onCollapse={collapseChat}
        onEditCard={projectId ? handleEditCard : undefined}
        reviewNotice={reviewNotice}
        onDismissReviewNotice={() => setReviewNotice(null)}
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
    </div>
  );
}
