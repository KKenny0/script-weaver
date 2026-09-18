# Script Weaver V8 生产工作台实施 To Do

- 状态：阶段 1–5 已实施并通过主审；待创作者整体验收
- 日期：2026-08-30
- 产品基线：已确认的 V8 可点击原型
- 技术边界：Workbench-first、CLI + Skills、`script-weaverd` 单写 SQLite

## 完成标准

V8 原型只作为行为验收规格。生产实现必须让创作者真实保存、编辑、审批和追踪作品事实；Codex 只能通过 Task、AgentRun 和 ChangeSet 提案；生成媒体在接受前只能是候选。

## 不变约束

- 浏览器只访问相对 `/api`，daemon 只监听 loopback。
- SQLite 是结构化事实源，Media Store 保存不可变二进制对象。
- Agent 不得 apply ChangeSet、审批文档、确认付费生成或直接写数据库。
- 不引入 ORM、Redux、通用任务队列或通用依赖图。
- 历史版本不得删除或原地覆盖；恢复必须创建新版本。
- 五个阶段必须分别可合并、可使用，不能依赖下一阶段才能成立。

## 阶段 1：真实项目、输入与文本编辑

- [x] 新增 ADR 0006，明确 SQLite 内 Markdown 文本权威、单向剧本投影，以及对 ADR 0002、0004、0005 的修订关系。
- [x] 新增 additive migration，不修改 `0001_initial.sql`：
  - [x] `creative_documents`
  - [x] `creative_document_versions`
  - [x] `document_drafts`
  - [x] `source_snapshots`
  - [x] `projects.archived_at`
- [x] 增加 Idea、原著/长材料、单集剧本、多集整稿四种真实项目入口。
- [x] 原始输入形成 source document/version，不写入 project metadata 充当正文。
- [x] 实现项目库、继续创作、归档、恢复和项目内“新的输入”。
- [x] 实现 Markdown 草稿编辑、自动保存、保存失败保留本地内容、版本提交、整体接受、整体退回和反馈。
- [x] 单集剧本支持“直接采用原文”和“进入修订”；阶段 1 接受的是文本事实，明确标记 `not_projected` 并阻止下游就绪声明，完整原子投影仍属于阶段 3。
- [x] 项目或剧集切换时处理未保存草稿，不静默丢失。
- [x] 增加最小 GitHub Actions：Python 3.11 pytest、ruff、`npm ci`、Next build。
- [x] 自动化覆盖迁移、文档版本不变性、审批冲突、归档恢复、跨项目边界和 daemon 重启持久化。
- [x] 浏览器验收 1440×900 与 1024×700，四种入口无死路。

阶段 1 验收：不启动 Codex，也能从 Idea 或现成剧本创建项目、编辑、提交、接受、恢复历史并切换项目。

## 阶段 2：CLI + Skills 真实协作闭环

- [x] 实现 `workbench doctor`、`task list/claim/context/fail`、`changeset submit`、`search`、`export`。
- [x] 导入并固定 `short-drama-novel-analyze`、`short-drama-develop`、`short-drama-write` 的产品适配副本。
- [x] 不直接跟随 upstream `main`；升级必须单独审查破坏性 creator-first 变化。
- [x] Task claim 原子创建 AgentRun，context 固定项目、集数、文档版本、选择、revision 和 Skill hash。
- [x] Agent 仅能提交 `document.create`、`document.version.create` 等 typed ChangeSet operations。
- [x] Workbench 增加任务抽屉、ChangeSet diff、apply 和文档候选审批。
- [x] 基于 domain events 增加项目 SSE，CLI 提交后 Workbench 自动刷新。

阶段 2 验收：Idea → develop Task → CLI/Codex → ChangeSet submitted → UI apply → development candidate → 创作者整体接受。

## 阶段 3：多集事实、剧本投影与 stale

- [x] 接受 development 后，从同一份分集地图创建 Episode 骨架，不单独审批 episode map。
- [x] 多集整稿保存 source byte span/hash，确认分集候选后再建立各集。
- [x] accepted screenplay 在一个事务内校验并重建 ScriptScene/Segment；失败时旧版本和投影不变。
- [x] 为下游版本事实增加 `source_document_version_id`、`source_projection_revision`、`derived_from_ids_json`。
- [x] 新 accepted 上游只精确标记下游 stale，不自动重写。
- [x] 多集批量处理创建独立 Task，以 `batch_key` 聚合，不新增 TaskGroup 服务。
- [x] Review 结果复用 versioned document，`kind=review`，不新建 Finding 子系统。
- [x] 文档候选固定精确 source lineage；分集 `story` 参与 fingerprint；removed 集阻止旧候选复活。
- [x] UI 保留当前集/段选择，区分 removed、accepted stale 与草稿需更新，并提供退回重写和投影返回入口。

阶段 3 验收：EP03 可 reject → 新 Task → v2；EP04 可同时显示 accepted + stale；其余剧集不被误改。

## 阶段 4：真实视觉创作循环

- [x] 扩展 `media_versions`，记录 candidate status、generation job、父候选、来源提示词和正式 AssetVersion。
- [x] 生成批次所有输出先成为 candidate，不自动成为 current。
- [x] 局部编辑创建 B1 并保留 B。
- [x] 接受候选由 daemon 单事务创建正式 AssetVersion/REF，并精确标记下游 stale。
- [x] 增加绑定 Task、AgentRun、owner 和 SHA-256 的 Agent 二进制候选导入。
- [x] 二进制使用 `application/octet-stream` 进入 daemon staging，不进入 ChangeSet JSON，不接受任意 Media Store 路径。
- [x] GPT Image 与 Agent 生成复用同一候选比较面板。

阶段 4 验收：剧本引用 → 三候选 → B → B1 → 正式 REF → 分镜/关键帧引用 → 替换 REF 后下游 stale。

## 阶段 5：本地 H3 与版本化生产包

- [x] 第一版只接 H3 FL2VA；Ref2VA、2K Regenerate 和 Hosted API 不在本阶段范围。
- [x] 增加可选 `SCRIPT_WEAVER_H3_URL` 与 `SCRIPT_WEAVER_H3_SHARED_MEDIA_ROOT`，URL 必须是 loopback。
- [x] `doctor` 硬校验 endpoint 可达、共享目录读写和模型身份；FL2VA/768p 是客户端固定请求，服务端没有 capability endpoint 时明确标记为未验证。
- [x] GenerationJob 保存 H3 job ID，轮询异步状态并摄取完成 MP4。
- [x] daemon 重启后查询已有 H3 job，不自动重新提交。
- [x] 输入 revision 变化使确认失效；视频先成为 candidate media。
- [x] 从 accepted facts 生成版本化生产包 manifest，列出 missing、failed、stale、excluded。
- [x] 交付后提供下一集、下一季、新作品和项目库入口。
- [x] Script Weaver 不分发 H3 权重；用户自行部署并接受当前 License。

阶段 5 验收：冻结关键帧 + 视频提示词 → 本地 H3 → 视频候选 → 接受 → 版本化生产包 → 下一次创作。

## 全局验证清单

- [x] Idea、原著、单集和多集四条入口。
- [x] Codex 不可用时仍可手工编辑和审批。
- [x] ChangeSet apply 与创作者 accept 是两道独立闸门。
- [x] reject feedback 进入下一轮 Task context。
- [x] 跨项目、跨剧集、跨 Task/Run 的绑定全部拒绝。
- [x] 生成、上传、投影或 H3 失败不得改变 accepted facts。
- [x] daemon 重启后 accepted facts、反馈、历史、lineage 与候选仍存在。
- [x] 1440×900 与 1024×700 浏览器实跑通过，console 无 error。

## 明确不做

- 内嵌 Codex runtime 或聊天页。
- 多人协作、账号、云同步和远程 daemon。
- Markdown 与 ScriptScene 双向同步。
- 自动跨阶段运行或自动接受 AI 结果。
- 最终剪辑、配音、字幕、混音与发行系统。
