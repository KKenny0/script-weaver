"use client";

import { useEffect, useRef, useState } from "react";
import type { CardEditSession } from "./CardEditDrawer";

const labels: Record<string, string> = { ready: "待采用", stale: "已过期 · 项目发生变化，请重新发起", accepted: "已采用", rejected: "已放弃" };

/** Each mounted drawer owns its requests. No response can survive cleanup,
 * including delayed JSON parsing, catch and finally. Runs live on the server. */
export default function CardCandidates({ session, dirty, onAccepted, onReload, fieldLabels }: {
  session: Omit<CardEditSession, "kind"> & { kind: CardEditSession["kind"] | "global" }; dirty: boolean; fieldLabels: Record<string, string>; onAccepted: (snapshot: any) => void; onReload: (snapshot?: any) => void;
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
  const relevant = (item: any) => item.kind === session.kind && (session.kind === "global" || item.target_id === session.id);
  const runs = data.runs.filter(r => r.target?.kind === session.kind && (session.kind === "global" || r.target?.id === session.id));
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
      if (adopt) { onAccepted(result); await read(epoch); return; }
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

  return <section className="ced-ro" aria-label={session.kind === "global" ? "全局修改候选" : "AI 定向修改"} data-testid={session.kind === "global" ? "global-candidates" : "card-candidates"}>
    <h4>{session.kind === "global" ? "全局修改候选" : "AI 定向修改"}</h4>
    <p style={{ fontSize: 12 }}>候选采用前不会改变当前内容。生成期间仍可手工编辑。</p>
    <button type="button" className="btn-ghost" disabled={busy} onClick={async () => {
      if (working.current) return;
      if (session.kind !== "global") {
        if (!dirty || window.confirm("载入最新内容将丢弃未保存的手工草稿，继续吗？")) onReload();
        return;
      }
      working.current = true; setBusy(true);
      const epoch = lifetime.current, request = ++actionRequest.current;
      const current = () => lifetime.current === epoch && actionRequest.current === request;
      try {
        const response = await fetch(base);
        const body = await response.json();
        if (!current()) return;
        if (!response.ok) throw new Error("载入最新内容失败");
        onReload(body);
      } catch (e: any) { if (current()) setError(e.message); }
      finally { if (current()) { working.current = false; setBusy(false); } }
    }}>载入最新内容后重新发起</button>
    {session.kind !== "global" && <>
    <label htmlFor={`candidate-instruction-${session.seq}`}>修改要求</label>
    <textarea id={`candidate-instruction-${session.seq}`} className="ced-textarea" value={instruction}
      onChange={e => setInstruction(e.target.value)} rows={2} />
    <button type="button" className="btn-secondary" disabled={busy || !!active || !instruction.trim()}
      onClick={() => void act(`/artifacts/${session.kind}/${encodeURIComponent(session.id)}/candidates`, {
        instruction, expected_revision: session.revision, request_key: crypto.randomUUID(),
      })}>生成候选</button>
    </>}
    {active && <div role="status">
      {active.last_progress?.message || "正在生成候选…"}
      <button type="button" className="btn-secondary" disabled={busy || active.status === "stopping"}
        onClick={() => void act(`/runs/${active.run_id}/stop`)}>停止候选生成</button>
    </div>}
    {error && <p role="alert">{error}</p>}
    {runs.filter(r => ["failed", "cancelled", "interrupted"].includes(r.status)).map(run =>
      <p key={run.run_id} role="status">{run.error || "候选生成已停止"}；原内容保留，可重新发起。</p>)}
    {data.candidates.filter(relevant).map(candidate => <details key={candidate.id} open={candidate.status === "ready"}>
      <summary>{labels[candidate.status]} · 依据 r{candidate.base_revision}{candidate.accepted_revision ? ` → r${candidate.accepted_revision}` : ""}</summary>
      {session.kind === "global" && <p>替换范围：{fieldLabels[candidate.target_id] || candidate.target_id}整件产物<br />
        原始要求：{data.runs.find(r => r.run_id === candidate.run_id)?.instruction}<br />
        保守下游影响：{(candidate.affected_artifacts || []).map((a: string) => fieldLabels[a] || a).join("、") || "无已有下游产物"}；采用后标记待复核，不自动重生成。</p>}
      {candidate.diff?.map((change: any) => <div key={change.path} style={{ overflowWrap: "anywhere", margin: "8px 0" }}>
        <strong>{change.path}</strong><div>原值：{change.before_exists === false ? "不存在" : JSON.stringify(change.before)}</div><div>候选：{change.after_exists === false ? "不存在" : JSON.stringify(change.after)}</div>
      </div>)}
      <details open={session.kind !== "global"}><summary>完整原值与候选</summary>
      {Object.keys(candidate.changes).map(field => <div key={field} style={{ margin: "8px 0", overflowWrap: "anywhere" }}>
        <strong>{fieldLabels[field] ?? field}</strong>
        <div>原值：<pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(candidate.original[field], null, 2)}</pre></div>
        <div>候选：<pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(candidate.proposed[field], null, 2)}</pre></div>
      </div>)}
      </details>
      {candidate.status === "ready" && <button type="button" className="btn-primary" disabled={busy} onClick={() => accept(candidate.id)}>采用候选</button>}
      {["ready", "stale"].includes(candidate.status) && <button type="button" className="btn-secondary" disabled={busy}
        onClick={() => void act(`/candidates/${candidate.id}/reject`)}>放弃候选</button>}
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
