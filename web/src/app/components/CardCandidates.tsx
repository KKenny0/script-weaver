"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, RefreshCw, Sparkles } from "lucide-react";
import type { CardEditSession } from "./CardEditDrawer";

const labels: Record<string, string> = { ready: "待采用", stale: "已过期 · 项目发生变化，请重新发起", accepted: "已采用", rejected: "已放弃" };

/** Render a candidate value readably: strings as-is, others as compact JSON. */
function previewValue(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

/** Each mounted drawer owns its requests. No response can survive cleanup,
 * including delayed JSON parsing, catch and finally. Runs live on the server. */
export default function CardCandidates({ session, dirty, onAccepted, onReload, fieldLabels }: {
  session: CardEditSession; dirty: boolean; onAccepted: (snapshot: any) => void; onReload: () => void;
  fieldLabels: Record<string, string>;
}) {
  const [instruction, setInstruction] = useState("");
  const [data, setData] = useState<{ candidates: any[]; runs: any[] }>({ candidates: [], runs: [] });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const lifetime = useRef(0);
  const readRequest = useRef(0);
  const actionRequest = useRef(0);
  const working = useRef(false);
  const base = `/api/projects/${session.projectId}`;
  const relevant = (item: any) => item.kind === session.kind && item.target_id === session.id;
  const runs = data.runs.filter(r => r.target?.kind === session.kind && r.target?.id === session.id);
  const active = runs.find(r => r.status === "running" || r.status === "stopping");

  const read = async (epoch: number) => {
    const request = ++readRequest.current;
    const current = () => lifetime.current === epoch && readRequest.current === request;
    try {
      const response = await fetch(`${base}/candidates`);
      const body = await response.json();
      if (!current()) return;
      if (!response.ok) throw new Error(body.detail?.message || "读取候选失败");
      setData(body);
    } catch (e: any) {
      if (current()) setError(e.message || "读取候选失败，可关闭后重开");
    }
  };

  useEffect(() => {
    const epoch = ++lifetime.current;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!working.current) await read(epoch);
      if (lifetime.current === epoch) timer = setTimeout(poll, 1500);
    };
    void poll();
    return () => { lifetime.current++; readRequest.current++; actionRequest.current++; clearTimeout(timer); };
    // A keyed drawer gives each project/open a new lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const act = async (path: string, body?: any, adopt = false) => {
    if (working.current) return;
    const epoch = lifetime.current;
    const request = ++actionRequest.current;
    const current = () => lifetime.current === epoch && actionRequest.current === request;
    working.current = true;
    readRequest.current++;
    setBusy(true); setError("");
    try {
      const response = await fetch(`${base}${path}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const result = await response.json();
      if (!current()) return;
      if (!response.ok) throw new Error(result.detail?.message || `请求失败 (${response.status})`);
      if (adopt) { onAccepted(result); return; }
      await read(epoch);
    } catch (e: any) {
      if (current()) { setError(e.message || "请求失败，请重新发起"); await read(epoch); }
    } finally {
      if (current()) { working.current = false; setBusy(false); }
    }
  };

  const accept = (id: string) => {
    if (dirty) { setConfirmId(id); return; }
    void act(`/candidates/${id}/accept`, undefined, true);
  };

  return <section className="ced-ai" aria-label="AI 定向修改" data-testid="card-candidates">
    <div className="ced-ai-title"><Sparkles size={13} /> AI 定向修改</div>
    <p className="ced-ai-hint">候选采用前不会改变当前内容。生成期间仍可手工编辑。</p>
    <label htmlFor={`candidate-instruction-${session.seq}`} className="ced-label">修改要求</label>
    <textarea id={`candidate-instruction-${session.seq}`} className="ced-textarea" value={instruction}
      onChange={e => setInstruction(e.target.value)} rows={2}
      placeholder="例如：把这一镜改成手持跟拍，增强紧迫感" />
    <div className="ced-ai-toolbar">
      <button type="button" className="btn-secondary" style={{ fontSize: 12, padding: "6px 12px" }} disabled={busy || !!active || !instruction.trim()}
        onClick={() => void act(`/artifacts/${session.kind}/${encodeURIComponent(session.id)}/candidates`, {
          instruction, expected_revision: session.revision, request_key: crypto.randomUUID(),
        })}>生成候选</button>
      <button type="button" className="btn-ghost" style={{ width: "auto", padding: "0 10px", fontSize: 12, gap: 4 }} onClick={() => {
        if (!dirty || window.confirm("载入最新内容将丢弃未保存的手工草稿，继续吗？")) onReload();
      }}><RefreshCw size={12} />载入最新内容后重新发起</button>
    </div>
    {active && <div className="ced-ai-status" role="status">
      <Loader2 size={12} className="spin" />
      {active.last_progress?.message || "正在生成候选…"}
      <button type="button" className="btn-secondary" style={{ fontSize: 12, padding: "4px 10px" }} disabled={busy || active.status === "stopping"}
        onClick={() => void act(`/runs/${active.run_id}/stop`)}>停止候选生成</button>
    </div>}
    {error && <p className="ced-ai-error" role="alert">{error}</p>}
    {runs.filter(r => ["failed", "cancelled", "interrupted"].includes(r.status)).map(run =>
      <p key={run.run_id} className="ced-ai-note" role="status">{run.error || "候选生成已停止"}；原内容保留，可重新发起。</p>)}
    {data.candidates.filter(relevant).map(candidate => <details key={candidate.id} className="ced-candidate" open={candidate.status === "ready"}>
      <summary>{labels[candidate.status]} · 依据 r{candidate.base_revision}{candidate.accepted_revision ? ` → r${candidate.accepted_revision}` : ""}</summary>
      {Object.keys(candidate.changes).map(field => <div key={field} className="ced-diff-field">
        <strong>{fieldLabels[field] ?? field}</strong>
        <div className="ced-diff-row before"><span className="ced-diff-label">原值</span>{previewValue(candidate.original[field])}</div>
        <div className="ced-diff-row after"><span className="ced-diff-label">候选</span>{previewValue(candidate.proposed[field])}</div>
      </div>)}
      {candidate.status === "ready" && <div className="ced-candidate-actions">
        <button type="button" className="btn-primary" style={{ fontSize: 12, padding: "5px 12px" }} disabled={busy} onClick={() => accept(candidate.id)}>采用候选</button>
        {["ready", "stale"].includes(candidate.status) && <button type="button" className="btn-secondary" style={{ fontSize: 12, padding: "5px 12px" }} disabled={busy}
          onClick={() => void act(`/candidates/${candidate.id}/reject`)}>放弃候选</button>}
      </div>}
    </details>)}
    {confirmId && <div className="ced-confirm" role="alertdialog" aria-label="采用候选前确认丢弃草稿">
      <div className="ced-confirm-card">
        <p>采用候选会丢弃当前未保存的手工草稿。</p>
        <button type="button" className="btn-secondary" onClick={() => setConfirmId(null)}>继续编辑</button>
        <button type="button" className="btn-primary" onClick={() => {
          const id = confirmId; setConfirmId(null); void act(`/candidates/${id}/accept`, undefined, true);
        }}>丢弃草稿并采用</button>
      </div>
    </div>}
  </section>;
}
