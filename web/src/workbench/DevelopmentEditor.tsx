"use client";

import { useState } from "react";
import { composeDevelopment, parseDevelopment, replaceDevelopmentBody } from "./development-editor.mjs";

export default function DevelopmentEditor({ content, onChange }: { content: string; onChange: (value: string) => void }) {
  const [raw, setRaw] = useState(false);
  const [error, setError] = useState("");
  const parsed = parseDevelopment(content);
  const change = (makeContent: () => string) => {
    try { onChange(makeContent()); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "修改失败，原文仍保留"); }
  };
  return <div className="development-editor">
    <button className="secondary-button" onClick={() => setRaw(!raw)}>{raw ? "返回故事与分集" : "原文编辑（高级）"}</button>
    {(error || parsed.error) && <p role="alert">{error || parsed.error} 原文仍保留，可打开原文编辑修复。</p>}
    {raw ? <textarea aria-label="开发稿原文" value={content} onChange={(event) => onChange(event.target.value)}/> : !parsed.error && <>
      <label>故事方向与梗概<textarea aria-label="故事方向与梗概" placeholder="写下故事想法、人物困境或你想保留的情节…" value={parsed.body} onChange={(event) => change(() => replaceDevelopmentBody(content, event.target.value))}/></label>
      <h2>分集安排 <small>{parsed.entries.length} 集</small></h2>
      <p>每集写清发生了什么、在哪里留下悬念。草稿可以未完成；提交前请补齐标题与剧情。</p>
      {parsed.hasSourceSpans && <p>这些分集绑定了原著片段，卡片只读。修订请使用原文编辑并保留来源映射。</p>}
      {parsed.entries.map((entry, index) => <fieldset key={index} disabled={parsed.hasSourceSpans}>
        <legend>第 {entry.episode} 集</legend>
        <label>标题<input aria-label={`第 ${entry.episode} 集标题`} value={entry.title} onChange={(event) => change(() => composeDevelopment(content, parsed.entries.map((item, i) => i === index ? { ...item, title: event.target.value } : item)))}/></label>
        <label>剧情与结尾悬念<textarea aria-label={`第 ${entry.episode} 集剧情`} value={entry.story} onChange={(event) => change(() => composeDevelopment(content, parsed.entries.map((item, i) => i === index ? { ...item, story: event.target.value } : item)))}/></label>
      </fieldset>)}
      {!parsed.hasSourceSpans && <button className="secondary-button" onClick={() => change(() => composeDevelopment(content, [...parsed.entries, { episode: parsed.entries.length + 1, title: "", story: "" }]))}>添加一集</button>}
      {parsed.validationError && <p className="development-hint">{parsed.validationError}</p>}
    </>}
  </div>;
}
