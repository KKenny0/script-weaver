# Script Weaver · Creator Journey Workbench V8

> 新一轮审阅入口：[以场次为核心的创作者工作台](scene-workbench.html)。行为说明与验证见 [原型审阅记录](scene-workbench-README.md)。下文保留既有 V8 原型说明，新入口未替换旧版本。


## Current review target

- Entry: `index.html`
- Local URL: `http://127.0.0.1:4312/designs/script-weaver-drama-workbench/index.html`
- Scope: a static, clickable 8-episode creator workbench prototype. It does not connect to the daemon, CLI, Codex runtime, image provider, or MiniMax H3.
- Visual direction: warm editorial screenplay desk, low decoration, dense but calm working surfaces. No circular workflow and no permanent visual control center.

## Product contract represented

- A creator can start from an idea, a legally usable novel, a single screenplay, or a multi-episode screenplay.
- Fresh idea/novel projects begin with only a source snapshot, then run develop Task → ChangeSet apply → whole development-document approval before unlocking the season. Single-screenplay direct and revise paths converge on whole screenplay approval; confirmed multi-episode splitting remains draft, not accepted truth.
- Source input is treated as an immutable snapshot. Novel intake includes rights and sensitive-material confirmation, duplicate detection, parse failure, and cancellation without project pollution. Multi-episode intake requires confirming episode boundaries.
- The development document includes creative promise, story engine, character conflict, and episode map. It is approved as one version; the episode map has no independent approval.
- Screenplay work supports scene-level reading/editing, local draft state, save failure, unsaved episode-switch protection, revision conflict, and recovery-as-new-candidate language.
- Codex collaboration is CLI-first and uses two gates: Task/AgentRun → submitted ChangeSet → creator applies operations → candidate enters the project → creator accepts or rejects the creative result.
- Text and media candidates do not become formal project facts automatically. Candidate B → edited candidate B1 → formal REF is shown as explicit lineage.
- Global character/location/prop identities are reused across episodes. Their look/view/state variants carry episode ranges plus incoming/outgoing continuity.
- Review, freshness, and execution are three independent state axes. Task failure and input drift do not mutate accepted facts.
- Codex prepares text, images, frozen keyframes, and video prompts. MiniMax H3 is shown only as a planned local video execution endpoint; the prototype never implies a real connection.
- The end product is a versioned production package, not a completed short drama. Final edit, voice, music, subtitles, mix, distribution, and full rendered video remain outside V8.

## Deep validation fixtures

- **EP01:** screenplay → visual fact → three image candidates → B → B1 → formal REF → storyboard → frozen keyframe → video prompt → planned H3 candidate → production package.
- **EP03:** submitted screenplay candidate → reject with feedback → new scoped Task/AgentRun → new candidate → accept.
- **EP04:** accepted upstream change makes screenplay/REF/shot inputs stale; creator chooses keep, revise, or later. Nothing is silently regenerated.
- **EP02 / EP05–EP08:** navigation, empty states, continuity, partial completion, and Task Group partial failure/retry.

## Main review paths

1. Project library → **继续创作** runs the EP01 visual loop.
2. Project library → **新建作品** exposes all four intake types.
3. Project library → EP03 **打开** tests ChangeSet and rejection/revision.
4. Workbench → EP04 tests stale impact handling.
5. Workbench → **批量任务** tests independent task outcomes.
6. Workbench → **后台任务** displays the three-axis status ledger.
7. EP01 → **生产包** → **交付本轮生产包** exposes next episode / next season / new project / project library.
8. **重置演示数据** returns to the deterministic project-library fixture.

## Files

- `index.html`: V8 entry.
- `prototype-data.jsx`: eight-episode, asset, status, and batch fixtures.
- `app.jsx`: prototype state, rendering, decisions, and failure paths.
- `styles.css`: warm editor visual system, light/dark, 1440×900 and 1024×700 layouts.
- `assets/v7-journey/nail-file-candidates.png`: reused A/B/C nail-file candidate sheet; the prototype crops each panel without stretching.
- `assets/v8-journey/nail-file-revision-b1.png`: imagegen revision derived from candidate B, preserving the nail file while reducing scratches and softening its worn edges.
- `assets/v7-journey/empty-cinema-keyframe.png`: reused empty-cinema keyframe matching SHOT-004 and PROP-001.
- `v5`–`v7` HTML/CSS/JS files remain untouched as historical comparisons.

## Verification

Serve the repository root and open the URL above. Static syntax check:

```sh
node --check - < designs/script-weaver-drama-workbench/app.jsx
node --check - < designs/script-weaver-drama-workbench/prototype-data.jsx
```

Browser acceptance is separate from static checks. Validate both 1440×900 and 1024×700, inspect the console, and run the EP01/EP03/EP04 paths before review.
