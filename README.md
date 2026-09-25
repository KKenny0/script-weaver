# Script-Weaver

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
pip install -e ".[dev]"
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
python -m script_weaver generate "一个穿越时空的古装爱情故事，女主角能看见别人的命运线"

# 指定标题和输出目录
python -m script_weaver generate "你的想法" --title "我的剧本" --output-dir ./output

# 管理 Skills
python -m script_weaver skills list                    # 列出所有可用 Skills
python -m script_weaver skills list --stage structuring   # 按阶段筛选
python -m script_weaver skills install ./my-skill.yaml    # 安装新 Skill
python -m script_weaver skills activate save-the-cat --stage structuring  # 激活 Skill

# 查看用户画像（学习到的偏好）
python -m script_weaver profile show
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
python -m script_weaver skills install ./my-skill.yaml
python -m script_weaver skills activate my-skill --stage scriptwriting
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
python -m script_weaver profile show
```

## Web 项目持久化

Web UI 创建的项目保存在 SQLite 数据库中，刷新页面或重启后端后仍可打开、继续修改和导出：

- 数据位置：`<data_dir>/main-web/projects.sqlite3`（默认 `~/.scriptweaver/main-web/`），可用 `SCRIPTWEAVER_DATA_DIR` 重定向；不新增其他环境变量。
- 打开哪个项目由页面 URL 的 `?project=<id>` 决定；项目 ID 与导出 JSON 中的 `meta.id` 一致。
- 每次保存都会追加一条不可变历史版本（`GET /api/projects/{id}/versions`），重命名同样推进项目 revision；携带过期 revision 的写入会返回 409，不会产生部分写入。
- 单实例运行：API 启动时对数据目录持有独占文件锁，同一数据目录上的第二个 API 实例会拒绝启动；正常退出后可立即重启。CLI 不会写该数据库。

### 升级与回滚

- 旧版本（纯内存 Web 项目）的内容不会迁移：升级前请先用旧版本导出需要的项目 JSON。
- 回滚到旧代码不会删除或改动 `main-web` 数据库与历史版本，但旧版本无法展示其中的项目；重新部署新版本后仍可读取。
- 数据库通过 `PRAGMA user_version` 管理格式版本；当数据库版本超出当前程序支持时会拒绝写入，不会自动降级。

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

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest

# 代码检查
ruff check src/ tests/

# 手动测试完整流程
python -m script_weaver generate "测试故事" --auto-approve --output-dir ./test-output
```

### Web 前端回归（Playwright）

针对 Web 端异步隔离行为（迟到响应、面板折叠、历史查看、生成连接生命周期）的端到端回归，使用页面内请求门闩与生成 SSE 替身（EventSource stand-in）复现竞争，不依赖固定等待：

```bash
cd web
npm install
npx playwright install chromium   # 首次运行前执行一次
npm run test:e2e                  # 自动启动隔离后端(8310)与前端(3100)
```

测试使用独立的临时数据目录（`/tmp/script-weaver-e2e-data`），不会触碰默认数据目录。生成一律通过页内 EventSource 替身驱动（支持等待、进度、完成、错误与关闭），且配置会用空密钥覆盖所有 `SCRIPTWEAVER_*_API_KEY`（真实环境变量优先于 `.env`），因此即使开发机配置了真实密钥，E2E 也不会、也不能发起任何真实模型调用；每个用例结束时都会断言没有任何请求真正到达 `/generate` 或 `/refine`。

## 真实模型验收

DeepSeek 适配器显式使用非思考模式，匹配当前工具消息协议。分镜按剧本场次分别生成，
全部成功后汇总；任一场次达到输出 token 上限会明确失败，不接受截断或缺场结果。

按前面的环境变量说明配置模型和 API Key 后，可使用诊断脚本运行同一条流水线：

```bash
python scripts/verify_real_model.py --output-dir /tmp/script-weaver-verification \
  --idea "60秒悬疑短片，两人一景。门牌为007，屏幕显示2026，结尾反转。"

# 失败后复用已完成的阶段；不要删除该目录中的 checkpoint.json
python scripts/verify_real_model.py --output-dir /tmp/script-weaver-verification --resume
```

每次新的验收使用新目录。`checkpoint.json` 保存已完成阶段的创作内容；`calls.jsonl`
记录模型、阶段、停止原因、token 用量和工具名称；请求失败时记录异常类型与耗时，
不记录 API Key 或消息正文。
成功时同时生成 `project.json`、`script.fountain` 和 `video_gen/`。
`PASS` 表示生成与导出完成，不代表时长、镜头数量等创作质量要求已全部满足；
严格时长预算与跨场分镜节奏控制留待后续完善。
已经完成的验收不能续跑，避免重复执行成长统计。恢复运行跳过的是已完成阶段，
当前失败阶段会重新执行，不支持从半个场次的截断输出继续。
第一阶段失败时尚无 checkpoint，需使用新目录重新执行，不能 `--resume`。

包含 SSE 回归的完整测试需要 Web 依赖：

```bash
pip install -e ".[dev,web]"
pytest
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
