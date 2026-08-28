"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Aperture, Archive, Boxes, ChevronDown, Clock3, Command, FileText, GalleryHorizontalEnd, History, Image, LayoutDashboard, Loader2, Palette, Plus, RefreshCcw, Save, ScrollText, Settings, Sparkles, UserRound, WandSparkles, X } from "lucide-react";
import { ApiError, api, patch, post, put, revisionHint } from "./api";
import type { Asset, ChangeSet, Episode, Project, Segment, Shot } from "./types";
import "./workbench.css";
import "./restore.css";
import "./generation.css";

const demoShots = [
  ["extreme-wide", "static", 3, "A mist-shrouded timber pier recedes into the river, the ferry barely visible beyond the fog.", "Locked frame, fog drifts slowly across the pier, restrained atmospheric movement."],
  ["wide", "static", 4, "An old ferryman stands at the bow, one hand cupped to call toward the silent shore.", "The ferryman raises his head, robe hem and lantern sway in the damp wind."],
  ["wide", "tracking", 5, "A porter crosses the wet boards with baskets balanced on a shoulder pole.", "Low tracking move retreats with his steps, water trembles under the pier."],
  ["close-up", "push_in", 3, "Weathered hands tighten around a frayed mooring rope, droplets gathering on the fibers.", "Slow push toward the knot as the rope strains and the boat shifts behind it."],
] as const;

const SESSION_KEY = "script-weaver-surface-session";

function prompt(shot: Shot, kind: "image" | "video") { return shot.prompts.find((item) => item.kind === kind && item.is_current)?.content || "尚未创建提示词"; }

function describe(reason: unknown, fallback: string): string {
  const message = reason instanceof Error ? reason.message : fallback;
  return `${message}${revisionHint(reason)}`;
}

export default function WorkbenchShell() {
  const [projects, setProjects] = useState<Project[]>([]), [project, setProject] = useState<Project | null>(null), [episode, setEpisode] = useState<Episode | null>(null), [segment, setSegment] = useState<Segment | null>(null), [selectedShot, setSelectedShot] = useState<Shot | null>(null), [changes, setChanges] = useState<ChangeSet[]>([]);
  const [page, setPage] = useState<"storyboard" | "changes" | "assets">("storyboard"), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState(""), [paletteOpen, setPaletteOpen] = useState(false);
  const [surfaceSession, setSurfaceSession] = useState("");
  useEffect(() => {
    let id = sessionStorage.getItem(SESSION_KEY);
    if (!id) { id = crypto.randomUUID(); sessionStorage.setItem(SESSION_KEY, id); }
    setSurfaceSession(id);
  }, []);
  const loadProject = useCallback(async (id: string) => { const loaded = await api<Project>(`/projects/${id}`); const firstEpisode = loaded.episodes[0] ? await api<Episode>(`/episodes/${loaded.episodes[0].id}`) : null; const firstSegment = firstEpisode?.segments[0] ? await api<Segment>(`/segments/${firstEpisode.segments[0].id}`) : null; setProject(loaded); setEpisode(firstEpisode); setSegment(firstSegment); setSelectedShot(firstSegment?.shots?.[0] || null); setChanges(await api<ChangeSet[]>(`/projects/${id}/changesets`)); }, []);
  const load = useCallback(async () => { try { const list = await api<Project[]>("/projects"); setProjects(list); if (list[0]) await loadProject(project?.id || list[0].id); } catch (reason) { setError(describe(reason, "无法连接 script-weaverd")); } }, [loadProject, project?.id]);
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const keydown = (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setPaletteOpen(true); } }; window.addEventListener("keydown", keydown); return () => window.removeEventListener("keydown", keydown); }, []);
  // Publish and freeze the active surface whenever the workbench selection moves.
  // PUTs are queued in call order so an older selection can never overwrite a newer one.
  const surfaceQueue = useRef<Promise<unknown>>(Promise.resolve());
  const selectedShotIds = useMemo(() => (selectedShot && (segment?.shots || []).some((item) => item.id === selectedShot.id) ? [selectedShot.id] : []), [selectedShot, segment]);
  const publishSurface = useCallback(async () => {
    if (!project || !surfaceSession) return null;
    const payload = {
      session_id: surfaceSession,
      project_id: project.id,
      episode_id: episode?.id ?? null,
      segment_id: segment?.id ?? null,
      route: page,
      selected_shot_ids: segment ? selectedShotIds : [],
    };
    const task = surfaceQueue.current.then(() => put(`/surface-contexts/${surfaceSession}`, payload));
    surfaceQueue.current = task.catch(() => undefined);
    return task;
  }, [project, surfaceSession, episode, segment, page, selectedShotIds]);
  useEffect(() => { publishSurface().catch(() => undefined); }, [publishSurface]);
  const selectSegment = async (item: Segment) => { const loaded = await api<Segment>(`/segments/${item.id}`); setSegment(loaded); setSelectedShot(loaded.shots?.[0] || null); };
  const seed = async () => { setBusy(true); setError(""); try { const created = await post<Project>("/projects", { title: "雾渡", format: "short_drama", aspect_ratio: "9:16", prompt_language: "en" }); const ep = await post<Episode>(`/projects/${created.id}/episodes`, { episode_number: 1, title: "雾中来客" }); await post(`/episodes/${ep.id}/scenes`, { order_index: 0, scene_number: "1", heading: { location: "渡口", time: "黎明" }, blocks: [{ type: "action", text: "雾吞没渡口，旧船缓缓靠岸。" }] }); const seg = await post<Segment>(`/episodes/${ep.id}/segments`, { code: "E01-01", order_index: 0, title: "雾渡", target_duration_seconds: 15 }); const shots: Shot[] = []; for (const [shot_size, camera_movement, duration_seconds, image_prompt, video_prompt] of demoShots) shots.push(await post<Shot>(`/segments/${seg.id}/shots`, { order_index: shots.length, shot_size, camera_angle: "eye_level", camera_movement, duration_seconds, image_prompt, video_prompt })); const character = await post<Asset>(`/projects/${created.id}/assets`, { kind: "character", name: "老周", content: { appearance: "weathered ferryman, indigo work coat" } }); const scene = await post<Asset>(`/projects/${created.id}/assets`, { kind: "scene", name: "雾渡口", content: { environment: "old timber pier, dense river fog" } }); for (const [index, shot] of shots.entries()) { await post("/reference-bindings", { shot_id: shot.id, asset_version_id: character.current_version_id, usage: "character", binding_mode: index === 0 ? "frozen" : "follow_latest" }); await post("/reference-bindings", { shot_id: shot.id, asset_version_id: scene.current_version_id, usage: "scene", binding_mode: "frozen" }); } await loadProject(created.id); setProjects(await api<Project[]>("/projects")); } catch (reason) { setError(describe(reason, "创建失败")); } finally { setBusy(false); } };
  const refreshShot = async (shotId: string) => { setSelectedShot(await api<Shot>(`/shots/${shotId}`)); if (segment) setSegment(await api<Segment>(`/segments/${segment.id}`)); };
  const updateShot = async (values: Record<string, unknown>) => { if (!selectedShot) return; setBusy(true); try { setSelectedShot(await patch<Shot>(`/shots/${selectedShot.id}`, { expected_revision: selectedShot.revision, changes: values })); if (segment) setSegment(await api<Segment>(`/segments/${segment.id}`)); } catch (reason) { setError(describe(reason, "保存失败")); await refreshShot(selectedShot.id); } finally { setBusy(false); } };
  const bindingAction = async (id: string, action: "sync" | "freeze" | "restore", version?: string) => { if (!selectedShot) return; try { await post(`/reference-bindings/${id}/actions`, { expected_shot_revision: selectedShot.revision, action, asset_version_id: version }); await refreshShot(selectedShot.id); } catch (reason) { setError(describe(reason, "引用操作失败")); await refreshShot(selectedShot.id); } };
  const restoreShot = async (sourceRevision: number) => { if (!selectedShot) return; try { await post(`/shots/${selectedShot.id}/restore`, { expected_revision: selectedShot.revision, source_revision: sourceRevision }); await refreshShot(selectedShot.id); } catch (reason) { setError(describe(reason, "恢复失败")); await refreshShot(selectedShot.id); } };
  const createAssetVersion = async (asset: Asset) => { try { await post(`/assets/${asset.id}/versions`, { expected_revision: asset.revision, content: { ...asset.versions[0]?.content, updated_from_workbench: new Date().toISOString() } }); if (project) await loadProject(project.id); } catch (reason) { setError(describe(reason, "创建版本失败")); if (project) await loadProject(project.id).catch(() => undefined); } };
  const applyChange = async (change: ChangeSet) => {
    if (!project) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await post(`/changesets/${change.id}/apply`, { fingerprint: change.validated_fingerprint });
      setNotice("变更已应用，项目事实已刷新。");
      await loadProject(project.id);
    } catch (reason) {
      setError(describe(reason, "应用失败"));
      if (reason instanceof ApiError && reason.status === 409) {
        // Surface the backend's terminal state (CONFLICTED + current revisions) instead of
        // leaving a clickable SUBMITTED button around.
        try {
          setChanges(await api<ChangeSet[]>(`/projects/${project.id}/changesets`));
        } catch {
          setError("应用失败且刷新变更列表未成功，请手动刷新页面确认变更状态。");
        }
      }
    } finally { setBusy(false); }
  };
  const episodeSegments = useMemo(() => episode?.segments || [], [episode]);
  return <main className="workbench">
    <header className="topbar"><div className="brand-mark">SW</div><span className="crumb">工作台</span><span className="crumb-separator">›</span><button className="project-switcher" aria-label="切换项目">{project?.title || "未选择项目"}<ChevronDown size={14} /></button><button className="library-button" onClick={() => setPage("assets")}><Archive size={15} />资产库</button></header>
    <nav className="primary-nav" aria-label="主导航"><div className="nav-stack"><NavIcon icon={FileText} label="剧本"/><NavIcon icon={UserRound} label="角色"/><NavIcon icon={Image} label="美术"/><NavIcon icon={GalleryHorizontalEnd} label="分镜" active={page === "storyboard"} onClick={() => setPage("storyboard")}/></div><div className="nav-stack nav-bottom"><NavIcon icon={LayoutDashboard} label="概览"/><NavIcon icon={ScrollText} label="大纲"/><NavIcon icon={History} label="变更" active={page === "changes"} onClick={() => setPage("changes")}/><NavIcon icon={Archive} label="素材库" active={page === "assets"} onClick={() => setPage("assets")}/><NavIcon icon={Settings} label="设置"/></div></nav>
    <aside className="segment-nav"><div className="eyebrow">分镜 · STORYBOARD</div><button className="overview-row">总表<span>{episodeSegments.length} 段</span></button>{episode && <><div className="episode-title">第 {episode.episode_number} 集</div><button className="overview-row">本集总表<span>{episodeSegments.reduce((sum, item) => sum + (item.shot_count || 0), 0)} 镜</span></button></>}{episodeSegments.map((item) => <button key={item.id} className={`segment-row ${segment?.id === item.id ? "active" : ""}`} onClick={() => void selectSegment(item)}><span>{item.code}</span><small>{item.target_duration_seconds || "—"}s · {item.shot_count || 0}镜</small></button>)}{!project && <button className="seed-button" onClick={() => void seed()} disabled={busy}>{busy ? <Loader2 className="spin" size={15}/> : <Plus size={15}/>}创建示例工作台</button>}{projects.length > 1 && <div className="project-list">{projects.map((item) => <button key={item.id} onClick={() => void loadProject(item.id)}>{item.title}</button>)}</div>}</aside>
    <section className="workspace">{error && <div className="error-banner">{error}<button onClick={() => setError("")} aria-label="关闭错误"><X size={14}/></button></div>}{notice && <div className="notice-banner">{notice}<button onClick={() => setNotice("")} aria-label="关闭提示"><X size={14}/></button></div>}{page === "storyboard" && <Storyboard segment={segment} selected={selectedShot} onSelect={setSelectedShot} onRefresh={() => selectedShot && void refreshShot(selectedShot.id)}/>} {page === "changes" && <ChangesPage changes={changes} busy={busy} onApply={applyChange}/>} {page === "assets" && <AssetsPage assets={project?.assets || []} onVersion={createAssetVersion}/>}</section>
    {page === "storyboard" && selectedShot && <><Inspector shot={selectedShot} busy={busy} onUpdate={updateShot} onBindingAction={bindingAction}/><GenerationControl projectId={project!.id} shot={selectedShot}/>{selectedShot.versions.length > 1 && <button className="restore-shot-button" onClick={() => void restoreShot(selectedShot.versions.at(-1)!.revision)}>恢复镜头 r{selectedShot.versions.at(-1)!.revision}</button>}</>}<button className="command-trigger" onClick={() => setPaletteOpen(true)} aria-label="打开命令面板"><Command size={15}/>Ctrl K</button>{paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} onTask={async (intent) => { if (!project) return; try { setBusy(true); await publishSurface(); await post("/tasks", { project_id: project.id, capability: "short-drama-storyboard", intent, surface_session_id: surfaceSession || null, skill_manifest: [{ name: "short-drama-storyboard", version: "1.0" }] }); setPaletteOpen(false); setNotice("已创建 Codex 任务，提案会出现在变更中心。"); } catch (reason) { setError(describe(reason, "任务创建失败")); } finally { setBusy(false); } }}/>}</main>;
}

function NavIcon({ icon: Icon, label, active, onClick }: { icon: typeof FileText; label: string; active?: boolean; onClick?: () => void }) { return <button className={`nav-icon ${active ? "active" : ""}`} onClick={onClick} aria-label={label}><Icon size={20}/><span>{label}</span></button>; }
function Storyboard({ segment, selected, onSelect, onRefresh }: { segment: Segment | null; selected: Shot | null; onSelect: (shot: Shot) => void; onRefresh: () => void }) { if (!segment) return <Empty/>; return <div className="storyboard-page"><div className="page-heading"><div><span className="eyebrow">{segment.code}</span><h1>{segment.title || "分镜"}</h1></div><button className="secondary-button" onClick={onRefresh}><RefreshCcw size={14}/>刷新</button></div><div className="shot-list">{(segment.shots || []).filter((shot) => shot.status !== "retired").map((shot) => <ShotCard key={shot.id} shot={shot} active={selected?.id === shot.id} onClick={() => onSelect(shot)}/>)}</div></div>; }
function ShotCard({ shot, active, onClick }: { shot: Shot; active: boolean; onClick: () => void }) { const stale = shot.bindings.some((item) => item.is_stale); const media = shot.media.find((item) => item.is_current && item.mime.startsWith("image/")); return <article className={`shot-card ${active ? "selected" : ""}`} onClick={onClick}><div className="shot-heading"><strong>#{shot.order_index + 1}</strong><span>{shot.duration_seconds}s</span><span>·</span><span>{shot.shot_size}</span><span>·</span><span>{shot.camera_movement}</span>{stale && <em>上游已换图</em>}</div><div className="shot-body"><div className="frame-wrap"><span>分镜图</span><img src={media ? `/api/media/${media.id}` : "/assets/mist-ferry-pier.png"} alt={`镜头 ${shot.order_index + 1} 雾渡分镜`} style={{ objectPosition: `${35 + shot.order_index * 12}% center` }}/></div><div className="prompt-column"><span className="field-label">画面提示词</span><p>{prompt(shot, "image")}</p><span className="field-label">视频提示词</span><p>{prompt(shot, "video")}</p></div><div className="binding-column"><div className="binding-header"><span>引用资产</span>{stale && <b>需同步</b>}</div>{shot.bindings.map((item) => <div className={`binding-chip ${item.is_stale ? "stale" : ""}`} key={item.id}><Boxes size={15}/><div><small>{item.usage} · {item.binding_mode === "frozen" ? "已冻结" : "跟随最新"}</small><strong>{item.name}</strong></div><span>v{item.asset_version_id.slice(0, 3)}</span></div>)}</div></div></article>; }
function Inspector({ shot, busy, onUpdate, onBindingAction }: { shot: Shot; busy: boolean; onUpdate: (changes: Record<string, unknown>) => void; onBindingAction: (id: string, action: "sync" | "freeze" | "restore", version?: string) => void }) { const [duration, setDuration] = useState(String(shot.duration_seconds)); useEffect(() => setDuration(String(shot.duration_seconds)), [shot]); return <aside className="inspector"><div className="inspector-title"><div><span className="eyebrow">镜头检查器</span><h2>SHOT {String(shot.order_index + 1).padStart(2, "0")}</h2></div><span className="revision">r{shot.revision}</span></div><label>秒数<div className="duration-input"><input aria-label="镜头秒数" type="number" min="0.1" max="600" step="0.1" value={duration} onChange={(event) => setDuration(event.target.value)}/><span>秒</span><button onClick={() => onUpdate({ duration_seconds: Number(duration) })} disabled={busy}><Save size={14}/>保存</button></div></label><label>景别<select aria-label="景别" value={shot.shot_size} onChange={(event) => onUpdate({ shot_size: event.target.value })}><option>extreme-wide</option><option>wide</option><option>medium</option><option>close-up</option></select></label><label>机位<select aria-label="机位" value={shot.camera_angle} onChange={(event) => onUpdate({ camera_angle: event.target.value })}><option>eye_level</option><option>low_angle</option><option>high_angle</option><option>over_shoulder</option></select></label><label>运镜<select aria-label="运镜" value={shot.camera_movement} onChange={(event) => onUpdate({ camera_movement: event.target.value })}><option>static</option><option>tracking</option><option>push_in</option><option>handheld</option></select></label><div className="inspector-section"><div className="section-title"><History size={15}/>引用与版本</div>{shot.bindings.map((item) => <div className="version-row" key={item.id}><div><strong>{item.name}</strong><small>{item.binding_mode} · {item.is_stale ? "上游已变化" : "当前"}</small></div><div className="version-actions">{item.is_stale && <button onClick={() => onBindingAction(item.id, "sync")}>同步</button>}<button onClick={() => onBindingAction(item.id, "freeze")}>冻结当前</button>{item.is_stale && <button onClick={() => onBindingAction(item.id, "restore", item.asset_version_id)}>恢复</button>}</div></div>)}</div><div className="inspector-section"><div className="section-title"><Clock3 size={15}/>提示词版本</div>{shot.prompts.map((item) => <div className="history-row" key={item.id}><span>{item.kind}</span><span>v{item.version_number}</span><small>{item.is_current ? "当前" : "历史"}</small></div>)}</div></aside>; }
function ChangesPage({ changes, busy, onApply }: { changes: ChangeSet[]; busy: boolean; onApply: (change: ChangeSet) => void }) { return <div className="content-page"><div className="page-heading"><div><span className="eyebrow">CHANGESETS</span><h1>变更中心</h1></div></div>{changes.length ? changes.map((change) => <article className="change-card" key={change.id}><header><div><strong>{change.summary || "未命名变更"}</strong><span className={`status ${change.status.toLowerCase()}`}>{change.status}</span></div><small>{change.operations.length} 项操作 · {change.impacts.length} 项影响</small></header><div className="change-ops">{change.operations.map((op) => <div key={op.ordinal}><b>{op.op}</b><span>{op.target_type} · {op.target_id?.slice(0, 8)}</span></div>)}</div>{change.warnings.map((warning) => <p className="warning" key={warning.code}>{warning.message}</p>)}{change.status === "SUBMITTED" && <button className="primary-button" onClick={() => onApply(change)} disabled={busy}>应用变更</button>}</article>) : <div className="empty-inline">暂无 ChangeSet。Codex 提案会出现在这里。</div>}</div>; }
function AssetsPage({ assets, onVersion }: { assets: Asset[]; onVersion: (asset: Asset) => void }) { return <div className="content-page"><div className="page-heading"><div><span className="eyebrow">ASSET LIBRARY</span><h1>资产库</h1></div></div><div className="asset-grid">{assets.map((asset) => <article className="asset-card" key={asset.id}><div className="asset-visual"><Palette size={28}/></div><small>{asset.kind}</small><h3>{asset.name}</h3><p>当前 v{asset.versions[0]?.version_number || 1} · {asset.versions.length} 个历史版本</p><button className="secondary-button" onClick={() => onVersion(asset)}><Plus size={14}/>创建新版本</button></article>)}</div></div>; }
function CommandPalette({ onClose, onTask }: { onClose: () => void; onTask: (intent: string) => Promise<void> }) { const [value, setValue] = useState(""); return <div className="palette-backdrop" onMouseDown={onClose}><div className="palette" onMouseDown={(event) => event.stopPropagation()}><div className="palette-input"><WandSparkles size={18}/><input autoFocus aria-label="Codex 命令" placeholder="描述你希望 Codex 如何调整当前分镜…" value={value} onChange={(event) => setValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && value.trim()) void onTask(value.trim()); if (event.key === "Escape") onClose(); }}/><kbd>Esc</kbd></div><p>任务会冻结当前选择与 revision，结果只会成为待审阅 ChangeSet。</p></div></div>; }
function Empty() { return <div className="empty-state"><div className="empty-icon"><Aperture size={28}/></div><h1>从结构化分镜开始</h1><p>创建示例工作台，或启动 daemon 后载入已有项目。</p><div className="empty-principle"><Sparkles size={15}/>项目事实保存在本地 SQLite，而不是聊天上下文。</div></div>; }

type GenerationJob = { id: string; state: string; fingerprint: string; spec: { prompt: string; count: number; model: string; adapter: string } };

function GenerationControl({ projectId, shot }: { projectId: string; shot: Shot }) {
  const [job, setJob] = useState<GenerationJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [adapter, setAdapter] = useState<"fake" | "gpt-image-2">("fake");
  const prepare = async () => {
    setBusy(true); setMessage("");
    try { setJob(await post<GenerationJob>("/generation-jobs", { project_id: projectId, owner_type: "shot", owner_id: shot.id, prompt: prompt(shot, "image"), count: 1, model: "gpt-image-2", parameters: { size: "1024x1024" }, reference_asset_version_ids: shot.bindings.map((item) => item.asset_version_id), adapter, output_spec: { mime: "image/png", role: "storyboard_frame" } })); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "准备失败"); } finally { setBusy(false); }
  };
  const confirm = async () => {
    if (!job) return; setBusy(true);
    try {
      const confirmed = await post<GenerationJob & { confirmation_token: string }>(`/generation-jobs/${job.id}/confirm`, { fingerprint: job.fingerprint });
      const finished = await post<GenerationJob>(`/generation-jobs/${job.id}/run`, { confirmation_token: confirmed.confirmation_token });
      setJob(finished);
      setMessage(finished.state === "SUCCEEDED" ? "图片已作为不可变 MediaVersion 摄取" : `生成未完成：${finished.state}`);
      if (finished.state === "SUCCEEDED") setTimeout(() => window.location.reload(), 500);
    }
    catch (reason) { setMessage(describe(reason, "生成失败")); } finally { setBusy(false); }
  };
  return <><select className="adapter-select" aria-label="图片生成适配器" value={adapter} onChange={(event) => setAdapter(event.target.value as "fake" | "gpt-image-2")}><option value="fake">Fake</option><option value="gpt-image-2">GPT Image 2</option></select><button className="generate-button" onClick={() => void prepare()} disabled={busy}><Sparkles size={13}/>{busy ? "处理中" : "生成分镜图"}</button>{job?.state === "AWAITING_CONFIRMATION" && <div className="generation-backdrop"><section className="generation-dialog" role="dialog" aria-modal="true" aria-label="确认图片生成"><span className="eyebrow">GENERATION PREVIEW</span><h2>确认本次图片生成</h2><dl><div><dt>Prompt</dt><dd>{job.spec.prompt}</dd></div><div><dt>数量 / 模型</dt><dd>{job.spec.count} 张 · {job.spec.model}</dd></div><div><dt>Adapter</dt><dd>{job.spec.adapter}</dd></div><div><dt>Fingerprint</dt><dd className="fingerprint">{job.fingerprint}</dd></div></dl><p>任何直接输入变化都会使本次确认失效；令牌只消费一次。</p><footer><button className="secondary-button" onClick={() => setJob(null)}>取消</button><button className="primary-button" onClick={() => void confirm()} disabled={busy}>{busy ? "生成中…" : "确认并运行"}</button></footer></section></div>}{message && <div className="generation-toast">{message}<button onClick={() => setMessage("")} aria-label="关闭提示"><X size={12}/></button></div>}</>;
}
