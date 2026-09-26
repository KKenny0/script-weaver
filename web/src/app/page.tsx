"use client";

import React, { useState, useCallback, useRef, useEffect } from "react";
import { PanelLeftOpen } from "lucide-react";
import ChatPanel from "./components/ChatPanel";
import ArtifactPanel from "./components/ArtifactPanel";
import ProjectList, { ProjectSummary } from "./components/ProjectList";
import { HistoryPanelState, VersionSnapshot } from "./components/VersionHistory";
import { ArtifactData } from "./components/ArtifactContent";

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
  const [historyPanel, setHistoryPanel] = useState<HistoryPanelState>(HISTORY_CLOSED);

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
  // Set by ChatPanel; closing the stream ends the subscription only (the
  // backend owns the task).
  const closeStreamRef = useRef<() => void>(() => {});

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

  const openProject = useCallback(async (id: string) => {
    closeStreamRef.current();
    setIsGenerating(false);
    setProjectId(id);
    projectRef.current = id;
    setArtifactData({});
    setActiveTab("outline");
    setProjectStatus("idle");
    setHistoryPanel(HISTORY_CLOSED);
    historyReqRef.current++; // in-flight history responses belong to the old project
    window.history.replaceState(null, "", `/?project=${encodeURIComponent(id)}`);
    nextNotice("📂 正在打开项目…");
    try {
      const fullState = await apiGet(`/projects/${id}`);
      if (projectRef.current !== id) return; // stale response, another project is open
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
      if (projectRef.current !== id) return;
      setProjectStatus("error");
      nextNotice(`❌ 打开项目失败: ${err.message}`);
    }
    refreshProjects();
  }, [nextNotice, refreshProjects]);

  const startNewProject = useCallback(() => {
    closeStreamRef.current();
    setIsGenerating(false);
    setProjectId("");
    projectRef.current = "";
    setArtifactData({});
    setActiveTab("outline");
    setProjectStatus("idle");
    setHistoryPanel(HISTORY_CLOSED);
    historyReqRef.current++;
    window.history.replaceState(null, "", "/");
    nextNotice("🆕 已开始一个新项目，输入故事想法开始生成。");
  }, [nextNotice]);

  // A generation that just created its project keeps the current chat
  // session; only the URL and the project list change.
  const handleProjectCreated = useCallback((id: string) => {
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
          onTabSwitch={setActiveTab}
          onCollapse={collapseChat}
          sessionNotice={sessionNotice}
          sessionEpoch={sessionNotice?.epoch ?? 0}
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
        historyPanel={historyPanel}
        onOpenHistory={handleOpenHistory}
        onRefreshHistory={() => projectId && refreshHistory(projectId)}
        onSelectHistoryVersion={handleSelectHistoryVersion}
        onBackToHistoryList={handleBackToHistoryList}
        onCloseHistory={handleCloseHistory}
      />
    </div>
  );
}
