"use client";

import { useEffect, useState } from "react";
import { Check, ChevronLeft, Pencil, Sparkles } from "lucide-react";
import { api, post } from "./api";
import type { Asset, MediaCandidate } from "./types";
import { candidateDisplayName, selectAfterGeneration } from "./visual-decision-state.mjs";

type Job = { id: string; state: string; fingerprint: string; spec: { prompt: string; count: number } };

export default function AssetVisualStudio({ projectId, asset, onBack, onRefresh }: {
  projectId: string; asset: Asset; onBack: () => void; onRefresh: () => Promise<void>;
}) {
  const [candidates, setCandidates] = useState<MediaCandidate[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [prompt, setPrompt] = useState(String(asset.versions[0]?.content.prompt || `${asset.name}，短剧视觉设定参考图`));
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [adapter, setAdapter] = useState<"fake" | "gpt-image-2">("fake");
  const [message, setMessage] = useState("");
  const load = async () => {
    const list = await api<MediaCandidate[]>(`/projects/${projectId}/media-candidates?owner_type=asset&owner_id=${asset.id}`);
    setCandidates(list); setSelected((current) => current && list.some((item) => item.id === current) ? current : list[0]?.id || null);
    return list;
  };
  useEffect(() => { const refresh = () => void load(); void load(); window.addEventListener("script-weaver-project-refresh", refresh); return () => window.removeEventListener("script-weaver-project-refresh", refresh); }, [asset.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const prepare = async (parent?: string) => {
    setBusy(true); setMessage("");
    try {
      setJob(await post<Job>("/generation-jobs", {
        project_id: projectId, owner_type: "asset", owner_id: asset.id, prompt,
        count: parent ? 1 : 3, model: "gpt-image-2", parameters: { size: "1024x1024" },
        reference_asset_version_ids: [], adapter,
        output_spec: { mime: "image/png", role: "asset_reference" }, parent_candidate_id: parent,
        target_asset_id: asset.id,
      }));
    } catch (error) { setMessage(error instanceof Error ? error.message : "准备失败"); }
    finally { setBusy(false); }
  };
  const run = async () => {
    if (!job) return; setBusy(true);
    try {
      const confirmed = await post<Job & { confirmation_token: string }>(`/generation-jobs/${job.id}/confirm`, { fingerprint: job.fingerprint });
      await post(`/generation-jobs/${job.id}/run`, { confirmation_token: confirmed.confirmation_token });
      const completedJobId = job.id;
      setJob(null); setMessage("图片已生成，但仍是候选；接受后才会成为正式 REF。");
      const list = await load();
      setSelected((current) => selectAfterGeneration(list, completedJobId, job.spec.count, current));
    } catch (error) { setMessage(error instanceof Error ? error.message : "生成失败"); }
    finally { setBusy(false); }
  };
  const accept = async () => {
    if (!selected) return; setBusy(true);
    try {
      await post(`/media-candidates/${selected}/accept`, { asset_id: asset.id, expected_asset_revision: asset.revision });
      setMessage("已登记为正式 REF；跟随旧 REF 的下游引用已标记 stale，不会自动重生成。");
      await onRefresh(); await load();
    } catch (error) { setMessage(error instanceof Error ? error.message : "接受失败"); }
    finally { setBusy(false); }
  };
  const current = candidates.find((item) => item.id === selected);
  return <div className="visual-studio">
    <header><button onClick={onBack}><ChevronLeft size={14}/>返回资产库</button><div><small>{asset.kind} · VISUAL DECISION</small><h1>{asset.name}</h1><p>图片候选和文字候选遵循同一原则：只有创作者接受后才成为项目事实。</p></div></header>
    <section className="visual-prompt"><label>本轮图片要求<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)}/></label><label>生成器<select value={adapter} onChange={(event) => setAdapter(event.target.value as "fake" | "gpt-image-2")}><option value="gpt-image-2">GPT Image</option><option value="fake">Fake（测试）</option></select></label><button className="primary-button" disabled={busy || !prompt.trim()} onClick={() => void prepare()}><Sparkles size={14}/>生成 3 张候选</button></section>
    {job?.state === "AWAITING_CONFIRMATION" && <section className="candidate-confirm"><div><strong>确认生成 {job.spec.count} 张图片</strong><p>{job.spec.prompt}</p></div><button className="primary-button" disabled={busy} onClick={() => void run()}>确认并运行</button></section>}
    {message && <p className="visual-message">{message}</p>}
    <section className="candidate-grid" aria-label="图片候选比较">{candidates.map((item) => <button className={item.id === selected ? "selected" : ""} key={item.id} onClick={() => setSelected(item.id)}><img src={`/api/media/${item.id}`} alt={`候选 ${item.candidate_label || item.id.slice(0, 4)}`}/><span>{candidateDisplayName(item)}</span>{item.parent_candidate_id && <small>基于父候选局部修订</small>}</button>)}</section>
    {current && <section className="candidate-detail"><div><small>候选详情</small><p>{current.prompt_text}</p><code>SHA-256 {current.sha256}</code></div><div className="candidate-actions"><button disabled={busy} onClick={() => void prepare(current.id)}><Pencil size={13}/>基于此候选继续编辑</button>{current.candidate_status === "candidate" && <button className="primary-button" disabled={busy} onClick={() => void accept()}><Check size={13}/>接受为正式 REF</button>}</div><div className="candidate-impact"><strong>引用与影响</strong>{current.bindings.length ? current.bindings.map((binding) => <p key={binding.id}>{binding.segment_code} · 镜头 {binding.shot_order + 1} · {binding.binding_mode} {binding.is_stale ? "· stale" : "· 当前引用"}</p>) : <p>尚无分镜引用。正式 REF 可在分镜中绑定；替换时不会自动重生成图片或视频。</p>}<small>Codex 可生成和编辑图片；接受并冻结关键帧后，可从分镜交给本地 MiniMax-H3 生成视频候选。</small></div></section>}
  </div>;
}
