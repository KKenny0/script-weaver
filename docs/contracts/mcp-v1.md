# Local MCP v1 契约

传输：STDIO。stdout 只允许 MCP 协议帧；日志写 stderr。MCP 通过 daemon 会话令牌访问 `127.0.0.1`，不直接读写 SQLite。

工具白名单：

- `workbench.get_active_context`
- `workbench.start_task`
- `workbench.get_task_snapshot`
- `workbench.get_project`
- `workbench.get_episode`
- `workbench.get_segment`
- `workbench.get_shot`
- `workbench.list_assets`
- `workbench.get_asset_version`
- `workbench.search_project`
- `workbench.create_changeset`
- `workbench.append_changeset_operation`
- `workbench.validate_changeset`
- `workbench.submit_changeset`
- `workbench.render_shot_preview`
- `workbench.render_segment_contact_sheet`
- `workbench.compare_shot_versions`
- `workbench.render_asset_board`
- `workbench.get_deep_link`
- `workbench.prepare_generation_job`
- `workbench.get_generation_job`

调用约定：所有进入 URL path 的 ID/类型参数由 MCP 客户端做百分号编码（`quote(..., safe="")`），`SCRIPT_WEAVER_DAEMON_URL` 只允许 loopback。ChangeSet operation payload 为强类型 Pydantic 契约（未知字段拒绝、负值拒绝），语义校验在 validate/submit 阶段完成；apply 阶段在同一事务内复检 fingerprint、base revisions、operation revisions 与项目归属，任一冲突则零事实写入并标记 CONFLICTED。

禁止工具：`apply_changeset`、`confirm_generation_job`、`run_generation_job`、Shell、SQL、文件写入。`get_active_context` 在多个工作面无法唯一确定时返回 `ambiguous`，不能猜测。`start_task` 冻结选择、revision 与 Skill manifest；`task_snapshots` 由数据库触发器保证不可修改和删除。
