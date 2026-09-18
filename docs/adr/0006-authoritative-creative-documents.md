# ADR 0006：权威创作文档与单向投影

- 状态：Accepted
- 日期：2026-08-30

Idea、原著、故事开发稿和剧本的 Markdown 正文以 SQLite 中不可变的
`creative_document_versions` 为权威事实。`document_drafts` 只是可覆盖的编辑缓冲，
`source_snapshots` 保存创建项目或追加输入时收到的原文；正文不得藏在
`projects.metadata_json`。接受、退回和恢复历史都创建或决定一个明确的文档版本，
不得原地覆盖历史版本。

`ScriptScene`、`Segment` 及其导出文件是 accepted screenplay 的单向投影，不得反向
改写 Markdown。阶段 1 尚未提供安全的完整剧本投影器，因此接受剧本文本只确认该
文本版本，必须返回 `drives_downstream=false` 与 `projection_status=not_projected`；
界面不得把它描述为已经可以驱动分镜。投影将在后续 ADR 明确的原子事务中完成。

本 ADR 补充并在冲突处修订：

- ADR 0002：产品入口采用 CLI + Skills；STDIO MCP 保持只读/提案兼容入口，不再是
  Codex 的首选写入入口。
- ADR 0004：结构化事实仍是唯一事实源，但创作 Markdown 本身是一级结构化版本事实，
  不属于可丢弃的导出投影；只有它派生出的场次、分段和导出物是投影。
- ADR 0005：创作者通过 Workbench 可以直接保存草稿、提交和审批文档；Agent 将来新增
  文档只能走 typed ChangeSet，阶段 1 不扩展 Agent 允许操作集合。
