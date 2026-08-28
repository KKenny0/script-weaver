# ADR 0005：ChangeSet-only agent mutation

- 状态：Accepted
- 日期：2026-08-28

Codex 产生的任何修改先进入 `DRAFT → SUBMITTED → APPLIED | CONFLICTED | REJECTED` ChangeSet。MCP 只能创建、追加、校验和提交，不能 apply。UI apply 在一个事务内重验 fingerprint 与全部 revision，任一冲突则零写入。重复 apply 已成功 ChangeSet 返回原结果。

允许操作仅为：`segment.create`、`shot.create`、`shot.update`、`shot.retire`、`shot.reorder`、`asset.create`、`asset.version.create`、`reference.bind`、`reference.rebind`、`reference.set_mode`、`prompt.version.create`。
