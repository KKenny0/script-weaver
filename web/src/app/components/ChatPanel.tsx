"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import {
  Send, Sparkles, Wand2, PanelLeftClose,
  Loader2,
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
    throw new Error(extractError(err, `HTTP ${res.status}`));
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
  onTabSwitch: (tab: string) => void;
  onCollapse: () => void;
  sessionNotice: SessionNotice | null;
  closeStreamRef: React.MutableRefObject<() => void>;
  onProjectCreated: (id: string) => void;
  onProjectMutated: () => void;
}

export default function ChatPanel({
  projectId,
  isGenerating, setIsGenerating,
  projectStatus, setProjectStatus,
  onArtifactUpdate, onTabSwitch,
  onCollapse,
  sessionNotice,
  closeStreamRef,
  onProjectCreated,
  onProjectMutated,
}: ChatPanelProps) {
  const [inputValue, setInputValue] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [showSkillsPanel, setShowSkillsPanel] = useState(false);
  const [availableSkills, setAvailableSkills] = useState<any[]>([]);
  const [activeSkillIds, setActiveSkillIds] = useState<Set<string>>(new Set());

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  // Latest-open project: async handlers compare against this so a late
  // response for another project never lands in the current session.
  const currentProjectRef = useRef(projectId);
  // Session token (from page epochs): distinguishes two not-yet-created
  // project sessions, which a project id alone cannot.
  const sessionRef = useRef(0);
  const noticeEpochRef = useRef<number>(-1);

  useEffect(() => {
    currentProjectRef.current = projectId;
  }, [projectId]);

  useEffect(() => {
    sessionRef.current = sessionNotice?.epoch ?? 0;
  }, [sessionNotice]);

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  useEffect(() => {
    apiGet("/skills?stage=structuring").then(setAvailableSkills).catch(console.error);
  }, []);

  useEffect(() => () => { eventSourceRef.current?.close(); }, []);

  // Let the page close the subscription when the user switches projects.
  useEffect(() => {
    closeStreamRef.current = () => {
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      setIsGenerating(false);
    };
  }, [setIsGenerating]);

  // Session notices from the page (project opened / new project). A new epoch
  // resets the conversation; follow-up texts with the same epoch append.
  useEffect(() => {
    if (!sessionNotice) return;
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

  const handleGenerate = useCallback(async () => {
    if (!inputValue.trim() || isGenerating) return;

    const ideaText = inputValue.trim();
    const originalInput = inputValue;
    const epochAtStart = sessionRef.current;
    const userMsg: Message = { role: "user", content: ideaText, timestamp: Date.now() };
    setMessages((prev) => [...prev, userMsg]);
    setIsGenerating(true);
    setProjectStatus("running");
    onArtifactUpdate({});

    setMessages((prev) => [...prev, { role: "assistant", content: "正在分析创意，启动 Pipeline...", timestamp: Date.now() }]);

    try {
      const proj = await apiPost("/projects", { user_input: ideaText, auto_approve_gates: true, active_skills: {} });
      if (sessionRef.current !== epochAtStart) {
        // The user moved to another project/session while this creation was
        // in flight: the project exists server-side, so refresh the list,
        // but do not touch the current session, URL, draft or subscription.
        onProjectMutated();
        return;
      }
      // The project exists in persistent storage now — consume the input,
      // but only if the user has not started typing a new draft meanwhile.
      setInputValue((prev) => (prev === originalInput ? "" : prev));
      onProjectCreated(proj.project_id);

      const evtSource = new EventSource(`${API}/projects/${proj.project_id}/generate`);
      eventSourceRef.current = evtSource;

      evtSource.addEventListener("progress", (e: MessageEvent) => {
        const data = JSON.parse(e.data);
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant") {
            return [...prev.slice(0, -1), { ...last, content: data.message || (data.result_summary ? `✅ ${data.result_summary.details.join(" | ")}` : last.content), timestamp: Date.now() }];
          }
          return prev;
        });
      });

      evtSource.addEventListener("done", async (e: MessageEvent) => {
        evtSource.close();
        eventSourceRef.current = null;
        setIsGenerating(false);

        const doneData = JSON.parse(e.data);
        if (currentProjectRef.current !== proj.project_id) return; // late response after switching

        if (doneData.error) {
          setProjectStatus("error");
          setMessages((prev) => [...prev, { role: "assistant", content: `❌ 生成出错: ${doneData.error}`, timestamp: Date.now() }]);
          return;
        }

        setProjectStatus("complete");

        try {
          const fullState = await apiGet(`/projects/${proj.project_id}`);
          if (currentProjectRef.current !== proj.project_id) return;
          applyFullState(fullState);
          onProjectMutated();

          setMessages((prev) => [...prev, { role: "assistant", content: `🎉 全部生成完成！\n\n${formatResultSummary(fullState)}`, timestamp: Date.now() }]);

          if (fullState.outline) onTabSwitch("outline");
          else if (fullState.characters) onTabSwitch("characters");
          else if (fullState.script) onTabSwitch("script");
        } catch (fetchErr) {
          console.error("Failed to fetch final state:", fetchErr);
        }
      });

      evtSource.onerror = () => {
        evtSource.close();
        eventSourceRef.current = null;
        setIsGenerating(false);
        setProjectStatus("error");
        setMessages((prev) => [...prev, { role: "assistant", content: "⚠️ 连接中断或生成启动失败，请检查后端服务与模型配置。输入的内容已保存在项目中。", timestamp: Date.now() }]);
      };
    } catch (err: any) {
      if (sessionRef.current !== epochAtStart) return; // late failure: not this session's concern
      setIsGenerating(false);
      setProjectStatus("error");
      // Keep inputValue so the user's text is not lost on failure.
      setMessages((prev) => [...prev, { role: "assistant", content: `❌ 错误: ${err.message}`, timestamp: Date.now() }]);
    }
  }, [inputValue, isGenerating, applyFullState, onProjectCreated, onProjectMutated, setProjectStatus, setIsGenerating, onArtifactUpdate, onTabSwitch]);

  const handleRefine = useCallback(async () => {
    if (!projectId || !inputValue.trim() || isGenerating) return;

    const refineText = inputValue.trim();
    const originalInput = inputValue;
    setMessages((prev) => [
      ...prev,
      { role: "user", content: `[修改] ${refineText}`, timestamp: Date.now() },
      { role: "assistant", content: "正在应用修改...", timestamp: Date.now() },
    ]);

    try {
      await apiPost(`/projects/${projectId}/refine`, { message: refineText });
      if (currentProjectRef.current !== projectId) return;
      // Consume the submitted instruction only if the composer still holds
      // it; a draft typed while the model was processing must survive.
      setInputValue((prev) => (prev === originalInput ? "" : prev));
      const fullState = await apiGet(`/projects/${projectId}`);
      if (currentProjectRef.current !== projectId) return;
      applyFullState(fullState);
      onProjectMutated();

      setMessages((prev) => [...prev.slice(0, -1), { role: "assistant", content: "✅ 修改已应用。", timestamp: Date.now() }]);
    } catch (err: any) {
      if (currentProjectRef.current !== projectId) return; // late failure
      // Keep inputValue so the user's text is not lost on failure.
      if (err?.code === "revision_conflict") {
        setMessages((prev) => [...prev.slice(0, -1), { role: "assistant", content: "⚠️ 项目已在其他窗口被修改，已为你重新加载最新内容。请基于最新内容重试修改。", timestamp: Date.now() }]);
        try {
          const fullState = await apiGet(`/projects/${projectId}`);
          if (currentProjectRef.current === projectId) applyFullState(fullState);
        } catch (fetchErr) {
          console.error("Failed to reload project:", fetchErr);
        }
      } else {
        setMessages((prev) => [...prev.slice(0, -1), { role: "assistant", content: `❌ 修改失败: ${err.message}`, timestamp: Date.now() }]);
      }
    }
  }, [projectId, inputValue, isGenerating, applyFullState, onProjectMutated]);

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
        <button onClick={onCollapse} className="btn-ghost" style={{ width: 32, height: 32 }}>
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
            <Loader2 size={14} className="spin" />正在生成...
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
