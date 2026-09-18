(function () {
  "use strict";

  const app = document.getElementById("app");
  const projections = [
    ["screenplay", "剧本"],
    ["facts", "视觉事实"],
    ["storyboard", "分镜"],
    ["prompts", "提示词"],
    ["results", "成果"]
  ];
  const episodes = [
    { id: "EP01", title: "没有寄出的声音", format: "dynamic", scenes: ["SC01 修复室", "SC02 档案间"] },
    { id: "EP02", title: "被剪断的雨声", format: "static", scenes: ["SC01 雨棚", "SC02 旧车站"] },
    { id: "EP03", title: "第一次篡改", format: "dynamic", scenes: ["SC01 修复室", "SC02 档案间", "SC03 地下通道"] },
    { id: "EP04", title: "销毁请求", format: "dynamic", scenes: ["SC01 档案馆门厅", "SC02 销毁室"] },
    { id: "EP05", title: "无辜的人", format: "dynamic", scenes: ["SC01 医院走廊", "SC02 录音棚"] },
    { id: "EP06", title: "她的录音", format: "static", scenes: ["SC01 老屋", "SC02 修复室"] }
  ];
  const taskTimers = [];

  let state = {
    theme: "light",
    projectName: "没有寄出的声音",
    episode: "EP03",
    scene: "SC02",
    expandedEpisode: "EP03",
    projection: "screenplay",
    spineOpen: false,
    decision: "text",
    overlay: null,
    notice: "",
    intakeType: "idea",
    screenplayVersion: 2,
    screenplayAccepted: 2,
    textTask: null,
    textChoice: "",
    textSubmitted: false,
    downstreamStale: false,
    visualTask: null,
    visualStep: "fact",
    selectedVisual: "",
    candidateStale: false,
    officialRef: "",
    refReplaced: false,
    refHistory: [],
    keyframeTask: null,
    keyframeReady: false,
    productionPrepared: false,
    productionCreated: false,
    codexAvailable: true,
    setup: null,
    setupTask: null,
    intakeDraft: { name: "没有寄出的声音", source: "一名声音修复师在旧母带里听见尚未发生的犯罪。" }
  };

  let returnFocus = "";
  let noticeTimer = 0;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const currentEpisode = () => episodes.find((episode) => episode.id === state.episode);
  const currentSceneLabel = () => currentEpisode().scenes.find((scene) => scene.startsWith(state.scene)) || currentEpisode().scenes[0];
  const taskStatusLabel = (task) => ({ queued: "queued · 等待 Codex 领取", running: "running · Codex 正在处理", submitted: "submitted · 候选已交回", failed: "failed · 当前事实未改变" }[task?.status] || "尚未创建");

  function notify(message) {
    state.notice = message;
    clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(() => {
      state.notice = "";
      document.querySelector(".notice")?.remove();
    }, 3600);
  }

  function taskLifecycle(target, task) {
    state[target] = task;
    render();
    const runningTimer = window.setTimeout(() => {
      if (state[target]?.id !== task.id || state[target].status !== "queued") return;
      state[target].status = "running";
      render();
    }, 450);
    const submittedTimer = window.setTimeout(() => {
      if (state[target]?.id !== task.id || !["queued", "running"].includes(state[target].status)) return;
      state[target].status = "submitted";
      if (target === "visualTask") {
        state.visualStep = task.kind === "edit" ? "derived" : "gallery";
        state.candidateStale = task.sourceScreenplayVersion !== state.screenplayAccepted;
      }
      if (target === "keyframeTask") state.keyframeReady = true;
      render();
      notify(target === "keyframeTask" ? "冻结关键帧候选已返回，尚未进入外部视频生成" : "Codex 已提交候选；当前正式事实没有改变");
    }, 1050);
    taskTimers.push(runningTimer, submittedTimer);
  }

  function statusSummary() {
    if (state.setup) return { label: setupStatus(), tone: state.setupTask?.status === "submitted" ? "warn" : "good" };
    if (!state.codexAvailable) return { label: "Codex 不可用 · 既有内容仍可编辑", tone: "warn" };
    if (state.screenplayAccepted === 3 && state.downstreamStale) return { label: "剧本 v3 accepted / current · 下游需同步", tone: "warn" };
    if (state.refReplaced) return { label: "正式 REF 已替换 · 下游需同步", tone: "warn" };
    if (state.officialRef) return { label: `${state.officialRef} · 正式`, tone: "good" };
    if (state.visualTask?.status === "submitted") return { label: "图片候选待决定", tone: "warn" };
    if (state.textSubmitted && state.screenplayAccepted < state.screenplayVersion) return { label: `剧本 v${state.screenplayVersion} 待整体确认`, tone: "warn" };
    return { label: "继续上次创作决定", tone: "warn" };
  }

  function header() {
    const summary = statusSummary();
    const locus = state.setup ? "项目设置 · 输入快照" : `${state.episode}-${state.scene} · ${currentSceneLabel().replace(/^SC\d+\s*/, "")}`;
    return `
      <header class="topbar" data-screen-label="项目与当前创作焦点">
        <div class="brand"><span class="brand-mark" aria-hidden="true">SW</span><span class="brand-copy"><strong>Script Weaver</strong><small>创作焦点工作台</small></span></div>
        <div class="locus-bar"><strong>${escapeHtml(state.projectName)}</strong><span class="locus-divider">/</span><span>${escapeHtml(locus)}</span><span class="status-dot" data-tone="${summary.tone}">${escapeHtml(summary.label)}</span></div>
        <div class="top-actions"><button class="menu-button" data-action="toggle-spine" aria-expanded="${state.spineOpen}" aria-label="打开作品脊柱">作品</button><button class="quiet-button" data-action="open-intake">新建项目</button><button class="theme-button" data-action="theme" aria-label="切换明暗主题">${state.theme === "light" ? "夜" : "昼"}</button></div>
      </header>`;
  }

  function storySpine() {
    return `
      ${state.spineOpen ? `<button class="mobile-spine-backdrop" data-action="close-spine" aria-label="关闭作品脊柱"></button>` : ""}
      <aside class="story-spine ${state.spineOpen ? "open" : ""}" data-screen-label="作品脊柱">
        <div class="spine-head"><span>STORY LOCUS</span><strong>作品脊柱</strong></div>
        <nav class="spine-nav" aria-label="全剧、分集与场景">
          <button class="spine-link" data-action="series"><span>全剧</span><small>6 集</small></button>
          <div class="spine-rule"></div>
          ${episodes.map((episode) => episodeNav(episode)).join("")}
        </nav>
      </aside>`;
  }

  function episodeNav(episode) {
    const expanded = state.expandedEpisode === episode.id;
    return `
      <section>
        <button class="episode-button ${state.episode === episode.id ? "current" : ""}" data-action="toggle-episode" data-episode="${episode.id}" aria-expanded="${expanded}"><span>${episode.id} · ${escapeHtml(episode.title)}</span><small>${episode.format === "static" ? "静态" : "动态"}</small></button>
        ${expanded ? `<div class="scene-group">${episode.scenes.map((scene) => {
          const sceneId = scene.slice(0, 4);
          const active = state.episode === episode.id && state.scene === sceneId;
          return `<button class="scene-link ${active ? "active" : ""}" data-action="select-scene" data-episode="${episode.id}" data-scene="${sceneId}"><span>${escapeHtml(scene)}</span><small>${active ? "当前" : ""}</small></button>${active && state.projection === "storyboard" ? shotNav() : ""}`;
        }).join("")}</div>` : ""}
      </section>`;
  }

  function shotNav() {
    return `<div class="shot-group"><button class="shot-link" data-action="focus-shot" data-shot="SHOT-001"><span>SHOT-001</span><small>建立</small></button><button class="shot-link active" data-action="focus-shot" data-shot="SHOT-004"><span>SHOT-004</span><small>关键帧</small></button><button class="shot-link" data-action="focus-shot" data-shot="SHOT-006"><span>SHOT-006</span><small>交接</small></button></div>`;
  }

  function stageHeader() {
    if (state.setup) return `<header class="stage-head"><div class="stage-identity"><div><span class="eyebrow">NEW PROJECT · ${state.setup.type === "script" ? "现成剧本" : state.setup.type === "original" ? "合法持有的原著" : "故事想法"}</span><h1>建立创作起点</h1></div><span class="version-note">${escapeHtml(setupStatus())}</span></div></header>`;
    const accepted = state.screenplayAccepted;
    return `
      <header class="stage-head">
        <div class="stage-identity"><div><span class="eyebrow">${state.episode}-${state.scene} · ${escapeHtml(currentSceneLabel().replace(/^SC\d+\s*/, ""))}</span><h1>当前创作焦点</h1></div><span class="version-note">剧本 v${accepted} 正式${state.screenplayVersion > accepted ? ` · v${state.screenplayVersion} 待确认` : ""}</span></div>
        <nav class="projection-tabs" aria-label="同一故事位置的投影">${projections.map(([id, label]) => `<button class="projection-tab ${state.projection === id ? "active" : ""}" data-action="projection" data-projection="${id}" aria-pressed="${state.projection === id}">${label}</button>`).join("")}</nav>
      </header>`;
  }

  function stage() {
    return `<main class="creative-stage" id="main-content" tabindex="-1" data-screen-label="当前创作舞台">${stageHeader()}<div class="stage-scroll"><article class="stage-canvas ${stageClass()}">${stageBody()}</article></div></main>`;
  }

  function stageClass() {
    if (state.decision === "prop" && ["gallery", "edit", "derived", "formal"].includes(state.visualStep)) return "candidate-stage";
    if (state.decision === "text" && state.textTask?.status === "submitted" && !state.textSubmitted) return "candidate-stage";
    return "";
  }

  function stageBody() {
    if (state.setup) return setupStage();
    if (state.episode !== "EP03") return genericEpisodeStage();
    if (state.decision === "prop" && ["gallery", "edit"].includes(state.visualStep)) return candidateGallery();
    if (state.decision === "prop" && ["derived", "formal"].includes(state.visualStep)) return derivedComparison();
    if (state.decision === "text" && state.textTask?.status === "submitted" && !state.textSubmitted) return textComparison();
    return {
      screenplay: screenplayStage,
      facts: factsStage,
      storyboard: storyboardStage,
      prompts: promptsStage,
      results: resultsStage
    }[state.projection]();
  }

  function screenplayStage() {
    if (state.textSubmitted) return screenplayV3();
    return `
      <span class="eyebrow">SCREENPLAY · ACCEPTED v${state.screenplayAccepted}</span>
      <h2>第一次篡改</h2>
      <section class="script">
        <h3 class="scene-heading">EP03-SC02　内景 · 档案间 · 深夜</h3>
        <p>顶灯在母带架上投下一条窄白光。许真戴着监听耳机，倒带声突然停止。</p>
        <p>她从最里层抽出一盒<button class="inline-fact" data-action="open-prop">无日期母带</button>。盒盖闭合，封口处有一道新鲜划痕。</p>
        <p class="character-cue">录音中的许真</p><p class="dialogue">别把它交出去。明天的雨会替你回答。</p>
        <div class="selected-beat"><p>许真摘下耳机，看向左后方的门。脚步声正在靠近。</p><p>她把母带放进右侧外套内袋，盒盖仍然闭合。</p></div>
        <p>门把手压下一半。许真按住内袋，没有退路。</p>
      </section>
      <div class="script-note"><strong>当前决定</strong><div><p>加强越界的代价，但不能改变 PROP-001 已进入右侧内袋、盒盖闭合的事实。右侧面板可继续这次文本决定，也可直接从母带引用发起图片创作。</p><button class="secondary-action" data-action="open-text-decision">调整这一处</button></div></div>`;
  }

  function screenplayV3() {
    return `
      <span class="eyebrow">SCREENPLAY · ${state.screenplayAccepted === 3 ? "ACCEPTED / CURRENT v3" : "SUBMITTED v3"}</span>
      <h2>第一次篡改</h2>
      <section class="script">
        <h3 class="scene-heading">EP03-SC02　内景 · 档案间 · 深夜</h3>
        <p>顶灯在母带架上投下一条窄白光。许真从最里层抽出<button class="inline-fact" data-action="open-prop">无日期母带</button>，盒盖闭合。</p>
        <p class="character-cue">录音中的许真</p><p class="dialogue">别把它交出去。明天的雨会替你回答。</p>
        <div class="selected-beat"><p>脚步声逼近。许真删掉修复台上的原始校验记录，屏幕只剩一行无法撤回的确认。</p><p>她把母带塞进右侧外套内袋，盒盖仍然闭合。门把手压下时，她的工牌从桌沿滑落在门外。</p></div>
        <p>她按住内袋，知道明早第一个被调查的人会是自己。</p>
      </section>
      <div class="script-note"><strong>${state.screenplayAccepted === 3 ? "当前正式版本" : "待整体确认"}</strong><div><p>${state.screenplayAccepted === 3 ? "v3 已整体接受；视觉事实、分镜和后续提示词已标记需要同步。" : "局部候选已经进入完整剧本 v3，但正式版本仍是 v2。接受前不会覆盖 v2。"}</p><button class="secondary-action" data-action="open-text-decision">${state.screenplayAccepted === 3 ? "查看这次决定" : "继续整体确认"}</button></div></div>`;
  }

  function factsStage() {
    return `
      <span class="eyebrow">VISUAL FACTS · SAME LOCUS</span><h2>当前场景的视觉事实</h2><p class="lead">人物、造型、地点和道具共享同一套候选交互。点击任何事实都在同一个创作决定面板中继续。</p>
      ${state.downstreamStale ? `<div class="boundary-callout"><strong>剧本 v3 已成为正式上游</strong><p>这些视觉事实仍来源于剧本 v2，已标记 stale；旧事实保留，等待创作者主动同步。</p></div>` : ""}
      <div class="fact-ledger">
        <section class="fact-row"><span class="row-id">CHAR-001<br>LOOK-001-A</span><div><b><button class="inline-fact" data-action="open-generic-fact">许真 · 深夜造型</button></b><p>短发，旧深灰风衣，右侧内袋可完整容纳母带盒；疲惫但克制。</p></div><span class="row-state ${state.downstreamStale ? "stale" : ""}">${state.downstreamStale ? "来源 v2 · stale" : "事实当前"}</span></section>
        <section class="fact-row"><span class="row-id">LOC-002<br>VIEW-002-A</span><div><b><button class="inline-fact" data-action="open-generic-fact">档案间 · 夜态</button></b><p>窄纵深，金属母带架，单点顶灯，门位于画面左后方。</p></div><span class="row-state ${state.downstreamStale ? "stale" : ""}">${state.downstreamStale ? "来源 v2 · stale" : "事实当前"}</span></section>
        <section class="fact-row"><span class="row-id">PROP-001<br>PSTATE-001-B</span><div><b><button class="inline-fact" data-action="open-prop">无日期母带</button></b><p>盒盖闭合；持有人许真；位于右侧外套内袋。正式 REF：${state.officialRef || "尚未登记"}${state.refReplaced ? "（来自已审阅候选 C2；v1 保留历史）" : ""}。</p></div><span class="row-state ${state.refReplaced ? "stale" : ""}">${state.officialRef ? "正式 REF" : "等待视觉"}</span></section>
      </div>
      ${state.officialRef ? `<h3>引用关系</h3><div class="fact-ledger"><section class="fact-row"><span class="row-id">SHOT-004</span><div><b>母带藏入右侧内袋</b><p>引用 ${state.officialRef}。</p></div><span class="row-state ${state.refReplaced ? "stale" : ""}">${state.refReplaced ? "需同步" : "引用当前"}</span></section><section class="fact-row"><span class="row-id">KF-004</span><div><b>冻结关键帧</b><p>${state.keyframeReady ? `引用 ${state.officialRef}` : "等待 Codex 生成"}。</p></div><span class="row-state ${state.refReplaced ? "stale" : ""}">${state.keyframeReady ? state.refReplaced ? "需同步" : "当前" : "未生成"}</span></section></div>` : ""}`;
  }

  function storyboardStage() {
    return `
      <span class="eyebrow">STORYBOARD · EP03-SC02</span><h2>分镜与冻结关键帧</h2><p class="lead">镜头只在分镜投影下展开。SHOT-004 引用正式 REF，而不是未接受的图片候选。</p>${state.downstreamStale ? `<div class="boundary-callout"><strong>分镜来源已过期</strong><p>剧本 v3 已接受；当前分镜仍引用剧本 v2，SHOT-001 / 004 / 006 均需主动同步。</p></div>` : ""}
      <div class="shot-ledger">
        <section class="shot-row"><span class="row-id">SHOT-001</span><div><b>建立档案间窄纵深</b><p>许真位于右前，门留在左后方，固定 4 秒。</p></div><span class="row-state">当前</span></section>
        <section class="shot-row"><span class="row-id">SHOT-004<br>KF-004</span><div><b>母带进入右侧内袋</b><p>起点 PSTATE-001-A，终点 PSTATE-001-B；${state.officialRef ? `道具引用 ${state.officialRef}` : "等待正式 PROP REF"}。</p>${state.keyframeReady ? `<img class="keyframe" src="assets/visual-loop/shot-kf-004.png" alt="许真在昏暗档案间把盒盖闭合的无日期母带放入右侧外套内袋的冻结关键帧">` : ""}</div><span class="row-state ${state.refReplaced ? "stale" : ""}">${state.refReplaced ? "REF 已换 · 需同步" : state.keyframeReady ? "候选待审" : "未生成"}</span></section>
        <section class="shot-row"><span class="row-id">SHOT-006</span><div><b>场末状态交接</b><p>许真按住内袋，视线仍指向左后方声源。</p></div><span class="row-state">当前</span></section>
      </div>
      <div class="button-row">${state.officialRef && !state.keyframeReady ? `<button class="primary-action" data-action="generate-keyframe">让 Codex 生成冻结关键帧</button>` : ""}${state.keyframeReady ? `<button class="secondary-action" data-action="projection" data-projection="prompts">继续编写视频提示词</button>` : ""}</div>`;
  }

  function promptsStage() {
    if (currentEpisode().format === "static") return `<div class="empty-stage"><span class="eyebrow">${state.episode} · STATIC DRAMA</span><strong>本集跳过视频提示词</strong><p>静态漫剧从冻结关键帧直接进入配音与画面编排。跳过不是缺失，也不会删除历史。</p></div>`;
    const missing = !state.officialRef ? "缺少正式 PROP REF" : !state.keyframeReady ? "缺少 KF-004 冻结关键帧" : "";
    return `
      <span class="eyebrow">VIDEO PROMPT · MOTION-004</span><h2>把镜头准备给外部视频服务</h2><p class="lead">Codex 负责参考图、冻结关键帧、动作、表演、运镜与起止状态；视频成片由外部视频服务生成。</p>
      ${state.downstreamStale ? `<div class="boundary-callout"><strong>提示词来源已过期</strong><p>剧本 v3 已接受；MOTION-004 仍来源于旧分镜，必须同步后才能生产。</p></div>` : ""}
      <div class="prompt-ledger"><section class="prompt-row"><span class="row-id">MOTION-004</span><div><b>6 秒 · 竖屏 · 缓慢推近</b><div class="prompt-box">起点：许真右手持盒盖闭合的 PROP-001，站在母带架前。她听见左后方脚步，视线先到门，再把母带放入右侧外套内袋。终点：PSTATE-001-B，盒盖仍闭合。表演克制，不越轴。</div></div><span class="row-state ${state.refReplaced || missing || state.downstreamStale ? "stale" : ""}">${state.refReplaced ? "REF 已换 · 需同步" : state.downstreamStale ? "来源 v2 · stale" : missing || "Codex 已准备"}</span></section></div>
      <div class="boundary-callout"><strong>能力边界</strong><p>这里没有生成视频。确认后才会把冻结关键帧和 MOTION-004 交给外部服务。</p></div>
      <div class="button-row">${missing ? `<button class="primary-action" data-action="projection" data-projection="storyboard">返回分镜 · ${missing}</button>` : `<button class="primary-action" data-action="open-production" ${state.refReplaced || state.downstreamStale ? "disabled" : ""}>准备外部视频生成</button>`}</div>`;
  }

  function resultsStage() {
    return `
      <span class="eyebrow">PRODUCTION RESULTS · EP03</span><h2>制作成果</h2><p class="lead">真实 REF、冻结关键帧和外部视频任务在这里汇合；准备与确认是两个动作。</p>
      <div class="result-ledger">
        <section class="result-row"><span class="row-id">${state.officialRef || "REF-PROP-001"}</span><div><b>无日期母带 · 正式参考</b><p>${state.refReplaced ? "v2 来自已审阅候选 C2；v1（来自 B1）保留为历史，Task、AgentRun 与提示词版本均可追溯。" : state.officialRef ? "v1 由候选 B1 接受登记，保留 Task、AgentRun、来源事实和提示词版本。" : "尚未接受任何图片候选。"}</p></div><span class="row-state">${state.officialRef ? "正式 REF" : "未生成"}</span></section>
        <section class="result-row"><span class="row-id">KF-004</span><div><b>冻结关键帧</b><p>${state.keyframeReady ? "Codex 图片候选已生成，可作为外部视频输入。" : "等待分镜投影生成。"}</p></div><span class="row-state ${state.refReplaced ? "stale" : ""}">${state.keyframeReady ? state.refReplaced ? "需同步" : "候选" : "未生成"}</span></section>
        <section class="result-row"><span class="row-id">VIDEO-004</span><div><b>外部视频成片</b><p>${state.productionCreated ? "外部视频任务已创建；Codex 没有生成这段视频。" : "尚未创建外部任务。"}</p></div><span class="row-state">${state.productionCreated ? "外部服务 queued" : "未创建"}</span></section>
      </div>`;
  }

  function setupStatus() {
    if (!state.setup) return "";
    if (state.setupTask?.status === "submitted") return state.setup.step === "development" ? "故事开发候选待整体确认" : "剧本候选待整体确认";
    if (state.setupTask?.status === "running") return "Codex 正在创作";
    if (state.setupTask?.status === "queued") return "等待 Codex 领取";
    if (state.setup.step === "screenplay" && state.setup.directCandidate) return "剧本候选待整体确认";
    if (state.setup.step === "development") return "输入快照已保存";
    if (state.setup.step === "screenplay") return state.setup.developmentAccepted ? "故事开发已接受 · 等待剧本" : "导入原文待处理";
    return "建立项目";
  }

  function setupStage() {
    const source = escapeHtml(state.setup.source);
    if (state.setup.step === "development" && state.setupTask?.status === "submitted") return `
      <span class="eyebrow">DEVELOPMENT · CANDIDATE v1</span><h2>故事开发文档</h2><p class="lead">一名声音修复师从无日期母带中听见尚未发生的犯罪。每次干预都能改变一个结果，也会把代价转移给另一个人。</p><div class="script-note"><strong>系列承诺</strong><p>六集围绕“听见 → 选择 → 付出代价”推进；母带身份跨集不变。</p></div><div class="script-note"><strong>来源</strong><p>${source}</p></div>`;
    if (state.setup.step === "screenplay" && (state.setupTask?.status === "submitted" || state.setup.directCandidate)) return `
      <span class="eyebrow">SCREENPLAY · CANDIDATE v1</span><h2>${state.setup.type === "script" && state.setup.directCandidate ? "导入剧本原文" : "第一集剧本"}</h2><section class="script">${state.setup.directCandidate ? `<div class="prompt-box">${source}</div>` : `<h3 class="scene-heading">EP01-SC01　内景 · 声音修复室 · 深夜</h3><p>${source}</p><p>许真停下倒带，第一次听见自己的声音从尚未发生的明天传来。</p>`}</section><div class="script-note"><strong>整体审批</strong><p>接受后才进入视觉事实。原文和候选版本都会保留，不会静默覆盖。</p></div>`;
    if (state.setup.step === "screenplay" && state.setup.developmentAccepted) return `<span class="eyebrow">DEVELOPMENT · ACCEPTED v1</span><h2>故事开发已整体接受</h2><p class="lead">现在可以让 Codex 形成第一集完整剧本。视觉事实仍未解锁。</p><div class="script-note"><strong>正式上游</strong><p>故事开发文档 v1 · accepted</p></div>`;
    return `<span class="eyebrow">SOURCE SNAPSHOT · v1 · IMMUTABLE</span><h2>${state.setup.type === "script" ? "现成剧本已导入" : state.setup.type === "original" ? "原著输入已保存" : "故事想法已保存"}</h2><p class="lead">${source}</p><div class="script-note"><strong>真实边界</strong><p>${state.setup.type === "script" ? "可以直接把原文作为剧本候选，也可以让 Codex 修订；两条路径都要整体确认。" : "下一步先形成故事开发候选并整体确认，再进入剧本候选。"}</p></div>`;
  }

  function genericEpisodeStage() {
    const episode = currentEpisode();
    if (state.projection === "prompts" && episode.format === "static") return `<div class="empty-stage"><span class="eyebrow">${episode.id} · STATIC DRAMA</span><strong>本集跳过视频提示词</strong><p>静态漫剧保留剧本、视觉事实、分镜和冻结关键帧，只跳过动态视频提示词。</p></div>`;
    if (state.projection === "facts") return `<span class="eyebrow">${episode.id}-${state.scene} · VISUAL FACTS</span><h2>从已接受剧本拆解视觉事实</h2><p class="lead">剧本 v${state.screenplayAccepted} 已整体接受。现在可在同一创作决定面板中拆解人物、造型、地点和道具，再发起图片候选。</p><div class="fact-ledger"><section class="fact-row"><span class="row-id">CHAR-NEW</span><div><b>人物与造型</b><p>等待从当前场景确认可复用身份和状态变体。</p></div><span class="row-state">可开始</span></section><section class="fact-row"><span class="row-id">LOC-NEW</span><div><b>地点与视图</b><p>等待确认空间事实、时态和镜头可见范围。</p></div><span class="row-state">可开始</span></section><section class="fact-row"><span class="row-id">PROP-NEW</span><div><b>道具与状态</b><p>等待确认身份、持有人和场景进出状态。</p></div><span class="row-state">可开始</span></section></div>`;
    return `<span class="eyebrow">${episode.id}-${state.scene} · ${projections.find(([id]) => id === state.projection)[1]}</span><h2>${escapeHtml(episode.title)}</h2><p class="lead">切换投影后仍停留在 ${episode.id}-${state.scene}。这个场景的详细创作状态尚未展开，EP03-SC02 保留完整演示链路。</p>`;
  }

  function candidateGallery() {
    return `
      <header class="candidate-head"><div><span class="eyebrow">PROP-001 · CODEX IMAGE CANDIDATES</span><h2>同一道具的三个候选</h2><p>候选媒体尚未进入项目事实；点击大图比较并选择。</p></div><span class="row-state">3 candidates</span></header>
      <div class="candidate-gallery">${["A", "B", "C"].map((id) => `<button class="candidate-card ${state.selectedVisual === id ? "selected" : ""}" data-action="select-candidate" data-candidate="${id}" aria-pressed="${state.selectedVisual === id}" ${state.candidateStale ? "disabled" : ""}><span class="candidate-image ${id.toLowerCase()}" role="img" aria-label="无日期母带图片候选 ${id}"></span><span class="candidate-label"><strong>候选 ${id}</strong><small>${id === "A" ? "克制目录视角" : id === "B" ? "低机位 · 表面划痕较强" : "俯视检查视角"}</small><small class="candidate-warning">候选 · 尚非正式 REF</small></span></button>`).join("")}</div>`;
  }

  function derivedComparison() {
    if (state.visualStep === "formal" && state.refReplaced) return `
      <header class="candidate-head"><div><span class="eyebrow">PROP-001 · FORMAL REF HISTORY</span><h2>${state.officialRef} 是当前正式版本</h2><p>v1/B1 保留为可追溯历史；当前 v2 来源为已审阅候选 C2。</p></div><span class="row-state">${state.officialRef} · current</span></header>
      <div class="comparison">
        <section class="comparison-pane"><span>历史 · REF-PROP-001-v1 · from B1</span><img src="assets/visual-loop/prop-revision-b1.png" alt="已保留的 REF-PROP-001-v1 历史图，来自候选 B1"><p>该位图只代表 v1/B1，未被冒充为当前 v2。</p></section>
        <section class="comparison-pane audit-pane"><span>当前 · ${state.officialRef} · from reviewed C2</span><div class="prompt-box"><b>审核登记摘要</b><p>TASK-IMG-032 / RUN-032-C2</p><p>已比较、已审阅、已通过影响确认。原型不伪造未提供的 C2 位图。</p></div><p>SHOT-004、KF-004、MOTION-004 与 VIDEO-004 已标记 stale，没有自动重新生成。</p></section>
      </div>`;
    return `
      <header class="candidate-head"><div><span class="eyebrow">PROP-001 · DERIVED CANDIDATE</span><h2>${state.visualStep === "formal" ? "候选 B1 已登记为正式 REF" : "局部编辑没有覆盖父候选"}</h2><p>B 保留；B1 记录 derived from Candidate B。</p></div><span class="row-state">${state.visualStep === "formal" ? state.officialRef : "B → B1"}</span></header>
      <div class="comparison">
        <section class="comparison-pane"><span>父候选 B · 保留</span><div class="candidate-image b" role="img" aria-label="划痕较明显的父候选 B"></div><p>盒盖闭合、比例和身份基准不变；表面划痕较强。</p></section>
        <section class="comparison-pane"><span>${state.visualStep === "formal" ? `正式 REF · ${state.officialRef}` : "编辑候选 B1 · 尚非正式 REF"}</span><img src="assets/visual-loop/prop-revision-b1.png" alt="盒盖闭合且表面划痕减弱的无日期母带编辑候选 B1"><p>只削弱表面划痕；没有原地覆盖 B 或当前 REF。</p></section>
      </div>`;
  }

  function textComparison() {
    return `
      <header class="candidate-head"><div><span class="eyebrow">SCREENPLAY · TWO CANDIDATES</span><h2>比较“越界代价”的两种写法</h2><p>选择只形成完整剧本 v3 待确认，不会覆盖正式 v2。</p></div><span class="row-state">2 candidates</span></header>
      <div class="comparison">
        <section class="comparison-pane text-candidate"><span>候选 A · 外部代价</span><p>门外的同事听见母带盒撞上内袋拉链。许真删除校验记录，却留下了声音证据。</p><p>优点：即时危险清楚；风险：代价偏事件化。</p><button class="secondary-action" data-action="choose-text" data-choice="A">选择 A</button></section>
        <section class="comparison-pane text-candidate"><span>候选 B · 职业代价</span><p>许真删除原始校验记录，工牌滑落在门外。她保住母带，却把第二天的调查引向自己。</p><p>优点：越界与职业身份直接相撞；不改变母带状态。</p><button class="primary-action" data-action="choose-text" data-choice="B">选择 B</button></section>
      </div>`;
  }

  function decisionPanel() {
    if (state.setup) return setupDecisionPanel();
    if (!state.decision) return `<aside class="decision-panel decision-empty"><p>选择一段文字、视觉事实或镜头后<br>“创作决定”会在这里出现。</p></aside>`;
    return `<aside class="decision-panel" data-screen-label="统一创作决定面板">${decisionHeader()}<div class="decision-body">${decisionBody()}</div>${decisionFooter()}</aside>`;
  }

  function setupDecisionPanel() {
    const task = state.setupTask;
    let body = `<section class="decision-section"><h3>当前起点</h3><p>${state.setup.type === "script" ? "导入原文已成为不可变 source v1。" : state.setup.type === "original" ? "原著分析按需使用；当前直接进入故事开发。" : "故事想法已成为不可变 source v1。"}</p></section>`;
    if (task) body += taskSection(task, "setupTask");
    if (state.setup.step === "development" && task?.status === "submitted") body += `<section class="decision-section"><h3>整体确认</h3><p>接受故事开发 v1 后才会解锁正式剧本创作。</p></section>`;
    if (state.setup.step === "screenplay" && (task?.status === "submitted" || state.setup.directCandidate)) body += `<section class="decision-section"><h3>整体确认</h3><p>接受剧本 v1 后进入视觉事实；接受前不会建立正式下游。</p></section>`;
    let footer = "";
    if (state.setup.type !== "script" && state.setup.step === "development" && !task) footer = `<footer class="decision-foot"><button class="primary-action" data-action="start-development-task">让 Codex 发展故事</button></footer>`;
    else if (state.setup.step === "development" && task?.status === "submitted") footer = `<footer class="decision-foot"><button class="primary-action" data-action="accept-development-setup">整体接受故事开发 v1</button></footer>`;
    else if (state.setup.type !== "script" && state.setup.step === "screenplay" && !task) footer = `<footer class="decision-foot"><button class="primary-action" data-action="start-setup-screenplay">让 Codex 创作剧本</button></footer>`;
    else if (state.setup.type === "script" && state.setup.step === "screenplay" && !task && !state.setup.directCandidate) footer = `<footer class="decision-foot"><button class="secondary-action" data-action="accept-import-source">直接接受原文</button><button class="primary-action" data-action="revise-import-source">让 Codex 修订</button></footer>`;
    else if (state.setup.step === "screenplay" && (task?.status === "submitted" || state.setup.directCandidate)) footer = `<footer class="decision-foot"><button class="primary-action" data-action="accept-setup-screenplay">整体接受剧本 v1，进入视觉事实</button></footer>`;
    return `<aside class="decision-panel" data-screen-label="项目建立决定"><header class="decision-head"><div><span>Project setup</span><h2>${state.setup.step === "development" ? "故事开发" : "剧本确认"}</h2></div></header><div class="decision-body">${body}</div>${footer}</aside>`;
  }

  function decisionHeader() {
    const title = state.decision === "prop" ? "无日期母带" : state.decision === "storyboard" ? "冻结关键帧" : state.decision === "generic" ? "视觉事实" : "加强越界代价";
    return `<header class="decision-head"><div><span>Creative decision</span><h2>${title}</h2></div><button class="close-button" data-action="close-decision" aria-label="关闭创作决定面板">×</button></header>`;
  }

  function decisionBody() {
    if (state.decision === "prop") return propDecisionBody();
    if (state.decision === "storyboard") return storyboardDecisionBody();
    if (state.decision === "generic") return `<section class="decision-section"><h3>复用同一交互</h3><p>人物、造型和地点同样从事实发起图片候选、比较、局部编辑和正式 REF 接受，不建立新工作区。</p></section>`;
    return textDecisionBody();
  }

  function textDecisionBody() {
    return `
      <section class="decision-section"><h3>当前接受版本</h3><p>剧本 v${state.screenplayAccepted} · EP03-SC02。选中的两段负责“发现风险 → 藏起母带”。</p><div class="decision-quote">增强许真越过职业边界的代价；保持母带在右侧内袋，盒盖闭合。</div></section>
      ${state.textTask ? taskSection(state.textTask, "textTask") : `<section class="decision-section"><h3>继续上次</h3><button class="resume-action" data-action="start-text-task"><span><strong>让 Codex 提出两个候选</strong><small>short-drama-write · 全文 v3</small></span><span aria-hidden="true">→</span></button></section>`}
      ${state.textTask?.status === "submitted" ? `<section class="decision-section"><h3>候选决定</h3><p>${state.screenplayAccepted === 3 ? `候选 ${state.textChoice || "B"} 已进入并整体接受为剧本 v3；本次决定完成。` : state.textChoice ? `已选择候选 ${state.textChoice}。采纳只会生成完整剧本 v3 待确认。` : "在中央比较两个候选，选择后再形成新版本。"}</p></section>` : ""}
      <section class="decision-section"><h3>视觉也由 Codex 创作</h3><p>从剧本里的 PROP-001 可直接生成和编辑图片；图片候选不会自动成为事实。</p><button class="secondary-action" data-action="open-prop">打开母带视觉事实</button></section>`;
  }

  function propDecisionBody() {
    if (state.visualStep === "fact") return `
      <section class="decision-section"><h3>事实与当前状态</h3><div class="task-ledger"><div class="ledger-row"><span>事实</span><code>PROP-001 · 无日期母带</code></div><div class="ledger-row"><span>场景状态</span><code>PSTATE-001-B</code></div><div class="ledger-row"><span>盒盖</span><code>闭合</code></div><div class="ledger-row"><span>位置</span><code>许真右侧外套内袋</code></div><div class="ledger-row"><span>正式 REF</span><code>${state.officialRef || "尚未登记"}</code></div></div></section>
      <section class="decision-section"><h3>生成约束</h3><p>深灰盒体、盒盖闭合、无日期文字；身份不变，只比较表面磨损和镜头视角。</p></section>`;
    if (["task", "gallery", "edit", "derived"].includes(state.visualStep)) return `
      ${state.visualTask ? taskSection(state.visualTask, "visualTask") : ""}
      ${state.visualStep === "gallery" ? `<section class="decision-section"><h3>选择候选</h3><p>${state.selectedVisual ? `已选择 ${state.selectedVisual}。${state.selectedVisual === "B" ? "可继续局部编辑。" : "本演示建议选择 B 继续编辑。"}` : "中央展示 A/B/C 三张候选。接受前都不是正式 REF。"}</p><details class="candidate-details"><summary>候选详情 · Prompt v1</summary><p>深灰无日期母带盒，盒盖闭合；保持同一盒体比例、铰链与闭合结构；比较表面磨损和视角；禁止文字、标签、水印或打开盒盖。</p></details>${state.candidateStale ? `<p class="task-state failed">来源剧本已经变化，旧候选不可继续选择或编辑。刷新上下文并重新生成候选。</p>` : ""}</section>` : ""}
      ${state.visualStep === "edit" ? `<section class="decision-section"><label class="field"><span>局部修改意图</span><textarea id="visual-intent">保留盒盖闭合，削弱表面划痕</textarea></label><p>不改变盒体比例、铰链、闭合状态和候选 B 的身份。</p></section>` : ""}
      ${state.visualStep === "derived" ? `<section class="decision-section"><h3>候选 B1</h3><p>derived from B · 只削弱划痕。接受前仍是可清理的候选媒体。</p><details class="candidate-details"><summary>候选详情 · Prompt v1</summary><p>生成约束：保持 PROP-001 身份、比例、铰链与盒盖闭合。局部编辑指令：“保留盒盖闭合，削弱表面划痕”。</p></details>${state.candidateStale ? `<p class="task-state failed">来源剧本已经变化，接受已禁用。刷新上下文并重新提交候选。</p>` : ""}</section>` : ""}`;
    return `
      <section class="decision-section"><h3>正式 REF</h3>${state.refReplaced ? `<div class="prompt-box"><b>${state.officialRef} · 当前</b><br>来源：已审阅候选 C2。下方 v1/B1 只作为版本历史保留。</div>` : `<img class="ref-thumb" src="assets/visual-loop/prop-revision-b1.png" alt="REF-PROP-001-v1 正式参考图，来自候选 B1">`}<div class="task-ledger"><div class="ledger-row"><span>当前标识</span><code>${state.officialRef}</code></div><div class="ledger-row"><span>来源</span><code>${state.refReplaced ? "TASK-IMG-032 / RUN-032-C2" : "TASK-IMG-031 / RUN-031-B1"}</code></div><div class="ledger-row"><span>事实状态</span><code>PROP-001 / PSTATE-001-B</code></div>${state.refHistory.length ? `<div class="ledger-row"><span>版本历史</span><code>${state.refHistory.join(" · ")} · from B1 · retained</code></div>` : ""}</div></section>
      <section class="decision-section"><h3>下游引用</h3><p>SHOT-004 · KF-004 · MOTION-004${state.productionCreated ? " · VIDEO-004" : ""}</p>${state.refReplaced ? `<p class="task-state">正式 REF 已替换；以上内容都标记 stale，没有自动重新生成。</p>` : ""}</section>`;
  }

  function storyboardDecisionBody() {
    return `${state.keyframeTask ? taskSection(state.keyframeTask, "keyframeTask") : `<section class="decision-section"><h3>冻结关键帧输入</h3><p>SHOT-004 + ${state.officialRef || "等待正式 REF"} + PSTATE-001-A → B。</p></section>`}${state.keyframeReady ? `<section class="decision-section"><h3>Codex 图片候选</h3><p>KF-004 是冻结关键帧图片，不是视频成片。下一步编写 MOTION-004，再交给外部视频服务。</p></section>` : ""}`;
  }

  function taskSection(task, target) {
    return `<section class="decision-section"><h3>Codex Task</h3><div class="task-ledger"><div class="ledger-row"><span>Task</span><code>${task.id}</code></div><div class="ledger-row"><span>Skill</span><code>${task.skill}</code></div><div class="ledger-row"><span>来源</span><code>${task.source}</code></div><div class="ledger-row"><span>状态</span><code class="task-state ${task.status}">${taskStatusLabel(task)}</code></div></div><div class="button-row"><button class="text-action" data-action="copy-context" data-target="${target}">复制 CLI 上下文</button>${!["submitted", "failed"].includes(task.status) ? `<button class="text-action" data-action="fail-task" data-target="${target}">连接失败</button>` : ""}</div>${task.status === "failed" ? `<p>失败或空结果不会改变当前 REF/剧本。可重试，也可手工创建新版本。</p><div class="button-row"><button class="secondary-action" data-action="retry-task" data-target="${target}">重试</button><button class="text-action" data-action="manual-fallback">手工新建版本</button><button class="text-action" data-action="work-offline">Codex 暂不可用</button></div>` : ""}</section>`;
  }

  function decisionFooter() {
    if (state.decision === "text") {
      if (!state.textTask) return "";
      if (state.textTask.status === "submitted" && state.textChoice && !state.textSubmitted) return `<footer class="decision-foot"><button class="secondary-action" data-action="clear-text-choice">重新比较</button><button class="primary-action" data-action="adopt-text">采纳候选 ${state.textChoice}，形成 v3</button></footer>`;
      if (state.textSubmitted && state.screenplayAccepted < 3) return `<footer class="decision-foot"><button class="secondary-action" data-action="reject-text">退回 v3</button><button class="primary-action" data-action="accept-screenplay">整体接受剧本 v3</button></footer>`;
      return "";
    }
    if (state.decision === "prop") {
      if (state.visualStep === "fact") return `<footer class="decision-foot"><button class="primary-action" data-action="generate-visual">让 Codex 生成 3 张图片候选</button></footer>`;
      if (state.visualStep === "gallery" && state.candidateStale) return `<footer class="decision-foot"><button class="primary-action" data-action="refresh-visual">刷新上下文并重新生成候选</button></footer>`;
      if (state.visualStep === "gallery" && state.selectedVisual) return `<footer class="decision-foot"><button class="secondary-action" data-action="keep-current-ref">保留当前</button><button class="primary-action" data-action="edit-candidate" ${state.selectedVisual !== "B" ? "disabled" : ""}>继续编辑候选 ${state.selectedVisual}</button></footer>`;
      if (state.visualStep === "edit") return `<footer class="decision-foot"><button class="secondary-action" data-action="back-gallery">返回候选</button><button class="primary-action" data-action="submit-image-edit">生成编辑候选</button></footer>`;
      if (state.visualStep === "derived") return `<footer class="decision-foot">${state.candidateStale ? `<button class="primary-action" data-action="refresh-visual">刷新上下文并重新提交</button>` : `<button class="secondary-action" data-action="back-gallery">保留 B1，继续比较</button><button class="primary-action" data-action="confirm-ref">接受 B1 为正式 REF</button>`}</footer>`;
      if (state.visualStep === "formal") return `<footer class="decision-foot"><button class="secondary-action" data-action="replace-ref">接受已审阅候选为新 REF 版本</button><button class="primary-action" data-action="go-storyboard">查看分镜引用</button></footer>`;
    }
    return "";
  }

  function overlay() {
    return { impact: impactOverlay, replace: replaceOverlay, production: productionOverlay, intake: intakeOverlay }[state.overlay]();
  }

  function sheetShell(kicker, title, body, footer, label) {
    return `<div class="overlay" data-action="overlay-backdrop"><section class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title" data-screen-label="${label}"><header class="sheet-head"><div><span>${kicker}</span><h2 id="sheet-title">${title}</h2></div><button class="close-button" data-action="close-overlay" aria-label="关闭">×</button></header><div class="sheet-body">${body}</div><footer class="sheet-foot">${footer}</footer></section></div>`;
  }

  function impactOverlay() {
    return sheetShell("Candidate → official ref", "接受 B1 为 REF-PROP-001-v1", `<p>daemon 只在确认成功后登记正式 REF。导入失败时，当前 REF 保持不变。</p><ul class="impact-list"><li><strong>正式事实</strong><span>PROP-001 / PSTATE-001-B 将引用 B1</span></li><li><strong>分镜</strong><span>SHOT-004 可引用新 REF</span></li><li><strong>冻结关键帧</strong><span>KF-004 尚未生成，不自动创建</span></li><li><strong>来源记录</strong><span>TASK-IMG-031 · RUN-031-B1 · prompt v1</span></li></ul>`, `<button class="secondary-action" data-action="close-overlay">取消</button><button class="primary-action" data-action="accept-ref">确认登记正式 REF</button>`, "正式 REF 影响确认");
  }

  function replaceOverlay() {
    return sheetShell("Accept reviewed candidate", "接受已审阅候选 C2 为 REF-PROP-001-v2", `<p>候选 C2 已完成比较与审阅。确认只把它提升为新的正式版本；${state.officialRef} 保留为历史，下游只标记 stale，不自动重新生成。</p><ul class="impact-list"><li><strong>SHOT-004</strong><span>分镜参考需要同步</span></li><li><strong>KF-004</strong><span>冻结关键帧需要重新审阅</span></li><li><strong>MOTION-004</strong><span>视频提示词来源需要同步</span></li><li><strong>VIDEO-004</strong><span>已有外部媒体保留历史并标记 stale</span></li></ul>`, `<button class="secondary-action" data-action="close-overlay">保留 ${state.officialRef}</button><button class="danger-action" data-action="confirm-replace-ref">接受 v2 并标记下游 stale</button>`, "替换 REF 影响确认");
  }

  function productionOverlay() {
    const frozen = state.productionPrepared;
    return sheetShell("Prepare → confirm", frozen ? "明确确认外部视频生成" : "准备外部视频生成", `<p>${frozen ? "manifest 已冻结，但尚未创建外部任务。最后核对后再确认。" : "这一步只核对来源和输出，不会调用外部服务。Codex 只准备输入。"}</p><ul class="impact-list"><li><strong>参考图</strong><span>${state.officialRef || "缺失"}</span></li><li><strong>冻结关键帧</strong><span>KF-004 · ${state.keyframeReady ? "候选可用" : "缺失"}</span></li><li><strong>动作提示词</strong><span>MOTION-004 · 6 秒 · 竖屏</span></li><li><strong>执行方</strong><span>外部视频服务 · 不是 Codex</span></li></ul>`, `<button class="secondary-action" data-action="close-overlay">返回修改</button>${frozen ? `<button class="danger-action" data-action="confirm-production">我已核对，创建外部任务</button>` : `<button class="primary-action" data-action="prepare-production">冻结这份预览</button>`}`, "外部视频生产确认");
  }

  function intakeOverlay() {
    const copy = { idea: ["从故事想法开始", "故事想法"], original: ["从合法持有的原著开始", "原著摘要或章节"], script: ["从现成剧本开始", "剧本文本"] }[state.intakeType];
    return sheetShell("New project", "创建短剧项目", `<div class="intake-grid"><div class="intake-options">${[["idea", "故事想法", "发展故事"], ["original", "合法持有的原著", "分析可选"], ["script", "现成剧本", "直接确认或修订"]].map(([id, label, note]) => `<button class="intake-option ${state.intakeType === id ? "active" : ""}" data-action="intake-type" data-type="${id}" aria-pressed="${state.intakeType === id}"><span><strong>${label}</strong><small>${note}</small></span></button>`).join("")}</div><div class="intake-form"><p>${copy[0]}。输入先保存为不可变快照，不强迫补齐名义流程。</p><label class="field"><span>项目名称</span><input id="intake-name" value="${escapeHtml(state.intakeDraft.name)}"></label><label class="field"><span>${copy[1]}</span><textarea id="intake-source">${escapeHtml(state.intakeDraft.source)}</textarea></label></div></div>`, `<button class="secondary-action" data-action="close-overlay">取消</button><button class="primary-action" data-action="create-project">保存输入</button>`, "新建项目三种起点");
  }

  function workspace() {
    return `<div class="workbench" data-screen-label="Script Weaver v5 多模态创作工作台"><a class="skip-link" href="#main-content">跳到当前创作舞台</a>${header()}<div class="desk">${storySpine()}${stage()}${decisionPanel()}</div>${state.overlay ? overlay() : ""}${state.notice ? `<div class="notice" role="status" aria-live="polite">${escapeHtml(state.notice)}</div>` : ""}</div>`;
  }

  function render() {
    document.documentElement.dataset.theme = state.theme;
    app.innerHTML = workspace();
  }

  function focusSelector(button) {
    const attrs = ["action", "episode", "scene", "projection", "candidate", "target"];
    const selectors = attrs.filter((key) => button.dataset[key]).map((key) => `[data-${key}="${CSS.escape(button.dataset[key])}"]`).join("");
    return `button${selectors}`;
  }

  function openOverlay(kind, button) {
    returnFocus = button ? focusSelector(button) : "";
    state.overlay = kind;
    render();
    requestAnimationFrame(() => document.querySelector(".sheet button")?.focus());
  }

  function closeOverlay() {
    state.overlay = null;
    render();
    if (returnFocus) requestAnimationFrame(() => document.querySelector(returnFocus)?.focus());
  }

  function retryTask(target) {
    const prior = state[target];
    if (!prior) return;
    const next = { ...prior, id: `${prior.id}-R`, status: "queued" };
    if (target === "visualTask") state.visualStep = prior.kind === "edit" ? "edit" : "task";
    taskLifecycle(target, next);
  }

  app.addEventListener("click", (event) => {
    if (event.target.matches('[data-action="overlay-backdrop"]')) { closeOverlay(); return; }
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    const action = button.dataset.action;

    if (action === "theme") state.theme = state.theme === "light" ? "dark" : "light";
    else if (action === "toggle-spine") state.spineOpen = !state.spineOpen;
    else if (action === "close-spine") state.spineOpen = false;
    else if (action === "series") { state.episode = "EP03"; state.scene = "SC02"; state.expandedEpisode = "EP03"; state.spineOpen = false; notify("已回到全剧当前焦点：EP03-SC02"); }
    else if (action === "toggle-episode") { state.expandedEpisode = state.expandedEpisode === button.dataset.episode ? "" : button.dataset.episode; }
    else if (action === "select-scene") { state.episode = button.dataset.episode; state.scene = button.dataset.scene; state.expandedEpisode = button.dataset.episode; state.spineOpen = false; state.decision = state.episode === "EP03" && state.scene === "SC02" ? "text" : null; state.visualStep = state.officialRef ? "formal" : "fact"; }
    else if (action === "projection") { state.projection = button.dataset.projection; if (state.projection === "storyboard" && state.episode === "EP03") state.decision = "storyboard"; }
    else if (action === "focus-shot") state.decision = "storyboard";
    else if (action === "close-decision") state.decision = null;
    else if (action === "open-text-decision") state.decision = "text";
    else if (action === "open-prop") { state.decision = "prop"; if (state.officialRef) state.visualStep = "formal"; else if (!["gallery", "edit", "derived"].includes(state.visualStep)) state.visualStep = "fact"; }
    else if (action === "open-generic-fact") state.decision = "generic";
    else if (action === "start-text-task") {
      if (!state.codexAvailable) { notify("Codex 当前不可用；你仍可查看和手工编辑正式剧本 v2"); }
      else taskLifecycle("textTask", { id: "TASK-WRITE-203", skill: "short-drama-write", source: "EP03 screenplay v2 / SC02 selection", status: "queued", kind: "text" });
    }
    else if (action === "choose-text") state.textChoice = button.dataset.choice;
    else if (action === "clear-text-choice") state.textChoice = "";
    else if (action === "adopt-text") { state.textSubmitted = true; state.screenplayVersion = 3; state.projection = "screenplay"; notify("候选已进入完整剧本 v3；正式剧本仍是 v2"); }
    else if (action === "accept-screenplay") { state.screenplayAccepted = 3; state.downstreamStale = true; if (state.visualTask && !state.officialRef) state.candidateStale = state.visualTask.sourceScreenplayVersion !== state.screenplayAccepted; notify("剧本 v3 已整体接受；下游只标记 stale，没有自动生成"); }
    else if (action === "reject-text") { state.textSubmitted = false; state.screenplayVersion = 2; state.textChoice = ""; notify("剧本 v3 已退回；正式 v2 保持不变"); }
    else if (action === "generate-visual") {
      if (!state.codexAvailable) notify("Codex 当前不可用；当前 REF 与既有工作不受影响");
      else { state.visualStep = "task"; taskLifecycle("visualTask", { id: "TASK-IMG-031", skill: "short-drama-image-prompts + image generation", source: `PROP-001 / PSTATE-001-B / screenplay v${state.screenplayAccepted}`, sourceScreenplayVersion: state.screenplayAccepted, status: "queued", kind: "generate" }); }
    }
    else if (action === "select-candidate") { state.selectedVisual = button.dataset.candidate; state.decision = "prop"; }
    else if (action === "edit-candidate") state.visualStep = "edit";
    else if (action === "back-gallery") state.visualStep = "gallery";
    else if (action === "keep-current-ref") { state.visualStep = state.officialRef ? "formal" : "fact"; notify("未接受任何候选；当前正式事实没有改变"); }
    else if (action === "submit-image-edit") {
      const intent = document.getElementById("visual-intent")?.value.trim();
      if (!intent) { document.getElementById("visual-intent")?.focus(); return; }
      taskLifecycle("visualTask", { id: "TASK-IMG-031-B1", skill: "image edit", source: `Candidate B / PROP-001 / screenplay v${state.screenplayAccepted}`, sourceScreenplayVersion: state.screenplayAccepted, status: "queued", kind: "edit" });
    }
    else if (action === "confirm-ref") { if (state.candidateStale) notify("来源已变化，必须先刷新上下文"); else { openOverlay("impact", button); return; } }
    else if (action === "accept-ref") { state.officialRef = "REF-PROP-001-v1"; state.refHistory = []; state.visualStep = "formal"; state.overlay = null; state.projection = "facts"; notify("REF-PROP-001-v1 已登记；候选 B 和 B1 来源均保留"); }
    else if (action === "refresh-visual") {
      const staleStep = state.visualStep;
      state.candidateStale = false;
      state.visualTask = null;
      if (staleStep === "gallery") {
        state.selectedVisual = "";
        state.visualStep = "fact";
        notify("旧图片候选已退出；请基于最新剧本来源重新生成");
      } else {
        state.visualStep = "edit";
        notify("已刷新到最新剧本来源，请重新提交编辑候选");
      }
    }
    else if (action === "go-storyboard") { state.projection = "storyboard"; state.decision = "storyboard"; state.visualStep = "formal"; }
    else if (action === "generate-keyframe") { state.decision = "storyboard"; taskLifecycle("keyframeTask", { id: "TASK-KF-004", skill: "image generation", source: `SHOT-004 / ${state.officialRef}`, status: "queued", kind: "keyframe" }); }
    else if (action === "replace-ref") { openOverlay("replace", button); return; }
    else if (action === "confirm-replace-ref") { if (state.officialRef) state.refHistory.push(state.officialRef); state.officialRef = "REF-PROP-001-v2"; state.refReplaced = true; state.overlay = null; state.projection = "facts"; notify("REF-PROP-001-v2 已接受；v1 保留历史，下游已标记 stale"); }
    else if (action === "open-production") { if (!state.keyframeReady) notify("先生成冻结关键帧，再准备外部视频"); else { state.productionPrepared = false; openOverlay("production", button); return; } }
    else if (action === "prepare-production") { state.productionPrepared = true; render(); requestAnimationFrame(() => document.querySelector('[data-action="confirm-production"]')?.focus()); return; }
    else if (action === "confirm-production") { state.productionCreated = true; state.overlay = null; state.projection = "results"; notify("外部视频任务已创建；Codex 只提供了参考图、关键帧和提示词"); }
    else if (action === "copy-context") { const task = state[button.dataset.target]; navigator.clipboard?.writeText(`script-weaver workbench task context ${task?.id || ""} --section all --json`).catch(() => {}); notify("CLI 上下文命令已复制；请切换到 Codex 继续"); }
    else if (action === "fail-task") { const task = state[button.dataset.target]; if (task) task.status = "failed"; notify("任务失败或返回空结果；当前正式事实保持不变"); }
    else if (action === "retry-task") retryTask(button.dataset.target);
    else if (action === "manual-fallback") { notify("已保留当前内容，可在 Workbench 手工新建版本；无需等待 Codex"); }
    else if (action === "work-offline") { state.codexAvailable = false; notify("已进入手工工作模式；既有内容、版本和审批仍可使用"); }
    else if (action === "open-intake") { state.intakeDraft = { name: state.projectName, source: "一名声音修复师在旧母带里听见尚未发生的犯罪。" }; openOverlay("intake", button); return; }
    else if (action === "intake-type") { state.intakeType = button.dataset.type; render(); requestAnimationFrame(() => document.querySelector(`[data-type="${state.intakeType}"]`)?.focus()); return; }
    else if (action === "create-project") { const name = state.intakeDraft.name.trim(); const source = state.intakeDraft.source.trim(); if (!name || !source) { document.getElementById(name ? "intake-source" : "intake-name")?.focus(); return; } state.projectName = name; state.setup = { type: state.intakeType, source, step: state.intakeType === "script" ? "screenplay" : "development", developmentAccepted: false, directCandidate: false }; state.setupTask = null; state.overlay = null; state.decision = null; notify("输入快照已保存；继续完成整体审批后进入视觉事实"); }
    else if (action === "start-development-task") taskLifecycle("setupTask", { id: "TASK-DEVELOP-001", skill: "short-drama-develop", source: "source v1", status: "queued", kind: "development" });
    else if (action === "accept-development-setup") { state.setup.developmentAccepted = true; state.setup.step = "screenplay"; state.setupTask = null; notify("故事开发 v1 已整体接受；现在创建第一集剧本"); }
    else if (action === "start-setup-screenplay") taskLifecycle("setupTask", { id: "TASK-WRITE-001", skill: "short-drama-write", source: "development v1 accepted", status: "queued", kind: "screenplay" });
    else if (action === "accept-import-source") { state.setup.directCandidate = true; notify("导入原文已成为剧本 v1 候选，等待整体确认"); }
    else if (action === "revise-import-source") taskLifecycle("setupTask", { id: "TASK-WRITE-IMPORT-001", skill: "short-drama-write", source: "imported source v1", status: "queued", kind: "screenplay" });
    else if (action === "accept-setup-screenplay") { state.setup = null; state.setupTask = null; state.screenplayVersion = 1; state.screenplayAccepted = 1; state.textSubmitted = false; state.downstreamStale = false; state.episode = "EP01"; state.scene = "SC01"; state.expandedEpisode = "EP01"; state.projection = "facts"; state.decision = "generic"; notify("剧本 v1 已整体接受；视觉事实已解锁"); }
    else if (action === "close-overlay") { closeOverlay(); return; }

    render();
  });

  app.addEventListener("input", (event) => {
    if (event.target.id === "intake-name") state.intakeDraft.name = event.target.value;
    else if (event.target.id === "intake-source") state.intakeDraft.source = event.target.value;
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (state.overlay) { event.preventDefault(); closeOverlay(); }
      else if (state.spineOpen) { state.spineOpen = false; render(); }
      else if (state.decision) { state.decision = null; render(); }
      return;
    }
    if (!state.overlay || event.key !== "Tab") return;
    const focusable = [...document.querySelectorAll('.overlay button:not([disabled]), .overlay input:not([disabled]), .overlay textarea:not([disabled]), .overlay select:not([disabled])')];
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  render();
})();
