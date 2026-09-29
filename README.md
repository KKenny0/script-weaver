# Script-Weaver

> **Agent-Native Screenplay & Storyboard Generation Engine** — An autonomous AI creative pipeline that transforms raw premises into production-grade screenplays and shot-by-shot video generation prompts, growing alongside creators.

<p align="center">
  <a href="README.md"><b>English</b></a> | <a href="README_zh.md"><b>简体中文</b></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12-blue?logo=python" alt="Python Version" />&nbsp;<img src="https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi" alt="FastAPI" />&nbsp;<img src="https://img.shields.io/badge/Next.js-15-black?logo=next.js" alt="Next.js" />&nbsp;<img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" />&nbsp;<img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome" />
</p>

---

## Highlights

- 🎬 **Idea to Production-Ready Assets**: Takes a rough logline or premise and systematically builds beat outlines, character sheets, scene designs, standardized screenplays (Fountain / ScreenJSON), and granular visual storyboards.
- 🤖 **9 Specialized Autonomous Agents**: Features a multi-agent cooperative architecture (Idea Refiner, Structurer, Character Designer, Scene Designer, Art Director, Script Writer, Storyboard Artist, Visual Highlights, and Orchestrator) — each running an autonomous tool-use reasoning loop.
- 🎥 **Downstream AI Video Ready**: Direct prompt generation for modern video models (**Kling, Runway Gen-3, Luma Dream Machine, Hailuo / Minimax, Pika, Sora**). Generates both first-frame image prompts and temporal video motion prompts.
- 🧩 **Extensible Skill Registry**: Plug-and-play creative methodologies (*Save the Cat 15-Beat Sheet*, *Dan Harmon's Story Circle*, *Jungian Character Prototypes*, *Wong Kar-wai Visual Style*, *Cinematography Lexicon*). Seamlessly adapts Native YAML, JSON, and Claude Code `.md` skills.
- 🧠 **Grows With User (3-Tier Evolution)**:
  - **Project Memory**: Captures revisions and extracts editing patterns.
  - **User Profile**: Learns your narrative pacing, tone preferences, and dialogue density across projects.
  - **Skill Self-Evolution**: Automatically proposes new skills and optimizes existing ones based on approval metrics.
- ⚡ **Production-Grade Engineering Rigor**: Built-in SQLite persistence, CAS (Compare-And-Swap) revision control, detached background run execution with SSE streaming (survives tab close/reconnect), and atomic stage checkpoints (`--resume`).
- 💻 **Dual Interfaces**: High-productivity dual-panel **Web Workbench** (Next.js 15 + Tailwind + Lucide) and scriptable **CLI**.

---

## Web Workbench Layout

Script-Weaver provides a responsive dual-panel Web Workbench tailored for narrative and storyboard production:

```
┌───────────────────────────────────────┬─────────────────────────────────────────────────────────┐
│ Left Panel: Chat & Co-Pilot Console   │ Right Panel: Structured Production Artifacts            │
├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
│ • Natural-language story idea input   │ • [Overview]    Project metadata & concept breakdown    │
│ • Live SSE pipeline execution stream  │ • [Outline]     Beat sheets (Save the Cat / Harmon etc.)│
│ • Targeted card refinement & prompts  │ • [Characters]  Character dossiers & visual prompts     │
│ • Semantic diff review (Adopt/Discard)│ • [Scenes]      Atmosphere, color palette, lighting     │
│ • One-click stop & checkpoint resume  │ • [Script]      ScreenJSON / Fountain screenplay        │
│ • Multi-format export (JSON/Ftn/ZIP)  │ • [Storyboard]  Shots, camera moves, image/video prompts│
└───────────────────────────────────────┴─────────────────────────────────────────────────────────┘
```

---

## Pipeline Architecture

```
User Input (Logline, Premise, Outline, or Notes)
       │
       ▼
┌──────────────┐
│ Orchestrator │ ── Routes requests and manages stage transitions
└──────┬───────┘
       │
       ├──────────────┐
       ▼              │
┌──────────────┐      │
│  IdeaRefiner │──[Human Gate]──▶ Structurer ──[Human Gate]──▶ Designers (Parallel)
│(Concept Dev) │                     │                           ├── CharacterDesigner
└──────────────┘                     │                           ├── SceneDesigner
                                     ▼                           └── ArtDirector
                               ┌─────────────┐                          │
                               │ScriptWriter │──[Human Gate]──▶ StoryboardArtist
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
                                                              │ Record Decisons │
                                                              │ Update Profile  │
                                                              │  Evolve Skills  │
                                                              └─────────────────┘
```

---

## Quick Start

### 1. Prerequisites & Installation

- **Python**: `>= 3.10` (tested with 3.11 and 3.12)
- **Node.js**: `>= 18.0` (required for Web UI)

```bash
# Clone the repository
git clone https://github.com/KKenny0/script-weaver.git
cd script-weaver

# Install Python backend dependencies (including dev and web optional dependencies)
pip install -e ".[dev,web]"

# Install Web frontend dependencies
cd web && npm install && cd ..
```

### 2. Multi-Provider LLM Configuration

Copy the example environment file and configure your preferred model provider:

```bash
cp .env.example .env
```

Script-Weaver supports leading cloud providers and local open-source models out of the box:

| Environment Variable | Description | Supported Values |
|----------------------|-------------|------------------|
| `SCRIPTWEAVER_LLM_PROVIDER` | Active LLM provider | `anthropic` / `openai` / `deepseek` / `glm` / `qwen` / `openai_compatible` |
| `SCRIPTWEAVER_LLM_MODEL` | Target model name | e.g. `claude-3-7-sonnet-latest`, `gpt-4o`, `deepseek-chat`, `glm-4-plus`, `qwen-plus` |
| `SCRIPTWEAVER_ANTHROPIC_API_KEY` | Anthropic API Key | `sk-ant-...` |
| `SCRIPTWEAVER_OPENAI_API_KEY` | OpenAI / DeepSeek API Key | `sk-...` |
| `SCRIPTWEAVER_GLM_API_KEY` | Zhipu GLM API Key | API Key from Zhipu Open Platform |
| `SCRIPTWEAVER_QWEN_API_KEY` | Alibaba Qwen API Key | DashScope API Key |
| `SCRIPTWEAVER_OPENAI_BASE_URL` | Custom OpenAI-compatible endpoint | e.g. `http://localhost:11434/v1` for Ollama / vLLM |

---

### 3. Launching the Web Workbench

Run the backend and frontend in separate terminals:

```bash
# Terminal 1: Start the FastAPI backend (runs on port 8000)
python web/api/main.py

# Terminal 2: Start the Next.js frontend (runs on port 3000)
cd web
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) in your browser. The frontend automatically proxies `/api/*` requests to the FastAPI backend at `http://localhost:8000`.

---

### 4. Running via CLI

Generate a complete screenplay, character dossiers, scene designs, and video storyboard in one command:

```bash
# Generate from a creative premise with auto-approved gates
python -m script_weaver generate "A neon-noir cyberpunk thriller where a memory detective discovers his own childhood was manufactured." --auto-approve --output-dir ./output

# Specify title and custom output directory
python -m script_weaver generate "A time-travel drama set in Song dynasty" --title "Threads of Destiny" --output-dir ./dist

# Resume a failed or interrupted generation run from its last checkpoint
python -m script_weaver generate --resume --output-dir ./output
```

#### Skill & Profile CLI Utilities

```bash
# List all available skills
python -m script_weaver skills list

# Filter skills by pipeline stage
python -m script_weaver skills list --stage structuring

# Install a custom skill
python -m script_weaver skills install ./my-skill.yaml

# Activate a skill for a specific stage
python -m script_weaver skills activate save-the-cat --stage structuring

# Inspect current learned User Profile (creative preferences)
python -m script_weaver profile show
```

---

## Downstream AI Video Generation (VideoGen)

Script-Weaver's core differentiator is the **VideoGen Exporter**, which bridges conceptual writing and downstream visual rendering.

### Output Structure

When exporting with `video_gen` format (CLI `--format video_gen` or Web export), the following bundle is generated:

```
output/video_gen/
├── video_gen_shots.json      # Structured data for all shots
├── video_gen_shots.csv       # Tabular summary (Excel / spreadsheet compatible)
└── shots/
    ├── shot_001.txt          # Shot 1: Image & Video prompts, metadata
    ├── shot_002.txt          # Shot 2: Image & Video prompts, metadata
    └── ...
```

### Concrete Shot Breakdown Sample

Each shot file provides synchronized prompts ready for video models:

```yaml
Shot ID: shot_scene1_01
Scene: INT. RAIN-SLICKED LAB - NIGHT
Duration: 4.5s
Camera: Low-angle tracking shot, 35mm anamorphic lens, shallow depth of field

Image Prompt (First-frame key visual):
  Cinematic 35mm film still, low angle, medium close-up of Kaelen in a dim, rain-streaked cybernetics lab. Harsh neon-amber backlight catching cigarette smoke, volumetric shadows, intricate cybernetic eye with subtle blue aperture ring, photorealistic textures, muted teal and tungsten palette, 8k resolution.

Video Prompt (Motion & Temporal dynamics):
  Camera slowly tracks forward from low angle as Kaelen exhales smoke, his mechanical iris clicking and recalibrating. Neon rain reflections ripple across the window pane behind him. Atmospheric dust motes floating through volumetric amber beam. Smooth organic motion, cinematic pacing, 24fps.

Audio & Dialogue:
  KAELEN (V.O.): "Memories are just code. And code can be forged."
Transition Out: Hard cut on eye reflex
```

> **Compatible Tools**: Directly copy and paste prompts into **Kling AI**, **Runway Gen-3 Alpha**, **Luma Dream Machine**, **Hailuo AI (Minimax)**, or batch-upload via API.

---

## Skill Registry & Methodologies

Script-Weaver decouples storytelling methodology from core LLM logic. Techniques are encapsulated as reusable, composable **Skills**.

### Built-in Skills

| Skill ID | Name | Target Stage | Methodology / Style |
|----------|------|--------------|---------------------|
| `save-the-cat` | Save the Cat! Beat Sheet | `structuring` | Blake Snyder’s 15-beat narrative structure |
| `story-circle` | Dan Harmon Story Circle | `structuring` | 8-step cyclical character journey |
| `three-act` | Classical Three-Act Structure | `structuring` | Universal dramatic setup, confrontation, resolution |
| `character-prototype` | Archetype & Character Arc | `character_design` | Jungian archetypes, internal flaws, transformation arcs |
| `wong-kar-wai-style` | Wong Kar-wai Visual Aesthetics | `art_direction` | Step-printed slow motion, saturated neon, urban melancholy |
| `cinematography-basics` | Cinematography Lexicon | `storyboarding` | Standard framing, camera axes, and cinematic grammar |

### Authoring Custom Skills

#### Format 1: Native YAML (Recommended)

```yaml
# my-film-noir.yaml
id: modern-neo-noir
name: "Modern Neo-Noir Style"
stage: art_direction
description: "High-contrast shadows, practical lighting, and cynicism"
version: "1.0"

prompt_injection: |
  Apply neo-noir cinematic conventions:
  1. Contrast ratio must exceed 8:1 with prominent venetian blind or neon slash shadows.
  2. Frame subjects through architectural silhouettes, glass reflections, or rainy windshields.

constraints:
  - "Avoid bright high-key lighting"
  - "Color palette must prioritize deep shadows, amber, and cold cyan"
```

#### Format 2: Claude Code Markdown (`.md`)

Script-Weaver natively parses `.md` skill files:

```markdown
---
name: dialogue-punch-up
description: Sharp, Subtext-Driven Dialogue
---

# Guidelines
1. Characters must never state their true intention directly.
2. Every dialogue turn must advance status or uncover leverage.
```

Install and activate with:

```bash
python -m script_weaver skills install ./my-film-noir.yaml
python -m script_weaver skills activate modern-neo-noir --stage art_direction
```

---

## Grows With User (3-Tier Evolution)

Script-Weaver is designed as an evolving creative companion rather than a stateless prompt:

1. **Layer 1: Project Memory (`ProjectMemory`)**
   - Logs accepted, revised, and rejected suggestions throughout a project.
   - Extracts reusable refinement patterns (e.g., "User consistently trims dialogue blocks by 30%").
2. **Layer 2: User Profile (`UserProfile`)**
   - Stored globally in `~/.scriptweaver/user_profile.json`.
   - Accumulates cross-project tastes: favored narrative pacing, character archetypes, visual styles, and color temperatures.
   - Dynamically tailors agent system prompts to your creative voice.
3. **Layer 3: Skill Self-Evolution**
   - Automatically detects recurring prompt modifications and drafts new custom skills.
   - Ranks and optimizes active skills based on real user acceptance rates.

---

## Architecture & Engineering Rigor

Script-Weaver is built with production reliability at its core:

### 1. SQLite Persistence & CAS Revisions
- All Web projects are stored in an ACID SQLite database (`<data_dir>/main-web/projects.sqlite3`).
- Updates use Compare-And-Swap (CAS) revision checks (`revision` counter). Concurrent or stale writes return `409 Conflict`, strictly preventing partial writes or state corruption.
- Every save automatically creates an immutable version snapshot (`GET /api/projects/{id}/versions`).

### 2. Detached Background Execution & SSE Streaming
- Generation runs are owned by the server process, not tied to the HTTP socket.
- Closing the browser tab, refreshing, or network disconnections will **never** cancel an active generation.
- The Web client uses SSE (`/runs/{run_id}/events`) with snapshot replay upon reconnection.
- Runs can be idempotently stopped via `POST /runs/{run_id}/stop` or resumed from the latest checkpoint.

### 3. Atomic Checkpoints & Fault-Tolerant Resumption
- Successful agent stages commit atomically within a single transaction.
- If a run fails or is interrupted (e.g. process termination), `--resume` (CLI) or `/resume` (Web API) verifies fingerprint compatibility and resumes execution from the exact failed stage without repeating costly upstream LLM calls.

### 4. Granular Refinement Contracts
- **API compatibility change:** `POST /api/projects/{id}/refine` now requires `{message, expected_revision, request_key}` and returns `{run, created}` immediately. A successful submission or `global_candidate` run produces a candidate, never an automatic project save. Observe it through the run endpoints or `GET /api/projects/{id}/candidates`; explicitly `POST /api/projects/{id}/candidates/{candidate_id}/accept` or `/reject`.
- The chat keeps its natural-language entry point. The persistent **Global modification candidates** control restores runs and candidates after refresh or project switching. Compare the complete field differences, replacement scope, original instruction and conservative downstream impacts before adopting. Until adoption, current content, history and all three exports remain unchanged.
- Stage 1 supports exactly one existing whole characters, scenes, script or storyboard artifact. Outline and art-style routes are explicitly refused before execution. IDs, ownership, references, collection order and manual-edit read-only fields remain protected; additions, removals and reordering are unsupported. CLI routing retains its existing six-agent scope.
- Adoption shares card candidates' revision check, atomic content/history/review transaction, stale handling and idempotence. Any intervening save expires ready candidates; repeated acceptance returns the original adopted version. Characters/scenes flag existing script, storyboard and highlights; script flags existing storyboard/highlights; storyboard flags existing highlights. These are review warnings, not automatic regeneration.
- Global and card candidates share the existing SQLite candidate table without a schema change. Existing card candidates remain readable. Candidate runs are distinct from full generation: latest-generation and resume never treat them as generation checkpoints. Interrupted, stopped and failed candidates remain discoverable and can be resubmitted with a new request key.

---

## Testing & Verification

### Unit & Integration Tests

```bash
# Run backend pytest suite
pytest

# Code style and linting
ruff check src/ tests/
```

### Web E2E Tests (Playwright with Port Isolation)

Script-Weaver features comprehensive E2E tests for Web state management, SSE reconnects, and CAS conflict handling using port isolation (ensuring no accidental calls to live models):

```bash
cd web
npm install
npx playwright install chromium    # Run once
npm run test:e2e                   # Runs isolated backend (8310) and frontend (3100)

# Verify port isolation
npm run check:e2e-ports
```

### Real-Model Acceptance Testing

Verify the end-to-end pipeline against live LLM providers using the diagnostic verification script:

```bash
python scripts/verify_real_model.py \
  --output-dir /tmp/script-weaver-verification \
  --idea "A 60-second sci-fi suspense short where two astronauts realize their ship's clock runs backwards."

# Resume from previous checkpoint if interrupted
python scripts/verify_real_model.py \
  --output-dir /tmp/script-weaver-verification \
  --resume
```

---

## Project Structure

```
script-weaver/
├── src/script_weaver/
│   ├── cli.py                      # Click CLI entry point
│   ├── core/
│   │   ├── types.py                # Core Pydantic domain models
│   │   ├── config.py               # Settings and environment management
│   │   ├── pipeline.py             # Pipeline orchestration engine
│   │   ├── project_store.py        # SQLite persistence, CAS locking, run state machine
│   │   ├── refinement.py           # Global & card refinement validation
│   │   └── resume.py               # Checkpoint fingerprinting and resume logic
│   ├── agents/
│   │   ├── base.py                 # BaseAgent (autonomous tool-use loop)
│   │   └── impl.py                 # Implementations of all 9 specialized agents
│   ├── tools/
│   │   └── definitions.py          # Agent tool definitions and dispatchers
│   ├── llm/
│   │   ├── client.py               # Unified LLM client interface
│   │   └── providers.py            # Provider adapters (Anthropic, OpenAI, DeepSeek, etc.)
│   ├── skills/
│   │   ├── adapters.py             # Adapters for YAML, JSON, Claude Code Markdown
│   │   ├── registry.py             # Discovery, loading, activation, and prompt merging
│   │   └── builtin/                # 6 Built-in production skills
│   ├── memory/
│   │   ├── profile.py              # UserProfile persistence & learning
│   │   └── evolution.py            # Skill self-evolution engine
│   └── exporters/
│       ├── json_exporter.py        # ProjectState JSON export
│       ├── fountain_exporter.py    # Industry-standard Fountain export
│       └── video_gen_exporter.py   # VideoGen multi-file & CSV export
├── web/
│   ├── api/
│   │   └── main.py                 # FastAPI backend (REST + SSE streaming)
│   ├── src/app/
│   │   └── page.tsx                # Next.js interactive dual-pane workbench
│   └── tests/e2e/                  # Playwright E2E isolation test suites
├── scripts/
│   └── verify_real_model.py        # Real-model pipeline diagnostic harness
├── pyproject.toml                  # Python package metadata and dependencies
└── README.md                       # Project documentation
```

---

## License

This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for details.
