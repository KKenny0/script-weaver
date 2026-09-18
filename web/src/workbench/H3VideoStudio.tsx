"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, ChevronLeft, Film, PackageCheck, RefreshCcw } from "lucide-react";
import { api, post } from "./api";
import type { Binding, MediaCandidate, Project, Shot } from "./types";
import { canStartNewH3Round, h3Readiness, resolveKeyframeChoice } from "./h3-state.mjs";

type Job = {
  id: string; state: string; fingerprint: string; external_status?: string;
  spec: { prompt: string; duration_seconds: number; shot_id: string; keyframes: { media_id: string; binding_id: string; frame_index: 0 | -1 }[] };
  error?: { message?: string; code?: string };
};
type Package = {
  id: string; version_number: number;
  manifest: {
    ready: boolean; missing: Record<string, unknown>[]; failed: Record<string, unknown>[];
    stale: Record<string, string[]>; excluded: unknown[];
    stale_details?: { documents?: Record<string, unknown>[]; media?: Record<string, unknown>[] };
  };
};

function PackageIssues({ item, currentShotId, onDocuments, onShot, onCurrentShot }: {
  item: Package; currentShotId: string; onDocuments: (episodeId?: string) => void;
  onShot: (issue: Record<string, unknown>) => void; onCurrentShot: () => void;
}) {
  const { manifest } = item;
  const documents = manifest.stale_details?.documents || [];
  const media = manifest.stale_details?.media || [];
  const unresolved = manifest.missing.length + manifest.failed.length
    + Object.values(manifest.stale).reduce((sum, values) => sum + values.length, 0);
  if (!unresolved) return null;
  return <details className="package-issues" open><summary>查看 {unresolved} 个待处理问题</summary>
    {manifest.missing.length > 0 && <section><strong>缺失</strong>{manifest.missing.map((issue, index) => {
      const type = String(issue.type || "fact");
      const episodeId = typeof issue.episode_id === "string" ? issue.episode_id : undefined;
      return <div key={`${type}-${index}`}><span>{type === "accepted_screenplay" ? "缺少已接受剧本" : "缺少正式视频"} · {String(issue.episode_id || issue.shot_id || "未知对象")}</span>{type === "accepted_screenplay" && <button onClick={() => onDocuments(episodeId)}>回到本集剧本</button>}{issue.shot_id === currentShotId && <button onClick={onCurrentShot}>为当前镜头生成</button>}</div>;
    })}</section>}
    {documents.length > 0 && <section><strong>剧本文档已过期</strong>{documents.map((issue) => <div key={String(issue.id)}><span>{String(issue.title || issue.id)} · {String(issue.reason || "需要更新")}</span><button onClick={() => onDocuments(typeof issue.episode_id === "string" ? issue.episode_id : undefined)}>回到文档处理</button></div>)}</section>}
    {media.length > 0 && <section><strong>正式媒体已过期</strong>{media.map((issue) => {
      const isCurrent = issue.owner_type === "shot" && issue.owner_id === currentShotId;
      const label = issue.owner_type === "shot" ? `EP${String(issue.episode_number || "?").padStart(2, "0")} · ${String(issue.segment_code || "分镜")} · 镜头 #${Number(issue.shot_order ?? 0) + 1}` : `${String(issue.kind || "media")} · ${String(issue.id)}`;
      return <div key={String(issue.id)}><span>{label}</span><button onClick={() => isCurrent ? onCurrentShot() : onShot(issue)}>{isCurrent ? "开始新一轮 H3" : "打开对应镜头"}</button></div>;
    })}</section>}
    {manifest.failed.length > 0 && <section><strong>生成失败</strong>{manifest.failed.map((issue) => <div key={String(issue.id)}><span>{String(issue.kind || "生成")} · {String(issue.owner_id || issue.id)}</span>{issue.owner_type === "shot" && issue.owner_id === currentShotId && <button onClick={onCurrentShot}>重新准备</button>}</div>)}</section>}
  </details>;
}

export default function H3VideoStudio({ project, shot, onBack, onRefresh, onNextEpisode, hasNextEpisode, onNextSeason, onNewWork, onLibrary, onOpenDocuments, onOpenShotH3 }: {
  project: Project; shot: Shot; onBack: () => void; onRefresh: () => Promise<void>;
  onNextEpisode: () => void; hasNextEpisode: boolean; onNextSeason: () => void;
  onNewWork: () => void; onLibrary: () => void;
  onOpenDocuments: (episodeId?: string) => void;
  onOpenShotH3: (issue: Record<string, unknown>) => void;
}) {
  type KeyframeChoice = { binding: Binding; media: MediaCandidate };
  const videoPrompt = shot.prompts.find((item) => item.kind === "video" && item.is_current)?.content || "";
  const [prompt, setPrompt] = useState(videoPrompt);
  const [duration, setDuration] = useState(Math.max(4, Math.min(15, Math.round(shot.duration_seconds))));
  const [job, setJob] = useState<Job | null>(null);
  const [ignoredJobId, setIgnoredJobId] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<MediaCandidate[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [keyframes, setKeyframes] = useState<KeyframeChoice[]>([]);
  const [keyframeChoice, setKeyframeChoice] = useState<KeyframeChoice | null>(null);
  const [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  const [productionPackage, setProductionPackage] = useState<Package | null>(null);
  const current = useMemo(() => candidates.find((item) => item.id === selected), [candidates, selected]);
  const target = keyframeChoice?.binding || null;
  const keyframe = keyframeChoice?.media || null;
  const readiness = h3Readiness(target, keyframe, prompt, duration);
  const locked = Boolean(job);

  const load = async () => {
    const [allMedia, latest] = await Promise.all([
      api<MediaCandidate[]>(`/projects/${project.id}/media-candidates?owner_type=shot&owner_id=${shot.id}`),
      api<Job | null>(`/projects/${project.id}/shots/${shot.id}/h3-video-job`),
    ]);
    const videos = allMedia.filter((item) => item.kind === "video");
    setCandidates(videos);
    setSelected((value) => value && videos.some((item) => item.id === value) ? value : videos[0]?.id || null);
    if (latest && latest.spec.shot_id === shot.id && latest.id !== ignoredJobId) setJob(latest);
    const frozen = shot.bindings.filter((binding) => binding.binding_mode === "frozen" && !binding.is_stale);
    const choices = (await Promise.all(frozen.map(async (binding) => {
      const refs = await api<MediaCandidate[]>(`/projects/${project.id}/media-candidates?owner_type=asset&owner_id=${binding.asset_id}`);
      const media = refs.find((item) => item.candidate_status === "accepted" && item.accepted_asset_version_id === binding.asset_version_id && item.mime === "image/png" && !item.is_stale);
      return media ? { binding, media } : null;
    }))).filter((item): item is KeyframeChoice => Boolean(item));
    setKeyframes(choices);
    setKeyframeChoice((value) => {
      return resolveKeyframeChoice(
        choices, value?.binding.id || null, latest?.spec.keyframes?.[0]?.binding_id || null,
      );
    });
  };

  useEffect(() => {
    setPrompt(videoPrompt);
    setDuration(Math.max(4, Math.min(15, Math.round(shot.duration_seconds))));
    setJob(null); setIgnoredJobId(null); setProductionPackage(null); setMessage("");
    void load();
  }, [shot.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const refresh = () => void load();
    window.addEventListener("script-weaver-project-refresh", refresh);
    return () => window.removeEventListener("script-weaver-project-refresh", refresh);
  });
  useEffect(() => {
    if (!job || !["H3_SUBMITTED", "H3_RUNNING"].includes(job.state)) return;
    const timer = window.setTimeout(() => void poll(), 1800);
    return () => window.clearTimeout(timer);
  }, [job?.id, job?.state]); // eslint-disable-line react-hooks/exhaustive-deps

  const prepare = async () => {
    if (!target || !keyframe) return;
    setBusy(true); setMessage("");
    try {
      const prepared = await post<Job>("/h3/video-jobs", {
        project_id: project.id, shot_id: shot.id, target_asset_id: target.asset_id,
        prompt, keyframes: [{ media_id: keyframe.id, binding_id: target.id, frame_index: 0 }],
        duration_seconds: duration,
      });
      setIgnoredJobId(null); setJob(prepared);
    } catch (error) { setMessage(error instanceof Error ? error.message : "H3 准备失败"); }
    finally { setBusy(false); }
  };
  const submit = async () => {
    if (!job || job.spec.shot_id !== shot.id) return;
    setBusy(true);
    try {
      const confirmed = await post<Job & { confirmation_token: string }>(`/generation-jobs/${job.id}/confirm`, { fingerprint: job.fingerprint });
      setJob(await post<Job>(`/h3/video-jobs/${job.id}/submit`, { confirmation_token: confirmed.confirmation_token }));
      setMessage("任务已提交给本地 MiniMax-H3；完成前不会成为项目事实。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "H3 提交失败"); }
    finally { setBusy(false); }
  };
  const poll = async () => {
    if (!job || job.spec.shot_id !== shot.id) return;
    try {
      const next = await post<Job>(`/h3/video-jobs/${job.id}/poll`);
      setJob(next);
      if (next.state === "SUCCEEDED") {
        await load(); setMessage("视频已返回，但仍是候选；接受后才成为本镜头的正式视频。");
      }
    } catch (error) { setMessage(error instanceof Error ? error.message : "H3 状态刷新失败"); }
  };
  const reprepare = () => {
    if (job) setIgnoredJobId(job.id);
    setJob(null); setProductionPackage(null); setMessage("可以修改输入并重新准备；旧任务会保留在历史中。");
  };
  const accept = async () => {
    if (!current) return;
    setBusy(true);
    try {
      await post(`/media-candidates/${current.id}/accept`, { expected_shot_revision: shot.revision });
      await onRefresh(); await load(); setMessage("视频候选已接受为本镜头的正式视频；不会替换图片 REF。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "接受失败"); }
    finally { setBusy(false); }
  };
  const buildPackage = async () => {
    setBusy(true);
    try { setProductionPackage(await post<Package>(`/projects/${project.id}/production-packages`, { expected_project_revision: project.revision })); }
    catch (error) { setMessage(error instanceof Error ? error.message : "生产包生成失败"); }
    finally { setBusy(false); }
  };

  const terminalProblem = job && ["FAILED", "STALE", "CONFIRMED", "SUBMITTING"].includes(job.state);
  return <div className="h3-studio"><header><button onClick={onBack}><ChevronLeft size={14}/>返回分镜</button><div><small>LOCAL VIDEO · FL2VA / 768</small><h1>镜头 #{shot.order_index + 1} · MiniMax-H3</h1><p>冻结关键帧和视频提示词会交给你本地部署的 H3；返回视频仍需创作者接受。</p></div></header>
    <section className="h3-inputs"><div><strong>冻结关键帧</strong>{keyframes.length > 1 && <label>选择精确绑定<select disabled={locked} value={keyframeChoice?.binding.id || ""} onChange={(event) => setKeyframeChoice(keyframes.find((item) => item.binding.id === event.target.value) || null)}><option value="">请选择冻结 REF</option>{keyframes.map((item) => <option key={item.binding.id} value={item.binding.id}>{item.binding.usage} · {item.binding.name} · {item.binding.asset_version_id}</option>)}</select></label>}{keyframe ? <img src={`/api/media/${keyframe.id}`} alt="H3 冻结关键帧"/> : <p>{keyframes.length > 1 ? "该镜头有多个冻结 REF，请明确选择一个。" : "先在镜头检查器中冻结一个已接受的图片 REF。"}</p>}</div><label>视频提示词<textarea disabled={locked} value={prompt} onChange={(event) => setPrompt(event.target.value)}/></label><label>时长（4–15 秒）<input disabled={locked} type="number" min={4} max={15} value={duration} onChange={(event) => setDuration(Number(event.target.value))}/></label></section>
    {!job && <><button className="primary-button" disabled={busy || !readiness.ready} onClick={() => void prepare()}><Film size={14}/>准备本地 H3 任务</button>{!readiness.ready && <p className="visual-message">{readiness.reason}</p>}</>}
    {job && <section className="candidate-confirm"><div><strong>已冻结的任务输入</strong><p>FL2VA · MiniMaxAI/MiniMax-H3 · 768 short edge · {job.spec.duration_seconds}s</p><p>{job.spec.prompt}</p></div>{job.state === "AWAITING_CONFIRMATION" && <button className="primary-button" disabled={busy} onClick={() => void submit()}>确认并提交</button>}</section>}
    {job && ["H3_SUBMITTED", "H3_RUNNING"].includes(job.state) && <button className="secondary-button" onClick={() => void poll()}><RefreshCcw size={13}/>H3 状态：{job.external_status || job.state}</button>}
    {terminalProblem && <div className="visual-message">{job.error?.message || (job.state === "CONFIRMED" || job.state === "SUBMITTING" ? "任务在重启前未完成提交，不能自动重提。" : `H3 任务 ${job.state.toLowerCase()}`)} <button onClick={reprepare}>重新准备</button></div>}
    {job?.state === "AWAITING_CONFIRMATION" && <button className="secondary-button" onClick={reprepare}>修改输入并重新准备</button>}
    {canStartNewH3Round(job?.state) && <button className="secondary-button" onClick={reprepare}>开始新一轮</button>}
    {message && <p className="visual-message">{message}</p>}
    <section className="h3-candidates" aria-label="视频候选比较">{candidates.map((item) => <button className={item.id === selected ? "selected" : ""} key={item.id} onClick={() => setSelected(item.id)}><video src={`/api/media/${item.id}`} controls preload="metadata"/><span>候选 {item.candidate_label} · {item.candidate_status === "accepted" ? `本镜头正式视频 r${item.accepted_shot_revision}` : item.is_stale ? "stale" : "未接受"}</span></button>)}</section>
    {current?.candidate_status === "candidate" && <button className="primary-button" disabled={busy || Boolean(current.is_stale)} onClick={() => void accept()}><Check size={13}/>接受为本镜头正式视频</button>}
    {candidates.some((item) => item.candidate_status === "accepted") && <button className="secondary-button" disabled={busy} onClick={() => void buildPackage()}><PackageCheck size={13}/>生成版本化生产包</button>}
    {productionPackage && <section className="package-result"><strong>生产包 v{productionPackage.version_number} 已冻结</strong><p>missing {productionPackage.manifest.missing.length} · failed {productionPackage.manifest.failed.length} · stale {Object.values(productionPackage.manifest.stale).flat().length} · excluded {productionPackage.manifest.excluded.length}</p><PackageIssues item={productionPackage} currentShotId={shot.id} onDocuments={onOpenDocuments} onShot={onOpenShotH3} onCurrentShot={reprepare}/>{productionPackage.manifest.ready ? <div><button disabled={!hasNextEpisode} onClick={onNextEpisode}>下一集</button><button onClick={onNextSeason}>下一季</button><button onClick={onNewWork}>新作品</button><button onClick={onLibrary}>项目库</button></div> : <p>生产包尚未就绪；请按上方问题逐项处理后重新生成。</p>}</section>}
  </div>;
}
