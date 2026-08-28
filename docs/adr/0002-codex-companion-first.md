# ADR 0002：Codex Companion first

- 状态：Accepted
- 日期：2026-08-28

v1 采用 Codex Desktop/CLI + 本地 STDIO MCP + `script-weaverd`。先稳定领域契约、TaskSnapshot 和 ChangeSet，再评估 Codex App Server 内嵌。外部 Codex 与未来内嵌入口必须共享同一 daemon、Skills 和项目事实。
