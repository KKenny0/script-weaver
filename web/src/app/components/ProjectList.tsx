"use client";

import React, { useState } from "react";
import {
  Plus, Pencil, ChevronLeft, ChevronRight, RefreshCw, FolderOpen, Check, X,
} from "lucide-react";

export interface ProjectSummary {
  project_id: string;
  title: string;
  revision: number;
  stage: string;
  created_at: string;
  updated_at: string;
}

const STAGE_LABELS: Record<string, string> = {
  idea_input: "创意",
  refining: "精炼中",
  structured: "已结构化",
  designing: "设计中",
  scripting: "编剧中",
  storyboarding: "分镜中",
  reviewing: "审核中",
  complete: "已完成",
};

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] || stage || "创意";
}

function formatTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = Date.now();
  const diffMin = Math.floor((now - d.getTime()) / 60000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin} 分钟前`;
  if (diffMin < 24 * 60) return `${Math.floor(diffMin / 60)} 小时前`;
  return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

interface ProjectListProps {
  projects: ProjectSummary[];
  currentProjectId: string;
  open: boolean;
  onToggle: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRefresh: () => void;
  onRename: (id: string, title: string, expectedRevision: number) => Promise<void>;
}

export default function ProjectList({
  projects, currentProjectId, open, onToggle, onSelect, onNew, onRefresh, onRename,
}: ProjectListProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const startRename = (p: ProjectSummary) => {
    setError(null);
    setEditingId(p.project_id);
    setDraftTitle(p.title);
  };

  const submitRename = async (p: ProjectSummary) => {
    const title = draftTitle.trim();
    if (!title || title === p.title) {
      setEditingId(null);
      setError(null);
      return;
    }
    try {
      await onRename(p.project_id, title, p.revision);
      setEditingId(null);
      setError(null);
    } catch (err: any) {
      // Keep the draft so the user's typing is not lost on failure.
      setError(err?.message || "重命名失败");
    }
  };

  if (!open) {
    return (
      <nav aria-label="项目列表（已收起）" style={{
        width: 44, flexShrink: 0, borderRight: "1px solid var(--border-default)",
        background: "var(--bg-sidebar)", display: "flex", flexDirection: "column",
        alignItems: "center", padding: "12px 0", gap: 10,
      }}>
        <button onClick={onToggle} className="btn-ghost" style={{ width: 32, height: 32 }} title="展开项目列表" aria-label="展开项目列表">
          <ChevronRight size={16} />
        </button>
        <span style={{ fontSize: 11, color: "var(--text-tertiary)", writingMode: "vertical-rl" }}>
          项目 {projects.length}
        </span>
      </nav>
    );
  }

  return (
    <nav aria-label="项目列表" style={{
      width: 236, flexShrink: 0, borderRight: "1px solid var(--border-default)",
      background: "var(--bg-sidebar)", display: "flex", flexDirection: "column",
      overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{
        padding: "14px 14px 10px", display: "flex", alignItems: "center", gap: 8,
        flexShrink: 0,
      }}>
        <FolderOpen size={15} style={{ color: "var(--brand-primary)" }} />
        <span style={{ fontWeight: 600, fontSize: 13 }}>项目</span>
        <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>{projects.length}</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <button onClick={onRefresh} className="btn-ghost" style={{ width: 26, height: 26 }} title="刷新列表" aria-label="刷新项目列表">
            <RefreshCw size={13} />
          </button>
          <button onClick={onToggle} className="btn-ghost" style={{ width: 26, height: 26 }} title="收起项目列表" aria-label="收起项目列表">
            <ChevronLeft size={14} />
          </button>
        </div>
      </div>

      <div style={{ padding: "0 14px 10px", flexShrink: 0 }}>
        <button onClick={onNew} className="btn-primary" style={{ width: "100%", padding: "7px 10px", fontSize: 12, borderRadius: 10, display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
          <Plus size={13} />新建项目
        </button>
      </div>

      {/* List */}
      <ul style={{
        flex: 1, overflowY: "auto", margin: 0, padding: "0 8px 12px", listStyle: "none",
        display: "flex", flexDirection: "column", gap: 4,
      }}>
        {projects.length === 0 && (
          <li style={{ padding: "16px 8px", fontSize: 12, color: "var(--text-tertiary)", textAlign: "center" }}>
            还没有项目。输入一个故事想法开始创作。
          </li>
        )}
        {projects.map((p) => {
          const isCurrent = p.project_id === currentProjectId;
          const isEditing = editingId === p.project_id;
          return (
            <li key={p.project_id} style={{ position: "relative" }}>
              {isEditing ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "4px 4px 0" }}>
                  <div style={{ display: "flex", gap: 4 }}>
                    <input
                      autoFocus
                      value={draftTitle}
                      onChange={(e) => setDraftTitle(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") { e.preventDefault(); submitRename(p); }
                        if (e.key === "Escape") { e.preventDefault(); setEditingId(null); setError(null); }
                      }}
                      maxLength={200}
                      aria-label="项目名称"
                      className="composer-input"
                      style={{ fontSize: 12, padding: "6px 8px", borderRadius: 8, flex: 1 }}
                    />
                    <button onClick={() => submitRename(p)} className="btn-ghost" style={{ width: 26, height: 26 }} title="保存名称" aria-label="保存名称">
                      <Check size={13} />
                    </button>
                    <button onClick={() => { setEditingId(null); setError(null); }} className="btn-ghost" style={{ width: 26, height: 26 }} title="取消" aria-label="取消重命名">
                      <X size={13} />
                    </button>
                  </div>
                  {error && <span role="alert" style={{ fontSize: 11, color: "var(--danger, #e5484d)" }}>{error}</span>}
                </div>
              ) : (
                <div style={{ display: "flex", alignItems: "stretch" }}>
                  <button
                    onClick={() => onSelect(p.project_id)}
                    aria-current={isCurrent ? "true" : undefined}
                    style={{
                      flex: 1, minWidth: 0, textAlign: "left", cursor: "pointer",
                      padding: "8px 10px", borderRadius: 10,
                      border: isCurrent ? "1px solid var(--brand-primary)" : "1px solid transparent",
                      background: isCurrent ? "var(--brand-soft)" : "transparent",
                      display: "flex", flexDirection: "column", gap: 3,
                      transition: "all 0.15s ease",
                    }}
                    title={`打开项目「${p.title}」`}
                  >
                    <span style={{
                      fontSize: 13, fontWeight: isCurrent ? 600 : 400, color: "var(--text-primary)",
                      whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                    }}>
                      {p.title || "未命名项目"}
                    </span>
                    <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>
                      {stageLabel(p.stage)} · 更新于 {formatTime(p.updated_at)}
                    </span>
                  </button>
                  <button
                    onClick={() => startRename(p)}
                    className="btn-ghost"
                    style={{ width: 26, alignSelf: "center", height: 26, flexShrink: 0 }}
                    title="重命名项目"
                    aria-label={`重命名项目 ${p.title}`}
                  >
                    <Pencil size={12} />
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
