# ADR 0009：候选媒体、正式 REF 与 daemon 摄取边界

- 状态：Accepted for phase 4 implementation; pending product review
- 日期：2026-08-30

图片生成结果首先是 `media_versions(candidate_status=candidate)`，不是项目事实，也不改变
Asset 的 current version。GenerationJob 的同一批输出全部保留；局部编辑用
`parent_candidate_id` 建立 B → B1 谱系，不覆盖 B。GPT Image、Fake adapter 与 Agent 导入
最终都进入同一候选查询、比较和接受面板。

创作者接受候选时，daemon 在一个 SQLite 事务内创建新的 AssetVersion、切换 Asset 的
current version、把候选关联为正式 REF，并按现有 `reference_bindings` 的 `follow_latest`
关系精确标记对应 Shot、Prompt、Media 和 Binding stale。事务失败时 Asset current REF 与
候选状态都不改变；stale 不触发自动重生成。

Agent 图片使用 `application/octet-stream` 交给 daemon。请求元数据必须绑定 project、
RUNNING Task、RUNNING AgentRun、冻结 owner、prompt 与预期 SHA-256；daemon 在写 staging 前
和入库事务中各校验一次 Task/Run/Skill/owner 范围。接口不接受客户端文件路径，二进制不放入
ChangeSet JSON。MediaStore 继续负责 staging 与 content-addressed immutable objects；未接受
候选可在未来由清理策略回收，正式 REF 保留 AssetVersion、来源和 hash。

Agent token 只可导入候选，creator token 不能冒充 Agent 导入；接受 REF 仍只允许 creator。
这是一条 daemon capability boundary，不提供同一 OS 用户恶意代码的进程隔离保证。

本 ADR 具体化 ADR 0004 的媒体投影边界，并沿用 ADR 0005 的“Agent 只能提案/候选”原则。
视频成片仍由外部服务完成；H3、视频轮询和生产包属于阶段 5。
