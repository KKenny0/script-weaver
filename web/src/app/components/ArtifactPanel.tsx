"use client";

import React from "react";
import {
  FileText, Users, Map, Palette, Film, Eye,
  Download, ChevronRight,
  CheckCircle2, AlertCircle, Loader2,
  Sun, Moon,
} from "lucide-react";
import { ArtifactData, renderArtifactContent } from "./ArtifactContent";
import { useTheme } from "./ThemeProvider";

const ARTIFACT_TABS = [
  { id: "outline", label: "剧本概要", icon: FileText },
  { id: "characters", label: "主角列表", icon: Users },
  { id: "scenes", label: "场景列表", icon: Map },
  { id: "art_style", label: "美术风格", icon: Palette },
  { id: "script", label: "分镜脚本", icon: Film },
  { id: "storyboard", label: "影像亮点", icon: Eye },
] as const;

const API = "/api";

async function apiGet(path: string) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

interface ArtifactPanelProps {
  projectId: string;
  artifactData: ArtifactData;
  projectStatus: "idle" | "running" | "complete" | "error";
  activeTab: string;
  onTabChange: (tab: string) => void;
  onCollapse: () => void;
}

function iconBtnStyle(enabled: boolean): React.CSSProperties {
  return {
    width: 36, height: 36, borderRadius: 10,
    border: "1px solid var(--border-default)",
    background: enabled ? "var(--bg-surface-3)" : "transparent",
    color: enabled ? "var(--text-primary)" : "var(--text-disabled)",
    cursor: enabled ? "pointer" : "not-allowed",
    display: "flex", alignItems: "center", justifyContent: "center",
    transition: "all 0.15s ease",
  };
}

export default function ArtifactPanel({
  projectId, artifactData, projectStatus,
  activeTab, onTabChange, onCollapse,
}: ArtifactPanelProps) {
  const { theme, toggleTheme } = useTheme();

  const handleExport = async (format: string) => {
    if (!projectId) return;
    try {
      const result = await apiGet(`/projects/${projectId}/export/${format}`);
      const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${projectId}_${format}.${format === "fountain" ? "fountain" : format === "video_gen" ? "zip" : "json"}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(`导出失败: ${err.message}`);
    }
  };

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden", background: "var(--bg-app)" }}>
      {/* Top bar */}
      <div style={{
        padding: "14px 24px",
        borderBottom: "1px solid var(--border-default)",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexShrink: 0,
        background: "var(--bg-surface)",
      }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h3 style={{ fontSize: 15, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {artifactData.outline ? ((artifactData.outline as any)?.basic_info?.title || "剧本概要") : "Script-Weaver"}
          </h3>
        </div>

        <span className={`badge ${projectStatus === "complete" ? "success" : projectStatus === "error" ? "error" : projectStatus === "running" ? "running" : "default"}`}>
          {projectStatus === "complete" && <CheckCircle2 size={12} style={{ marginRight: 4 }} />}
          {projectStatus === "running" && <Loader2 size={12} className="spin" style={{ marginRight: 4 }} />}
          {projectStatus === "error" && <AlertCircle size={12} style={{ marginRight: 4 }} />}
          {projectStatus === "idle" ? "等待生成" : projectStatus === "running" ? "生成中..." : projectStatus === "complete" ? "已完成" : "出错了"}
        </span>

        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {/* Theme toggle */}
          <button onClick={toggleTheme} className="btn-ghost" title={theme === "dark" ? "切换到亮色模式" : "切换到暗色模式"}>
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button onClick={() => handleExport("json")} disabled={!projectId} style={iconBtnStyle(!!projectId)} title="导出 JSON"><Download size={14} /></button>
          <button onClick={() => handleExport("fountain")} disabled={!projectId || !artifactData.script} style={iconBtnStyle(!!projectId && !!artifactData.script)} title="导出 Fountain 格式"><FileText size={14} /></button>
          <button onClick={() => handleExport("video_gen")} disabled={!projectId || !artifactData.storyboard} style={iconBtnStyle(!!projectId && !!artifactData.storyboard)} title="导出 VideoGen 提示词"><Film size={14} /></button>
        </div>
      </div>

      {/* Tab bar — Segment/Pill style per Design Spec */}
      <div style={{ padding: "12px 24px", flexShrink: 0 }}>
        <div className="segment-tabs">
          {ARTIFACT_TABS.map((tab) => (
            <button key={tab.id} className={`segment-tab ${activeTab === tab.id ? "active" : ""}`} onClick={() => onTabChange(tab.id)}>
              <tab.icon size={14} />{tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Content area */}
      <div style={{ flex: 1, overflowY: "auto", padding: "20px 24px" }}>
        {renderArtifactContent(activeTab, artifactData)}
      </div>

      {/* Bottom bar */}
      {artifactData.storyboard && (
        <div style={{
          padding: "12px 24px",
          borderTop: "1px solid var(--border-default)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexShrink: 0,
          background: "var(--bg-surface)",
        }}>
          <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}>分镜配置</span>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>分镜数量</label>
            <select defaultValue="auto" style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--border-default)", background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 12 }}>
              <option value="auto">一致性较强</option><option value="high">高细节</option>
            </select>
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>画幅比</label>
            <select defaultValue="16:9" style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--border-default)", background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 12 }}>
              <option value="16:9">16:9</option><option value="9:16">9:16</option><option value="1:1">1:1</option>
            </select>
            <button className="btn-primary" style={{ borderRadius: 10, padding: "7px 16px", fontSize: 13 }}>
              去生成视频<ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
