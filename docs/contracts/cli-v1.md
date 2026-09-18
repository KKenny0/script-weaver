# Workbench CLI v1

CLI 仅访问 `SCRIPT_WEAVER_DAEMON_URL` 指向的 loopback daemon，并读取每次启动轮换的 proposal-only agent token。默认路径是 daemon 数据目录下的 `agent.token`，可用 `SCRIPT_WEAVER_AGENT_TOKEN_FILE` 覆盖；creator UI 使用独立的 `SCRIPT_WEAVER_TOKEN_FILE`。stdout 的查询与变更命令输出单个 JSON 文档。

```text
script-weaver workbench doctor --json
script-weaver task list [--project-id ID] [--status QUEUED]
script-weaver task claim --task-id ID --worker-label codex
script-weaver task context TASK_ID [--section all|source|target] [--cursor N]
script-weaver task fail --task-id ID --run-id ID --message TEXT
script-weaver changeset submit TASK_ID --run-id ID --file proposal.json
script-weaver search PROJECT_ID --query TEXT [--limit 50]
script-weaver export PROJECT_ID --output DIRECTORY
```

Proposal 最大 2 MiB、500 operations、JSON 深度 32。服务端仍重新执行强类型、revision、项目归属和 task/run 绑定校验。退出码：`0` 成功、`2` 输入/API 校验失败、`3` daemon 不可用、`4` revision/状态冲突、`5` daemon 内部失败。

`changeset submit` 只形成提案。CLI 不提供 apply 或 accept/reject 命令。

`export` 要求目标目录不存在。CLI 在同一父目录完整写入临时目录后原子 rename；任何写入失败都会清理临时目录，绝不覆盖既有导出。

## 局部剧本修订

创作者 `POST /api/tasks` 可提供 `edit_scope: {draft_revision, start, end}`，必须同时指定剧本文档和 `short-drama-write`。范围为 Unicode 码点的半开区间 `[start,end)`，不是浏览器 UTF-16 单元；前端在请求前转换。后台按给定 revision 读取并冻结草稿，不信任客户端传入的正文。

`task context` 的 `selection.edit_scope` 保存范围；`target.draft_content` 与 `target.draft_revision` 保存当时完整原稿。`document_version_id` 仍为当前集的开发稿来源，不能用草稿替代来源链。

Worker 必须返回恰好一个 `document.version.create`，目标为快照中的剧本，`payload.content` 为完整修订稿，选区之前和之后逐字保持原样。后台拒绝额外操作、其他目标及越界改写。提案提交、应用、文档接受均检查草稿 revision；过期后保留当前稿，退回旧候选并重新创建任务。应用仍只创建待审批候选，不改写草稿或自动接受。

无需新增 CLI 命令或数据库迁移；旧任务没有 `edit_scope` 时沿用既有语义。
