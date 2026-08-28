# Script-Weaver 开发约束

## 当前架构

- `script-weaverd` 是 SQLite 唯一写入者，只监听 `127.0.0.1`。
- `src/script_weaver/domain/` 定义信任边界模型与允许的 DomainOperation。
- `application/workbench_service.py` 提供直接 Command/Query；`changeset_service.py` 提供原子 ChangeSet；`generation_service.py` 提供 prepare/confirm/run。
- `infrastructure/sqlite.py` 启用 WAL、外键、busy timeout、全局写锁与 `BEGIN IMMEDIATE`。
- `web/src/workbench/` 是主产品界面；所有浏览器请求使用相对 `/api`。
- `src/script_weaver/mcp/workbench_server.py` 是只读/提案 STDIO MCP；不得增加 apply、confirm、run 或通用写工具。
- `.agents/skills/` 是五个短剧 Skill 的 Canonical 副本；确定性检查只在 `capabilities/drama/`。
- 旧 `core/types.py`、pipeline、CLI 保留兼容，但不再是工作台事实源。

## 命令

```powershell
python -m pip install -e ".[dev]"
pytest -q
ruff check src tests web/api
script-weaverd
npm --prefix web install
npm --prefix web run dev
npm --prefix web run build
mcp dev src/script_weaver/mcp/workbench_server.py
```

不要直接写 SQLite，不要硬编码浏览器 daemon URL，不要引入 ORM、Redux、任务队列或通用依赖图。恢复业务事实时创建新版本/revision，不删除历史。
