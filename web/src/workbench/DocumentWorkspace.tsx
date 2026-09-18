"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Archive, Check, FilePlus2, History, Loader2, RotateCcw, Save, X } from "lucide-react";
import { ApiError, post, put } from "./api";
import type { CreativeDocument, Episode, Project } from "./types";
import { createSaveCoordinator } from "./save-coordinator.mjs";
import { episodeDocumentState, episodeIdForScreenplay, initialDocumentId } from "./selection-state.mjs";
import DevelopmentEditor from "./DevelopmentEditor";
import { validateDevelopment } from "./development-editor.mjs";
import ScopedRevisionPanel from "./ScopedRevisionPanel";

type Props = {
  project: Project;
  selectedEpisodeId: string | null;
  onEpisodeChange: (episode: Episode) => Promise<void>;
  onOpenAsset: (assetId: string) => void;
  onReload: () => Promise<void>;
  onDirtyChange: (dirty: boolean) => void;
  onArchived: () => Promise<void>;
  onCreateTask: (capability: string, intent: string, documentId: string | null, versionId: string) => Promise<void>;
};

export default function DocumentWorkspace({ project, selectedEpisodeId, onEpisodeChange, onOpenAsset, onReload, onDirtyChange, onArchived, onCreateTask }: Props) {
  const editable = useMemo(() => project.documents.filter((item) => item.draft), [project.documents]);
  const [documentId, setDocumentId] = useState(initialDocumentId(project.documents, selectedEpisodeId));
  const document = project.documents.find((item) => item.id === documentId) || editable[0] || project.documents[0];
  const documentEpisode = project.episodes.find((item) => item.id === document?.episode_id);
  const episodeRemoved = documentEpisode?.status === "removed";
  const [content, setContent] = useState(document?.draft?.content || "");
  const [draftRevision, setDraftRevision] = useState(document?.draft?.revision || 0);
  const [dirty, setDirty] = useState(false), [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("已保存"), [error, setError] = useState("");
  const [inputOpen, setInputOpen] = useState(false);
  const [reviewedVersions, setReviewedVersions] = useState<Record<string, boolean>>({});
  const [textSelection, setTextSelection] = useState({ start: 0, end: 0 });
  const [taskCapability, setTaskCapability] = useState("short-drama-develop");
  const episodeDocuments = useMemo(() => project.episodes.map((episode) => ({
    episode,
    document: project.documents.find((item) => item.episode_id === episode.id && item.kind === "screenplay"),
  })), [project.episodes, project.documents]);
  const saveState = useRef({ documentId: document?.id || "", content, revision: draftRevision, dirty: false });
  const coordinator = useRef<ReturnType<typeof createSaveCoordinator> | null>(null);
  if (!coordinator.current) coordinator.current = createSaveCoordinator({
    read: () => saveState.current,
    write: async (snapshot: typeof saveState.current) => {
      const updated = await put<CreativeDocument>(`/documents/${snapshot.documentId}/draft`, { expected_revision: snapshot.revision, content: snapshot.content });
      return updated.draft!.revision;
    },
    settled: (snapshot: typeof saveState.current, revision: number, exact: boolean) => {
      if (saveState.current.documentId !== snapshot.documentId) return;
      saveState.current = { ...saveState.current, revision, dirty: exact ? false : saveState.current.dirty };
      setDraftRevision(revision);
      if (exact) { setDirty(false); onDirtyChange(false); setStatus("已自动保存"); }
      else { setDirty(true); onDirtyChange(true); setStatus("检测到更新，继续保存"); }
    },
    failed: (snapshot: typeof saveState.current, reason: unknown) => {
      if (saveState.current.documentId !== snapshot.documentId) return;
      saveState.current.dirty = true; setDirty(true); onDirtyChange(true);
      setStatus("保存失败，内容仍保留在本页"); setError(reason instanceof Error ? reason.message : "保存失败");
    },
    busy: setSaving,
  });

  useEffect(() => {
    setDocumentId((current) => project.documents.some((item) => item.id === current)
      ? current
      : editable[0]?.id || project.documents[0]?.id || "");
  }, [project.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const target = project.documents.find((item) => item.episode_id === selectedEpisodeId && item.kind === "screenplay");
    if (!project.documents.some((item) => item.id === documentId) && target) setDocumentId(target.id);
  }, [selectedEpisodeId, project.documents, documentId]);
  useEffect(() => {
    setContent(document?.draft?.content || "");
    setDraftRevision(document?.draft?.revision || 0);
    saveState.current = { documentId: document?.id || "", content: document?.draft?.content || "", revision: document?.draft?.revision || 0, dirty: false };
    setDirty(false); onDirtyChange(false); setStatus("已保存");
  }, [document?.id, document?.draft?.revision]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setTaskCapability(document?.kind === "screenplay" ? "short-drama-write" : "short-drama-develop");
    setTextSelection({ start: 0, end: 0 });
    setError("");
  }, [document?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!dirty || !document?.draft) return;
    const timer = window.setTimeout(() => { void saveDraft(); }, 800);
    return () => window.clearTimeout(timer);
  }, [content, dirty, document?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const markDirty = (value: string) => { saveState.current = { ...saveState.current, content: value, dirty: true }; setContent(value); setDirty(true); onDirtyChange(true); setStatus("等待自动保存"); };
  const saveDraft = (): Promise<number> => { setError(""); setStatus("保存中"); return coordinator.current!.save(); };
  const submit = async (adopt = false) => {
    if (!document?.draft) return;
    setError("");
    if (document.kind === "development") {
      const validationError = validateDevelopment(saveState.current.content);
      if (validationError) { setError(validationError); return; }
    }
    try {
      const revision = await coordinator.current!.flush(document.id);
      await post(`/documents/${document.id}/${adopt ? "adopt-source" : "submit"}`, { expected_document_revision: document.revision, expected_draft_revision: revision });
      await onReload(); setStatus(adopt ? "原文已接受为剧本文本" : "已提交整体审批");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "提交失败"); }
  };
  const decide = async (versionId: string, action: "accept" | "reject") => {
    if (action === "accept" && !reviewedVersions[versionId]) {
      setError("请先展开并核对完整候选与当前基线"); return;
    }
    const feedback = action === "reject" ? window.prompt("请填写退回原因") : null;
    if (action === "reject" && !feedback?.trim()) return;
    try {
      if (action === "accept" && document?.draft) await coordinator.current!.flush(document.id);
      await post(`/documents/${document!.id}/versions/${versionId}/decision`, { expected_document_revision: document!.revision, action, feedback });
      await onReload();
    } catch (reason) {
      if (action === "accept" && document?.kind === "development" && reason instanceof ApiError && reason.code?.startsWith("development_episode_map_")) {
        try {
          await post(`/documents/${document.id}/versions/${versionId}/decision`, {
            expected_document_revision: document.revision,
            action: "reject",
            feedback: `分集地图校验失败：${reason.message}`,
          });
          await post(`/documents/${document.id}/draft/restore`, {
            expected_draft_revision: saveState.current.revision,
            source_version_id: versionId,
          });
          await onReload();
          setError(`${reason.message} 已退回候选并恢复为草稿，请修改后提交新版本。`);
          return;
        } catch (recoveryError) {
          setError(`${reason.message} 自动恢复草稿失败：${recoveryError instanceof Error ? recoveryError.message : "请刷新后重试"}`);
          return;
        }
      }
      setError(reason instanceof Error ? reason.message : "审批失败");
    }
  };
  const restore = async (versionId: string) => {
    if (!document?.draft || !window.confirm("把这个历史版本复制为当前草稿？历史不会被覆盖。")) return;
    try {
      await post(`/documents/${document.id}/draft/restore`, { expected_draft_revision: saveState.current.revision, source_version_id: versionId });
      await onReload();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "恢复失败"); }
  };
  const retryProjection = async () => {
    if (!document || document.kind !== "screenplay") return;
    try {
      await post(`/documents/${document.id}/project`, { expected_document_revision: document.revision });
      await onReload(); setStatus("剧本已投影为新的场次与分段 revision");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "投影失败"); }
  };
  const createTask = async (capabilityOverride?: string) => {
    const capability = capabilityOverride || taskCapability;
    if (episodeRemoved && (capabilityOverride === "short-drama-write" || taskCapability === "short-drama-write")) { setError("这一集已移出分集地图，重新纳入后才能发起剧本任务"); return; }
    const sources = project.documents.filter((item) => item.kind === "source" && item.current_version_id);
    const acceptedDevelopment = project.documents.filter((item) => item.kind === "development" && item.current_version_id).at(-1);
    const source = capability === "short-drama-write" && acceptedDevelopment ? acceptedDevelopment : sources.at(-1);
    if (!source?.current_version_id) { setError("项目没有可冻结的来源版本"); return; }
    const target = capability === "short-drama-write"
      ? document.kind === "screenplay" ? document : project.documents.find((item) => item.kind === "screenplay")
      : project.documents.find((item) => item.kind === "development");
    const intent = window.prompt("这次希望 Codex 完成什么？", capability === "short-drama-write" ? `根据来源写出或修订 ${target?.title || "当前剧本"}` : "根据来源发展完整故事");
    if (!intent?.trim()) return;
    try { await onCreateTask(capability, intent.trim(), target?.id || null, source.current_version_id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "任务创建失败"); }
  };

  if (!document) return <div className="empty-inline">这个项目还没有创作文档。</div>;
  return <div className="document-workspace">
    <div className="document-toolbar">
      <div><span className="eyebrow">CREATIVE DOCUMENTS</span><h1>{document.title}</h1></div>
      <div className="document-actions">
        <select aria-label="Codex 创作能力" value={taskCapability} onChange={(event) => setTaskCapability(event.target.value)}><option value="short-drama-novel-analyze">分析长材料</option><option value="short-drama-develop">发展故事</option><option value="short-drama-write">写 / 改剧本</option></select>
        <button className="primary-button" disabled={Boolean(episodeRemoved)} onClick={() => void createTask()}>交给 Codex</button>
        <button className="secondary-button" onClick={() => setInputOpen(true)}><FilePlus2 size={14}/>新的输入</button>
        <button className="secondary-button" onClick={() => { if (!dirty || window.confirm("草稿尚未保存，仍要归档项目吗？")) void onArchived(); }}><Archive size={14}/>归档项目</button>
      </div>
    </div>
    {episodeDocuments.length > 0 && <div className="episode-navigator" aria-label="多集导航">{episodeDocuments.map(({ episode, document: episodeDocument }) => {
      const state = episodeDocumentState(episode, episodeDocument);
      return <button key={episode.id} disabled={episode.status === "removed"} className={episodeDocument?.id === document.id ? "active" : ""} onClick={() => { if (episodeDocument && (!dirty || window.confirm("草稿尚未保存，仍要切换集数吗？"))) { setDocumentId(episodeDocument.id); void onEpisodeChange(episode); } }}><strong>EP{String(episode.episode_number).padStart(2, "0")}</strong><span>{episode.title}</span><small className={state.kind === "stale" || state.kind === "removed" ? "stale-text" : ""}>{state.label}</small></button>;
    })}</div>}
    {project.assets.length > 0 && <div className="document-asset-links" aria-label="剧本视觉事实">{project.assets.map((asset) => <button key={asset.id} onClick={() => onOpenAsset(asset.id)}>{asset.kind} · {asset.name}<small>打开视觉候选</small></button>)}</div>}
    <div className="document-tabs" role="tablist">{project.documents.map((item) => <button role="tab" aria-selected={item.id === document.id} className={item.id === document.id ? "active" : ""} key={item.id} onClick={() => { if (!dirty || window.confirm("草稿尚未保存，仍要切换吗？")) { setDocumentId(item.id); const episodeId = episodeIdForScreenplay(item); const targetEpisode = project.episodes.find((episode) => episode.id === episodeId); if (targetEpisode) void onEpisodeChange(targetEpisode); } }}>{item.title}<small>{item.kind}</small></button>)}</div>
    <div className="document-grid">
      <section className="editor-panel">
        <header><span>{document.kind === "source" ? "原始输入，只读" : "创作草稿"}</span><small role="status" className={error ? "save-error" : ""}>{saving && <Loader2 className="spin" size={12}/>} {error || status}</small></header>
        {document.kind === "development" && document.draft && <div className="development-contract">
          <div><strong>先把想法发展成故事，再决定每集讲什么</strong><p>可以让 Codex 起草，也可以直接写故事、添加分集。接受整份开发稿后，上方会出现各集入口，选择一集开始写剧本。</p></div>
          <div className="development-contract-actions"><button className="primary-button" onClick={() => void createTask("short-drama-develop")}>让 Codex 发展故事</button></div>
          <small>接受前必须有一份分集地图。卡片会自动保存为它，无需编写数据格式；接受仍以后台校验为准。</small>
        </div>}
        {document.draft ? document.kind === "development" ? <DevelopmentEditor key={document.id} content={content} onChange={markDirty}/> : <textarea aria-label="文档 Markdown" value={content} disabled={Boolean(episodeRemoved)} onSelect={event => setTextSelection({ start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd })} onChange={(event) => { setTextSelection({ start: 0, end: 0 }); markDirty(event.target.value); }}/> : <pre>{document.versions[0]?.content}</pre>}
        {document.draft && !episodeRemoved && <footer>
          <button className="secondary-button" disabled={saving || !dirty} onClick={() => void saveDraft()}><Save size={14}/>保存草稿</button>
          {document.kind === "screenplay" && document.episode_id && document.versions.length === 0 && <button className="secondary-button" onClick={() => void submit(true)}>直接采用原文</button>}
          <button className="primary-button" onClick={() => void submit(false)}>提交整体审批</button>
        </footer>}
        {document.kind === "screenplay" && document.draft && !episodeRemoved && <ScopedRevisionPanel key={document.id} document={document} content={content} draftRevision={draftRevision} selection={textSelection} onFlush={async expectedContent => { const revision = await coordinator.current!.flush(document.id); if (saveState.current.content !== expectedContent) throw new Error("保存期间正文已变化，请重新选择文字"); return revision; }} onReload={onReload}/>}
        {episodeRemoved && <p className="projection-warning">本集已移出当前分集地图，历史保留为只读；重新纳入后才能修订或投影。</p>}
        {!episodeRemoved && document.kind === "screenplay" && document.current_version_id && !document.drives_downstream && <p className="projection-warning">剧本文本已接受，但格式尚未成功投影，当前不能驱动分镜。<button onClick={() => void retryProjection()}>重试投影</button></p>}
        {!episodeRemoved && document.kind === "screenplay" && document.is_stale === 1 && <p className="projection-warning">{document.current_version_id ? `当前接受版本仍保留，但上游分集事实已经变化：${document.stale_reason || "需要复核"}。` : "本集只有草稿，上游分集事实已经变化，草稿需更新。"}<button onClick={() => void createTask("short-drama-write")}>重新交给 Codex</button></p>}
      </section>
      <aside className="version-panel"><h2><History size={15}/>版本与审批</h2>{document.versions.map((version) => <article key={version.id}>
        <div><strong>v{version.version_number}</strong><span className={`status ${version.status.toLowerCase()}`}>{version.status}</span></div>
        <small>{new Date(version.created_at).toLocaleString()}</small>
        {version.decision_feedback && <p>反馈：{version.decision_feedback}</p>}
        {!episodeRemoved && document.kind === "screenplay" && version.status === "REJECTED" && <button className="retry-task-button" onClick={() => void createTask("short-drama-write")}>按反馈重新交给 Codex</button>}
        {version.status === "SUBMITTED" && <details onToggle={(event) => { if (event.currentTarget.open) setReviewedVersions((current) => ({ ...current, [version.id]: true })); }}><summary>核对完整候选与基线</summary><h4>当前已接受基线</h4><pre>{document.versions.find((item) => item.status === "ACCEPTED")?.content || "（无已接受基线）"}</pre><h4>待审批候选</h4><pre>{version.content}</pre></details>}
        <div className="version-buttons">{version.status === "SUBMITTED" && !episodeRemoved && <><button onClick={() => void decide(version.id, "reject")}><X size={12}/>退回</button><button disabled={!reviewedVersions[version.id]} title={!reviewedVersions[version.id] ? "请先核对完整候选" : undefined} onClick={() => void decide(version.id, "accept")}><Check size={12}/>{document.kind === "screenplay" ? "接受并投影" : "接受"}</button></>}{document.draft && !episodeRemoved && <button onClick={() => void restore(version.id)}><RotateCcw size={12}/>恢复为草稿</button>}</div>
      </article>)}</aside>
    </div>
    {inputOpen && <NewInputDialog projectId={project.id} onClose={() => setInputOpen(false)} onSaved={async () => { setInputOpen(false); await onReload(); }}/>}
  </div>;
}

function NewInputDialog({ projectId, onClose, onSaved }: { projectId: string; onClose: () => void; onSaved: () => Promise<void> }) {
  const [kind, setKind] = useState<"supplement" | "revision">("supplement"), [title, setTitle] = useState("新的输入"), [content, setContent] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const save = async () => { setBusy(true); try { await post(`/projects/${projectId}/inputs`, { intake_kind: kind, title, content }); await onSaved(); } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); } finally { setBusy(false); } };
  return <div className="dialog-backdrop" onMouseDown={onClose}><section className="intake-dialog" role="dialog" aria-modal="true" aria-label="项目新的输入" onMouseDown={(event) => event.stopPropagation()}><h2>项目新的输入</h2><div className="kind-toggle"><button className={kind === "supplement" ? "active" : ""} onClick={() => setKind("supplement")}>补充材料</button><button className={kind === "revision" ? "active" : ""} onClick={() => setKind("revision")}>修订要求</button></div><input aria-label="输入标题" value={title} onChange={(event) => setTitle(event.target.value)}/><textarea aria-label="输入正文" placeholder="粘贴或输入新的材料…" value={content} onChange={(event) => setContent(event.target.value)}/>{error && <p className="save-error">{error}</p>}<footer><button className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy || !title.trim() || !content.trim()} onClick={() => void save()}>{busy ? "保存中" : "保存为原始输入"}</button></footer></section></div>;
}
