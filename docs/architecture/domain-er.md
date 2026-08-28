# Workbench 领域 ER

```mermaid
erDiagram
  PROJECTS ||--o{ EPISODES : owns
  EPISODES ||--o{ SCRIPT_SCENES : contains
  EPISODES ||--o{ SEGMENTS : contains
  SEGMENTS ||--o{ SHOTS : orders
  SHOTS ||--o{ SHOT_VERSIONS : records
  PROJECTS ||--o{ ASSETS : owns
  ASSETS ||--o{ ASSET_VERSIONS : versions
  SHOTS ||--o{ REFERENCE_BINDINGS : uses
  ASSETS ||--o{ REFERENCE_BINDINGS : referenced
  SHOTS ||--o{ PROMPT_VERSIONS : owns
  SHOTS ||--o{ MEDIA_VERSIONS : owns
  PROJECTS ||--o{ TASKS : starts
  TASKS ||--|| TASK_SNAPSHOTS : freezes
  TASKS ||--o{ AGENT_RUNS : executes
  AGENT_RUNS ||--o{ AGENT_RUN_EVENTS : emits
  TASKS ||--o{ CHANGESETS : proposes
  CHANGESETS ||--o{ CHANGESET_OPERATIONS : contains
  PROJECTS ||--o{ DOMAIN_EVENTS : emits
  PROJECTS ||--o{ GENERATION_JOBS : prepares
```

初始数据库表：`schema_migrations`、`projects`、`episodes`、`script_scenes`、`segments`、`shots`、`shot_versions`、`assets`、`asset_versions`、`reference_bindings`、`prompt_versions`、`media_versions`、`surface_contexts`、`tasks`、`task_snapshots`、`agent_runs`、`agent_run_events`、`changesets`、`changeset_operations`、`changeset_impacts`、`changeset_warnings`、`domain_events`、`generation_jobs`。

真实依赖只由 `reference_bindings`、Prompt/Media owner 和明确查询表达；v1 不建通用 `dependency_edges`。
