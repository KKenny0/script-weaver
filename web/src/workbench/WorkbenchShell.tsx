"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Aperture, Archive, Boxes, ChevronDown, Clock3, Command, FileText, Film, GalleryHorizontalEnd, History, Image, LayoutDashboard, Loader2, Palette, Plus, RefreshCcw, RotateCcw, Save, ScrollText, Settings, Sparkles, UserRound, WandSparkles, X } from "lucide-react";
import { ApiError, api, patch, post, put, revisionHint } from "./api";
import type { Asset, ChangeSet, Episode, Project, Segment, Shot, WorkbenchTask } from "./types";
import DocumentWorkspace from "./DocumentWorkspace";
import AssetVisualStudio from "./AssetVisualStudio";
import H3VideoStudio from "./H3VideoStudio";
import { loadLatestEpisodeSelection, resolveSelection, visibleSegmentCode } from "./selection-state.mjs";
import { shotStaleLabel } from "./visual-decision-state.mjs";
import "./workbench.css";
import "./restore.css";
import "./generation.css";
import "./collaboration.css";
import "./phase3.css";

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
  const [archivedProjects, setArchivedProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<WorkbenchTask[]>([]), [tasksOpen, setTasksOpen] = useState(false);
  const [page, setPage] = useState<"library" | "documents" | "storyboard" | "changes" | "assets">("library"), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState(""), [paletteOpen, setPaletteOpen] = useState(false);
  const [documentDirty, setDocumentDirty] = useState(false), [intakeOpen, setIntakeOpen] = useState(false);
  const [seasonSource, setSeasonSource] = useState<Project | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [assetFocusId, setAssetFocusId] = useState<string | null>(null);
  const [h3Shot, setH3Shot] = useState<Shot | null>(null);
  const [surfaceSession, setSurfaceSession] = useState("");
  const documentDirtyRef = useRef(false);
  const eventCursors = useRef<Record<string, string>>({});
  const selectionRef = useRef({ projectId: "", episodeId: null as string | null, segmentId: null as string | null, shotId: null as string | null });
  useEffect(() => { documentDirtyRef.current = documentDirty; }, [documentDirty]);
  useEffect(() => {
    let id = sessionStorage.getItem(SESSION_KEY);
    if (!id) { id = crypto.randomUUID(); sessionStorage.setItem(SESSION_KEY, id); }
    setSurfaceSession(id);
  }, []);
  const loadProject = useCallback(async (id: string) => {
    const loaded = await api<Project>(`/projects/${id}`);
    const initial = resolveSelection(selectionRef.current, loaded, null);
    const loadedEpisode = initial.episodeId ? await api<Episode>(`/episodes/${initial.episodeId}`) : null;
    const resolved = resolveSelection(selectionRef.current, loaded, loadedEpisode);
    const loadedSegment = resolved.segmentId ? await api<Segment>(`/segments/${resolved.segmentId}`) : null;
    const selected = loadedSegment?.shots?.find((item) => item.id === resolved.shotId) || loadedSegment?.shots?.[0] || null;
    selectionRef.current = { ...resolved, shotId: selected?.id || null };
    const [nextChanges, nextTasks] = await Promise.all([api<ChangeSet[]>(`/projects/${id}/changesets`), api<WorkbenchTask[]>(`/tasks?project_id=${encodeURIComponent(id)}`)]);
    setProject(loaded); setEpisode(loadedEpisode); setSegment(loadedSegment); setSelectedShot(selected);
    setChanges(nextChanges); setTasks(nextTasks);
  }, []);
  const load = useCallback(async () => { try { const [list, archived] = await Promise.all([api<Project[]>("/projects"), api<Project[]>("/projects?archived=true")]); setProjects(list); setArchivedProjects(archived); const activeId = project && list.some((item) => item.id === project.id) ? project.id : list[0]?.id; if (activeId) await loadProject(activeId); else { setProject(null); setEpisode(null); setSegment(null); } } catch (reason) { setError(describe(reason, "无法连接 script-weaverd")); } }, [loadProject, project]);
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!project) return;
    const cursor = eventCursors.current[project.id];
    const events = new EventSource(`/api/projects/${encodeURIComponent(project.id)}/events${cursor ? `?after=${encodeURIComponent(cursor)}` : ""}`);
    let timer: number | undefined;
    events.onmessage = (event) => {
      if (event.lastEventId) eventCursors.current[project.id] = event.lastEventId;
      window.clearTimeout(timer);
      timer = window.setTimeout(async () => {
        try {
          const [nextChanges, nextTasks] = await Promise.all([api<ChangeSet[]>(`/projects/${project.id}/changesets`), api<WorkbenchTask[]>(`/tasks?project_id=${encodeURIComponent(project.id)}`)]);
          setChanges(nextChanges); setTasks(nextTasks);
          if (!documentDirtyRef.current) await loadProject(project.id);
          window.dispatchEvent(new Event("script-weaver-project-refresh"));
        } catch { /* regular API banners handle actionable failures */ }
      }, 180);
    };
    return () => { window.clearTimeout(timer); events.close(); };
  }, [project?.id, loadProject]); // eslint-disable-line react-hooks/exhaustive-deps
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
  const selectSegment = async (item: Segment) => { setH3Shot(null); const loaded = await api<Segment>(`/segments/${item.id}`); const shot = loaded.shots?.[0] || null; selectionRef.current = { ...selectionRef.current, segmentId: loaded.id, shotId: shot?.id || null }; setSegment(loaded); setSelectedShot(shot); };
  const selectEpisode = async (item: Episode) => { if (item.status === "removed") return; setH3Shot(null); selectionRef.current = { projectId: project?.id || "", episodeId: item.id, segmentId: null, shotId: null }; setEpisode(item); setSegment(null); setSelectedShot(null); await loadLatestEpisodeSelection({ loadEpisode: () => api<Episode>(`/episodes/${item.id}`), loadSegment: (loaded: Episode) => loaded.segments?.[0] ? api<Segment>(`/segments/${loaded.segments[0].id}`) : Promise.resolve(null), isCurrent: (episodeId: string) => selectionRef.current.episodeId === episodeId, commit: (loaded: Episode, first: Segment | null) => { const shot = first?.shots?.[0] || null; selectionRef.current = { projectId: project?.id || "", episodeId: loaded.id, segmentId: first?.id || null, shotId: shot?.id || null }; setEpisode(loaded); setSegment(first); setSelectedShot(shot); } }); };
  const switchProject = async (id: string) => { if (documentDirty && !window.confirm("草稿还没有成功保存，仍要切换项目吗？")) return; setDocumentDirty(false); await loadProject(id); setPage("documents"); };
  const switchPage = (next: typeof page) => { if (documentDirty && next !== "documents" && !window.confirm("草稿还没有成功保存，仍要离开编辑器吗？")) return; setDocumentDirty(false); setH3Shot(null); setPage(next); };
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
  const openAssetVisuals = (assetId: string) => { setAssetFocusId(assetId); setPage("assets"); };
  const openPackageDocumentIssue = (episodeId?: string) => {
    setH3Shot(null); setPage("documents");
    const target = project?.episodes.find((item) => item.id === episodeId);
    if (target) void selectEpisode(target);
  };
  const openPackageShotIssue = async (issue: Record<string, unknown>) => {
    if (!project || typeof issue.episode_id !== "string" || typeof issue.segment_id !== "string" || typeof issue.owner_id !== "string") return;
    const loadedEpisode = await api<Episode>(`/episodes/${issue.episode_id}`);
    const loadedSegment = await api<Segment>(`/segments/${issue.segment_id}`);
    const target = loadedSegment.shots?.find((item) => item.id === issue.owner_id);
    if (!target) return;
    selectionRef.current = { projectId: project.id, episodeId: loadedEpisode.id, segmentId: loadedSegment.id, shotId: target.id };
    setEpisode(loadedEpisode); setSegment(loadedSegment); setSelectedShot(target);
    setH3Shot(target); setPage("storyboard");
  };
  const activeH3Shot = h3Shot?.id === selectedShot?.id ? h3Shot : null;
  const activeEpisodes = project?.episodes.filter((item) => item.status === "active") || [];
  const hasNextEpisode = activeEpisodes.findIndex((item) => item.id === episode?.id) < activeEpisodes.length - 1;
  return <main className={`workbench ${page === "storyboard" && selectedShot && inspectorOpen ? "inspector-open" : ""}`}>
    <header className="topbar"><div className="brand-mark">SW</div><span className="crumb">工作台</span><span className="crumb-separator">›</span><button className="project-switcher" aria-label="打开项目库" onClick={() => switchPage("library")}>{project?.title || "项目库"}<ChevronDown size={14} /></button><button className="library-button" onClick={() => setTasksOpen(!tasksOpen)}><Clock3 size={15}/>任务 {tasks.filter((item) => item.status === "QUEUED" || item.status === "RUNNING").length}</button><button className="library-button" onClick={() => switchPage("library")}><Archive size={15} />项目库</button></header>
    <nav className="primary-nav" aria-label="主导航"><div className="nav-stack"><NavIcon icon={FileText} label="剧本" active={page === "documents"} onClick={() => switchPage("documents")}/><NavIcon icon={UserRound} label="角色" disabled/><NavIcon icon={Image} label="美术" disabled/><NavIcon icon={GalleryHorizontalEnd} label="分镜" active={page === "storyboard"} onClick={() => switchPage("storyboard")}/></div><div className="nav-stack nav-bottom"><NavIcon icon={LayoutDashboard} label="项目库" active={page === "library"} onClick={() => switchPage("library")}/><NavIcon icon={ScrollText} label="大纲" disabled/><NavIcon icon={History} label="变更" active={page === "changes"} onClick={() => switchPage("changes")}/><NavIcon icon={Archive} label="素材库" active={page === "assets"} onClick={() => switchPage("assets")}/><NavIcon icon={Settings} label="设置" disabled/></div></nav>
    <aside className="segment-nav"><div className="eyebrow">{page === "storyboard" ? "分镜 · STORYBOARD" : "作品 · PROJECT"}</div>{page === "storyboard" && <>{project && project.episodes.length > 1 && <div className="storyboard-episodes" aria-label="分镜集数">{project.episodes.map((item) => <button disabled={item.status === "removed"} className={episode?.id === item.id ? "active" : ""} key={item.id} onClick={() => void selectEpisode(item)}>EP{String(item.episode_number).padStart(2, "0")}<small>{item.status === "removed" ? "已移出分集地图" : item.is_stale ? "需复核" : item.title}</small></button>)}</div>}<div className="overview-row">本集<span>{episodeSegments.length} 段</span></div>{episode && <><div className="episode-title">第 {episode.episode_number} 集</div><div className="overview-row">本集总表<span>{episodeSegments.reduce((sum, item) => sum + (item.shot_count || 0), 0)} 镜</span></div></>}{episodeSegments.map((item) => <button key={item.id} className={`segment-row ${segment?.id === item.id ? "active" : ""}`} onClick={() => void selectSegment(item)}><span>{visibleSegmentCode(item.code)}</span><small>{item.target_duration_seconds || "—"}s · {item.shot_count || 0}镜</small></button>)}</>}{page !== "storyboard" && <><button className="seed-button" onClick={() => setIntakeOpen(true)}><Plus size={15}/>开始新作品</button><div className="project-list">{projects.map((item) => <button className={project?.id === item.id ? "active" : ""} key={item.id} onClick={() => void switchProject(item.id)}>{item.title}</button>)}</div></>}{!project && page === "storyboard" && <button className="seed-button" onClick={() => void seed()} disabled={busy}>{busy ? <Loader2 className="spin" size={15}/> : <Plus size={15}/>}创建示例工作台</button>}</aside>
    <section className="workspace">{error && <div className="error-banner">{error}<button onClick={() => setError("")} aria-label="关闭错误"><X size={14}/></button></div>}{notice && <div className="notice-banner">{notice}<button onClick={() => setNotice("")} aria-label="关闭提示"><X size={14}/></button></div>}{page === "library" && <ProjectLibrary projects={projects} archived={archivedProjects} onOpen={switchProject} onCreate={() => setIntakeOpen(true)} onArchive={async (item, archived) => { await post(`/projects/${item.id}/${archived ? "restore" : "archive"}`, { expected_revision: item.revision }); await load(); }}/>} {page === "documents" && project && <DocumentWorkspace project={project} selectedEpisodeId={episode?.id || null} onEpisodeChange={selectEpisode} onOpenAsset={openAssetVisuals} onDirtyChange={setDocumentDirty} onReload={() => loadProject(project.id)} onCreateTask={async (capability, intent, documentId, versionId) => { await post("/tasks", { project_id: project.id, capability, intent, document_id: documentId, document_version_id: versionId, skill_manifest: [{ name: capability, version: "3ab6b855" }] }); setTasks(await api<WorkbenchTask[]>(`/tasks?project_id=${encodeURIComponent(project.id)}`)); setNotice("任务已排队。Codex 提案应用后仍需你在版本栏独立接受或退回。"); }} onArchived={async () => { await post(`/projects/${project.id}/archive`, { expected_revision: project.revision }); await load(); setPage("library"); }}/>} {page === "storyboard" && (!activeH3Shot ? <Storyboard episode={episode} segment={segment} selected={selectedShot} onReturn={() => switchPage("documents")} onOpenAsset={openAssetVisuals} onSelect={(shot) => { setH3Shot(null); selectionRef.current = { ...selectionRef.current, shotId: shot.id }; setSelectedShot(shot); }} onRefresh={() => selectedShot && void refreshShot(selectedShot.id)}/> : project && <H3VideoStudio project={project} shot={activeH3Shot} onBack={() => setH3Shot(null)} onRefresh={() => loadProject(project.id)} hasNextEpisode={hasNextEpisode} onNextEpisode={() => { const next = activeEpisodes[activeEpisodes.findIndex((item) => item.id === episode?.id) + 1]; setH3Shot(null); if (next) void selectEpisode(next); }} onNextSeason={() => { setH3Shot(null); setSeasonSource(project); setIntakeOpen(true); }} onNewWork={() => { setH3Shot(null); setSeasonSource(null); setIntakeOpen(true); }} onLibrary={() => switchPage("library")} onOpenDocuments={openPackageDocumentIssue} onOpenShotH3={(issue) => void openPackageShotIssue(issue)}/>)} {page === "changes" && <ChangesPage changes={changes} busy={busy} onApply={applyChange}/>} {page === "assets" && project && <AssetsPage project={project} initialSelectedId={assetFocusId} onVersion={createAssetVersion} onRefresh={() => loadProject(project.id)}/>}</section>
    {page === "storyboard" && selectedShot && !activeH3Shot && <><button className="inspector-toggle" onClick={() => setInspectorOpen((value) => !value)} aria-expanded={inspectorOpen}>{inspectorOpen ? "收起镜头检查器" : "打开镜头检查器"}</button>{inspectorOpen && <Inspector shot={selectedShot} busy={busy} onUpdate={updateShot} onBindingAction={bindingAction} onOpenAsset={openAssetVisuals}/>}<GenerationControl shot={selectedShot} onOpenAsset={openAssetVisuals} onOpenH3={() => setH3Shot(selectedShot)}/>{selectedShot.versions.length > 1 && <button className="restore-shot-button" onClick={() => void restoreShot(selectedShot.versions.at(-1)!.revision)}>恢复镜头 r{selectedShot.versions.at(-1)!.revision}</button>}</>}{tasksOpen && <TaskDrawer tasks={tasks} onClose={() => setTasksOpen(false)} onChanges={() => { setTasksOpen(false); setPage("changes"); }}/>} {project && <button className="command-trigger" onClick={() => setPaletteOpen(true)} aria-label="打开命令面板"><Command size={15}/>Ctrl K</button>}{paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} onTask={async (intent) => { if (!project) return; try { setBusy(true); await publishSurface(); await post("/tasks", { project_id: project.id, capability: "short-drama-storyboard", intent, surface_session_id: surfaceSession || null, skill_manifest: [{ name: "short-drama-storyboard", version: "1.0" }] }); setPaletteOpen(false); setNotice("已创建 Codex 任务，提案会出现在变更中心。"); } catch (reason) { setError(describe(reason, "任务创建失败")); } finally { setBusy(false); } }}/>} {intakeOpen && <IntakeDialog continuationSource={seasonSource} onClose={() => { setIntakeOpen(false); setSeasonSource(null); }} onCreated={async (created) => { setIntakeOpen(false); setSeasonSource(null); await load(); await loadProject(created.id); setPage("documents"); }}/>}</main>;
}

function TaskDrawer({ tasks, onClose, onChanges }: { tasks: WorkbenchTask[]; onClose: () => void; onChanges: () => void }) { return <aside className="task-drawer"><header><div><span className="eyebrow">AGENT TASKS</span><h2>任务</h2></div><button onClick={onClose} aria-label="关闭任务抽屉"><X size={15}/></button></header>{tasks.length ? tasks.map((task) => <article key={task.id}><div><strong>{task.capability.replace("short-drama-", "")}</strong><span className={`status ${task.status.toLowerCase()}`}>{task.status}</span></div><p>{task.intent}</p><small>{task.worker_label ? `由 ${task.worker_label} 处理` : "等待 CLI claim"}</small>{task.status === "SUBMITTED" && <button onClick={onChanges}>查看 ChangeSet</button>}</article>) : <div className="empty-inline">暂无任务。人工编辑路径始终可用。</div>}</aside>; }

function ProjectLibrary({ projects, archived, onOpen, onCreate, onArchive }: { projects: Project[]; archived: Project[]; onOpen: (id: string) => Promise<void>; onCreate: () => void; onArchive: (project: Project, archived: boolean) => Promise<void> }) {
  return <div className="project-library"><div className="page-heading"><div><span className="eyebrow">PROJECT LIBRARY</span><h1>你的作品</h1><p>从一个想法、原著或现成剧本开始，所有正文与历史都保存在本地。</p></div><button className="primary-button" onClick={onCreate}><Plus size={14}/>开始新作品</button></div><div className="project-grid">{projects.map((item) => <article key={item.id}><small>{(item.episode_count || 0) > 0 ? "剧本项目" : "故事项目"}</small><h2>{item.title}</h2><p>{item.document_count || 0} 份文档 · {item.episode_count || 0} 集</p><footer><button onClick={() => void onArchive(item, false)}><Archive size={13}/>归档</button><button className="primary-button" onClick={() => void onOpen(item.id)}>继续创作</button></footer></article>)}</div>{!projects.length && <div className="empty-inline">还没有作品。创建后无需 Codex 也能直接编辑和审批。</div>}{archived.length > 0 && <section className="archived-projects"><h2>已归档</h2>{archived.map((item) => <div key={item.id}><span>{item.title}</span><button onClick={() => void onArchive(item, true)}><RotateCcw size={13}/>恢复</button></div>)}</section>}</div>;
}

const intakeKinds = [
  ["idea", "一个想法", "一句话或一段尚未展开的故事"],
  ["novel", "原著 / 长材料", "小说、文章或其他改编来源"],
  ["single_script", "单集剧本", "可以直接采用原文，也可以继续修订"],
  ["multi_script", "多集整稿", "先保存整稿，确认分集地图后再创建各集剧本"],
] as const;

function IntakeDialog({ continuationSource, onClose, onCreated }: { continuationSource: Project | null; onClose: () => void; onCreated: (project: Project) => Promise<void> }) {
  const [kind, setKind] = useState<typeof intakeKinds[number][0]>("idea"), [title, setTitle] = useState(continuationSource ? `${continuationSource.title} · 第二季` : ""), [content, setContent] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const create = async () => { setBusy(true); setError(""); try { await onCreated(await post<Project>("/project-intakes", { title, intake_kind: kind, content, format: "short_drama", aspect_ratio: "9:16", prompt_language: "zh", ...(continuationSource ? { source_project_id: continuationSource.id, continuation_kind: "season" } : {}) })); } catch (reason) { setError(reason instanceof Error ? reason.message : "创建失败"); } finally { setBusy(false); } };
  return <div className="dialog-backdrop" onMouseDown={onClose}><section className="intake-dialog" role="dialog" aria-modal="true" aria-label={continuationSource ? "开始下一季" : "开始新作品"} onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow">{continuationSource ? "NEXT SEASON" : "NEW PROJECT"}</span><h2>{continuationSource ? "下一季从什么新冲突开始？" : "你现在手上有什么？"}</h2></div><button onClick={onClose} aria-label="关闭"><X size={16}/></button></header>{!continuationSource && <div className="intake-kinds">{intakeKinds.map(([value, label, help]) => <button className={kind === value ? "active" : ""} key={value} onClick={() => setKind(value)}><strong>{label}</strong><small>{help}</small></button>)}</div>}<label>作品名称<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：未通过的好友申请"/></label><label>{continuationSource ? "新一季 idea" : "原始内容"}<textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder={continuationSource ? "必须输入这一季的新故事起点…" : "在这里输入或粘贴…"}/></label>{error && <p className="save-error">{error}</p>}<footer><button className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy || !title.trim() || !content.trim()} onClick={() => void create()}>{busy ? "创建中" : continuationSource ? "创建下一季并进入开发" : "创建并进入工作台"}</button></footer></section></div>;
}

function NavIcon({ icon: Icon, label, active, disabled, onClick }: { icon: typeof FileText; label: string; active?: boolean; disabled?: boolean; onClick?: () => void }) { return <button className={`nav-icon ${active ? "active" : ""}`} disabled={disabled} title={disabled ? `${label}将在后续阶段开放` : undefined} onClick={onClick} aria-label={label}><Icon size={20}/><span>{label}</span></button>; }
function Storyboard({ episode, segment, selected, onReturn, onSelect, onRefresh, onOpenAsset }: { episode: Episode | null; segment: Segment | null; selected: Shot | null; onReturn: () => void; onSelect: (shot: Shot) => void; onRefresh: () => void; onOpenAsset: (assetId: string) => void }) { if (!segment) return <Empty episode={episode} onReturn={onReturn}/>; return <div className="storyboard-page"><div className="page-heading"><div><span className="eyebrow">{visibleSegmentCode(segment.code)}</span><h1>{segment.title || "分镜"}</h1></div><button className="secondary-button" onClick={onRefresh}><RefreshCcw size={14}/>刷新</button></div><div className="shot-list">{(segment.shots || []).filter((shot) => shot.status !== "retired").map((shot) => <ShotCard key={shot.id} shot={shot} active={selected?.id === shot.id} onClick={() => onSelect(shot)} onOpenAsset={onOpenAsset}/>)}</div></div>; }
function ShotCard({ shot, active, onClick, onOpenAsset }: { shot: Shot; active: boolean; onClick: () => void; onOpenAsset: (assetId: string) => void }) { const stale = Boolean(shot.is_stale || shot.bindings.some((item) => item.is_stale)); const media = shot.media.find((item) => item.is_current && item.mime.startsWith("image/")); return <article className={`shot-card ${active ? "selected" : ""}`} onClick={onClick}><div className="shot-heading"><strong>#{shot.order_index + 1}</strong><span>{shot.duration_seconds}s</span><span>·</span><span>{shot.shot_size}</span><span>·</span><span>{shot.camera_movement}</span>{stale && <em>{shotStaleLabel(shot)}</em>}</div><div className="shot-body"><div className="frame-wrap"><span>分镜图</span><img src={media ? `/api/media/${media.id}` : "/assets/mist-ferry-pier.png"} alt={`镜头 ${shot.order_index + 1} 雾渡分镜`} style={{ objectPosition: `${35 + shot.order_index * 12}% center` }}/></div><div className="prompt-column"><span className="field-label">画面提示词</span><p>{prompt(shot, "image")}</p><span className="field-label">视频提示词</span><p>{prompt(shot, "video")}</p></div><div className="binding-column"><div className="binding-header"><span>引用资产</span>{stale && <b>需复核</b>}</div>{shot.bindings.map((item) => <button className={`binding-chip ${item.is_stale ? "stale" : ""}`} key={item.id} onClick={(event) => { event.stopPropagation(); onOpenAsset(item.asset_id); }}><Boxes size={15}/><div><small>{item.usage} · {item.binding_mode === "frozen" ? "已冻结" : "跟随最新"}</small><strong>{item.name}</strong></div><span>打开视觉候选</span></button>)}</div></div></article>; }
function Inspector({ shot, busy, onUpdate, onBindingAction, onOpenAsset }: { shot: Shot; busy: boolean; onUpdate: (changes: Record<string, unknown>) => void; onBindingAction: (id: string, action: "sync" | "freeze" | "restore", version?: string) => void; onOpenAsset: (assetId: string) => void }) { const [duration, setDuration] = useState(String(shot.duration_seconds)); useEffect(() => setDuration(String(shot.duration_seconds)), [shot]); return <aside className="inspector"><div className="inspector-title"><div><span className="eyebrow">镜头检查器</span><h2>SHOT {String(shot.order_index + 1).padStart(2, "0")}</h2></div><span className="revision">r{shot.revision}</span></div><label>秒数<div className="duration-input"><input aria-label="镜头秒数" type="number" min="0.1" max="600" step="0.1" value={duration} onChange={(event) => setDuration(event.target.value)}/><span>秒</span><button onClick={() => onUpdate({ duration_seconds: Number(duration) })} disabled={busy}><Save size={14}/>保存</button></div></label><label>景别<select aria-label="景别" value={shot.shot_size} onChange={(event) => onUpdate({ shot_size: event.target.value })}><option>extreme-wide</option><option>wide</option><option>medium</option><option>close-up</option></select></label><label>机位<select aria-label="机位" value={shot.camera_angle} onChange={(event) => onUpdate({ camera_angle: event.target.value })}><option>eye_level</option><option>low_angle</option><option>high_angle</option><option>over_shoulder</option></select></label><label>运镜<select aria-label="运镜" value={shot.camera_movement} onChange={(event) => onUpdate({ camera_movement: event.target.value })}><option>static</option><option>tracking</option><option>push_in</option><option>handheld</option></select></label><div className="inspector-section"><div className="section-title"><History size={15}/>引用与版本</div>{shot.bindings.map((item) => { const hasNewRef = item.asset_version_id !== item.current_asset_version_id; return <div className="version-row" key={item.id}><div><strong>{item.name}</strong><small>{item.binding_mode} · {item.is_stale ? "上游已变化" : hasNewRef ? "有新正式 REF" : "当前"}</small></div><div className="version-actions"><button onClick={() => onOpenAsset(item.asset_id)}>视觉候选</button>{(item.is_stale || hasNewRef) && <button onClick={() => onBindingAction(item.id, "sync")}>使用正式 REF</button>}<button onClick={() => onBindingAction(item.id, "freeze")}>冻结当前</button>{item.is_stale && <button onClick={() => onBindingAction(item.id, "restore", item.asset_version_id)}>恢复</button>}</div></div>; })}</div><div className="inspector-section"><div className="section-title"><Clock3 size={15}/>提示词版本</div>{shot.prompts.map((item) => <div className="history-row" key={item.id}><span>{item.kind}</span><span>v{item.version_number}</span><small>{item.is_current ? "当前" : "历史"}</small></div>)}</div></aside>; }
function ChangesPage({ changes, busy, onApply }: { changes: ChangeSet[]; busy: boolean; onApply: (change: ChangeSet) => void }) { return <div className="content-page"><div className="page-heading"><div><span className="eyebrow">CHANGESETS</span><h1>变更中心</h1></div></div>{changes.length ? changes.map((change) => <article className="change-card" key={change.id}><header><div><strong>{change.summary || "未命名变更"}</strong><span className={`status ${change.status.toLowerCase()}`}>{change.status}</span></div><small>{change.operations.length} 项操作 · {change.impacts.length} 项影响</small></header><div className="change-ops">{change.operations.map((op) => <div key={op.ordinal}><b>{op.op}</b><span>{op.target_type} · {op.target_id?.slice(0, 8)}</span>{typeof op.payload.content === "string" && <details><summary>查看完整候选（{op.payload.content.length} 字）</summary><pre>{op.payload.content}</pre></details>}</div>)}</div>{change.impacts.map((impact) => <p className="change-impact" key={`${impact.entity_id}-${impact.impact_type}`}>影响：{impact.impact_type} · {impact.entity_type} {impact.entity_id.slice(0, 8)}</p>)}{change.warnings.map((warning) => <p className="warning" key={warning.code}>{warning.message}</p>)}{change.status === "SUBMITTED" && <><p className="apply-note">应用只会创建待审批候选，不会自动接受。</p><button className="primary-button" onClick={() => onApply(change)} disabled={busy}>应用为候选</button></>}{change.status === "APPLIED" && <p className="apply-note">已创建候选；请到剧本页独立接受或退回。</p>}</article>) : <div className="empty-inline">暂无 ChangeSet。Codex 提案会出现在这里。</div>}</div>; }
function AssetsPage({ project, initialSelectedId, onVersion, onRefresh }: { project: Project; initialSelectedId: string | null; onVersion: (asset: Asset) => void; onRefresh: () => Promise<void> }) { const [selectedId, setSelectedId] = useState<string | null>(initialSelectedId); const selected = project.assets.find((item) => item.id === selectedId); if (selected) return <AssetVisualStudio projectId={project.id} asset={selected} onBack={() => setSelectedId(null)} onRefresh={onRefresh}/>; return <div className="content-page"><div className="page-heading"><div><span className="eyebrow">ASSET LIBRARY</span><h1>视觉事实与正式 REF</h1><p>人物、造型、地点和道具复用同一套候选决策面板。</p></div></div><div className="asset-grid">{project.assets.map((asset) => <article className="asset-card" key={asset.id}><div className="asset-visual"><Palette size={28}/></div><small>{asset.kind}</small><h3>{asset.name}</h3><p>当前 v{asset.versions[0]?.version_number || 1} · {asset.versions.length} 个历史版本</p><button className="primary-button" onClick={() => setSelectedId(asset.id)}><Sparkles size={14}/>生成视觉候选</button><button className="secondary-button" onClick={() => onVersion(asset)}><Plus size={14}/>仅创建文字版本</button></article>)}</div></div>; }
function CommandPalette({ onClose, onTask }: { onClose: () => void; onTask: (intent: string) => Promise<void> }) { const [value, setValue] = useState(""); return <div className="palette-backdrop" onMouseDown={onClose}><div className="palette" onMouseDown={(event) => event.stopPropagation()}><div className="palette-input"><WandSparkles size={18}/><input autoFocus aria-label="Codex 命令" placeholder="描述你希望 Codex 如何调整当前分镜…" value={value} onChange={(event) => setValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && value.trim()) void onTask(value.trim()); if (event.key === "Escape") onClose(); }}/><kbd>Esc</kbd></div><p>任务会冻结当前选择与 revision，结果只会成为待审阅 ChangeSet。</p></div></div>; }
function Empty({ episode, onReturn }: { episode: Episode | null; onReturn: () => void }) { const returnToEpisode = () => { if (episode) sessionStorage.setItem("script-weaver-document-focus-episode", episode.id); onReturn(); }; return <div className="empty-state"><div className="empty-icon"><Aperture size={28}/></div><h1>{episode ? "当前集尚未完成剧本投影" : "从结构化分镜开始"}</h1><p>{episode ? "只有接受且成功投影的剧本，才能驱动本集分镜。" : "创建作品并接受剧本后，分镜会出现在这里。"}</p>{episode && <button className="primary-button" onClick={returnToEpisode}>返回当前集剧本并投影</button>}<div className="empty-principle"><Sparkles size={15}/>项目事实保存在本地 SQLite，而不是聊天上下文。</div></div>; }

function GenerationControl({ shot, onOpenAsset, onOpenH3 }: { shot: Shot; onOpenAsset: (assetId: string) => void; onOpenH3: () => void }) {
  const target = shot.bindings[0];
  return <div className="generation-actions"><button disabled={!target} onClick={() => target && onOpenAsset(target.asset_id)}><Sparkles size={13}/>{target ? `打开 ${target.name} 的视觉候选` : "先绑定视觉资产"}</button><button disabled={!target} onClick={onOpenH3}><Film size={13}/>本地 H3 生成视频</button></div>;
}
