# Script-Weaver

Script-Weaver 是 local-first、workbench-first 的 AI 短剧视频制作工作台。SQLite 中的项目事实由 `script-weaverd` 单写，Workbench UI 与 Codex MCP 共享同一领域内核；Codex 只能提出 ChangeSet，不能直接 apply 或确认付费生成。

## Workbench 启动

首次只需执行一次准备：

```powershell
uv sync --python 3.11 --extra dev
npm --prefix web ci
npm --prefix web run build
```

准备完成后需要同时保持两个终端运行。

终端 A — 启动本地 daemon：

```powershell
uv run script-weaverd
```

终端 B — 启动生产 Workbench：

```powershell
npm --prefix web start
```

打开 `http://127.0.0.1:3000`。Next dev/start 显式绑定 `127.0.0.1`，daemon 只监听 `127.0.0.1:8000`；不支持局域网访问。浏览器只请求相对 `/api`；Next 服务端读取 creator `runtime.token`，CLI/MCP 读取 proposal-only `agent.token`。数据默认位于当前用户应用数据目录，包含 SQLite、Media Store 和每次启动轮换的两个 token。

`uv` 是唯一的主安装与 Python 启动流程。若需要直接调用项目虚拟环境，`.venv/bin/script-weaverd` 与 `uv run script-weaverd` 等价，仅作为排障补充。Python 低于 3.11 时，CLI 与 daemon 会报告当前版本，并提示重新运行上面的 `uv sync --python 3.11 --extra dev`。

日常使用走上面的 production server。只有开发前端时，才在终端 B 改用热更新模式：

```powershell
npm --prefix web run dev
```

项目级 Codex MCP 配置位于 `.codex/config.toml`；五个短剧能力位于 `.agents/skills/`。验证命令：

```powershell
XDG_DATA_HOME="$(mktemp -d)" uv run --extra dev --python 3.11 python -m pytest -q
uv run --extra dev --python 3.11 ruff check src tests web/api
npm --prefix web run build
npm --prefix web run check:proxy
npm --prefix web run check:start
mcp dev src/script_weaver/mcp/workbench_server.py
```

架构决策见 `docs/adr/`，MCP 白名单见 `docs/contracts/mcp-v1.md`，垂直切片见 `docs/testing/vertical-slice.md`。

**Agent-native 剧本 + 分镜生成系统** — 随用户成长的 AI 创作管线

> 从一个故事想法出发，自动生成完整剧本和专业分镜脚本，可直接用于下游视频生成工具（Seiko/Runway/Kling 等）。

## 核心特性

- **Agent-Native 架构**: 9 个专业 Agent（创意细化、大纲构建、角色设计、场景设计、美术指导、剧本写作、分镜拆解、质量审查、编排调度），每个 Agent 拥有自主 tool-use 循环
- **多 LLM 支持**: 统一接口支持 Anthropic Claude / OpenAI GPT / DeepSeek / GLM / Qwen / Ollama 等任意 OpenAI 兼容服务
- **Skill 插件系统**: 支持用户自定义 Skills（YAML / JSON / Claude Code .md 格式），可在每个 Pipeline 阶段注入，支持动态激活/禁用/排序
- **Grows With User**: 三层成长机制 — 项目级经验记忆 + 跨项目用户画像 + Skill 自进化
- **下游就绪**: 分镜输出包含逐镜头的 image/video prompt，可直接喂给 AI 视频生成工具
- **多格式导出**: JSON / Fountain (Final Draft 兼容) / VideoGen CSV

## 快速开始

### 安装

```bash
cd script-weaver
uv sync --python 3.11 --extra dev
```

### 配置

```bash
cp .env.example .env
# 编辑 .env，填入你的 LLM API Key
```

支持的 Provider:

| 环境变量 | 说明 |
|----------|------|
| `SCRIPTWEAVER_LLM_PROVIDER` | `anthropic` / `openai` / `deepseek` / `glm` / `qwen` / `openai_compatible` |
| `SCRIPTWEAVER_LLM_MODEL` | 模型名称 (默认: `claude-sonnet-4-20250514`) |
| `SCRIPTWEAVER_ANTHROPIC_API_KEY` | Anthropic API Key |
| `SCRIPTWEAVER_OPENAI_API_KEY` | OpenAI / DeepSeek / 兼容服务 Key |
| `SCRIPTWEAVER_GLM_API_KEY` | 智谱 GLM API Key |
| `SCRIPTWEAVER_QWEN_API_KEY` | 通义千问 API Key |
| `SCRIPTWEAVER_OPENAI_BASE_URL` | OpenAI 兼容端点 (Ollama/vLLM 等) |

### 运行

```bash
# 从一个想法生成完整剧本+分镜
uv run script-weaver generate "一个穿越时空的古装爱情故事，女主角能看见别人的命运线"

# 指定标题和输出目录
uv run script-weaver generate "你的想法" --title "我的剧本" --output-dir ./output

# 管理 Skills
uv run script-weaver skills list                    # 列出所有可用 Skills
uv run script-weaver skills list --stage structuring   # 按阶段筛选
uv run script-weaver skills install ./my-skill.yaml    # 安装新 Skill
uv run script-weaver skills activate save-the-cat --stage structuring  # 激活 Skill

# 查看用户画像（学习到的偏好）
uv run script-weaver profile show
```

## 系统架构

```
用户输入 (idea/大纲/任何格式)
       │
       ▼
┌─────────────┐
│ Orchestrator │ ← 编排器：路由请求到正确的 Agent
└──────┬──────┘
       │
       ├──────────────┐
       ▼              │
┌─────────────┐      │
│ IdeaRefiner  │──[Gate]──▶ Structurer ──[Gate]──▶ Designers (并行)
│ (创意细化)   │              │              │  ├── CharacterDesigner
└─────────────┘              │              │  ├── SceneDesigner
                              │              │  └── ArtDirector
                              ▼              │
                        ┌──────────┐        │
                        │ScriptWriter│──[Gate]──▶ StoryboardArtist
                        └──────────┘                │
                                                   ▼
                                           VisualHighlights
                                                   │
                                                   ▼
                                          ┌──────────────┐
                                          │  Exporters    │
                                          │ JSON/Fountain │
                                          │ /VideoGen     │
                                          └──────────────┘
                                                   │
                                                   ▼
                                          ┌──────────────┐
                                          │ Growth Loop   │ ◄── 记录决策 → 提取模式 → 更新画像
                                          │ (Grows With U)│     → 评估 Skill → 提议新 Skill
                                          └──────────────┘
```

## 数据模型

所有核心数据模型定义在 `src/script_weaver/core/types.py`：

| 模型 | 说明 |
|------|------|
| `ProjectState` | 项目状态容器（所有 artifact 的集合） |
| `Outline` | 结构化大纲（基本信息 + 角色概要 + 情节节拍） |
| `Character` | 角色设计（外貌/性格/形象描述/image_prompt） |
| `SceneDesign` | 场景设计（环境/氛围/色调） |
| `ArtStyle` | 美术风格指南 |
| `Script` | 剧本（ScreenJSON 风格：场景标题/动作/对白/转场） |
| `Shot` | 单个分镜镜头（景别/机位运动/画面描述/时长/video_prompt） |
| `Storyboard` | 完整分镜脚本（Shot 列表） |
| `Skill` | 统一 Skill 表示（格式无关） |
| `SkillBinding` | 项目级 Skill 激活配置 |
| `UserProfile` | 跨项目用户创作画像 |
| `ProjectMemory` | 项目级决策记录和模式提取 |

## 内置 Skills

| Skill ID | 名称 | 适用阶段 | 说明 |
|----------|------|----------|------|
| `save-the-cat` | 救猫咪节拍表 | structuring | Blake Snyder 15 节拍结构 |
| `story-circle` | Dan Harmon 故事圈 | structuring | 8 段式循环叙事 |
| `three-act` | 经典三幕式 | structuring | 通用戏剧结构 |
| `character-prototype` | 人物原型系统 | character_design | 荣格原型 + 角色弧光 |
| `wong-kar-wai-style` | 王家卫视觉风格 | art_direction | 抽帧慢动作/高饱和色彩/都市孤独感 |
| `cinematography-basics` | 基础运镜语言库 | storyboarding | 专业影视分镜术语参考 |

## Skill 自定义

### 格式 1: Native YAML（推荐）

```yaml
# my-skill.yaml
id: my-custom-skill
name: "我的自定义方法"
stage: scriptwriting
description: "我的特殊编剧技巧"
version: "1.0"

prompt_injection: |
  在写剧本时，请遵循以下规则：
  1. 每场戏必须有一个"情感转折点"
  2. ...

constraints:
  - "规则1"
  - "规则2"
```

### 格式 2: Claude Code Markdown (.md)

直接使用 Claude Code 原生 `.md` skill 文件，系统会自动适配：

```markdown
---
name: screenwriting-master
description: 山音超级编剧大师
TRIGGER when: 用户提到写剧本、写短片...
---

# 技能说明
这是一套完整的编剧方法论...

## 输出要求
1. 必须包含人物小传
2. ...
```

### 安装和使用

```bash
uv run script-weaver skills install ./my-skill.yaml
uv run script-weaver skills activate my-skill --stage scriptwriting
```

## Grows With User — 成长系统

Script-Weaver 不是一次性工具——它会随着使用越来越懂你：

### Layer 1: 项目记忆 (ProjectMemory)
- 记录你在项目中做的每一个决定（接受/修改/拒绝）
- 自动提取重复的修改模式为可复用 Pattern

### Layer 2: 用户画像 (UserProfile)
- 跨项目累积的创作偏好（存储在 `~/.scriptweaver/user_profile.json`）
- 学习你喜欢的叙事结构、对话密度、视觉风格
- 推荐你可能喜欢的 Skills

### Layer 3: Skill 自进化
- 分析你的修改模式，自动提议创建新 Skill
- 根据 Skill 使用效果（接受率/修改频率）自动优化
- 基于历史成功率对 Skills 排序推荐

```bash
# 查看系统学到了什么
uv run script-weaver profile show
```

## 导出格式

### VideoGen 导出（核心差异化功能）

每个 Shot 都会被导出为视频生成工具可用的格式：

```
output/video_gen/
├── video_gen_shots.json      # 全部 shot 的结构化数据
├── video_gen_shots.csv       # 表格格式（可用 Excel 打开）
└── shots/
    ├── shot_xxx_1.txt        # Shot 1: image_prompt + video_prompt
    ├── shot_xxx_2.txt        # Shot 2
    └── ...
```

每个 shot 包含：
- **image_prompt**: 首帧图像生成提示词（构图/光线/色彩/主体姿态）
- **video_prompt**: 视频生成提示词（动作/运镜/物理真实感）
- **duration_seconds**: 时长
- **transition_in/out**: 转场方式
- **dialogue_text / voiceover_text**: 音频轨道信息

### 可选：本地 MiniMax-H3 FL2VA

生产工作台只接 H3-Base 768p FL2VA，不下载或分发模型权重。先按模型当前 License 自行部署：

```bash
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va
```

再给 daemon 配置字面量 loopback endpoint 和 H3 进程可见的同路径共享目录：

```bash
export SCRIPT_WEAVER_H3_URL=http://127.0.0.1:30000
export SCRIPT_WEAVER_H3_SHARED_MEDIA_ROOT=/absolute/shared/script-weaver-h3
uv run script-weaver workbench doctor
```

工作台只把已接受并冻结的关键帧复制到该共享目录。H3 视频从 `/content` 接口摄取后仍是
候选，创作者接受后才成为正式 REF；生产包 manifest 不包含权重或未接受媒体。

## 开发

```bash
# 安装开发依赖
uv sync --python 3.11 --extra dev

# 运行测试
XDG_DATA_HOME="$(mktemp -d)" uv run --extra dev --python 3.11 python -m pytest

# 代码检查
uv run --extra dev --python 3.11 ruff check src tests web/api

# 手动测试完整流程
uv run script-weaver generate "测试故事" --auto-approve --output-dir ./test-output
```

## 项目结构

```
script-weaver/
├── src/script_weaver/
│   ├── __init__.py
│   ├── cli.py                      # CLI 入口
│   ├── core/
│   │   ├── types.py                # 全部 Pydantic 数据模型 ★
│   │   ├── config.py               # 配置管理
│   │   └── pipeline.py             # Pipeline 编排引擎
│   ├── agents/
│   │   ├── base.py                 # BaseAgent (tool-use 循环) ★
│   │   └── impl.py                 # 全部 9 个 Agent 实现
│   ├── tools/
│   │   └── definitions.py          # 工具定义与注册
│   ├── prompts/
│   │   └── system_prompts.py       # 中文 System Prompt 模板
│   ├── llm/
│   │   ├── client.py               # LLM Client 封装
│   │   └── providers.py            # 6 个 LLM Provider 实现
│   ├── skills/
│   │   ├── adapters.py             # 多格式 Skill 适配器
│   │   ├── registry.py             # Skill 注册管理中心
│   │   └── builtin/                # 6 个内置 Skills (YAML)
│   ├── memory/
│   │   ├── profile.py              # UserProfile 持久化
│   │   └── evolution.py            # Skill 自进化引擎
│   ├── validators/
│   └── exporters/
│       ├── json_exporter.py
│       ├── fountain_exporter.py
│       └── video_gen_exporter.py    # VideoGen 导出 ★
├── tests/
│   └── unit/
│       └── test_models.py
├── snapshot/                       # 参考截图
├── pyproject.toml
├── .env.example
└── README.md
```

## License

MIT
