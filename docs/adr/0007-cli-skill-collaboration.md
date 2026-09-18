# ADR 0007：CLI + 固定 Skills 的提案协作边界

## 状态

Accepted for phase 2 implementation; pending product review.

## 决定

Codex 通过 `script-weaver` CLI 与 loopback daemon 协作。daemon 每次启动分别轮换 creator token（`runtime.token`）与 proposal-only agent token（`agent.token`）；生产 UI 只使用前者，CLI/MCP 只使用后者。服务端逐 route 校验 capability：agent 可读、claim/context/fail、原子提交 proposal 与 prepare generation，但不能 apply/reject ChangeSet、审批文档、归档/恢复项目或 confirm/run generation。

Task 只绑定一个与 capability 同名的 Skill。创建时冻结 project、episode、document 的 metadata/current version/reject feedback、source document version、selection、revisions 与 Skill tree SHA-256；`task claim` 在同一 SQLite 事务内把 QUEUED Task 变为 RUNNING 并创建一个 AgentRun。Skill hash 对 direct-child、单段安全 ID、无 symlink、文件数和总字节设界，并使用长度前缀 framing。`task context` 只从 daemon 返回冻结事实，长正文用 cursor 分页。

三个上游创作 Skill 以仓库内产品适配副本固定到明确 commit 和 Git tree，不在运行时跟随 upstream `main`。任务再次 claim 时校验实际 Skill tree hash 与快照一致。

Agent 只能提交 Pydantic 判别联合验证的 ChangeSet operation。阶段 2 新增 `document.create` 与 `document.version.create`；CLI/MCP 的一次 submit 原子校验 task/run/project/Skill 绑定并形成 SUBMITTED ChangeSet。一个 AgentRun 只能有一个终态提案；失败或成功后重放都拒绝。`document.version.create` 只能命中 Task 冻结的 target document，且 source document 永远不能由 agent 生成新版本。MCP 不暴露旧的 start task 或多步 ChangeSet lifecycle。

创作者在 Workbench 完成两道独立决定：先 apply ChangeSet，daemon 只创建 `SUBMITTED` candidate document/version；再在文档版本栏 accept 或 reject。apply 不改变 `current_version_id`。SSE 只通知项目事实变化，浏览器仍通过相对 `/api` 读取 daemon。

## 修订关系

本 ADR 落实 ADR 0005 的 ChangeSet-only agent mutation，并修订其中“由 MCP 暴露多步 create/append/validate/submit”的旧决定：阶段 2 起只暴露原子 `submit_proposal`。它补充 ADR 0003 的 CLI HTTP 客户端，不改变 ADR 0006 的 Markdown 权威与单向投影：accepted screenplay 到 ScriptScene/Segment 的投影仍属于阶段 3。

## 不做

不内嵌 Codex runtime，不增加通用队列、ORM、Redux 或依赖图；不做 screenplay projection/stale、媒体候选或 H3。

两个 bearer token 只提供 daemon capability 隔离，不是 OS 进程隔离。同一 OS 用户下运行的恶意代码仍可能读取 token 文件；生产部署若需要对不可信 agent code 建立安全边界，必须另加进程/容器沙箱与文件权限隔离。
