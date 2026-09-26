"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import {
  Send, Sparkles, Wand2, PanelLeftClose,
  Loader2, Square,
} from "lucide-react";

interface Message {
  role: "user" | "assistant";
  content: string;
  timestamp: number;
}

interface SessionNotice {
  epoch: number;
  text: string;
}

const API = "/api";

function extractError(err: any, fallback = "请求失败"): string {
  const d = err?.detail;
  if (typeof d === "string") return d;
  if (d?.message) return d.message;
  return err?.message || fallback;
}

async function apiPost(path: string, body?: object) {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const e: any = new Error(extractError(err, `HTTP ${res.status}`));
    e.code = err?.detail?.code;
    throw e;
  }
  return res.json();
}

async function apiGet(path: string) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const e: any = new Error(extractError(err, `HTTP ${res.status}`));
    e.code = err?.detail?.code; // e.g. "no_run": a fact, not a failure
    throw e;
  }
  return res.json();
}

interface ChatPanelProps {
  projectId: string;
  isGenerating: boolean;
  setIsGenerating: (v: boolean) => void;
  projectStatus: "idle" | "running" | "complete" | "error";
  setProjectStatus: (s: "idle" | "running" | "complete" | "error") => void;
  onArtifactUpdate: (data: Record<string, any>) => void;
  refreshProjectContent: (id: string) => Promise<Record<string, any> | null>;
  onTabSwitch: (tab: string) => void;
  onCollapse: () => void;
  sessionNotice: SessionNotice | null;
  sessionEpoch: number;
  /** Bumped when the user re-clicks the already-open project: re-check run
   * state without resetting a live observation. */
  projectOpenEpoch: number;
  isSessionActive: (epoch: number) => boolean;
  isProjectActive: (id: string) => boolean;
  closeStreamRef: React.MutableRefObject<() => void>;
  onProjectCreated: (id: string) => void;
  onProjectMutated: () => void;
}

export default function ChatPanel({
  projectId,
  isGenerating, setIsGenerating,
  projectStatus, setProjectStatus,
  onArtifactUpdate, onTabSwitch,
  refreshProjectContent,
  onCollapse,
  sessionNotice,
  sessionEpoch,
  projectOpenEpoch,
  isSessionActive,
  isProjectActive,
  closeStreamRef,
  onProjectCreated,
  onProjectMutated,
}: ChatPanelProps) {
  const [inputValue, setInputValue] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [showSkillsPanel, setShowSkillsPanel] = useState(false);
  const [availableSkills, setAvailableSkills] = useState<any[]>([]);
  const [activeSkillIds, setActiveSkillIds] = useState<Set<string>>(new Set());
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [stopRequested, setStopRequested] = useState(false);
  // A finished run whose content refresh failed: offer a GET-only retry.
  const [contentRefreshPending, setContentRefreshPending] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  // The run this panel observes server-side (ticket #14): generation lives
  // in the backend, so the subscription is only a view onto it.
  const activeRunIdRef = useRef<string | null>(null);
  const sessionEpochRef = useRef(0);
  // Project-session sequence: bumped ONLY when the observed project really
  // changes (a switch — including A→B→A — or a fresh generation creating
  // its project), never for follow-up notices inside one open. Async
  // continuations capture it and become stale the moment the panel moves to
  // another project session, while the current session's own notices stay
  // harmless.
  const openSeqRef = useRef(0);
  const lastProjectRef = useRef<string | null>(null);
  // Run ids whose outcome this panel already presented (live done or
  // terminal recovery): re-clicks and re-opens never re-announce them.
  const presentedRunsRef = useRef<Set<string>>(new Set());
  // False once this instance unmounts (chat collapse). Every async
  // continuation re-checks it: an unmounted instance no longer owns any UI,
  // so its late responses and events must never write state, open a
  // subscription, or move the project/URL.
  const mountedRef = useRef(false);
  const noticeEpochRef = useRef<number>(-1);
  const noticeShownKeyRef = useRef<string>("");

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);
  useEffect(() => { sessionEpochRef.current = sessionEpoch; }, [sessionEpoch]);
  useEffect(() => {
    if (lastProjectRef.current !== projectId) {
      lastProjectRef.current = projectId;
      openSeqRef.current += 1;
    }
  }, [projectId]);

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  useEffect(() => {
    apiGet("/skills?stage=structuring").then(setAvailableSkills).catch(console.error);
  }, []);

  // The subscription's lifetime is bound to this instance: unmounting only
  // closes the progress view — the run itself keeps running server-side
  // (ticket #14) and is found again by reopening the project. The generating
  // flag ends with the view that owned it.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      setIsGenerating(false);
    };
  }, [setIsGenerating]);

  // Let the page close the subscription when the user switches projects.
  // Closing ends the view only; the background run continues.
  useEffect(() => {
    const close = () => {
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      activeRunIdRef.current = null;
      setActiveRunId(null);
      setStopRequested(false);
      setIsGenerating(false);
    };
    closeStreamRef.current = close;
    return () => {
      if (closeStreamRef.current === close) closeStreamRef.current = () => {};
    };
  }, [setIsGenerating]);

  // Session notices from the page (project opened / new project). A new epoch
  // resets the conversation; follow-up texts with the same epoch append.
  // The shown-key guard makes this idempotent under React StrictMode's
  // double-invoked effects in dev builds.
  useEffect(() => {
    if (!sessionNotice) return;
    const key = `${sessionNotice.epoch}:${sessionNotice.text}`;
    if (noticeShownKeyRef.current === key) return;
    noticeShownKeyRef.current = key;
    const isNewEpoch = sessionNotice.epoch !== noticeEpochRef.current;
    noticeEpochRef.current = sessionNotice.epoch;
    const notice: Message = { role: "assistant", content: sessionNotice.text, timestamp: Date.now() };
    setMessages((prev) => (isNewEpoch ? [notice] : [...prev, notice]));
  }, [sessionNotice]);

  // ── Handlers ──────────────────────────────────

  const applyFullState = useCallback((fullState: any) => {
    onArtifactUpdate({
      refined_idea: fullState.refined_idea,
      outline: fullState.outline,
      characters: fullState.characters,
      scenes: fullState.scenes,
      art_style: fullState.art_style,
      script: fullState.script,
      storyboard: fullState.storyboard,
      visual_highlights: fullState.visual_highlights,
    });
  }, [onArtifactUpdate]);

  // ── Unified run-outcome presentation (review R3: one path, no drift) ──
  //
  // The live SSE ``done`` event and the terminal recovery of ``runs/latest``
  // share this single path: status, message wording, artifact refresh and
  // the refresh-failed retry entry can never diverge again. Every write is
  // guarded by the session epoch captured at the start, the project id, the
  // component's lifetime AND subscription ownership (a listener whose
  // subscription was closed or replaced writes nothing — not even a
  // message), plus the content token above.
  const presentRunOutcome = useCallback(async (
    pid: string,
    outcome: { status?: string; error?: string | null },
    source: "done" | "restore",
    origin?: { seq: number; evtSource: EventSource | null },
  ): Promise<boolean> => {
    const started = origin ?? { seq: openSeqRef.current, evtSource: null };
    // R6: done.status alone decides the run's outcome; the follow-up GET
    // only decides whether the CONTENT view was refreshed.
    const status: "succeeded" | "cancelled" | "failed" | "interrupted" =
      outcome.status === "cancelled" || outcome.status === "failed" || outcome.status === "interrupted"
        ? outcome.status
        : "succeeded"; // legacy done {} = success
    const stillOwns = () =>
      mountedRef.current &&
      openSeqRef.current === started.seq &&
      isProjectActive(pid) &&
      !(started.evtSource && eventSourceRef.current && eventSourceRef.current !== started.evtSource);
    if (!stillOwns()) return false;
    // The run has ended even if a newer content read supersedes this one.
    setProjectStatus(status === "succeeded" ? "complete" : status === "cancelled" ? "idle" : "error");
    let fullState: any = null;
    let superseded = false;
    try {
      fullState = await refreshProjectContent(pid);
      if (fullState === null) superseded = true; // a newer refresh owns the UI
    } catch (fetchErr) {
      console.error("Failed to fetch final state:", fetchErr);
    }
    if (superseded) return false;
    if (!stillOwns()) return false; // async continuation returned into another session/view
    if (fullState) {
      applyFullState(fullState);
      onProjectMutated();
      setContentRefreshPending(false);
    }
    const refreshFailNote = fullState ? "" : "\n（内容刷新失败，已保留当前显示；可点击「刷新内容」重试。）";

    if (status === "succeeded") {
      if (fullState) {
        setMessages((prev) => [...prev, {
          role: "assistant",
          content: source === "done"
            ? `🎉 全部生成完成！\n\n${formatResultSummary(fullState)}`
            : `🔄 已恢复上次生成的结果：\n\n${formatResultSummary(fullState)}`,
          timestamp: Date.now(),
        }]);
        if (source === "done") {
          if (fullState.outline) onTabSwitch("outline");
          else if (fullState.characters) onTabSwitch("characters");
          else if (fullState.script) onTabSwitch("script");
        }
      } else {
        setContentRefreshPending(true);
        setMessages((prev) => [...prev, {
          role: "assistant",
          content: source === "done"
            ? "✅ 生成已完成，但内容刷新失败。已保留当前显示的内容，可点击下方「刷新内容」重试。"
            : "✅ 上次生成已完成，但内容刷新失败。已保留当前显示的内容，可点击下方「刷新内容」重试。",
          timestamp: Date.now(),
        }]);
      }
    } else if (status === "cancelled") {
      setProjectStatus(fullState ? "complete" : "idle");
      if (!fullState) setContentRefreshPending(true);
      setMessages((prev) => [...prev, {
        role: "assistant",
        content: source === "done"
          ? `🛑 生成已停止。已完成并保存的阶段保留在项目中，可继续修改或重新生成。${refreshFailNote}`
          : `ℹ️ 上次生成已停止；已保存并同步完成的阶段。${refreshFailNote}`,
        timestamp: Date.now(),
      }]);
    } else {
      // failed / interrupted
      if (!fullState) setContentRefreshPending(true);
      const reason = outcome.error
        || (status === "interrupted" ? "服务在生成期间重启，运行已中断" : "生成失败");
      setMessages((prev) => [...prev, {
        role: "assistant",
        content: source === "done"
          ? `❌ 生成未完成：${reason}${fullState ? "\n已完成的阶段已保存，可重新提交生成。" : refreshFailNote}`
          : `ℹ️ 上次生成未完成（${reason}）。已同步已保存的阶段，可重新提交生成。${refreshFailNote}`,
        timestamp: Date.now(),
      }]);
    }
    return true;
  }, [isProjectActive, refreshProjectContent, applyFullState, onProjectMutated, onTabSwitch, setProjectStatus]);

  // ── Background run subscription (ticket #14) ─────────

  // Observe a server-owned run. The EventSource is only a view: closing it
  // (unmount, project switch, network drop) never cancels the run — only
  // the explicit stop button or a backend shutdown ends it.
  const subscribeRun = useCallback((pid: string, runId: string) => {
    const evtSource = new EventSource(`${API}/projects/${pid}/runs/${runId}/events`);
    eventSourceRef.current = evtSource;
    activeRunIdRef.current = runId;
    setActiveRunId(runId);
    setStopRequested(false);
    setIsGenerating(true);

    // The subscription belongs to (this instance, this session, this
    // project) AND must still be the CURRENT subscription: a listener whose
    // connection was closed or replaced writes nothing at all — not even a
    // message. The connection itself is closed by its owner, never by a
    // stale listener.
    const isStaleEvent = () =>
      !mountedRef.current ||
      !isProjectActive(pid) ||
      eventSourceRef.current !== evtSource;

    // One progress renderer for live events AND snapshot replays: the
    // latest assistant message is replaced (never duplicated), so repeated
    // snapshots after a reconnect never stack up messages.
    const renderProgress = (text: string) => {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last?.role === "assistant") {
          return [...prev.slice(0, -1), { ...last, content: text, timestamp: Date.now() }];
        }
        return [...prev, { role: "assistant", content: text, timestamp: Date.now() }];
      });
    };

    // R7: the first event on every (re)connect is the run's snapshot —
    // show where the run is right now instead of waiting for the next
    // progress event. Terminal snapshots are handled by the done event.
    evtSource.addEventListener("status", (e: MessageEvent) => {
      if (isStaleEvent()) return;
      const data = JSON.parse(e.data);
      if (data.status !== "running" && data.status !== "stopping") return;
      const steps = Array.isArray(data.completed_steps) ? data.completed_steps.length : 0;
      const text =
        data.last_progress?.message ||
        (steps > 0 ? `生成进行中，已完成 ${steps} 个阶段…` : "生成正在后台进行…");
      renderProgress(text);
    });

    evtSource.addEventListener("progress", (e: MessageEvent) => {
      if (isStaleEvent()) return;
      const data = JSON.parse(e.data);
      renderProgress(data.message || (data.result_summary ? `✅ ${data.result_summary.details.join(" | ")}` : ""));
    });

    evtSource.addEventListener("done", async (e: MessageEvent) => {
      if (isStaleEvent()) return;
      const seqAtEvent = openSeqRef.current;
      const doneData = JSON.parse(e.data);
      // The view ends here; the run's outcome goes through the ONE shared
      // presentation path (also used by terminal recovery), guarded by this
      // subscription's identity so a closed or replaced connection can
      // never write — not even its message.
      evtSource.close();
      eventSourceRef.current = null;
      activeRunIdRef.current = null;
      setActiveRunId(null);
      setIsGenerating(false);

      const presented = await presentRunOutcome(pid, doneData, "done", {
        seq: seqAtEvent,
        evtSource,
      });
      if (presented && doneData.run_id) {
        presentedRunsRef.current.add(doneData.run_id);
      }
    });

    evtSource.onerror = () => {
      if (isStaleEvent()) return;
      if (evtSource.readyState === EventSource.CLOSED) {
        // The stream ended without a done event and will not retry. That is
        // a LOST VIEW, not a failed run: generation keeps running in the
        // backend. Clear the observation markers so re-opening the project
        // (or the panel's restore path) can re-query and resubscribe.
        evtSource.close();
        eventSourceRef.current = null;
        activeRunIdRef.current = null;
        setActiveRunId(null);
        setStopRequested(false);
        setIsGenerating(false);
        setMessages((prev) => [...prev, { role: "assistant", content: "⚠️ 与生成进度的连接已断开。生成仍在后台进行，不会因此取消；重新点击当前项目或刷新页面可重新连接进度。", timestamp: Date.now() }]);
      } else {
        // Transient drop: the browser reconnects on its own and the server
        // replays the current snapshot on reopen — show it, keep waiting.
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant") {
            return [...prev.slice(0, -1), { ...last, content: "连接中断，正在重新连接生成进度…", timestamp: Date.now() }];
          }
          return prev;
        });
      }
    };
  }, [isProjectActive, presentRunOutcome, setIsGenerating]);

  // Opening a project finds its real progress: an active run is resubscribed
  // (the page refresh / close / switch never cancelled it); a TERMINAL run —
  // it finished while this page wasn't watching — restores its outcome AND
  // content through the same presentation path the live done event uses.
  // Re-clicks on the CURRENT project re-run this check via projectOpenEpoch
  // — a live observation is kept untouched, a lost view is recovered — while
  // other switches go through the page's full reset path.
  useEffect(() => {
    if (!projectId) return;
    if (eventSourceRef.current || activeRunIdRef.current) return; // already observing
    const seqAtStart = openSeqRef.current;
    let cancelled = false;
    (async () => {
      let run: any;
      try {
        run = await apiGet(`/projects/${projectId}/runs/latest`);
      } catch (err: any) {
        if (cancelled || !mountedRef.current || !isProjectActive(projectId) || openSeqRef.current !== seqAtStart) return;
        if (err?.code === "no_run") return; // no generation history yet — a fact, not a failure
        // A real query failure must be visible and retryable (a re-click
        // re-runs this check); it must not be mistaken for "no run".
        setMessages((prev) => [...prev, { role: "assistant", content: `⚠️ 查询生成进度失败：${(err as Error).message}。重新点击当前项目可重试。`, timestamp: Date.now() }]);
        return;
      }
      if (cancelled || !mountedRef.current || !isProjectActive(projectId) || openSeqRef.current !== seqAtStart) return;
      if (run.status === "running" || run.status === "stopping") {
        setProjectStatus("running");
        setMessages((prev) => [...prev, { role: "assistant", content: "🔄 该项目正在后台生成，已重新连接进度。", timestamp: Date.now() }]);
        subscribeRun(projectId, run.run_id);
        return;
      }
      if (presentedRunsRef.current.has(run.run_id)) return; // already announced once
      const presented = await presentRunOutcome(projectId, run, "restore");
      if (presented && run.run_id) {
        presentedRunsRef.current.add(run.run_id);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, projectOpenEpoch]);

  // A pending content-refresh retry belongs to the session that produced
  // it: switching sessions (a new epoch) clears it so a stale retry entry
  // never leaks into the next project's chat. Re-clicking the CURRENT
  // project keeps it (no epoch bump, nothing invalidated).
  useEffect(() => {
    setContentRefreshPending(false);
  }, [sessionEpoch]);

  // R6: read-only content refresh — a finished run's outcome stands; this
  // only re-reads the project (never submits another generation). Guarded
  // like every other async write: session epoch, project, mount state, and
  // the newest-refresh token.
  const handleRefreshContent = useCallback(async () => {
    if (!projectId) return;
    const seqAtStart = openSeqRef.current;
    try {
      const fullState = await refreshProjectContent(projectId);
      if (fullState === null) return; // superseded by a newer refresh
      if (!mountedRef.current || openSeqRef.current !== seqAtStart || !isProjectActive(projectId)) return;
      applyFullState(fullState);
      onProjectMutated();
      setContentRefreshPending(false);
      setMessages((prev) => [...prev, { role: "assistant", content: "🔄 项目内容已刷新。", timestamp: Date.now() }]);
    } catch (err) {
      if (!mountedRef.current || openSeqRef.current !== seqAtStart || !isProjectActive(projectId)) return;
      setMessages((prev) => [...prev, { role: "assistant", content: `⚠️ 刷新仍失败：${(err as Error).message}`, timestamp: Date.now() }]);
    }
  }, [projectId, isProjectActive, refreshProjectContent, applyFullState, onProjectMutated]);

  const handleStop = useCallback(async () => {
    const runId = activeRunIdRef.current;
    if (!runId || !projectId) return;
    setStopRequested(true);
    try {
      await apiPost(`/projects/${projectId}/runs/${runId}/stop`, {});
      // The done(cancelled) event closes the view; repeated clicks are
      // idempotent server-side.
    } catch (err: any) {
      if (!mountedRef.current || !isProjectActive(projectId)) return;
      setStopRequested(false);
      setMessages((prev) => [...prev, { role: "assistant", content: `⚠️ 停止请求失败：${err.message}。生成仍在进行。`, timestamp: Date.now() }]);
    }
  }, [projectId, isProjectActive]);


  const handleGenerate = useCallback(async () => {
    if (!inputValue.trim() || isGenerating) return;

    const ideaText = inputValue.trim();
    const originalInput = inputValue;
    const epochAtStart = sessionEpoch;
    const userMsg: Message = { role: "user", content: ideaText, timestamp: Date.now() };
    setMessages((prev) => [...prev, userMsg]);
    setIsGenerating(true);
    setProjectStatus("running");
    onArtifactUpdate({});

    setMessages((prev) => [...prev, { role: "assistant", content: "正在分析创意，启动 Pipeline...", timestamp: Date.now() }]);

    try {
      const proj = await apiPost("/projects", { user_input: ideaText, auto_approve_gates: true, active_skills: {} });
      if (!mountedRef.current || !isSessionActive(epochAtStart)) {
        // Stale continuation: this instance unmounted (chat collapsed) or the
        // session moved on while the creation was in flight. The project
        // exists server-side, so refresh the list, but do not touch the
        // current session, URL or draft — and never open a subscription from
        // an instance that no longer owns the chat UI.
        onProjectMutated();
        return;
      }

      // Submit the run (ticket #14): the request_key makes double clicks and
      // network retries of this one intent idempotent server-side.
      const submitted = await apiPost(`/projects/${proj.project_id}/generate`, {
        request_key: crypto.randomUUID(),
        user_input: ideaText,
      });
      if (!mountedRef.current || !isSessionActive(epochAtStart)) {
        onProjectMutated();
        return;
      }

      // The run exists in the backend from here on — consume the input, but
      // only if the user has not started typing a new draft meanwhile.
      setInputValue((prev) => (prev === originalInput ? "" : prev));
      onProjectCreated(proj.project_id);
      setProjectStatus("running");
      setMessages((prev) => [...prev, { role: "assistant", content: submitted.created ? "生成已提交，正在后台运行。可以离开此页，进度会自动保存。" : "检测到该项目已有进行中的生成，已连接其进度。", timestamp: Date.now() }]);
      subscribeRun(proj.project_id, submitted.run.run_id);
    } catch (err: any) {
      if (!mountedRef.current || !isSessionActive(epochAtStart)) return; // late failure: not this session's concern
      setIsGenerating(false);
      setProjectStatus("error");
      // Keep inputValue so the user's text is not lost on failure.
      setMessages((prev) => [...prev, { role: "assistant", content: `❌ 错误: ${err.message}`, timestamp: Date.now() }]);
    }
  }, [inputValue, isGenerating, sessionEpoch, isSessionActive, isProjectActive, subscribeRun, onProjectCreated, onProjectMutated, setProjectStatus, setIsGenerating, onArtifactUpdate]);

  const handleRefine = useCallback(async () => {
    if (!projectId || !inputValue.trim() || isGenerating) return;

    const refineText = inputValue.trim();
    const originalInput = inputValue;
    const seqAtStart = openSeqRef.current;
    setMessages((prev) => [
      ...prev,
      { role: "user", content: `[修改] ${refineText}`, timestamp: Date.now() },
      { role: "assistant", content: "正在应用修改...", timestamp: Date.now() },
    ]);

    try {
      const body = await apiPost(`/projects/${projectId}/refine`, { message: refineText });
      if (!mountedRef.current || openSeqRef.current !== seqAtStart || !isProjectActive(projectId)) return;
      // The change is saved server-side from here on. Consume the submitted
      // instruction only if the composer still holds it; a draft typed while
      // the model was processing must survive.
      setInputValue((prev) => (prev === originalInput ? "" : prev));

      let reloaded = false;
      try {
        const fullState = await refreshProjectContent(projectId);
        if (fullState === null) return;
        if (!mountedRef.current || openSeqRef.current !== seqAtStart || !isProjectActive(projectId)) return;
        applyFullState(fullState);
        onProjectMutated();
        reloaded = true;
      } catch (fetchErr) {
        // Saved, but the reload failed — the catch must NOT write anything
        // itself; the shared exit guard below decides whether this instance
        // still owns the chat.
        console.error("Failed to reload project:", fetchErr);
      }
      // Common exit after the refresh settled, success or failure: the
      // outcome message may only be written while this instance is still
      // mounted AND the session/project are still current — otherwise a
      // late refresh failure would splice A's summary into B's chat and
      // delete B's latest message (an A→B→A round trip included).
      if (!mountedRef.current || openSeqRef.current !== seqAtStart || !isProjectActive(projectId)) return;

      const summary = formatChangeSummary(body);
      const outcome = summary
        ? `✅ 修改已保存。\n\n${summary}`
        : "修改请求已返回，但未检测到内容差异；未产生新版本。";
      const content = reloaded
        ? outcome
        : `✅ 修改已保存，但刷新项目显示失败；请手动刷新页面查看最新内容，无需重新提交。\n\n${summary}`;
      setMessages((prev) => [...prev.slice(0, -1), { role: "assistant", content, timestamp: Date.now() }]);
    } catch (err: any) {
      if (!mountedRef.current || openSeqRef.current !== seqAtStart || !isProjectActive(projectId)) return; // late failure
      // Keep inputValue so the user's text is not lost on failure.
      if (err?.code === "revision_conflict") {
        setMessages((prev) => [...prev.slice(0, -1), { role: "assistant", content: "⚠️ 项目已在其他窗口被修改，已为你重新加载最新内容。请基于最新内容重试修改。", timestamp: Date.now() }]);
        try {
          const fullState = await refreshProjectContent(projectId);
          if (fullState === null) return;
          if (mountedRef.current && openSeqRef.current === seqAtStart && isProjectActive(projectId)) applyFullState(fullState);
        } catch (fetchErr) {
          console.error("Failed to reload project:", fetchErr);
        }
      } else if (REFINE_UNAPPLIED_CODES.has(err?.code)) {
        // The backend refused the modification (no substantive change,
        // constraint violation, unusable routing, missing target): nothing
        // was saved, so show the reason and keep the draft retryable.
        setMessages((prev) => [...prev.slice(0, -1), { role: "assistant", content: `⚠️ 未应用修改：${err.message}\n原文未变动，可调整指令后重试。`, timestamp: Date.now() }]);
      } else {
        setMessages((prev) => [...prev.slice(0, -1), { role: "assistant", content: `❌ 修改失败: ${err.message}`, timestamp: Date.now() }]);
      }
    }
  }, [projectId, inputValue, isGenerating, isProjectActive, refreshProjectContent, applyFullState, onProjectMutated]);

  const toggleSkill = (skillId: string) => {
    setActiveSkillIds((prev) => {
      const next = new Set(prev);
      if (next.has(skillId)) next.delete(skillId); else next.add(skillId);
      return next;
    });
  };

  // ── Render ────────────────────────────────────

  return (
    <div style={{
      width: 420, minWidth: 0, maxWidth: 600, flexShrink: 0,
      borderRight: "1px solid var(--border-default)",
      display: "flex", flexDirection: "column",
      background: "var(--bg-sidebar)",
      transition: "width 0.25s ease, background-color 0.3s ease",
      overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{
        padding: "16px 20px",
        borderBottom: "1px solid var(--border-default)",
        display: "flex",
        alignItems: "center",
        gap: 10,
        flexShrink: 0,
      }}>
        <Sparkles size={22} style={{ color: "var(--brand-primary)" }} />
        <span style={{ fontWeight: 600, fontSize: 15 }}>Script-Weaver</span>
        <button onClick={() => setShowSkillsPanel(!showSkillsPanel)} style={{
          marginLeft: "auto", padding: "4px 10px", borderRadius: 20,
          border: showSkillsPanel ? "1px solid var(--brand-primary)" : "1px solid var(--border-default)",
          background: showSkillsPanel ? "var(--brand-soft)" : "transparent",
          color: showSkillsPanel ? "var(--brand-primary)" : "var(--text-secondary)",
          cursor: "pointer", fontSize: 12, transition: "all 0.15s ease",
        }}>
          <Wand2 size={12} style={{ display: "inline", marginRight: 4 }} />Skills
        </button>
        <button onClick={onCollapse} className="btn-ghost" style={{ width: 32, height: 32 }} title="折叠对话面板" aria-label="折叠对话面板">
          <PanelLeftClose size={16} />
        </button>
      </div>

      {/* Skills panel */}
      {showSkillsPanel && (
        <div style={{ padding: "8px 16px", borderBottom: "1px solid var(--border-default)", flexShrink: 0 }}>
          <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginBottom: 6 }}>激活的 Skills ({activeSkillIds.size})</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {availableSkills.map((skill: any) => (
              <button key={skill.id} onClick={() => toggleSkill(skill.id)} className={`tag ${activeSkillIds.has(skill.id) ? "active" : ""}`}>
                {activeSkillIds.has(skill.id) && "✓ "}{skill.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Messages area */}
      <div ref={messagesEndRef} style={{ flex: 1, overflowY: "auto", padding: "16px 20px", display: "flex", flexDirection: "column", gap: 12 }}>
        {messages.length === 0 && (
          <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", color: "var(--text-tertiary)", textAlign: "center", gap: 12 }}>
            <div style={{ width: 80, height: 80, borderRadius: 20, background: "var(--bg-surface-3)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 32 }}>✨</div>
            <p style={{ fontSize: 15, fontWeight: 500, color: "var(--text-primary)" }}>开始编织这一集的故事</p>
            <p style={{ fontSize: 13 }}>输入 idea、梗概或分集大纲，ScriptWeaver 将生成完整剧本结构与分镜脚本</p>
            <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "center" }}>
              {["从一句 idea 开始", "从分集大纲开始", "从已有剧本改写"].map((t) => (
                <span key={t} className="tag">{t}</span>
              ))}
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} style={{ alignSelf: msg.role === "user" ? "flex-end" : "flex-start", maxWidth: "85%", animation: "fadeIn 0.3s ease" }}>
            <div style={{
              padding: "10px 14px",
              borderRadius: msg.role === "user" ? "16px 16px 4px 16px" : "4px 16px 16px 16px",
              background: msg.role === "user" ? "linear-gradient(135deg, var(--brand-primary), var(--brand-active))" : "var(--bg-surface-3)",
              color: msg.role === "user" ? "#fff" : "var(--text-primary)",
              fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word",
            }}>
              {msg.content.split("\n").map((line, li) => (
                <React.Fragment key={li}>{line}{li < msg.content.split("\n").length - 1 && <br />}</React.Fragment>
              ))}
            </div>
          </div>
        ))}

        {isGenerating && (
          <div style={{ alignSelf: "flex-start", padding: "10px 14px", borderRadius: "4px 16px 16px 16px", background: "var(--bg-surface-3)", display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-secondary)" }}>
            <Loader2 size={14} className="spin" />{stopRequested ? "正在停止…" : "正在生成..."}
            {activeRunId && (
              <button
                onClick={handleStop}
                disabled={stopRequested}
                className="btn-ghost"
                style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  padding: "2px 10px", borderRadius: 12, fontSize: 12,
                  border: "1px solid var(--border-default)",
                  cursor: stopRequested ? "default" : "pointer",
                  opacity: stopRequested ? 0.5 : 1,
                }}
                title="停止生成（已完成阶段将保留）"
                aria-label="停止生成"
              >
                <Square size={10} />停止
              </button>
            )}
          </div>
        )}

        {contentRefreshPending && (
          <div style={{ alignSelf: "flex-start", padding: "2px 6px" }}>
            <button
              onClick={handleRefreshContent}
              className="btn-ghost"
              style={{
                display: "inline-flex", alignItems: "center", gap: 4,
                padding: "4px 12px", borderRadius: 12, fontSize: 12,
                border: "1px solid var(--border-default)", cursor: "pointer",
              }}
              title="仅重新读取项目内容，不会重新生成"
              aria-label="刷新内容"
            >
              🔄 刷新内容
            </button>
          </div>
        )}
      </div>

      {/* Input area — Composer per Design Spec */}
      <div style={{ padding: "12px 16px", borderTop: "1px solid var(--border-default)", flexShrink: 0, display: "flex", gap: 8, background: "var(--bg-surface)" }}>
        <textarea
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (projectId && projectStatus !== "running") handleRefine();
              else handleGenerate();
            }
          }}
          placeholder={projectId ? "输入修改指令，按 Enter 发送..." : "输入你的故事想法，按 Enter 开始生成..."}
          rows={2} maxLength={2000}
          className="composer-input"
        />
        <button
          onClick={projectId && projectStatus !== "running" ? handleRefine : handleGenerate}
          disabled={!inputValue.trim() || isGenerating}
          className="btn-primary"
          style={{
            width: 48, height: 48, borderRadius: 14, flexShrink: 0,
            opacity: inputValue.trim() && !isGenerating ? 1 : 0.4,
          }}
        >
          <Send size={18} />
        </button>
      </div>
    </div>
  );
}

// ── Helpers ─────────────────────────────────────

/** detail.code values for a refused refine: nothing was saved, the draft
 * stays retryable, and the UI must not claim success. */
const REFINE_UNAPPLIED_CODES = new Set([
  "refine_no_meaningful_change",
  "refine_constraint_failed",
  "refine_not_executable",
  "refine_target_not_found",
]);

/** Render the backend's real diff (computed from the before/after
 * snapshots) as chat text: field path plus before/after preview, with the
 * out-of-cap hint and the storyboard-not-synced notice when present. */
function formatChangeSummary(body: any): string {
  const changes: Array<{ path: string; before: string; after: string }> = body?.changes ?? [];
  if (!changes.length) return "";
  const total = typeof body?.total_changes === "number" ? body.total_changes : changes.length;
  const lines = changes.map((c) => `- ${c.path}\n  修改前: ${c.before}\n  修改后: ${c.after}`);
  let text = `本次实际变化（${total} 处）:\n${lines.join("\n")}`;
  if (total > changes.length) {
    text += `\n…共 ${total} 处变化，仅显示前 ${changes.length} 处；完整内容见项目历史。`;
  }
  if (body?.notice) text += `\n\nℹ️ ${body.notice}`;
  return text;
}

function formatResultSummary(state: any): string {
  const parts: string[] = [];
  if (state.outline) parts.push(`📋 大纲: ${(state.outline as any).basic_info?.logline || ""}`);
  if (state.characters?.length) parts.push(`👥 角色: ${state.characters.length}个`);
  if (state.scenes?.length) parts.push(`🏞 场景: ${state.scenes.length}个`);
  if (state.art_style) parts.push(`🎨 美术风格已定义`);
  if (state.script) parts.push(`📝 剧本: ${(state.script as any).scenes?.length || 0}场`);
  if (state.storyboard) {
    const sb = state.storyboard as any;
    parts.push(`🎬 分镜: ${sb.total_shot_count}镜头 ~${Math.round(sb.total_estimated_duration)}s`);
  }
  return parts.join("\n");
}
