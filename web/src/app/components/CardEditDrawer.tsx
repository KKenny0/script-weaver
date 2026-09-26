"use client";

import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, Plus, Trash2, X } from "lucide-react";

/**
 * Right-side card edit drawer (ticket #16).
 *
 * One session edits exactly one card. The draft is built once from the
 * open-time snapshot and is NEVER re-synced by background refreshes — the
 * save carries the open-time revision as its CAS basis, so a stale draft is
 * rejected server-side (409) instead of silently rewritten. The modal
 * native <dialog> owns focus management, Escape (via the cancel event) and
 * focus restoration on close; unsaved drafts ask before they die.
 */

export type CardKind = "characters" | "scenes" | "shots";

export interface CardEditSession {
  seq: number;
  projectId: string;
  kind: CardKind;
  id: string;
  /** The revision the card content was read at — the draft's edit basis. */
  revision: number;
  snapshot: Record<string, any>;
}

export type SaveCardResult =
  | { type: "saved"; changed: boolean; payload: any }
  | { type: "conflict"; message: string; currentRevision: number | null }
  | { type: "invalid"; message: string }
  | { type: "error"; message: string }
  | { type: "superseded" };

interface CardEditDrawerProps {
  session: CardEditSession;
  /** Page-owned PATCH with session binding (A→B→A safe). */
  onSave: (
    kind: CardKind,
    id: string,
    changes: Record<string, unknown>,
    expectedRevision: number,
  ) => Promise<SaveCardResult>;
  /** Called with the server snapshot after a real save; the page refreshes and closes. */
  onSaved: (payload: any) => void;
  /** Discard the draft and close. */
  onDiscard: () => void;
  /** Re-read the project and reopen this card on the latest basis. */
  onReloadLatest: (kind: CardKind, id: string) => void;
}

const KIND_LABELS: Record<CardKind, string> = {
  characters: "角色",
  scenes: "场景",
  shots: "镜头",
};

// Canonical enum values only — mirrors the backend whitelist so a select can
// never emit a value the strict validation would refuse.
const ROLE_OPTIONS = [
  { value: "protagonist", label: "主角" },
  { value: "antagonist", label: "反派" },
  { value: "supporting", label: "配角" },
  { value: "extra", label: "龙套" },
];
const ENVIRONMENT_OPTIONS = [
  { value: "interior", label: "内景" },
  { value: "exterior", label: "外景" },
  { value: "mixed", label: "内外结合" },
];
const SHOT_SIZE_OPTIONS = [
  { value: "extreme_long_shot", label: "大远景" },
  { value: "long_shot", label: "远景" },
  { value: "full_shot", label: "全景" },
  { value: "medium_long_shot", label: "中远景" },
  { value: "medium_shot", label: "中景" },
  { value: "medium_close_up", label: "近景" },
  { value: "close_up", label: "特写" },
  { value: "extreme_close_up", label: "大特写" },
];
const CAMERA_ANGLE_OPTIONS = [
  { value: "eye_level", label: "平视" },
  { value: "low_angle", label: "仰拍" },
  { value: "high_angle", label: "俯拍" },
  { value: "dutch_angle", label: "倾斜角" },
  { value: "bird_eye", label: "鸟瞰" },
  { value: "over_shoulder", label: "过肩" },
  { value: "point_of_view", label: "主观视角" },
  { value: "two_shot", label: "双人镜头" },
];
const CAMERA_MOVEMENT_OPTIONS = [
  { value: "static", label: "固定" },
  { value: "push_in", label: "推" },
  { value: "pull_out", label: "拉" },
  { value: "pan_left", label: "左摇" },
  { value: "pan_right", label: "右摇" },
  { value: "tilt_up", label: "上仰" },
  { value: "tilt_down", label: "下俯" },
  { value: "dolly", label: "移动跟拍" },
  { value: "tracking", label: "跟踪" },
  { value: "arc", label: "弧形" },
  { value: "crane_up", label: "升降（升）" },
  { value: "crane_down", label: "升降（降）" },
  { value: "handheld", label: "手持" },
  { value: "steadicam", label: "斯坦尼康" },
  { value: "aerial", label: "航拍" },
  { value: "zoom_in", label: "变焦推" },
  { value: "zoom_out", label: "变焦拉" },
];
const TRANSITION_OPTIONS = [
  { value: "cut", label: "切" },
  { value: "fade_in", label: "淡入" },
  { value: "fade_out", label: "淡出" },
  { value: "dissolve", label: "叠化" },
  { value: "smash_cut", label: "碎切" },
  { value: "match_cut", label: "匹配剪辑" },
  { value: "jump_cut", label: "跳切" },
  { value: "cross_dissolve", label: "交叉叠化" },
  { value: "hard_cut", label: "硬切" },
  { value: "wipe", label: "划像" },
];

type FieldType = "text" | "textarea" | "select" | "list" | "pairs" | "duration";

interface FieldDef {
  name: string;
  label: string;
  type: FieldType;
  options?: { value: string; label: string }[];
  placeholder?: string;
}

const FIELD_DEFS: Record<CardKind, FieldDef[]> = {
  characters: [
    { name: "name", label: "名称", type: "text" },
    { name: "role", label: "角色类型", type: "select", options: ROLE_OPTIONS },
    { name: "appearance", label: "外观", type: "textarea" },
    { name: "personality", label: "性格", type: "textarea" },
    { name: "costume_description", label: "服装", type: "textarea" },
    { name: "key_props", label: "关键道具", type: "list" },
    { name: "backstory", label: "背景故事", type: "textarea" },
    { name: "motivation", label: "动机", type: "textarea" },
    { name: "relationship_map", label: "人物关系", type: "pairs" },
    { name: "image_prompt", label: "图片提示词", type: "textarea" },
  ],
  scenes: [
    { name: "name", label: "名称", type: "text" },
    { name: "location_type", label: "内景/外景", type: "select", options: ENVIRONMENT_OPTIONS },
    { name: "environment", label: "环境描述", type: "textarea" },
    { name: "time_of_day", label: "时间", type: "text" },
    { name: "weather", label: "天气", type: "text" },
    { name: "mood", label: "氛围", type: "text" },
    { name: "lighting_description", label: "灯光", type: "textarea" },
    { name: "color_palette", label: "色彩", type: "list" },
    { name: "key_elements", label: "关键元素", type: "list" },
    { name: "image_prompt", label: "图片提示词", type: "textarea" },
  ],
  shots: [
    { name: "shot_size", label: "景别", type: "select", options: SHOT_SIZE_OPTIONS },
    { name: "camera_angle", label: "角度", type: "select", options: CAMERA_ANGLE_OPTIONS },
    { name: "camera_movement", label: "运镜", type: "select", options: CAMERA_MOVEMENT_OPTIONS },
    { name: "movement_description", label: "运镜说明", type: "textarea" },
    { name: "visual_description", label: "画面描述", type: "textarea" },
    { name: "action_description", label: "动作描述", type: "textarea" },
    { name: "dialogue", label: "对白", type: "textarea" },
    { name: "voiceover", label: "旁白", type: "textarea" },
    { name: "on_screen_text", label: "屏幕文字", type: "text" },
    { name: "sound_effects", label: "音效", type: "list" },
    { name: "music_cue", label: "音乐提示", type: "text" },
    { name: "music_mood", label: "音乐情绪", type: "text" },
    { name: "duration_seconds", label: "时长（秒）", type: "duration" },
    { name: "transition_to_next", label: "转场", type: "select", options: TRANSITION_OPTIONS },
    { name: "image_prompt", label: "首帧图片提示词", type: "textarea" },
    { name: "video_prompt", label: "视频提示词", type: "textarea" },
    { name: "negative_prompt", label: "反向提示词", type: "textarea" },
  ],
};

const READ_ONLY_ROWS: Record<CardKind, { label: string; get: (s: any) => string }[]> = {
  characters: [
    { label: "角色 ID", get: (s) => s.id ?? "" },
    { label: "参考图 URL", get: (s) => s.image_reference_url ?? "" },
  ],
  scenes: [
    { label: "场景 ID", get: (s) => s.id ?? "" },
    { label: "参考图 URL", get: (s) => s.image_reference_url ?? "" },
  ],
  shots: [
    { label: "镜头 ID", get: (s) => s.shot_id ?? "" },
    { label: "所属剧本场景", get: (s) => s.scene_id ?? "" },
    { label: "场内序号", get: (s) => String(s.sequence_number ?? "") },
    { label: "参考图 URL", get: (s) => s.reference_image_url ?? "" },
  ],
};

// ── Draft <-> changes ────────────────────────────────

type Draft = Record<string, any>;

function buildDraft(kind: CardKind, snapshot: Record<string, any>): Draft {
  const draft: Draft = {};
  for (const f of FIELD_DEFS[kind]) {
    const current = snapshot[f.name];
    if (f.type === "list") draft[f.name] = [...(Array.isArray(current) ? current : [])];
    else if (f.type === "pairs")
      draft[f.name] = Object.entries(current ?? {}).map(([key, value]) => ({ key, value }));
    else if (f.type === "duration") draft[f.name] = String(current ?? "");
    else if (f.type === "select") draft[f.name] = String(current ?? "");
    else draft[f.name] = current == null ? "" : String(current);
  }
  return draft;
}

function buildChanges(
  kind: CardKind,
  snapshot: Record<string, any>,
  draft: Draft,
): { changes: Record<string, unknown>; errors: string[] } {
  const changes: Record<string, unknown> = {};
  const errors: string[] = [];
  for (const f of FIELD_DEFS[kind]) {
    const dv = draft[f.name];
    const current = snapshot[f.name];
    if (f.type === "list") {
      const list = dv as string[];
      const base = Array.isArray(current) ? current : [];
      if (list.length !== base.length || list.some((v, i) => v !== base[i])) {
        changes[f.name] = list;
      }
    } else if (f.type === "pairs") {
      const rows = dv as { key: string; value: string }[];
      const built: Record<string, string> = {};
      for (const row of rows) {
        if (!row.key && row.value) {
          errors.push(`「${f.label}」有一行缺少关系名称`);
        } else if (row.key) {
          built[row.key] = row.value;
        }
      }
      const base = current ?? {};
      if (JSON.stringify(built) !== JSON.stringify(base)) changes[f.name] = built;
    } else if (f.type === "duration") {
      const text = String(dv).trim();
      if (!text) {
        errors.push(`「${f.label}」不能为空`);
        continue;
      }
      const num = Number(text);
      if (!Number.isFinite(num) || num <= 0) {
        errors.push(`「${f.label}」必须是正数`);
        continue;
      }
      if (num !== Number(current)) changes[f.name] = num;
    } else if (f.type === "select") {
      const sv = String(dv ?? "");
      if (sv !== String(current ?? "")) changes[f.name] = sv;
    } else {
      const sv = dv == null ? "" : String(dv);
      if (sv !== (current == null ? "" : String(current))) changes[f.name] = sv;
    }
  }
  return { changes, errors };
}

// ── Small form controls ──────────────────────────────

function FieldShell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="ced-field">
      <label className="ced-label">{label}</label>
      {children}
    </div>
  );
}

function ListEditor({
  label, name, items, disabled, onChange,
}: {
  label: string; name: string; items: string[]; disabled: boolean;
  onChange: (items: string[]) => void;
}) {
  return (
    <FieldShell label={label}>
      {items.map((item, i) => (
        <div key={i} className="ced-list-row">
          <input
            className="ced-input"
            data-testid={`field-${name}-${i}`}
            value={item}
            disabled={disabled}
            onChange={(e) => onChange(items.map((v, j) => (j === i ? e.target.value : v)))}
            aria-label={`${label} ${i + 1}`}
          />
          <button
            type="button" className="ced-icon-btn" disabled={disabled}
            aria-label={`删除${label}第${i + 1}项`} title={`删除${label}第${i + 1}项`}
            onClick={() => onChange(items.filter((_, j) => j !== i))}
          >
            <Trash2 size={13} />
          </button>
        </div>
      ))}
      <button
        type="button" className="ced-add-btn" disabled={disabled}
        data-testid={`add-${name}`}
        onClick={() => onChange([...items, ""])}
      >
        <Plus size={12} /> 添加一项
      </button>
    </FieldShell>
  );
}

function PairsEditor({
  label, name, rows, disabled, onChange,
}: {
  label: string; name: string; rows: { key: string; value: string }[];
  disabled: boolean;
  onChange: (rows: { key: string; value: string }[]) => void;
}) {
  return (
    <FieldShell label={label}>
      {rows.map((row, i) => (
        <div key={i} className="ced-list-row">
          <input
            className="ced-input" style={{ flex: "0 0 38%" }}
            data-testid={`field-${name}-${i}-key`}
            value={row.key} disabled={disabled} placeholder="名称"
            aria-label={`${label}第${i + 1}项名称`}
            onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)))}
          />
          <input
            className="ced-input"
            data-testid={`field-${name}-${i}-value`}
            value={row.value} disabled={disabled} placeholder="关系"
            aria-label={`${label}第${i + 1}项关系`}
            onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))}
          />
          <button
            type="button" className="ced-icon-btn" disabled={disabled}
            aria-label={`删除${label}第${i + 1}项`} title={`删除${label}第${i + 1}项`}
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            <Trash2 size={13} />
          </button>
        </div>
      ))}
      <button
        type="button" className="ced-add-btn" disabled={disabled}
        data-testid={`add-${name}`}
        onClick={() => onChange([...rows, { key: "", value: "" }])}
      >
        <Plus size={12} /> 添加一项
      </button>
    </FieldShell>
  );
}

// ── Main component ───────────────────────────────────

export default function CardEditDrawer({
  session, onSave, onSaved, onDiscard, onReloadLatest,
}: CardEditDrawerProps) {
  const { kind, id, snapshot } = session;
  const [draft, setDraft] = useState<Draft>(() => buildDraft(kind, snapshot));
  const [phase, setPhase] = useState<"idle" | "saving" | "unchanged" | "error" | "conflict">("idle");
  const [errorMessage, setErrorMessage] = useState("");
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const aliveRef = useRef(true);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  // Dirty is derived from the same diff the save sends, so "no real changes"
  // never triggers the discard guard.
  const { changes, errors } = buildChanges(kind, snapshot, draft);
  const dirty = Object.keys(changes).length > 0 || errors.length > 0;
  const title = String(snapshot.name ?? id);

  const requestClose = () => {
    if (dirty) {
      setConfirmDiscard(true);
      return;
    }
    onDiscard();
  };
  const requestCloseRef = useRef(requestClose);
  requestCloseRef.current = requestClose;

  // Native <dialog> modal: focus management, Escape → guarded close, and
  // focus restoration to the card's edit button after unmount.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    // StrictMode dev runs this effect twice on the same open dialog: never
    // clobber the entry focus (it would point inside the drawer afterwards)
    // and never re-show an already-open dialog.
    if (!previouslyFocusedRef.current) {
      previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    }
    if (!dialog.open) {
      dialog.showModal();
    }
    const onCancel = (e: Event) => {
      e.preventDefault();
      requestCloseRef.current();
    };
    dialog.addEventListener("cancel", onCancel);
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      // Restore focus after the dialog has actually left the DOM (the
      // browser then resets focus to <body>), not while it still holds it.
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
  }, []);

  // Native leave-guard while a draft exists (refresh / external navigation).
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  // Fields and the save button freeze while a save is in flight (no double
  // submit); closing stays available — discarding mid-save is the user's
  // call, and the page-level session guard keeps the late response harmless.
  const disabled = phase === "saving";
  const update = (name: string, value: unknown) =>
    setDraft((prev) => ({ ...prev, [name]: value }));

  const handleSave = async () => {
    if (phase === "saving") return; // double-submit guard
    const { changes: pending, errors: pendingErrors } = buildChanges(kind, snapshot, draft);
    if (pendingErrors.length > 0) {
      setPhase("error");
      setErrorMessage(pendingErrors[0]);
      return;
    }
    if (Object.keys(pending).length === 0) {
      setPhase("unchanged");
      setErrorMessage("");
      return;
    }
    setPhase("saving");
    setErrorMessage("");
    const result = await onSave(kind, id, pending, session.revision);
    if (!aliveRef.current) return;
    switch (result.type) {
      case "saved":
        if (!result.changed) {
          setPhase("unchanged");
          break;
        }
        onSaved(result.payload); // the page applies the snapshot and closes
        break;
      case "conflict":
        setPhase("conflict");
        setErrorMessage(result.message);
        break;
      case "invalid":
      case "error":
        setPhase("error");
        setErrorMessage(result.message);
        break;
      case "superseded":
        // The page session moved on while this request flew; it owns the UI now.
        break;
    }
  };

  return (
    <dialog ref={dialogRef} className="card-edit-drawer" data-testid="card-edit-drawer"
      aria-labelledby="card-edit-title">
      <div className="ced-shell">
        <header className="ced-header">
          <div style={{ minWidth: 0 }}>
            <h3 id="card-edit-title" style={{ fontSize: 15, fontWeight: 600 }}>
              编辑{KIND_LABELS[kind]} · {title}
            </h3>
            <span className="badge" data-testid="drawer-basis" style={{ marginTop: 4, display: "inline-block" }}>
              编辑依据 r{session.revision}
            </span>
          </div>
          <button type="button" className="btn-ghost" data-testid="close-drawer"
            aria-label="关闭编辑面板" onClick={requestClose}>
            <X size={16} />
          </button>
        </header>

        <div className="ced-body">
          {phase === "conflict" && (
            <div className="ced-notice ced-notice-error" role="alert" data-testid="drawer-conflict">
              <AlertTriangle size={14} style={{ flexShrink: 0 }} />
              <span>
                保存冲突：内容已被其他修改更新，当前草稿依据已过期。{errorMessage}
              </span>
              <span className="ced-notice-actions">
                <button type="button" className="btn-secondary" data-testid="reload-latest"
                  onClick={() => onReloadLatest(kind, id)}>
                  载入最新内容（放弃当前草稿）
                </button>
                <button type="button" className="btn-ghost" data-testid="conflict-close"
                  aria-label="关闭编辑面板" onClick={requestClose}>
                  <X size={14} />
                </button>
              </span>
            </div>
          )}
          {phase === "error" && errorMessage && (
            <div className="ced-notice ced-notice-error" role="alert" data-testid="drawer-error">
              <AlertTriangle size={14} style={{ flexShrink: 0 }} />
              <span>{errorMessage}</span>
            </div>
          )}
          {phase === "unchanged" && (
            <div className="ced-notice" role="status" data-testid="drawer-unchanged">
              没有需要保存的修改。
            </div>
          )}

          <div className="ced-ro" data-testid="drawer-readonly">
            {READ_ONLY_ROWS[kind].map((row) => (
              row.get(snapshot) ? (
                <div key={row.label} className="ced-ro-row">
                  <span>{row.label}</span>
                  <span style={{ wordBreak: "break-all" }}>{row.get(snapshot)}</span>
                </div>
              ) : null
            ))}
          </div>

          {FIELD_DEFS[kind].map((f) => {
            const value = draft[f.name];
            if (f.type === "select") {
              return (
                <FieldShell key={f.name} label={f.label}>
                  <select className="ced-select" data-testid={`field-${f.name}`}
                    value={String(value ?? "")} disabled={disabled}
                    aria-label={f.label}
                    onChange={(e) => update(f.name, e.target.value)}>
                    {(f.options ?? []).map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </FieldShell>
              );
            }
            if (f.type === "list") {
              return (
                <ListEditor key={f.name} label={f.label} name={f.name}
                  items={value as string[]} disabled={disabled}
                  onChange={(items) => update(f.name, items)} />
              );
            }
            if (f.type === "pairs") {
              return (
                <PairsEditor key={f.name} label={f.label} name={f.name}
                  rows={value as { key: string; value: string }[]} disabled={disabled}
                  onChange={(rows) => update(f.name, rows)} />
              );
            }
            if (f.type === "textarea") {
              return (
                <FieldShell key={f.name} label={f.label}>
                  <textarea className="ced-textarea" data-testid={`field-${f.name}`}
                    rows={3} value={String(value ?? "")} disabled={disabled}
                    aria-label={f.label} placeholder={f.placeholder}
                    onChange={(e) => update(f.name, e.target.value)} />
                </FieldShell>
              );
            }
            return (
              <FieldShell key={f.name} label={f.label}>
                <input
                  className="ced-input" data-testid={`field-${f.name}`}
                  type={f.type === "duration" ? "number" : "text"}
                  step={f.type === "duration" ? "0.1" : undefined}
                  min={f.type === "duration" ? "0.1" : undefined}
                  value={String(value ?? "")} disabled={disabled}
                  aria-label={f.label} placeholder={f.placeholder}
                  onChange={(e) => update(f.name, e.target.value)} />
              </FieldShell>
            );
          })}
        </div>

        <footer className="ced-footer">
          <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}>
            {dirty ? "有未保存的修改" : "暂无修改"}
          </span>
          <span style={{ display: "flex", gap: 8 }}>
            <button type="button" className="btn-secondary" data-testid="cancel-edit"
              onClick={requestClose}>
              关闭
            </button>
            <button type="button" className="btn-primary" data-testid="save-card"
              disabled={disabled} onClick={handleSave}>
              {disabled && <Loader2 size={13} className="spin" style={{ marginRight: 4 }} />}
              保存修改
            </button>
          </span>
        </footer>

        {confirmDiscard && (
          <div className="ced-confirm" data-testid="discard-confirm" role="alertdialog"
            aria-label="确认丢弃未保存的修改">
            <div className="ced-confirm-card">
              <p style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>
                有未保存的修改
              </p>
              <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 16 }}>
                关闭将丢弃本次编辑的草稿，且无法恢复。确定要关闭吗？
              </p>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button type="button" className="btn-secondary" data-testid="confirm-keep"
                  onClick={() => setConfirmDiscard(false)}>
                  继续编辑
                </button>
                <button type="button" className="btn-primary" data-testid="confirm-discard"
                  onClick={() => {
                    setConfirmDiscard(false);
                    onDiscard();
                  }}>
                  丢弃修改
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </dialog>
  );
}
