# Script-Weaver

> **Agent-Native 剧本与分镜生成引擎** — 随创作者共同进化的 AI 创作管线，将原始创意一站式转化为影视级标准剧本与逐镜头 AI 视频生成提示词。

<p align="center">
  <a href="README.md"><b>English</b></a> | <a href="README_zh.md"><b>简体中文</b></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12-blue?logo=python" alt="Python Version" />&nbsp;<img src="https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi" alt="FastAPI" />&nbsp;<img src="https://img.shields.io/badge/Next.js-15-black?logo=next.js" alt="Next.js" />&nbsp;<img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" />&nbsp;<img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome" />
</p>

---

## 核心特性

- 🎬 **从创意到可落地资产**：输入一句话故事概念或粗纲，系统自动演进生成节拍大纲、多维角色小传、场景设计、行业通用剧本（Fountain / ScreenJSON）与高保真分镜脚本。
- 🤖 **9 个专业协同 Agent**：包含创意细化（Idea Refiner）、大纲构建（Structurer）、角色设计（Character Designer）、场景设计（Scene Designer）、美术指导（Art Director）、剧本写作（Script Writer）、分镜拆解（Storyboard Artist）、视觉高光（Visual Highlights）与编排调度（Orchestrator），每个 Agent 均具备独立的工具调用（Tool-use）循环与自主反思能力。
- 🎥 **直通下游 AI 视频生成**：专为当代主流视频模型（**可灵 Kling、Runway Gen-3、Luma Dream Machine、MiniMax 海螺、Pika、Sora** 等）深度定制。同时生成首帧静态图像提示词与时序动态运镜提示词。
- 🧩 **可扩展 Skill 技能库**：即插即用的编剧与视听创作方法论（《救猫咪》15 节拍表、Dan Harmon 故事圈、荣格人物原型系统、王家卫视觉风格、基础影视运镜语言库）。全面兼容原生 YAML、JSON 以及 Claude Code `.md` 技能格式。
- 🧠 **Grows With User（三层成长进化机制）**：
  - **项目级记忆（Project Memory）**：沉淀单项目修改决策并提取可复用 Pattern。
  - **跨项目用户画像（User Profile）**：持续学习你的叙事节奏、对白密度与视听美学偏好。
  - **Skill 自进化（Skill Evolution）**：根据历史采纳率与修改模式，自主提议新技能并迭代优化老技能。
- ⚡ **生产级工程可靠性**：内置 SQLite 状态持久化、CAS（Compare-And-Swap）版本乐观锁、独立于 HTTP 维持的服务端长驻运行与 SSE 流式断线重连、原子级阶段断点快照（`--resume`）。
- 💻 **双端交互体系**：兼具极佳交互体验的双栏 **Web 创作工作台**（Next.js 15 + Tailwind + Lucide）与高吞吐的自动化 **CLI 工具**。

---

## 双栏 Web 创作工作台布局

Script-Weaver 配备了专为剧作与分镜生产定制的响应式双栏 Web 创作工作台：

```
┌───────────────────────────────────────┬─────────────────────────────────────────────────────────┐
│ 左侧控制台：交互式 AI 协同与调度        │ 右侧主面板：全流程结构化生产物                          │
├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
│ • 自然语言创意输入与即时指令交互        │ • [总览]    项目元数据与核心概念梳理                    │
│ • 实时 SSE 流式生成进度与思考日志     │ • [大纲]    情节点与结构节拍表（救猫咪 / 故事圈等）     │
│ • 卡片级精准局部润色控制台              │ • [角色]    角色小传、原型弧光与人物首帧视觉 Prompt     │
│ • 修改 Diff 对比与采纳确认（Review）  │ • [场景]    场景氛围、色调定调与光影参考设计            │
│ • 随时一键终止运行与安全断点续跑        │ • [剧本]    ScreenJSON / Fountain 标准工业剧本          │
│ • 多格式一键打包导出（JSON/Fountain/ZIP)│ • [分镜]    逐镜头机位、运镜动力学及 AI 视频提示词      │
└───────────────────────────────────────┴─────────────────────────────────────────────────────────┘
```

---

## 系统流水线架构

```
用户输入 (故事概念 / 大纲 / 碎戏想法)
       │
       ▼
┌──────────────┐
│ Orchestrator │ ── 编排路由并管理各阶段状态转换
└──────┬───────┘
       │
       ├──────────────┐
       ▼              │
┌──────────────┐      │
│  IdeaRefiner │──[人工 Gate]──▶ Structurer ──[人工 Gate]──▶ Designers (并行设计)
│ (概念孵化)   │                     │                           ├── CharacterDesigner
└──────────────┘                     │                           ├── SceneDesigner
                                     ▼                           └── ArtDirector
                               ┌─────────────┐                          │
                               │ScriptWriter │──[人工 Gate]──▶ StoryboardArtist
                               └─────────────┘                         │
                                                                       ▼
                                                                VisualHighlights
                                                                       │
                                                                       ▼
                                                              ┌─────────────────┐
                                                              │    Exporters    │
                                                              │  JSON / Fountain│
                                                              │  / VideoGen ZIP │
                                                              └────────┬────────┘
                                                                       │
                                                                       ▼
                                                              ┌─────────────────┐
                                                              │   Growth Loop   │
                                                              │ 记录决策 / 提炼模式 │
                                                              │ 迭代画像 / 进化技能 │
                                                              └─────────────────┘
```

---

## 快速开始

### 1. 环境准备与依赖安装

- **Python**：`>= 3.10`（官方推荐 3.11 或 3.12）
- **Node.js**：`>= 18.0`（用于运行 Web 前端）

```bash
# 克隆代码仓库
git clone https://github.com/KKenny0/script-weaver.git
cd script-weaver

# 安装 Python 后端核心及扩展依赖 (含 dev 与 web 模块)
pip install -e ".[dev,web]"

# 安装 Web 前端依赖
cd web && npm install && cd ..
```

### 2. 多模型配置 (.env)

复制环境变量示例模板并填入对应模型供应商的 API Key：

```bash
cp .env.example .env
```

系统原生支持国内外各大主流大模型以及任意 OpenAI 兼容服务（如本地 Ollama / vLLM）：

| 环境变量 | 说明 | 支持取值 / 示例 |
|---------|------|----------------|
| `SCRIPTWEAVER_LLM_PROVIDER` | 当前使用的 LLM 供应商 | `anthropic` / `openai` / `deepseek` / `glm` / `qwen` / `openai_compatible` |
| `SCRIPTWEAVER_LLM_MODEL` | 调用的模型名称 | 如 `claude-3-7-sonnet-latest`、`deepseek-chat`、`gpt-4o`、`glm-4-plus`、`qwen-plus` |
| `SCRIPTWEAVER_ANTHROPIC_API_KEY` | Anthropic 专属 Key | `sk-ant-...` |
| `SCRIPTWEAVER_OPENAI_API_KEY` | OpenAI / DeepSeek Key | `sk-...` |
| `SCRIPTWEAVER_GLM_API_KEY` | 智谱 BigModel 开放平台 Key | 智谱开放平台 API Key |
| `SCRIPTWEAVER_QWEN_API_KEY` | 通义千问 DashScope Key | 阿里云 DashScope API Key |
| `SCRIPTWEAVER_OPENAI_BASE_URL` | 本地或第三方兼容接口 Base URL | 如 `http://localhost:11434/v1`（Ollama） |

---

### 3. 启动 Web 创作工作台

在两个独立的终端窗口中分别运行后端与前端服务：

```bash
# 终端 1: 启动 FastAPI 后端服务（运行于 8000 端口）
python web/api/main.py

# 终端 2: 启动 Next.js 前端工作台（运行于 3000 端口）
cd web
npm run dev
```

在浏览器打开 [http://localhost:3000](http://localhost:3000) 即可开始创作。前端已默认将 `/api/*` 请求透明反向代理至后端的 8000 端口。

---

### 4. 命令行 CLI 极速体验

通过一条指令直接从创意概念生成完整剧本、角色小传、场景氛围及视频分镜：

```bash
# 从创意构思全自动生成（开启自动通过门禁）
python -m script_weaver generate "一个赛博朋克霓虹悬疑故事：记忆侦探在侦破连环案时，发现自己的童年记忆是人工合成的伪造品。" --auto-approve --output-dir ./output

# 指定剧本标题与输出目录
python -m script_weaver generate "宋代时空穿越爱情悬疑短片" --title "命运丝线" --output-dir ./dist

# 从上次意外中断的阶段安全断点续跑
python -m script_weaver generate --resume --output-dir ./output
```

#### 技能（Skills）与用户画像（Profile）管理

```bash
# 查看当前所有可用 Skills
python -m script_weaver skills list

# 按阶段筛选查看 Skills
python -m script_weaver skills list --stage structuring

# 本地安装自定义 Skill
python -m script_weaver skills install ./my-skill.yaml

# 为特定阶段激活指定 Skill
python -m script_weaver skills activate save-the-cat --stage structuring

# 查看系统当前学习到的用户创作画像偏好
python -m script_weaver profile show
```

---

## 下游 AI 视频生成衔接（VideoGen 核心资产）

Script-Weaver 最具竞争力的功能是 **VideoGen 导出器**，它彻底打通了文学剧本与视觉生成之间的鸿沟。

### 导出产物结构

在指定 `video_gen` 格式时（CLI `--format video_gen` 或 Web 端导出），将输出如下规范结构：

```
output/video_gen/
├── video_gen_shots.json      # 全部镜头的完整结构化元数据
├── video_gen_shots.csv       # 表格摘要视图（支持 Excel / 飞书多维表格打开）
└── shots/
    ├── shot_001.txt          # 镜头 1：首帧图像提示词 + 运镜视频提示词 + 时长元数据
    ├── shot_002.txt          # 镜头 2：...
    └── ...
```

### 真实分镜镜头拆解范例

每个导出的镜头文件均包含高度专业且同步的提示词组合：

```yaml
Shot ID: shot_scene1_01
所属场次: INT. 雨夜机体维护实验室 - 夜
镜头时长: 4.5 秒
摄影运镜: 低角度慢速推进推轨，35mm 变形宽银幕镜头，浅景深

Image Prompt (首帧关键画面提示词):
  Cinematic 35mm film still, low angle, medium close-up of Kaelen in a dim, rain-streaked cybernetics lab. Harsh neon-amber backlight catching cigarette smoke, volumetric shadows, intricate cybernetic eye with subtle blue aperture ring, photorealistic textures, muted teal and tungsten palette, 8k resolution.

Video Prompt (时序运镜与物理动作动力学):
  Camera slowly tracks forward from low angle as Kaelen exhales smoke, his mechanical iris clicking and recalibrating. Neon rain reflections ripple across the window pane behind him. Atmospheric dust motes floating through volumetric amber beam. Smooth organic motion, cinematic pacing, 24fps.

台词与画外音:
  凯伦 (画外音): "记忆不过是代码。而只要是代码，就能被伪造。"
转场方式: 伴随机械眼收缩硬切 (Hard Cut)
```

> **下游工具直连**：可直接将上述生成的提示词粘贴进 **可灵 AI (Kling)**、**Runway Gen-3 Alpha**、**Luma Dream Machine**、**海螺 AI (MiniMax)** 等工具，或通过脚本批量投递。

---

## Skill 技能系统与编剧方法论

Script-Weaver 将编剧与导演技法从底层 LLM 推理逻辑中彻底解耦，封装为独立、即插即用的 **Skills**。

### 内置经典技能

| 技能标识 (ID) | 技能名称 | 适用阶段 | 核心方法论 / 风格特征 |
|--------------|----------|----------|---------------------|
| `save-the-cat` | 救猫咪节拍表 | `structuring` | 经典 Blake Snyder 商业故事 15 节拍体系 |
| `story-circle` | Dan Harmon 故事圈 | `structuring` | 循环式 8 步人物内心蜕变与冒险结构 |
| `three-act` | 经典三幕式 | `structuring` | 剧作通用建置、对抗、高潮与解决框架 |
| `character-prototype` | 人物原型与弧光 | `character_design` | 荣格心理原型、致命缺点与内心觉醒路径 |
| `wong-kar-wai-style` | 王家卫视听美学 | `art_direction` | 抽帧慢动作、高饱和色调、霓虹残影与都市疏离感 |
| `cinematography-basics` | 专业影视运镜库 | `storyboarding` | 影视工业标准机位轴线、运镜轨迹与构图语法 |

### 自定义技能开发

#### 格式 1：原生 YAML（推荐）

```yaml
# my-film-noir.yaml
id: modern-neo-noir
name: "现代新黑色电影风格"
stage: art_direction
description: "高对比暗调、百叶窗光影与宿命论冷峻色彩"
version: "1.0"

prompt_injection: |
  在进行美术指导与视觉定调时，严格遵循新黑色电影法则：
  1. 画面明暗反差比不低于 8:1，大量使用百叶窗投影或霓虹灯割裂光斑；
  2. 画面主体需与雨水车窗倒影、玻璃折射或建筑剪影深度交织。

constraints:
  - "杜绝明亮的高调顺光照明"
  - "主色调严格锚定在冷青色、琥珀黄与深邃阴影黑之间"
```

#### 格式 2：Claude Code Markdown (`.md`)

系统内置智能适配器，可直接载入 Claude Code 风格的 `.md` 技能定义：

```markdown
---
name: dialogue-punch-up
description: 强化潜台词与地位博弈的高张力对白
---

# 规则指引
1. 角色绝不可直白说出真实动机或内心情感。
2. 每一句台词交锋都必须伴随地位（Status）的升降或筹码的转移。
```

通过 CLI 快速安装并启用：

```bash
python -m script_weaver skills install ./my-film-noir.yaml
python -m script_weaver skills activate modern-neo-noir --stage art_direction
```

---

## Grows With User（三层成长系统）

Script-Weaver 拒绝成为一次性无记忆的生成工具，而是随着创作频次提升越发默契：

1. **第一层：项目决策记忆 (`ProjectMemory`)**
   - 记录用户在生成过程中的每一次确认、修改与拒绝。
   - 自动提炼高频修改行为（如：“用户总是倾向将对白压缩 30% 并强化动作描写”）。
2. **第二层：跨项目创作画像 (`UserProfile`)**
   - 统一存储于 `~/.scriptweaver/user_profile.json`。
   - 跨剧本沉淀个性化审美：包括钟爱的叙事节奏、对白密度、人物性格倾向及镜头色彩温度偏好。
   - 在各 Agent 发起生成时，自适应注入符合创作者调性的提示上下文。
3. **第三层：Skill 自我进化引擎**
   - 实时监测模型输出被人工修正的规律，自动提议创建专属定制 Skill。
   - 结合技能在实际生产中的采纳率，自动优化技能权重与推荐排序。

---

## 系统架构与工程可靠性

Script-Weaver 在底层融入了工业级系统设计的鲁棒性考量：

### 1. SQLite 持久化与 CAS 乐观并发控制
- Web 端所有项目统一落库于 SQLite 数据库（`<data_dir>/main-web/projects.sqlite3`）。
- 每次写操作均实施版本比对检查（`revision` CAS）。当发生并发写或过期版本提交时，直接返回 `409 Conflict`，杜绝部分覆写与数据错乱。
- 每次保存操作均追加一条不可变历史版本（`GET /api/projects/{id}/versions`），支持随时比对回滚。

### 2. 独立于页面的长驻运行与 SSE 流式通知
- 生成任务的生命周期由服务端进程全权托管，完全独立于前端 HTTP 请求。
- 页面刷新、意外关闭标签页或网络瞬断**绝不会**中止正在进行的模型生成。
- 前端重连后通过 SSE 端点（`/runs/{run_id}/events`）首先回放当前状态快照，继而无缝平滑衔接实时进度。
- 支持幂等停止（`POST /runs/{run_id}/stop`）以及通过最新断点安全恢复。

### 3. 原子阶段 Checkpoint 与断点续跑 (`--resume`)
- 流水线各 Agent 阶段（概念、大纲、角色、场景、美术、剧本、分镜）成功后，在单一数据库事务内完成产物落库与阶段检查点记录。
- 遇到模型网络抖动或服务重启中断时，使用 CLI `--resume` 或 Web 续跑 API 可精准识别阶段指纹，跳过耗时且已成功的上游环节，仅针对失败阶段重试。

### 4. 严密的局部修改（Refine）安全契约
- 全局与卡片级修改接口（`POST /api/projects/{id}/refine`）在执行后会核验实体引用完整性（例如 `shot.scene_id` 与 `script.scene_id` 的匹配性）。
- 修改成功后返回由前后快照严格计算得出的真实 Diff 摘要（`changed_artifacts` 与 `before/after`）；未发生实质变化或越界违背约束时，以 `422` 状态码迅速拒绝，绝不向数据库写入无效状态。

---

## 测试与工程验证

### 单元与集成测试

```bash
# 执行 Python 后端 pytest 套件
pytest

# 代码风格与静态检查
ruff check src/ tests/
```

### Web 端 Playwright 端口隔离 E2E 回归

针对 Web 端的异步状态隔离、SSE 重连机制与并发冲突，提供了完备的自动化测试：

```bash
cd web
npm install
npx playwright install chromium    # 首次执行时安装浏览器内核
npm run test:e2e                   # 自动拉起隔离的后端(8310)与前端(3100)服务

# 独立检测端口隔离性
npm run check:e2e-ports
```

### 真实大模型端到端诊断验收

通过官方验证脚本，验证在真实网络与真实模型环境下的全链路执行：

```bash
python scripts/verify_real_model.py \
  --output-dir /tmp/script-weaver-verification \
  --idea "60秒科幻悬疑短片：两名宇航员在维修空间站时，发现舰载时钟正在逆向倒流。"

# 若中途异常中断，支持复用已有阶段断点继续验证
python scripts/verify_real_model.py \
  --output-dir /tmp/script-weaver-verification \
  --resume
```

---

## 目录结构速览

```
script-weaver/
├── src/script_weaver/
│   ├── cli.py                      # Click CLI 交互入口
│   ├── core/
│   │   ├── types.py                # 核心 Pydantic 数据模型定义
│   │   ├── config.py               # 配置与环境变量管理
│   │   ├── pipeline.py             # 流水线编排引擎
│   │   ├── project_store.py        # SQLite 持久化、CAS 锁与 Run 状态机
│   │   ├── refinement.py           # 全局与卡片局部修改校验引擎
│   │   └── resume.py               # 断点指纹比对与续跑控制
│   ├── agents/
│   │   ├── base.py                 # BaseAgent (自主 Tool-use 循环)
│   │   └── impl.py                 # 9 个专业 Agent 的核心实现
│   ├── tools/
│   │   └── definitions.py          # Agent 调用的工具集定义
│   ├── llm/
│   │   ├── client.py               # 统一 LLM 客户端抽象
│   │   └── providers.py            # 各供应商适配层 (Anthropic, OpenAI, DeepSeek 等)
│   ├── skills/
│   │   ├── adapters.py             # YAML / JSON / Claude Code MD 适配器
│   │   ├── registry.py             # 技能注册、发现、激活与 Prompt 组装中心
│   │   └── builtin/                # 6 个内置经典编剧与导演技能
│   ├── memory/
│   │   ├── profile.py              # 用户画像持久化与分析
│   │   └── evolution.py            # 技能自进化逻辑
│   └── exporters/
│       ├── json_exporter.py        # ProjectState JSON 导出器
│       ├── fountain_exporter.py    # 工业标准 Fountain 格式导出器
│       └── video_gen_exporter.py   # VideoGen 多文件与 CSV 分镜导出器
├── web/
│   ├── api/
│   │   └── main.py                 # FastAPI 后端服务 (REST + SSE 流式)
│   ├── src/app/
│   │   └── page.tsx                # Next.js 交互式双栏创作工作台
│   └── tests/e2e/                  # Playwright 端到端自动化回归套件
├── scripts/
│   └── verify_real_model.py        # 真实模型全链路诊断验收脚本
├── pyproject.toml                  # Python 模块元数据与依赖定义
├── README.md                       # 英文主文档
└── README_zh.md                    # 中文主文档
```

---

## 开源协议

本项目采用 [MIT 许可证](LICENSE)。
