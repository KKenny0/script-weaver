# ADR 0008：多集地图、单向剧本投影与精准 stale

- 状态：Accepted for phase 3 implementation; pending product review
- 日期：2026-08-30

development Markdown 的正文与机器可读分集地图属于同一个不可变文档版本，不另建
episode-map 审批对象。版本中必须恰好包含一个 `script-weaver-episode-map` JSONL fenced
block；每行声明连续且唯一的集号、标题和非空 `story`。`story` 是该集真实行动、冲突与
状态变化的最小创作载荷，并参与 fingerprint；只改标题或外围 prose 不能代替剧情变化。
多集整稿还必须声明 UTF-8 byte span 与该片段
SHA-256。创作者接受 development 时，daemon 在同一事务内解析、验证并创建或更新
Episode skeleton；任一行失败则文档决定和 Episode 均不改变。被新地图删除的 Episode
显式转为 `removed`，相关文档 revision 同步推进；历史候选不能接受、重试或启动新 Task。
重新纳入后才恢复 `active`。

screenplay Markdown 是一级版本事实，`ScriptScene` 与 `Segment` 是它的单向投影。格式为
`# EP001 标题` 与连续的 `## EP001-SC001 内|外|内外 · 地点 · 时间`。创作者接受
screenplay 时，daemon 先完整解析，再在一个事务内建立新的 projection revision、retire
旧投影并切换 current version。解析或写入失败时，候选仍是 SUBMITTED，旧 current version
与旧投影完全不变。历史投影只 retire，不删除或原地覆盖。

每个 creative document version 也保存 `source_document_version_id` 和
`derived_from_ids_json`。接受候选时 document 的当前 source lineage 随候选推进；development
投影只使用该候选精确指向的 source version/source snapshot，不按创建顺序猜来源。

lineage 只添加到实际派生事实：Episode、创作文档、ScriptScene、Segment、Shot、Prompt、
Media 与 ReferenceBinding。新 development 只把分集条目实际变化的 screenplay/review 标为
stale；新 screenplay 投影只把同一 episode 旧 projection 派生的镜头及其提示词、媒体与引用
标为 stale。screenplay/review 候选、重试和 Task 必须匹配 Episode 当前 accepted development
source；同项目旧候选也不能复活已删除或已换源的集。投影 retire/stale 仅作用于当前
screenplay document 的非空 projection lineage，人工或 legacy NULL lineage 不受影响。
stale 只提示重新创作，不自动重写。批处理用多个独立 Task 共享 `batch_key`，
不引入 TaskGroup；review 继续使用 `creative_documents(kind=review)`。

本 ADR 完成 ADR 0006 延后的 screenplay projection，并在该范围内具体化 ADR 0004。
不建立通用 dependency graph，不涉及媒体候选、图片接受或视频生成（阶段 4/5）。
