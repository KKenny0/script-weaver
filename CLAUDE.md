# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Script-Weaver**: Agent-native 剧本 + 分镜生成系统。Python 3.11+, Pydantic v2, httpx.

从故事 idea → 完整剧本 → 分镜脚本（供下游 AI 视频生成使用）。支持多 LLM、Skill 插件、"Grows With User" 成长机制。

## Commands

```bash
# === Python Backend ===
# Install Python deps
pip install pydantic httpx pyyaml python-frontmatter rich click pytest fastapi uvicorn sse-starlette

# Run FastAPI backend (port 8000)
python web/api/main.py

# CLI generation
python -m script_weaver generate "你的故事想法" --auto-approve --output-dir ./output

# Skills / Profile
python -m script_weaver skills list
python -m script_weaver profile show

# Test
pytest

# === Web UI (Next.js) ===
cd web && npm install    # first time only
cd web && npm run dev    # starts on http://localhost:3000
                          # proxies /api/* → localhost:8000
```

## Architecture

### Python Backend
- **`src/script_weaver/core/types.py`** — 全部数据模型 (Pydantic)。所有模块的依赖根。
- **`src/script_weaver/agents/base.py`** — BaseAgent (tool-use 循环) + SimpleAgent。Agent-native 的核心。
- **`src/script_weaver/agents/impl.py`** — 9 个 Agent 实现，含 Skill 注入和 UserProfile 感知。
- **`src/script_weaver/core/pipeline.py`** — Pipeline 编排引擎：顺序执行 / 并行执行 / Human Gate / Growth Loop。
- **`src/script_weaver/llm/providers.py`** — 6 个 LLM Provider (Anthropic/OpenAI/DeepSeek/GLM/Qwen/Ollama)。
- **`src/script_weaver/skills/adapters.py`** — 多格式 Skill 适配器 (YAML/JSON/ClaudeCode MD)。
- **`src/script_weaver/skills/registry.py`** — Skill 注册管理中心（发现/加载/激活/排序/prompt 构建）。
- **`src/script_weaver/memory/`** — Grows With User: `profile.py` (UserProfile) + `evolution.py` (Skill 自进化)。
- **`src/script_weaver/exporters/video_gen_exporter.py`** — VideoGen 导出器（核心差异化功能）。

### Web UI (Next.js + FastAPI)
- **`web/api/main.py`** — FastAPI 后端，SSE streaming 端点 `/api/projects/{id}/generate`
- **`web/src/app/page.tsx`** — 前端主页面：左聊天面板 + 右结构化输出面板（6 tab）
- **`web/next.config.ts`** — API 代理: `/api/*` → `localhost:8000/api/*`

## Key Patterns

1. **Agent tool-use loop**: Agent 不是单次 LLM 调用，而是循环调用工具直到任务完成 (`agents/base.py`)
2. **Skill injection**: 每个 Agent 执行前通过 `SkillRegistry.build_agent_context()` 合并 prompt
3. **Provider-agnostic**: 通过 `create_provider()` 工厂函数切换 LLM，零代码改动
4. **Format adapters**: `BaseSkillAdapter` ABC 支持扩展新的 Skill 文件格式
5. **Growth loop**: Pipeline 完成后自动触发模式提取 → 画像更新 → Skill 评估

## Testing

Unit tests in `tests/unit/test_models.py`. Run with `pytest`.
