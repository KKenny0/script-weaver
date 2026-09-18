"use client";

import { useEffect, useState } from "react";
import { api, post } from "./api";
import type { ChangeSet, CreativeDocument } from "./types";

type ScopedTask = { id: string; intent: string; status: string; selection: { document_id?: string; edit_scope?: { start: number; end: number; draft_revision: number }; target?: { draft_content: string } } };

export default function ScopedRevisionPanel({ document, content, draftRevision, selection, onFlush, onReload }: {
  document: CreativeDocument; content: string; selection: { start: number; end: number };
  draftRevision: number; onFlush: (expectedContent: string) => Promise<number>; onReload: () => Promise<void>;
}) {
  const [intent, setIntent] = useState("");
  const [tasks, setTasks] = useState<ScopedTask[]>([]);
  const [changes, setChanges] = useState<(ChangeSet & { task_id: string })[]>([]);
  const [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  const [reviewed, setReviewed] = useState<Record<string, boolean>>({});
  const selectedText = content.slice(selection.start, selection.end);
  const refresh = async () => {
    const [nextTasks, nextChanges] = await Promise.all([
      api<ScopedTask[]>(`/tasks?project_id=${encodeURIComponent(document.project_id)}`),
      api<(ChangeSet & { task_id: string })[]>(`/projects/${document.project_id}/changesets`),
    ]);
    setTasks(nextTasks.filter(task => task.selection.document_id === document.id && task.selection.edit_scope));
    setChanges(nextChanges);
  };
  useEffect(() => { void refresh().catch(reason => setMessage(reason.message)); }, [document.id, document.revision]); // eslint-disable-line react-hooks/exhaustive-deps
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setMessage("");
    try { await action(); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "操作失败，原稿保留"); }
    finally { setBusy(false); }
  };
  const start = async () => {
    const revision = await onFlush(content);
    await post("/tasks", {
      project_id: document.project_id, document_id: document.id,
      document_version_id: document.source_document_version_id,
      capability: "short-drama-write", intent: intent.trim(),
      edit_scope: { draft_revision: revision, start: Array.from(content.slice(0, selection.start)).length, end: Array.from(content.slice(0, selection.end)).length },
    });
    await onReload(); await refresh(); setMessage("已创建局部修订任务，等待 Codex 领取。当前草稿不会被覆盖。");
  };
  return <section className="scoped-revision" aria-label="局部创作协作">
    <h2>与 Codex 修改这一段</h2>
    <p>在上方剧本中选中一场或一段文字。选区以外的内容必须保持不变。</p>
    <details><summary>{selectedText ? `已选择 ${Array.from(selectedText).length} 字 · 查看修改范围` : "尚未选择文字"}</summary><pre>{selectedText}</pre></details>
    <label>这次怎么改？<textarea aria-label="局部修订要求" value={intent} onChange={event => setIntent(event.target.value)} placeholder="例如：让这场对话更克制，保留事件结果"/></label>
    <button className="primary-button" disabled={busy || !selectedText.trim() || !intent.trim() || !document.source_document_version_id} onClick={() => void run(start)}>交给 Codex 修改选区</button>
    <button className="secondary-button" disabled={busy} onClick={() => void run(refresh)}>刷新候选</button>
    {message && <p role="status">{message}</p>}
    {tasks.map(task => {
      const scope = task.selection.edit_scope!;
      const change = changes.find(item => item.task_id === task.id);
      const candidate = document.versions.find(version => version.source_changeset_id === change?.id);
      const original = task.selection.target?.draft_content || "";
      const stale = draftRevision !== scope.draft_revision || original !== content;
      return <article key={task.id}>
        <h3>{task.intent}</h3><p>输入：草稿 r{scope.draft_revision} · 字符 {scope.start + 1}–{scope.end} · {change?.status || task.status}</p>
        {stale && <p>草稿已变化。请重新选择范围并发起任务；旧候选不会覆盖新稿。</p>}
        {change && <details onToggle={event => { if (event.currentTarget.open) setReviewed(current => ({ ...current, [change.id]: true })); }}>
          <summary>比较原稿与完整候选</summary><div className="revision-comparison"><div><h4>任务发起时的原稿</h4><pre>{original}</pre></div><div><h4>修订候选</h4><pre>{String(change.operations[0]?.payload.content || "")}</pre></div></div>
        </details>}
        {change?.status === "SUBMITTED" && <>
          <button className="primary-button" disabled={busy || stale || !reviewed[change.id]} onClick={() => void run(async () => { await onFlush(content); await post(`/changesets/${change.id}/apply`, { fingerprint: change.validated_fingerprint }); await onReload(); await refresh(); setMessage("候选已进入版本栏，请核对后接受或退回；正式版本尚未改变。"); })}>保留为待审批候选</button>
          <button className="secondary-button" disabled={busy} onClick={() => void run(async () => { await post(`/changesets/${change.id}/reject`, {}); await refresh(); setIntent(task.intent); setMessage("已退回，原稿保留。可调整要求、重新选择范围后再试。"); })}>退回提案</button>
        </>}
        {change?.status === "APPLIED" && <p>{candidate?.status === "ACCEPTED" ? "已接受为正式版本。要在修订结果上继续写，可在版本栏将它恢复为草稿。" : candidate?.status === "REJECTED" ? "文档候选已退回，可以调整要求再试。" : "请在版本与审批中接受或退回；这不会自动覆盖编辑中的草稿。"}</p>}
        {(["FAILED", "REJECTED", "CONFLICTED"].includes(change?.status || task.status) || candidate?.status === "REJECTED") && <button className="secondary-button" onClick={() => setIntent(task.intent)}>沿用要求再试</button>}
      </article>;
    })}
  </section>;
}
