"use client";

import React, { useEffect, useRef, useState } from "react";
import { Loader2, X } from "lucide-react";

/**
 * Pending-review panel (ticket #17).
 *
 * Shows WHY each downstream artifact awaits review — the persisted flag's
 * reason, its recorded upstream object and its own ``since_revision``
 * (never the project's current revision) — and lets the user confirm a
 * SELECTED scope as kept as-is. Confirming is a CAS POST against the
 * revision the panel was opened at: the backend matches every selection by
 * its full identity, so a stale panel can only ever answer 409 (the user
 * then re-reads; nothing is auto-retried against a newer revision).
 * Conservative scope is stated as such — never as per-shot analysis.
 */

export interface ReviewPanelSession {
  seq: number;
  projectId: string;
  flags: any[];
  /** The revision the flags were read at — the confirm request's CAS basis. */
  revision: number;
}

export interface ReviewSelection {
  /** Mandatory ownership (review round 2): two projects can hold
   * byte-identical flags, so the decision states the project it was made
   * in — taken from this panel's session, never guessed. */
  project_id: string;
  artifact: string;
  reason: string;
  upstream_kind: string;
  upstream_id: string;
  since_revision: number;
}

export type ConfirmReviewResult =
  | { type: "confirmed"; payload: any; confirmedCount: number }
  | { type: "conflict"; message: string; currentRevision: number | null }
  | { type: "invalid"; message: string }
  | { type: "error"; message: string }
  | { type: "superseded" };

interface ReviewPanelProps {
  session: ReviewPanelSession;
  /** Page-owned POST with session binding (A→B→A safe). */
  onConfirm: (selections: ReviewSelection[]) => Promise<ConfirmReviewResult>;
  /** Re-read the project and re-base the panel on the fresh flags. */
  onReloadLatest: () => void;
  onClose: () => void;
  /** Jump to the flagged artifact's tab (closes the panel). */
  onViewArtifact: (artifact: string) => void;
}

const ARTIFACT_LABELS: Record<string, string> = {
  script: "剧本",
  storyboard: "分镜",
  visual_highlights: "影像亮点",
};

const KIND_LABELS: Record<string, string> = {
  characters: "角色",
  scenes: "场景",
  shots: "镜头",
};

const REASON_LABELS: Record<string, string> = {
  upstream_manual_edit: "上游内容被手工修改",
};

/** Collision-free identity of one selection (mirrors the backend's match key). */
function selectionKeyOf(flag: any): string {
  return JSON.stringify([
    flag.artifact,
    flag.reason,
    flag.upstream_kind,
    flag.upstream_id,
    flag.since_revision,
  ]);
}

function toSelection(flag: any, projectId: string): ReviewSelection {
  return {
    project_id: projectId,
    artifact: String(flag.artifact),
    reason: String(flag.reason),
    upstream_kind: String(flag.upstream_kind ?? ""),
    upstream_id: String(flag.upstream_id ?? ""),
    since_revision: Number(flag.since_revision),
  };
}

function upstreamText(flag: any): string {
  const kind = KIND_LABELS[flag.upstream_kind] || flag.upstream_kind || "上游对象";
  const label = typeof flag.upstream_label === "string" && flag.upstream_label
    ? flag.upstream_label
    : flag.upstream_id;
  return `${kind}「${label}」`;
}

export default function ReviewPanel({
  session,
  onConfirm,
  onReloadLatest,
  onClose,
  onViewArtifact,
}: ReviewPanelProps) {
  const { flags, revision, projectId } = session;
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(flags.map(selectionKeyOf)),
  );
  const [phase, setPhase] = useState<"idle" | "confirming">("idle");
  const [errorMessage, setErrorMessage] = useState("");
  const [conflict, setConflict] = useState<{ message: string } | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  // Native <dialog> modal: focus management, Escape closes (a selection is
  // not a draft — nothing to guard), focus restored on close.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!previouslyFocusedRef.current) {
      previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    }
    if (!dialog.open) {
      dialog.showModal();
    }
    const onCancel = (e: Event) => {
      e.preventDefault();
      onClose();
    };
    dialog.addEventListener("cancel", onCancel);
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      const prev = previouslyFocusedRef.current;
      setTimeout(() => {
        if (
          prev &&
          document.contains(prev) &&
          document.activeElement === document.body
        ) {
          prev.focus();
        }
      }, 0);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleConfirm = async () => {
    if (phase === "confirming" || selected.size === 0) return;
    setPhase("confirming");
    setErrorMessage("");
    setConflict(null);
    const selections = flags
      .filter((f) => selected.has(selectionKeyOf(f)))
      .map((f) => toSelection(f, projectId));
    const result = await onConfirm(selections);
    if (result.type === "superseded") return; // session moved on; the panel is already gone
    setPhase("idle");
    if (result.type === "confirmed") return; // the page adopts the snapshot and closes this panel
    if (result.type === "conflict") {
      setConflict({ message: result.message });
      return;
    }
    setErrorMessage(result.message); // invalid / error: selection kept for retry
  };

  const disabled = phase === "confirming";

  return (
    <dialog ref={dialogRef} className="review-drawer" data-testid="review-panel"
      aria-label="复核下游内容">
      <div className="ced-shell">
        <header className="ced-header">
          <div>
            <h4 style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>
              待复核内容
              <span className="badge" style={{ marginLeft: 8 }} data-testid="review-count">
                {flags.length}
              </span>
            </h4>
            <p style={{ fontSize: 12, color: "var(--text-tertiary)", margin: "2px 0 0" }}
              data-testid="review-basis">
              依据 r{revision} · 保守影响范围，未自动重新生成
            </p>
          </div>
          <button type="button" className="btn-ghost" data-testid="review-close"
            aria-label="关闭复核面板" onClick={onClose} style={{ width: 28, height: 28, flexShrink: 0 }}>
            <X size={15} />
          </button>
        </header>

        <div className="ced-body">
          <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: 0 }}>
            以下产物尚未重新生成。请检查内容后，仅对确认沿用的项打勾；未选择的标记将保留。
          </p>

          {conflict && (
            <div className="ced-notice ced-notice-error" role="alert" data-testid="review-conflict">
              <span style={{ flex: 1, minWidth: 0 }}>{conflict.message}</span>
              <span className="ced-notice-actions">
                <button type="button" className="btn-secondary" data-testid="review-reload"
                  onClick={onReloadLatest} disabled={disabled}>
                  重新读取
                </button>
              </span>
            </div>
          )}
          {errorMessage && (
            <div className="ced-notice ced-notice-error" role="alert" data-testid="review-error">
              <span style={{ flex: 1, minWidth: 0 }}>{errorMessage}</span>
            </div>
          )}

          {flags.map((flag, i) => {
            const key = selectionKeyOf(flag);
            const artifactLabel = ARTIFACT_LABELS[flag.artifact] || flag.artifact;
            const reasonLabel = REASON_LABELS[flag.reason] || flag.reason;
            return (
              <label key={key} className="rv-flag" data-testid={`review-flag-${i}`}>
                <input type="checkbox" data-testid={`review-select-${i}`}
                  checked={selected.has(key)} onChange={() => toggle(key)}
                  disabled={disabled} />
                <span className="rv-flag-main">
                  <span className="rv-flag-title">
                    {artifactLabel}
                    <span className="rv-flag-sub">
                      原因：{reasonLabel}（{upstreamText(flag)}）· 自 r{flag.since_revision} 起
                    </span>
                  </span>
                  <span className="rv-flag-hint">
                    保守影响范围：该产物可能已过时，确认沿用即视为接受当前内容。
                  </span>
                </span>
                <button type="button" className="btn-secondary rv-view-btn"
                  data-testid={`review-view-${flag.artifact}`}
                  onClick={(e) => {
                    e.preventDefault();
                    onViewArtifact(flag.artifact);
                  }}
                  disabled={disabled}>
                  查看{artifactLabel}
                </button>
              </label>
            );
          })}
        </div>

        <footer className="ced-footer">
          <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}
            data-testid="review-selected-count">
            已选 {selected.size}/{flags.length} 项
          </span>
          <span style={{ display: "inline-flex", gap: 8 }}>
            <button type="button" className="btn-secondary" data-testid="review-cancel"
              onClick={onClose} disabled={disabled}>
              取消
            </button>
            <button type="button" className="btn-primary" data-testid="confirm-review-keep"
              disabled={disabled || selected.size === 0} onClick={handleConfirm}>
              {disabled && <Loader2 size={13} className="spin" style={{ marginRight: 4 }} />}
              确认沿用所选
            </button>
          </span>
        </footer>
      </div>
    </dialog>
  );
}
