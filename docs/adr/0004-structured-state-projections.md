# ADR 0004：Structured state + projections

- 状态：Accepted
- 日期：2026-08-28

SQLite 结构化领域数据是唯一事实源，Media Store 保存不可变二进制对象。Markdown、JSON、预览和联络表都是可重建投影，用于导入、导出、Agent 阅读和交付，不与数据库形成双事实源。

旧 `ProjectState` 的固定导入映射：`ProjectMeta→Project`，`Script→Episode+ScriptScene`，每个旧 `ScriptScene→Segment`，`Character/SceneDesign/ArtStyle→Asset+AssetVersion`，`Shot→Shot+PromptVersion`；无法确定的 `key_props` 产生导入警告。
