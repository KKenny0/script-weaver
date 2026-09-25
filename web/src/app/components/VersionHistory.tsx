"use client";

import React, { useEffect } from "react";
import { History, ChevronLeft, Loader2, AlertCircle, RefreshCw } from "lucide-react";
import { ArtifactData, renderArtifactContent } from "./ArtifactContent";

export interface VersionItem {
  revision: number;
  title: string;
  source: string;
  summary: string;
  created_at: string;
}

export interface VersionSnapshot {
  revision: number;
  title: string;
  source: string;
  summary: string;
  created_at: string;
  data: any;
}

const SOURCE_LABELS: Record<string, string> = {
  manual: "手工",
  pipeline: "生成",
  candidate: "候选",
};

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] || source;
}

function formatTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function snapshotToArtifactData(s: VersionSnapshot): ArtifactData {
  const d = s.data || {};
  return {
    refined_idea: d.refined_idea,
    outline: d.outline,
    characters: d.characters,
    scenes: d.scenes,
    art_style: d.art_style,
    script: d.script,
    storyboard: d.storyboard,
    visual_highlights: d.visual_highlights,
  };
}

export interface HistoryPanelState {
  open: boolean;
  loading: boolean;
  error: string | null;
  versions: VersionItem[];
  currentRevision: number;
  viewing: VersionSnapshot | null;
  viewingLoading: boolean;
}

interface VersionHistoryProps {
  state: HistoryPanelState;
  activeTab: string;
  onRefresh: () => void;
  onSelect: (revision: number) => void;
  onBackToList: () => void;
  onClose: () => void;
}

export default function VersionHistory({
  state, activeTab, onRefresh, onSelect, onBackToList, onClose,
}: VersionHistoryProps) {
  // Escape closes the history view; buttons remain the primary control.
  useEffect(() => {
    if (!state.open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [state.open, onClose]);

  const bannerStyle: React.CSSProperties = {
    display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
    padding: "10px 14px", marginBottom: 14, borderRadius: 12,
    border: "1px solid var(--border-default)", background: "var(--bg-surface-3)",
    fontSize: 13, color: "var(--text-secondary)",
  };

  if (state.viewing) {
    return (
      <div data-testid="version-history">
        <div style={bannerStyle} role="status">
          <History size={14} style={{ color: "var(--brand-primary)" }} />
          <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>
            正在查看历史版本 r{state.viewing.revision}
          </span>
          <span>· {sourceLabel(state.viewing.source)} · {formatTime(state.viewing.created_at)}</span>
          {state.viewing.summary && <span style={{ color: "var(--text-tertiary)" }}>· {state.viewing.summary}</span>}
          <span style={{ marginLeft: "auto", display: "flex", gap: 6, flexShrink: 0 }}>
            <button onClick={onBackToList} className="btn-ghost" style={{ padding: "4px 10px", fontSize: 12, whiteSpace: "nowrap" }}>
              <ChevronLeft size={12} style={{ display: "inline", marginRight: 4 }} />版本列表
            </button>
            <button onClick={onClose} className="btn-primary" style={{ padding: "4px 12px", fontSize: 12, borderRadius: 20, whiteSpace: "nowrap" }}>
              返回当前版本 (r{state.currentRevision})
            </button>
          </span>
        </div>
        <p style={{ fontSize: 12, color: "var(--text-tertiary)", margin: "0 0 14px" }}>
          历史版本为只读快照：查看不会改变当前内容，修改与导出始终作用于当前版本。
        </p>
        {renderArtifactContent(activeTab, snapshotToArtifactData(state.viewing))}
      </div>
    );
  }

  return (
    <div data-testid="version-history">
      <div style={bannerStyle}>
        <History size={14} style={{ color: "var(--brand-primary)" }} />
        <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>版本历史</span>
        <span>当前 r{state.currentRevision} · 只读</span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button onClick={onRefresh} className="btn-ghost" style={{ width: 26, height: 26 }} title="刷新版本列表" aria-label="刷新版本列表">
            <RefreshCw size={13} />
          </button>
          <button onClick={onClose} className="btn-ghost" style={{ padding: "4px 10px", fontSize: 12 }}>
            关闭
          </button>
        </span>
      </div>

      {state.loading && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 24, color: "var(--text-tertiary)", fontSize: 13 }}>
          <Loader2 size={14} className="spin" />正在加载版本列表…
        </div>
      )}

      {!state.loading && state.error && (
        <div role="alert" style={{ display: "flex", alignItems: "center", gap: 8, padding: 24, color: "var(--danger, #e5484d)", fontSize: 13 }}>
          <AlertCircle size={14} />
          <span>版本历史加载失败：{state.error}</span>
          <button onClick={onRefresh} className="btn-ghost" style={{ padding: "4px 10px", fontSize: 12 }}>重试</button>
        </div>
      )}

      {!state.loading && !state.error && (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {state.versions.length === 0 && (
            <li style={{ padding: 24, fontSize: 13, color: "var(--text-tertiary)", textAlign: "center" }}>
              暂无历史版本。
            </li>
          )}
          {state.versions.map((v) => (
            <li key={v.revision}>
              <button
                onClick={() => onSelect(v.revision)}
                disabled={state.viewingLoading}
                style={{
                  width: "100%", textAlign: "left", cursor: state.viewingLoading ? "wait" : "pointer",
                  padding: "10px 14px", borderRadius: 10,
                  border: v.revision === state.currentRevision
                    ? "1px solid var(--brand-primary)"
                    : "1px solid var(--border-default)",
                  background: "var(--bg-surface)", display: "flex", flexDirection: "column", gap: 3,
                }}
                aria-label={`查看版本 r${v.revision}`}
              >
                <span style={{ fontSize: 13, color: "var(--text-primary)" }}>
                  r{v.revision}
                  {v.revision === state.currentRevision && (
                    <span className="badge success" style={{ marginLeft: 8, fontSize: 11 }}>当前</span>
                  )}
                  <span style={{ marginLeft: 8, fontSize: 12, color: "var(--text-tertiary)" }}>
                    {sourceLabel(v.source)} · {formatTime(v.created_at)}
                  </span>
                </span>
                {v.summary && <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{v.summary}</span>}
                <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}>{v.title}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {state.viewingLoading && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 12, color: "var(--text-tertiary)", fontSize: 13 }}>
          <Loader2 size={14} className="spin" />正在载入快照…
        </div>
      )}
    </div>
  );
}
