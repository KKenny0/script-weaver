"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  FileText, Users, Map, Palette, Film, Eye,
  Download, ChevronRight, History,
  CheckCircle2, AlertCircle, Loader2,
  Sun, Moon, TriangleAlert, X,
} from "lucide-react";
import { ArtifactData, EditCardHandler, renderArtifactContent } from "./ArtifactContent";
import VersionHistory, { HistoryPanelState } from "./VersionHistory";
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

const REVIEW_ARTIFACT_LABELS: Record<string, string> = {
  script: "剧本",
  storyboard: "分镜",
  visual_highlights: "影像亮点",
};

const REVIEW_REASON_LABELS: Record<string, string> = {
  upstream_manual_edit: "上游内容被手工修改",
};

/** One compact export warning from the X-Review-Warnings header. */
export interface ExportReviewWarning {
  artifact: string;
  reason: string;
  since_revision: number;
}

/** Parse the ASCII-safe JSON warnings header; a missing/garbled header is
 * treated as "no warnings" — the file itself is still downloaded as-is. */
function parseReviewWarningsHeader(raw: string | null): ExportReviewWarning[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    const list = parsed?.review_warnings;
    if (!Array.isArray(list)) return [];
    return list.filter(
      (w: any) => w && typeof w.artifact === "string" && typeof w.reason === "string",
    );
  } catch {
    return [];
  }
}

/** An export response waiting for the user's still-export decision. The
 * blob is the exact bytes the server answered with — confirming downloads
 * THAT file, never a re-fetch (which could be a different snapshot). The
 * projectId+projectSession pair binds it to the session that launched the
 * request: an abandoned response never dialogs or downloads (A→B→A safe,
 * since the session id changes on every project open). */
interface PendingExport {
  projectId: string;
  projectSession: number;
  format: string;
  blob: Blob;
  filename: string;
  revision: number;
  warnings: ExportReviewWarning[];
}

interface ArtifactPanelProps {
  projectId: string;
  artifactData: ArtifactData;
  projectStatus: "idle" | "running" | "complete" | "error";
  activeTab: string;
  onTabChange: (tab: string) => void;
  onCollapse: () => void;
  onEditCard?: EditCardHandler;
  reviewNotice: string | null;
  onDismissReviewNotice: () => void;
  onOpenReviewPanel?: () => void;
  /** Page-owned session identity: the monotonic id of the currently open
   * project session (bumped synchronously on every project open). */
  projectSessionRef: { current: number };
  /** True only while (projectId, session) is the page's CURRENT open
   * project session — the ownership check every export result must pass. */
  isProjectSessionActive: (projectId: string, session: number) => boolean;
  historyPanel: HistoryPanelState;
  onOpenHistory: () => void;
  onRefreshHistory: () => void;
  onSelectHistoryVersion: (revision: number) => void;
  onBackToHistoryList: () => void;
  onCloseHistory: () => void;
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
  onEditCard, reviewNotice, onDismissReviewNotice, onOpenReviewPanel,
  projectSessionRef, isProjectSessionActive,
  historyPanel, onOpenHistory, onRefreshHistory,
  onSelectHistoryVersion, onBackToHistoryList, onCloseHistory,
}: ArtifactPanelProps) {
  const { theme, toggleTheme } = useTheme();
  // The export awaiting confirmation and the in-flight marker. Each result
  // belongs to the (projectId, projectSession) that STARTED its request:
  // the ownership predicate — page-owned, bumped synchronously on every
  // project open — makes a late response for an abandoned session a no-op
  // (no dialog, no download, no error, no busy release), so A→B→A cannot
  // revive it and an old finally cannot unfreeze a newer request. The
  // effect below only clears what the user sees right now on a switch.
  const [pendingExport, setPendingExport] = useState<PendingExport | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const exportDialogRef = useRef<HTMLDialogElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    setPendingExport(null);
    setExportBusy(false);
  }, [projectId]);

  // Native <dialog> for the export confirmation: showModal on open, Escape
  // cancels (nothing is downloaded), focus restored after close.
  useEffect(() => {
    const dialog = exportDialogRef.current;
    if (!pendingExport || !dialog) return;
    if (!previouslyFocusedRef.current) {
      previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    }
    if (!dialog.open) {
      dialog.showModal();
    }
    const onCancel = (e: Event) => {
      e.preventDefault();
      setPendingExport(null);
    };
    dialog.addEventListener("cancel", onCancel);
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      const prev = previouslyFocusedRef.current;
      if (!exportDialogRef.current) {
        setTimeout(() => {
          if (
            prev &&
            document.contains(prev) &&
            document.activeElement === document.body
          ) {
            prev.focus();
          }
        }, 0);
        previouslyFocusedRef.current = null;
      }
    };
  }, [pendingExport]);

  const triggerDownload = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleExport = async (format: string) => {
    if (!projectId || exportBusy) return;
    const pid = projectId;
    // Capture WHO launched this export: project id AND open-session id.
    // Every async continuation below re-checks this ownership before it
    // may touch the UI — after the blob read, on errors and in the finally
    // (an abandoned request must never release a newer request's busy).
    const launchedSession = projectSessionRef.current;
    const isMine = () => isProjectSessionActive(pid, launchedSession);
    setExportBusy(true);
    try {
      // The backend serves real files (JSON state / Fountain text / ZIP) with
      // a Content-Disposition filename; the blob is saved as-is, never
      // re-wrapped through JSON.stringify. The review headers describe the
      // SAME snapshot as the bytes — the confirmation below is bound to this
      // exact download, with no pre-check window in between.
      const res = await fetch(`${API}/projects/${pid}/export/${format}`);
      if (!res.ok) {
        let message = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body && typeof body.detail === "string") message = body.detail;
        } catch { /* non-JSON error body: keep the status message */ }
        throw new Error(message);
      }
      const blob = await res.blob();
      if (!isMine()) return; // session ended mid-flight: no dialog, no download
      const disposition = res.headers.get("Content-Disposition") ?? "";
      const filename = /filename="?([^";]+)"?/.exec(disposition)?.[1]
        ?? `${pid}.${format === "video_gen" ? "zip" : format}`;
      const revision = Number(res.headers.get("X-Project-Revision")) || 0;
      const warnings = parseReviewWarningsHeader(res.headers.get("X-Review-Warnings"));
      if (warnings.length === 0) {
        triggerDownload(blob, filename);
        return;
      }
      setPendingExport({
        projectId: pid, projectSession: launchedSession,
        format, blob, filename, revision, warnings,
      });
    } catch (err: any) {
      if (!isMine()) return; // no stale error in the new session
      alert(`导出失败: ${err.message}`);
    } finally {
      if (isMine()) setExportBusy(false);
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
          <button
            onClick={onOpenHistory}
            disabled={!projectId}
            style={iconBtnStyle(!!projectId)}
            title={historyPanel.open ? "关闭版本历史" : "查看版本历史"}
            aria-label={historyPanel.open ? "关闭版本历史" : "查看版本历史"}
          >
            <History size={14} style={historyPanel.open ? { color: "var(--brand-primary)" } : undefined} />
          </button>
          <button onClick={() => handleExport("json")} disabled={!projectId || exportBusy} style={iconBtnStyle(!!projectId)} title="导出 JSON"><Download size={14} /></button>
          <button onClick={() => handleExport("fountain")} disabled={!projectId || !artifactData.script || exportBusy} style={iconBtnStyle(!!projectId && !!artifactData.script)} title="导出 Fountain 格式"><FileText size={14} /></button>
          <button onClick={() => handleExport("video_gen")} disabled={!projectId || !artifactData.storyboard || exportBusy} style={iconBtnStyle(!!projectId && !!artifactData.storyboard)} title="导出 VideoGen 提示词"><Film size={14} /></button>
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

      {/* Content area — history view takes precedence while open; it renders
          read-only snapshots and never writes into the current artifacts. */}
      <div style={{ flex: 1, overflowY: "auto", padding: "20px 24px" }}>
        {historyPanel.open ? (
          <VersionHistory
            state={historyPanel}
            activeTab={activeTab}
            onRefresh={onRefreshHistory}
            onSelect={onSelectHistoryVersion}
            onBackToList={onBackToHistoryList}
            onClose={onCloseHistory}
          />
        ) : (
          <>
            {reviewNotice && (
              <div className="review-banner" data-testid="review-notice" role="status">
                <TriangleAlert size={14} style={{ flexShrink: 0, color: "var(--warning)" }} />
                <span style={{ flex: 1 }}>{reviewNotice}</span>
                {onOpenReviewPanel && (
                  <button className="btn-ghost" data-testid="open-review-panel"
                    onClick={onOpenReviewPanel} style={{ flexShrink: 0, fontSize: 12 }}>
                    去复核
                  </button>
                )}
                <button className="btn-ghost" data-testid="dismiss-review-notice"
                  aria-label="关闭复核提示" onClick={onDismissReviewNotice}
                  style={{ width: 26, height: 26, flexShrink: 0 }}>
                  <X size={13} />
                </button>
              </div>
            )}
            {renderArtifactContent(activeTab, artifactData, onEditCard)}
          </>
        )}
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

      {/* Export confirmation (ticket #17): the dialog offers the exact blob
          that was already downloaded into memory — 仍然导出 saves THAT file,
          取消 discards it without creating a download. */}
      {pendingExport && pendingExport.projectId === projectId && (
        <dialog ref={exportDialogRef} className="export-confirm" data-testid="export-confirm"
          aria-label="确认导出仍有待复核内容的项目">
          <div className="ec-card">
            <h4 style={{ fontSize: 15, fontWeight: 600, margin: "0 0 8px", display: "flex", alignItems: "center", gap: 6 }}>
              <TriangleAlert size={15} color="var(--warning)" />
              仍有内容待复核
            </h4>
            <p style={{ fontSize: 13, margin: "0 0 10px", lineHeight: 1.6 }}
              data-testid="export-confirm-scope">
              即将导出 r{pendingExport.revision}，仍有以下内容待复核
              （项目级保守警告，未自动重新生成）：
            </p>
            <ul className="ec-warning-list" data-testid="export-confirm-list">
              {pendingExport.warnings.map((w, i) => (
                <li key={`${w.artifact}-${w.reason}-${i}`}>
                  <strong>{REVIEW_ARTIFACT_LABELS[w.artifact] || w.artifact}</strong>
                  <span>
                    {REVIEW_REASON_LABELS[w.reason] || w.reason} · 自 r{w.since_revision} 起
                  </span>
                </li>
              ))}
            </ul>
            <p style={{ fontSize: 12, color: "var(--text-tertiary)", margin: "10px 0 14px" }}>
              导出文件与以上警告来自同一份快照；确认后下载该文件，取消则不会下载。
            </p>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button type="button" className="btn-secondary" data-testid="export-confirm-cancel"
                onClick={() => setPendingExport(null)}>
                取消
              </button>
              <button type="button" className="btn-primary" data-testid="export-confirm-yes"
                onClick={() => {
                  const pending = pendingExport;
                  setPendingExport(null);
                  // A stale dialog's confirm must never download (the
                  // project switches already clear the dialog; this guards
                  // any window the effect could miss).
                  if (
                    pending &&
                    !isProjectSessionActive(pending.projectId, pending.projectSession)
                  ) {
                    return;
                  }
                  if (pending) triggerDownload(pending.blob, pending.filename);
                }}>
                仍然导出
              </button>
            </div>
          </div>
        </dialog>
      )}
    </div>
  );
}
