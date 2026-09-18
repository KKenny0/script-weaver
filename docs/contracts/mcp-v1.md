# Local MCP v1 契约

传输：STDIO。stdout 只允许 MCP 协议帧；日志写 stderr。MCP 通过 proposal-only agent token 访问 `127.0.0.1`，不直接读写 SQLite。

工具白名单：

- `workbench.get_active_context`
- `workbench.list_tasks`
- `workbench.claim_task`
- `workbench.get_task_context`
- `workbench.fail_task`
- `workbench.submit_proposal`
- `workbench.get_task_snapshot`
- `workbench.get_project`
- `workbench.get_episode`
- `workbench.get_segment`
- `workbench.get_shot`
- `workbench.list_assets`
- `workbench.get_asset_version`
- `workbench.search_project`
- `workbench.render_shot_preview`
- `workbench.render_segment_contact_sheet`
- `workbench.compare_shot_versions`
- `workbench.render_asset_board`
- `workbench.get_deep_link`
- `workbench.prepare_generation_job`
- `workbench.get_generation_job`

调用约定：所有进入 URL path 的 ID/类型参数由 MCP 客户端做百分号编码（`quote(..., safe="")`），`SCRIPT_WEAVER_DAEMON_URL` 只允许 loopback。MCP 从 `SCRIPT_WEAVER_AGENT_TOKEN_FILE`（默认 `agent.token`）取 credential。ChangeSet operation payload 为强类型 Pydantic 契约（未知字段拒绝、负值拒绝），一次 `submit_proposal` 原子完成语义校验和提交；apply 阶段在同一事务内复检 fingerprint、base revisions、operation revisions 与项目归属，任一冲突则零事实写入并标记 CONFLICTED。

禁止工具：`start_task`、旧多步 ChangeSet lifecycle、`apply_changeset`、文档 accept/reject、`confirm_generation_job`、`run_generation_job`、Shell、SQL、文件写入。`get_active_context` 在多个工作面无法唯一确定时返回 `ambiguous`，不能猜测。Task 由 creator Workbench 建立并冻结选择、revision 与唯一 Skill manifest；`task_snapshots` 由数据库触发器保证不可修改和删除。
