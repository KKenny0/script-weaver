(function () {
  "use strict";

  const root = document.getElementById("root");
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const docLabels = { screenplay: "剧本", visual: "视觉设定", image: "图片提示词", storyboard: "分镜", video: "视频提示词" };
  const statusLabels = { missing: "MISSING", proposal: "PROPOSAL", current: "CURRENT", stale: "STALE", skipped: "SKIPPED", blocked: "BLOCKED", submitted: "SUBMITTED", accepted: "ACCEPTED", rejected: "REJECTED", ready: "READY", queued: "QUEUED", running: "RUNNING", failed: "FAILED", conflict: "CONFLICT", prepared: "PREPARED", confirmed: "CONFIRMED", review: "REVIEW" };
  const status = (value) => statusLabels[value] || String(value).toUpperCase();
  const chip = (value) => `<span class="status-chip ${esc(value)}">${esc(status(value))}</span>`;
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const productionRecord = () => ({ prepared: false, confirmed: false, inputHash: "", confirmKey: "", revision: 0, jobs: [], invalidated: false });
  const refRecords = () => [{ id: "REF-001", label: "人物造型参考", state: "missing" }, { id: "REF-002", label: "地点夜态参考", state: "missing" }, { id: "REF-003", label: "关键道具参考", state: "missing" }];

  const episodeSeed = [
    { id: "EP01", title: "没有寄出的声音", format: "dynamic", docs: { screenplay: { state: "accepted", version: 1, task: null, history: [{ version: 1, state: "accepted", source: "故事开发文档 v1" }] }, visual: { state: "current", version: 2, task: null, source: "EP01 剧本 v1" }, image: { state: "current", version: 1, task: null, source: "视觉设定 v2" }, storyboard: { state: "current", version: 2, task: null, source: "视觉设定 v2" }, video: { state: "current", version: 1, task: null, source: "分镜 v2" } }, production: productionRecord(), refs: refRecords() },
    { id: "EP02", title: "被剪断的雨声", format: "static", docs: { screenplay: { state: "accepted", version: 1, task: null, history: [{ version: 1, state: "accepted", source: "导入剧本原文 v1" }] }, visual: { state: "current", version: 1, task: null, source: "EP02 剧本 v1" }, image: { state: "current", version: 1, task: null, source: "视觉设定 v1" }, storyboard: { state: "proposal", version: 1, task: "submitted", source: "视觉设定 v1" }, video: { state: "skipped", version: null, task: null, source: "静态漫剧不需要" } }, production: productionRecord(), refs: refRecords() },
    { id: "EP03", title: "第一次篡改", format: "dynamic", docs: { screenplay: { state: "submitted", version: 2, task: "submitted", history: [{ version: 1, state: "rejected", source: "分集地图 v1", feedback: "主角越界缺少足够代价。" }, { version: 2, state: "submitted", source: "EP03 剧本 v1 + 退回反馈" }] }, visual: { state: "stale", version: 1, task: null, source: "EP03 剧本 v1" }, image: { state: "stale", version: 1, task: null, source: "视觉设定 v1" }, storyboard: { state: "stale", version: 1, task: null, source: "视觉设定 v1" }, video: { state: "missing", version: null, task: null, source: "等待当前分镜" } }, production: productionRecord(), refs: refRecords() },
    { id: "EP04", title: "销毁请求", format: "dynamic", docs: { screenplay: { state: "missing", version: null, task: null, history: [] }, visual: { state: "missing", version: null, task: null }, image: { state: "missing", version: null, task: null }, storyboard: { state: "missing", version: null, task: null }, video: { state: "missing", version: null, task: null } }, production: productionRecord(), refs: refRecords() },
    { id: "EP05", title: "无辜的人", format: "dynamic", docs: { screenplay: { state: "missing", version: null, task: null, history: [] }, visual: { state: "missing", version: null, task: null }, image: { state: "missing", version: null, task: null }, storyboard: { state: "missing", version: null, task: null }, video: { state: "missing", version: null, task: null } }, production: productionRecord(), refs: refRecords() },
    { id: "EP06", title: "她的录音", format: "static", docs: { screenplay: { state: "missing", version: null, task: null, history: [] }, visual: { state: "missing", version: null, task: null }, image: { state: "missing", version: null, task: null }, storyboard: { state: "missing", version: null, task: null }, video: { state: "skipped", version: null, task: null, source: "静态漫剧不需要" } }, production: productionRecord(), refs: refRecords() }
  ];

  const initialState = {
    theme: "light", view: "overview", route: "idea", episode: "EP01", doc: "visual", scenarioOpen: false, modal: null, notice: "", reviewReport: false,
    front: { source: "故事想法 v1", analysis: "skipped", development: "accepted", developmentVersion: 1, developmentHistory: [{ version: 1, state: "accepted", source: "故事想法 v1" }], map: "current" }, episodes: episodeSeed
  };
  let state = clone(initialState);
  let focusReturn = "";
  let noticeTimer;

  const currentEpisode = () => state.episodes.find((item) => item.id === state.episode);
  const currentDoc = () => currentEpisode().docs[state.doc];
  const currentProduction = () => currentEpisode().production;
  const docSummary = (doc) => doc.version ? `v${doc.version} · ${status(doc.state)}` : status(doc.state);
  const productionReady = (episode) => episode.docs.image.state === "current" && episode.docs.storyboard.state === "current" && (episode.format === "static" || episode.docs.video.state === "current");
  const canCreateDoc = (episode, key) => key === "screenplay" || (key === "visual" && episode.docs.screenplay.state === "accepted") || (["image", "storyboard"].includes(key) && episode.docs.visual.state === "current") || (key === "video" && episode.format === "dynamic" && episode.docs.storyboard.state === "current");
  const downstreamEdges = { visual: ["image", "storyboard", "video"], image: [], storyboard: ["video"], video: [] };
  function invalidateProduction(episode, reason = "创作输入已变化") {
    const production = episode.production;
    production.prepared = false; production.confirmed = false; production.inputHash = ""; production.confirmKey = ""; production.jobs = []; production.invalidated = true;
    return `${episode.id} ${reason}；旧生产确认已失效`;
  }
  function staleDependents(episode, key) {
    (downstreamEdges[key] || []).forEach((child) => {
      const doc = episode.docs[child];
      if (doc.state !== "missing" && doc.state !== "skipped") doc.state = "stale";
    });
  }
  function fingerprint(items) {
    const input = JSON.stringify(items);
    let hash = 2166136261;
    for (let index = 0; index < input.length; index += 1) hash = Math.imul(hash ^ input.charCodeAt(index), 16777619) >>> 0;
    return `fnv1a:${hash.toString(16).padStart(8, "0")}`;
  }
  const nextAction = (episode, key) => {
    const doc = episode.docs[key];
    if (doc.state === "missing") return canCreateDoc(episode, key) ? (key === "screenplay" ? "创建写作任务" : `创建${docLabels[key]} Task`) : "等待上游";
    if (doc.state === "proposal") return "审阅 ChangeSet";
    if (doc.state === "submitted") return "整体审批";
    if (doc.state === "stale") return "基于新上游修订";
    if (doc.state === "skipped") return "无需动作";
    return "查看当前版本";
  };
  const notify = (message) => {
    state.notice = message;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { state.notice = ""; render(); }, 2800);
  };

  function masthead() {
    return `<header class="masthead"><div class="wordmark"><b>Script Weaver</b><span>Creator desk</span></div><div class="project-name">没有寄出的声音</div><div class="mast-actions"><div class="codex-indicator"><span class="pulse"></span><span>CLI + Skills</span></div><button class="icon-button" data-action="theme" aria-label="切换明暗主题">${state.theme === "light" ? "夜" : "昼"}</button><button class="text-button" data-action="command">⌘ K</button></div></header>`;
  }

  function layerNav() {
    const items = [["overview", "01", "项目总览", "输入与前期材料"], ["episode", "02", "单集工作台", "五份创作文档"], ["production", "03", "生产中心", "唯一外部门禁"]];
    return `<nav class="creator-nav" aria-label="工作台层级">${items.map(([id, number, label, note]) => `<button class="layer-tab ${state.view === id ? "active" : ""}" data-action="view" data-view="${id}" aria-current="${state.view === id ? "page" : "false"}"><small>${number}</small><span><b>${label}</b><em>${note}</em></span></button>`).join("")}</nav>`;
  }

  function sidebar() {
    return `<aside class="creator-side"><section class="side-section"><div class="project-seal"><span class="eyebrow">Project evidence</span><h2>没有寄出的声音</h2><p>${state.route === "idea" ? "故事想法 → 故事开发" : state.route === "original" ? "合法持有原著 → 按需分析" : "现成多集剧本 → 直接分集"}</p></div></section><section class="side-section routes"><div class="side-title">Source route</div>${[["idea", "故事想法"], ["original", "合法持有原著"], ["existing", "现成剧本"]].map(([id, label]) => `<button class="side-route ${state.route === id ? "active" : ""}" data-action="route" data-route="${id}"><span>${label}</span><span>${state.route === id ? "CURRENT" : ""}</span></button>`).join("")}</section><section class="side-section"><div class="side-title">Episodes</div><div class="episode-side-list">${state.episodes.map((episode) => `<button class="side-episode ${state.episode === episode.id ? "active" : ""}" data-action="episode" data-episode="${episode.id}"><span>${episode.id}</span><span>${episode.format === "static" ? "STATIC" : "DYNAMIC"}</span></button>`).join("")}</div></section></aside>`;
  }

  function frontRows() {
    const rows = [{ mark: "SRC", label: state.front.source, note: "不可变输入快照", state: "current" }];
    if (state.route === "original") rows.push({ mark: "OPT", label: "原著分析", note: "抽样快评与分集候选；按需", state: state.front.analysis });
    if (state.route !== "existing") rows.push({ mark: "OPT", label: "故事开发文档", note: "系列承诺、故事引擎与分集地图", state: state.front.development });
    if (state.route === "existing") rows.push({ mark: "MAP", label: "分集地图", note: "仅多集整稿需要；逐集切片", state: state.front.map });
    return rows;
  }

  function matrix() {
    const keys = ["screenplay", "visual", "image", "storyboard", "video"];
    return `<section class="matrix-wrap"><header class="matrix-head"><div><span class="eyebrow">Episode × document</span><h2>六集创作事实</h2></div><span>MISSING 不是空文档；只有请求后才创建</span></header><div class="matrix-scroll"><div class="episode-matrix"><div class="matrix-cell header">剧集</div>${keys.map((key) => `<div class="matrix-cell header">${docLabels[key]}</div>`).join("")}<div class="matrix-cell header">生产</div>${state.episodes.map((episode) => `<div class="matrix-cell row-head"><b>${episode.id}</b><small>${episode.format === "static" ? "静态漫剧" : "动态漫剧"}</small></div>${keys.map((key) => { const doc = episode.docs[key]; return `<button class="matrix-cell" data-action="matrix-doc" data-episode="${episode.id}" data-doc="${key}">${chip(doc.state)}<b>${doc.version ? `v${doc.version}` : "未创建"}</b><small>${nextAction(episode, key)}</small></button>`; }).join("")}<button class="matrix-cell" data-action="matrix-production" data-episode="${episode.id}">${chip(productionReady(episode) ? (episode.production.confirmed ? "confirmed" : episode.production.prepared ? "prepared" : "ready") : "blocked")}<b>${productionReady(episode) ? "可准备任务" : "等待创作输入"}</b><small>${productionReady(episode) ? "进入生产中心" : "不会调用外部服务"}</small></button>`).join("")}</div></div></section>`;
  }

  function overview() {
    const rows = frontRows();
    const nextCopy = state.route === "idea" ? "想法已经形成已接受的故事开发文档；接下来逐集写剧本。" : state.route === "original" ? (state.front.analysis === "missing" ? "先决定是否做原著抽样快评；也可以跳过，直接进入故事开发。" : "原著分析是选用的材料，不是永久流水线门禁。") : "现成多集剧本保留原文并识别分集边界，直接进入逐集剧本审批。";
    let nextActions = `<button class="primary" data-action="view" data-view="episode">进入单集工作台</button>`;
    if (state.route === "original" && state.front.analysis === "missing") nextActions = `<button class="primary" data-action="analysis-start">创建原著分析任务</button><button class="secondary" data-action="analysis-skip">跳过分析</button>`;
    else if (state.route === "original" && state.front.analysis === "queued") nextActions = `<button class="primary" data-action="analysis-complete">模拟分析提交为当前材料</button>`;
    else if (state.route === "original" && state.front.development === "missing") nextActions = `<button class="primary" data-action="view" data-view="episode">直接进入单集写作</button><button class="secondary" data-action="development-start">可选：创建故事开发</button>`;
    else if (state.front.development === "missing") nextActions = `<button class="primary" data-action="development-start">创建故事开发 Task</button>`;
    else if (state.front.development === "queued") nextActions = `<button class="primary" data-action="development-run">模拟 Codex 领取</button>`;
    else if (state.front.development === "running") nextActions = `<button class="primary" data-action="development-submit">模拟提交故事开发文档</button>`;
    else if (state.front.development === "submitted") nextActions = `<button class="primary" data-action="development-accept">整体接受故事开发文档</button><button class="secondary" data-action="development-reject">退回并说明</button>`;
    else if (state.front.development === "rejected") nextActions = `<button class="primary" data-action="development-new-version">基于反馈创建 v${state.front.developmentVersion + 1}</button>`;
    const developmentHistory = state.front.developmentHistory?.length ? `<div class="development-history"><span class="eyebrow">Development versions</span>${[...state.front.developmentHistory].reverse().map((item) => `<div><b>v${item.version} · ${status(item.state)}</b><small>${esc(item.source)}</small>${item.feedback ? `<p>退回反馈：${esc(item.feedback)}</p>` : ""}</div>`).join("")}</div>` : "";
    return `<main class="creator-page" data-screen-label="项目总览与前期材料"><header class="creator-head"><div><span class="eyebrow">Project layer · creator-first</span><h1>先确认材料，再展开真正需要的文档</h1><p>原著分析与故事开发都按输入按需出现。已有剧本不会为了流程完整而倒补阶段。</p></div>${chip("current")}</header><div class="route-grid">${[["idea", "PATH 01", "故事想法", "发展故事承诺与分集方向，再进入单集写作。"], ["original", "PATH 02", "合法持有原著", "可先抽样快评；值得改编再形成故事开发契约。"], ["existing", "PATH 03", "现成单集 / 多集剧本", "保留原文；直接接受、规范化或识别分集边界。"]].map(([id, mark, title, copy]) => `<button class="route-card ${state.route === id ? "active" : ""}" data-action="route" data-route="${id}"><span>${mark}</span><h2>${title}</h2><p>${copy}</p><b>${state.route === id ? "当前入口" : "查看路线 →"}</b></button>`).join("")}</div><section class="front-ledger"><div class="evidence-sheet"><header><div><span class="eyebrow">Front materials</span><h2>实际使用的前期材料</h2></div><span>${rows.length} 项</span></header><div class="evidence-list">${rows.map((row, index) => `<div class="evidence-row"><i>${String(index + 1).padStart(2, "0")}</i><div><b>${esc(row.label)}</b><p>${esc(row.note)}</p></div>${chip(row.state)}</div>`).join("")}</div>${developmentHistory}</div><div class="next-card"><header><span class="eyebrow">One next action</span>${chip(rows.some((row) => ["missing", "queued", "running", "submitted", "rejected"].includes(row.state)) ? "missing" : "current")}</header><p>${nextCopy}</p><div class="button-row">${nextActions}</div></div></section>${matrix()}</main>`;
  }

  function branchMap(episode) {
    return `<div class="branch-map" aria-label="文档依赖关系"><div class="branch-node"><b>剧本 → 视觉设定</b><small>接受剧本提供正式视觉事实来源</small></div><div class="branch-arrow">→</div><div class="branch-split"><div class="branch-node"><b>图片提示词</b><small>与分镜并行</small></div><div class="branch-node"><b>分镜 + 冻结关键帧</b><small>与图片提示词并行</small></div></div><div class="branch-arrow">→</div><div class="branch-node ${episode.format === "static" ? "skipped" : ""}"><b>${episode.format === "static" ? "视频提示词 · SKIPPED" : "视频提示词"}</b><small>${episode.format === "static" ? "关键帧切换 + 配音直达生产" : "由当前分镜逐镜翻译动作"}</small></div></div>`;
  }

  function readerBody(episode, key, doc) {
    if (doc.state === "missing") return `<section class="waiting-card"><h2>${docLabels[key]}尚未创建</h2><p>${canCreateDoc(episode, key) ? "直接上游已满足，可以创建 Codex Task。" : "直接上游尚未成为当前事实，本步骤保持 BLOCKED。"}</p></section>`;
    if (doc.state === "skipped") return `<section class="waiting-card"><h2>${docLabels[key]}已按制作形态跳过</h2><p>静态漫剧由图片提示词和分镜冻结关键帧直接进入生产；历史动态版本仍被保留。</p></section>`;
    const scene = `${episode.id}-SC02`;
    const shot = `${episode.id}-SHOT-004`;
    if (key === "screenplay") return `<section class="reader-section"><h3>${scene} · ${esc(episode.title)}</h3><p>${episode.id === "EP01" ? "许真找到无日期母带，把它藏进右侧外套内袋。" : `本集围绕“${esc(episode.title)}”推进一次不可逆选择；当前展示的是 ${episode.id} 剧本 v${doc.version}。`}</p></section><section class="reader-section"><h3>审批权威</h3><p>整份剧本版本接受后，才成为视觉设定的正式上游；退回必须留下反馈，历史不会被覆盖。</p></section>`;
    if (key === "visual" && episode.id === "EP01") return `<section class="reader-section"><h3>四类可复用视觉事实</h3><div class="fact-grid"><div class="fact-card"><span>CHARACTER + LOOK</span><b>CHAR-001 / LOOK-001-A · 许真深夜造型</b><p>短发、旧深灰风衣、右侧内袋可放母带；疲惫但克制。</p></div><div class="fact-card"><span>LOCATION + VIEW</span><b>LOC-002 / VIEW-002-A · 档案间夜态</b><p>窄纵深、金属母带架、单点顶灯、门在画面左后方。</p></div><div class="fact-card"><span>PROP + STATE</span><b>PROP-001 / PSTATE-001-A→B</b><p>无日期母带，深灰盒体，盒盖闭合；只改变位置与持有人。</p></div><div class="fact-card"><span>CONTINUITY</span><b>EP02 incoming</b><p>继承 PSTATE-001-B，不重新创建母带身份。</p></div></div><div class="continuity-route"><div>档案架<br><b>PSTATE-001-A</b><br>盒盖闭合</div><i>→</i><div>许真右侧外套内袋<br><b>PSTATE-001-B</b><br>盒盖闭合</div><i>→</i><div>EP02 incoming<br><b>沿用 B</b><br>持有人：许真</div></div></section><section class="reader-section"><h3>应用边界</h3><p>ChangeSet 应用后直接形成当前视觉设定版本；不再增加第二次整体接受。</p></section>`;
    if (key === "visual") return `<section class="reader-section"><h3>${episode.id} · 四类视觉事实</h3><p>当前版本 v${doc.version} 记录本集 Character+Look、Location+View、Prop+State 与 incoming/outgoing 连续性；来源为 ${esc(doc.source)}。</p></section>`;
    if (key === "image") return `<section class="reader-section"><h3>${episode.id}-IMG-PROP-001 · 资产图片提示词</h3><p>本集可复制的角色、地点和关键道具文本规格；来源为 ${esc(doc.source)}。这是文本，不是 REF 媒体。</p></section><section class="reader-section"><h3>身份边界</h3><p>只有生产中心生成并通过结果审阅后，真实媒体才能进入参考槽位。</p></section>`;
    if (key === "storyboard") return `<section class="reader-section"><h3>${shot} · ${esc(episode.title)}</h3><p>当前分镜 v${doc.version} 记录镜头职责、起点、终点与声音职责。</p></section><section class="reader-section"><h3>${episode.id}-FROZEN-004 · 冻结关键帧</h3><p>只投影本镜起点，不提前出现动作结果。</p></section>`;
    return `<section class="reader-section"><h3>${episode.id}-MOTION-004 · 单镜视频提示词</h3><p>从 ${episode.id}-FROZEN-004 出发，只写这一镜发生的变化；来源为 ${esc(doc.source)}。</p></section><section class="reader-section"><h3>边界</h3><p>不重复静帧外观，不跨到下一镜。静态漫剧不会创建这份文档。</p></section>`;
  }

  function inspector(episode, doc) {
    const task = doc.task;
    const taskCopy = task ? { queued: "Task 已排队，等待 Codex claim。", running: "Run 正在生成 ChangeSet；当前事实未改变。", submitted: "ChangeSet 已提交，等待创作者审阅。", failed: "Run 失败；可见事实未改变。", conflict: "revision 已变化；刷新上下文后重新提交。" }[task] : "没有运行中的 Codex Task。";
    let actions = "";
    if (task === "queued") actions = `<button class="primary" data-action="task-run">模拟 Codex 领取</button>`;
    else if (task === "running") actions = `<button class="primary" data-action="task-submit">模拟提交</button><button class="secondary" data-action="task-fail">模拟失败</button>`;
    else if (task === "failed") actions = `<button class="primary" data-action="task-retry">重新排队</button>`;
    else if (task === "conflict") actions = `<button class="primary" data-action="task-refresh">刷新上下文</button>`;
    else if (task === "submitted" && state.doc === "screenplay") actions = `<button class="primary" data-action="formal-accept">整体接受</button><button class="danger" data-action="formal-reject">退回并说明</button>`;
    else if (task === "submitted") actions = `<button class="primary" data-action="apply-changeset">应用 ChangeSet</button><button class="secondary" data-action="task-conflict">模拟冲突</button>`;
    else if (doc.state === "rejected" && state.doc === "screenplay") actions = `<button class="primary" data-action="new-version">基于反馈创建 v${doc.version + 1}</button>`;
    else if (doc.state === "stale" && canCreateDoc(episode, state.doc)) actions = `<button class="primary" data-action="stale-revise">基于最新上游创建修订</button>`;
    else if (doc.state === "missing" && canCreateDoc(episode, state.doc)) actions = `<button class="primary" data-action="create-task">创建 ${esc(docLabels[state.doc])} Task</button>`;
    else if (["missing", "stale"].includes(doc.state)) actions = `<p class="await-line">BLOCKED · ${state.doc === "visual" ? "等待已接受剧本" : ["image", "storyboard"].includes(state.doc) ? "等待当前视觉设定" : "等待当前分镜"}</p>`;
    return `<aside class="doc-inspector"><div><span class="eyebrow">Task / ChangeSet</span><h2>协作台账</h2><div class="task-ticket"><header><b>${task ? `TASK-${episode.id}-${state.doc.toUpperCase()}` : "TASK READY"}</b>${chip(task || "ready")}</header><p>${taskCopy}</p><div class="button-stack">${actions}</div></div><div class="mini-ledger"><div><span>文档状态</span><b>${status(doc.state)}</b></div><div><span>来源版本</span><b>${esc(doc.source || "尚无")}</b></div><div><span>审批规则</span><b>${state.doc === "screenplay" ? "整体审批" : "应用即 current"}</b></div></div><button class="secondary" data-action="request-review">按需请求审查</button>${state.reviewReport ? `<div class="review-report"><h3>REVIEW REPORT · 按需</h3><p>PROP-001 在分镜与图片提示词中均引用 PSTATE-001-B；EP02 incoming 连续性成立。未自动阻断当前文档。</p></div>` : ""}</div>${state.doc === "screenplay" && doc.history ? `<div><span class="eyebrow">Version history</span>${[...doc.history].reverse().map((item) => `<div class="history-note"><b>v${item.version} · ${status(item.state)}</b><span>${esc(item.source)}</span>${item.feedback ? `<p>退回反馈：${esc(item.feedback)}</p>` : ""}</div>`).join("")}</div>` : ""}</aside>`;
  }

  function episodeView() {
    const episode = currentEpisode();
    const doc = currentDoc();
    return `<main class="creator-page" data-screen-label="单集五文档工作台"><header class="creator-head"><div><div class="episode-topline"><span class="eyebrow">Episode layer · ${episode.id}</span>${chip(doc.state)}</div><h1>${episode.title}</h1><p>五份创作文档按依赖展开；图片提示词与分镜是兄弟分支，不互相等待。</p></div><div class="head-actions"><div class="format-switch" aria-label="制作形态"><button class="${episode.format === "dynamic" ? "active" : ""}" data-action="format" data-format="dynamic">动态漫剧</button><button class="${episode.format === "static" ? "active" : ""}" data-action="format" data-format="static">静态漫剧</button></div><button class="secondary" data-action="view" data-view="overview">查看全季</button></div></header>${branchMap(episode)}<section class="doc-workspace"><nav class="doc-list" aria-label="本集创作文档"><header><span class="eyebrow">Five documents · on demand</span><h2>${episode.id} 文档</h2></header>${Object.keys(docLabels).map((key, index) => { const item = episode.docs[key]; return `<button class="doc-row ${state.doc === key ? "active" : ""}" data-action="doc" data-doc="${key}"><i>${String(index + 1).padStart(2, "0")}</i><span><b>${docLabels[key]}</b><small>${item.version ? `版本 v${item.version}` : "尚未创建"}</small></span>${chip(item.state)}</button>`; }).join("")}</nav><article class="doc-reader"><header class="reader-title"><div><span class="eyebrow">${esc(doc.source || `${episode.id} · 尚无当前来源`)}</span><h2>${docLabels[state.doc]}</h2><p>${state.doc === "screenplay" ? "正式创作审批对象" : "ChangeSet 应用后形成当前版本"}</p></div><span class="version-stamp">${doc.version ? `v${doc.version}` : "—"}</span></header>${readerBody(episode, state.doc, doc)}</article>${inspector(episode, doc)}</section></main>`;
  }

  function manifestItems(episode = currentEpisode()) {
    const { screenplay, image, storyboard, video } = episode.docs;
    const items = [
      { kind: "asset_ref", label: `${episode.title} · 人物造型参考`, source: `${episode.id} 图片提示词 v${image.version || "—"} / IMG-CHAR-001`, refs: "none", adapter: "gpt-image-2", params: "1536×2048 · 9:16", output: `${episode.id}/制作成果/REF-001.png`, cost: "1 image unit", resultRef: "REF-001" },
      { kind: "asset_ref", label: `${episode.title} · 关键道具参考`, source: `${episode.id} 图片提示词 v${image.version || "—"} / IMG-PROP-001`, refs: "none", adapter: "gpt-image-2", params: "2048×1536 · 4:3", output: `${episode.id}/制作成果/REF-003.png`, cost: "1 image unit", resultRef: "REF-003" },
      { kind: "frozen_keyframe", label: `${episode.id}-SHOT-004 起始帧`, source: `${episode.id} 分镜 v${storyboard.version || "—"} / FROZEN-004`, refs: "REF-001 + REF-003", adapter: "gpt-image-2", params: "1080×1920 · 9:16", output: `${episode.id}/制作成果/KF-004.png`, cost: "1 image unit", needsRefs: ["REF-001", "REF-003"] },
      { kind: "tts", label: `${episode.title} · 对白与声音时间点`, source: `${episode.id} 剧本 v${screenplay.version || "—"} + 分镜 v${storyboard.version || "—"}`, refs: "VOICE-CHAR-001", adapter: "tts-local", params: "48kHz · mono", output: `${episode.id}/制作成果/AUDIO-004.wav`, cost: "8 audio seconds" }
    ];
    if (episode.format === "dynamic") items.splice(3, 0, { kind: "video", label: `${episode.id}-SHOT-004 动态镜头`, source: `${episode.id} 视频提示词 v${video.version || "—"} / MOTION-004`, refs: "KF-004", adapter: "seedance", params: "6s · 1080×1920 · 24fps", output: `${episode.id}/制作成果/SHOT-004.mp4`, cost: "6 video seconds", needsJob: "frozen_keyframe" });
    return items;
  }

  function reconcileJobs(episode) {
    const approvedRefs = new Set(episode.refs.filter((ref) => ref.state === "approved").map((ref) => ref.id));
    const keyframe = episode.production.jobs.find((job) => job.kind === "frozen_keyframe");
    if (keyframe?.state === "blocked" && keyframe.needsRefs.every((id) => approvedRefs.has(id))) keyframe.state = "queued";
    const video = episode.production.jobs.find((job) => job.kind === "video");
    if (video?.state === "blocked" && keyframe?.state === "current") video.state = "queued";
  }

  function productionView() {
    const episode = currentEpisode(), production = episode.production, items = manifestItems(episode), ready = productionReady(episode);
    const hash = production.inputHash || "等待 prepare";
    const rows = production.jobs.length ? production.jobs : items.map((item) => ({ ...item, id: "—", state: ready ? "missing" : "blocked" }));
    const sourceVersions = `剧本 v${episode.docs.screenplay.version || "—"} · 图片提示词 v${episode.docs.image.version || "—"} · 分镜 v${episode.docs.storyboard.version || "—"}${episode.format === "dynamic" ? ` · 视频提示词 v${episode.docs.video.version || "—"}` : " · 视频提示词 SKIPPED"}`;
    const jobAction = (job) => job.state === "failed" ? `<button class="secondary" data-action="retry-production">重新准备</button>` : job.state === "review" && job.kind === "asset_ref" ? `<button class="secondary" data-action="approve-job-ref" data-id="${job.id}" data-ref="${job.resultRef}">提交 REF 审阅</button>` : job.state === "queued" ? `<button class="secondary" data-action="complete-job" data-id="${job.id}">模拟完成</button>` : job.state === "review" ? `<button class="secondary" data-action="approve-job" data-id="${job.id}">批准结果</button>` : "";
    return `<main class="creator-page" data-screen-label="统一生产中心"><header class="creator-head"><div><span class="eyebrow">Production center · ${episode.id}</span><h1>${episode.id} · 准确预览，然后明确确认</h1><p>Lookdev、资产参考图、冻结关键帧、视频、TTS 与音乐都在这里创建；创作文档本身不会调用供应商。</p></div><div class="head-actions">${chip(ready ? (production.confirmed ? "confirmed" : production.prepared ? "prepared" : "ready") : "blocked")}<button class="secondary" data-action="invalidate">修改输出参数</button></div></header><div class="fact-grid"><div class="fact-card"><span>LOOKDEV_FRAME · TEXT</span><b>项目级视觉方向测试</b><p>可选文本规格；不是每集资产，也不是生产结果。</p></div><div class="fact-card"><span>ASSET IMAGE PROMPT · TEXT</span><b>固定人物、地点、道具事实</b><p>来自图片提示词文档；本身没有共享像素依据。</p></div><div class="fact-card"><span>FROZEN KEYFRAME · TEXT</span><b>只投影单镜起点</b><p>属于分镜，不得提前出现动作结果。</p></div><div class="fact-card"><span>REF MEDIA · REAL</span><b>生产并审阅通过的媒体</b><p>只有真实 REF 文件可作为后续生成 references。</p></div></div><section class="production-layout"><div><div class="production-ledger"><header><span>JOB / KIND</span><span>准确输入</span><span>Adapter / 参数</span><span>状态</span></header>${rows.map((job) => `<div class="job-row"><div><b>${esc(job.id || "—")}</b><p>${esc(job.kind)}</p></div><div><b>${esc(job.label)}</b><p>${esc(job.source)}<br>refs: ${esc(job.refs)}<br>output: ${esc(job.output)}</p></div><div><code>${esc(job.adapter)}</code><p>${esc(job.params)}<br>${esc(job.cost)}</p></div><div>${chip(job.state)}${jobAction(job)}</div></div>`).join("")}</div><section><header class="matrix-head"><div><span class="eyebrow">Media truth · ${episode.id}</span><h2>真实参考图槽位</h2></div><span>文本提示词 ≠ REF 媒体</span></header><div class="media-review">${episode.refs.map((ref) => `<div class="media-card"><div class="media-art"><span>${esc(ref.id)}</span></div><b>${esc(ref.label)}</b><small>${ref.state === "approved" ? "真实媒体已通过审阅" : ref.state === "review" ? "生产结果等待创作者审阅" : "尚未生成真实媒体"}</small>${chip(ref.state === "approved" ? "current" : ref.state)}${ref.state === "review" ? `<button class="secondary" data-action="approve-ref" data-ref="${ref.id}">批准进入 REF 槽位</button>` : ""}</div>`).join("")}</div></section></div><aside class="manifest-panel"><span class="eyebrow">Prepared manifest</span><h2>${episode.id} · ${items.length} 个任务</h2><p>${production.invalidated ? `输入发生变化，旧确认已经失效。${ready ? "必须重新 prepare。" : "创作输入也尚未就绪。"}` : !ready ? "创作输入未就绪，不能 Prepare。" : "精确输入、输出和计费单位在确认前固定。"}</p>${[["来源版本", sourceVersions], ["Input hash", hash], ["Adapter", [...new Set(items.map((item) => item.adapter))].join(" · ")], ["输出根目录", `${episode.id}/制作成果/`], ["确认键", production.confirmKey || "尚未确认"]].map(([label, value]) => `<div class="manifest-line"><span>${label}</span><code>${esc(value)}</code></div>`).join("")}<div class="manifest-total">${items.length} jobs · ${episode.format === "dynamic" ? "3 image units · 6 video seconds · 8 audio seconds" : "3 image units · 8 audio seconds"}</div><div class="button-stack">${!ready ? `<button class="primary" disabled>BLOCKED · 等待当前创作输入</button>` : !production.prepared ? `<button class="primary" data-action="prepare-production">Prepare 精确预览</button>` : !production.confirmed ? `<button class="danger" data-action="open-confirm">确认这份输入并创建任务</button>` : `<button class="primary" data-action="open-confirm">${items.length} 个任务已创建 · 查看确认</button>`}<button class="secondary" data-action="simulate-production-fail">模拟一个任务失败</button></div><p class="await-line">失败重试不会直接 run；必须形成新的 prepare 与确认。</p></aside></section></main>`;
  }

  function modal() {
    if (!state.modal) return "";
    if (state.modal === "reject" || state.modal === "development-reject") {
      const development = state.modal === "development-reject", version = development ? state.front.developmentVersion : currentDoc().version;
      return `<div class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="reject-title"><span class="eyebrow">Formal document review</span><h2 id="reject-title">退回整份${development ? "故事开发文档" : "剧本"} v${version}</h2><p>反馈与被拒版本一起进入不可变历史。系统不会自动创建下一版。</p><label for="reject-feedback">退回反馈</label><textarea id="reject-feedback" data-field="reject-feedback"></textarea><div class="modal-actions"><button class="secondary" data-action="close-modal">取消</button><button class="danger" data-action="confirm-reject">确认退回</button></div></section></div>`;
    }
    const episode = currentEpisode(), production = episode.production, items = manifestItems(episode);
    return `<div class="modal-backdrop"><section class="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><span class="eyebrow">Explicit production confirmation</span><h2 id="confirm-title">${production.confirmed ? `${episode.id} 任务已经创建` : `确认 ${episode.id} 的 ${items.length} 个外部生产任务`}</h2><p>${production.confirmed ? "相同 confirm key 的重复确认不会创建新任务。" : "本次确认只对当前 input hash 有效。内容、参考、参数或输出变化都会使它失效。"}</p><div class="confirm-list">${items.map((item) => `<div><b>${esc(item.kind)} · ${esc(item.label)}</b><p>${esc(item.source)} · refs ${esc(item.refs)}<br>${esc(item.adapter)} · ${esc(item.params)} · ${esc(item.output)} · ${esc(item.cost)}</p></div>`).join("")}</div><div class="modal-actions"><button class="secondary" data-action="close-modal">${production.confirmed ? "关闭" : "返回修改"}</button>${production.confirmed ? "" : `<button class="danger" data-action="confirm-production">我已核对，创建任务</button>`}</div></section></div>`;
  }

  function scenarios() {
    const scenarios = [["idea", "想法 → 开发 → 单集"], ["original", "原著 · 可选分析"], ["existing", "现成多集剧本直达"], ["parallel", "图片提示词 / 分镜并行"], ["static", "静态漫剧跳过视频提示词"], ["running", "Task running → submit"], ["failed", "Task running → fail → retry"], ["reject", "正式剧本退回与新版本"], ["stale", "接受新剧本 → 下游 stale"], ["reference", "参考图生成与结果审阅"], ["production", "生产确认与幂等"], ["invalidated", "参数变化 → 确认失效"]];
    return `<div class="scenario-dock"><button class="scenario-toggle" data-action="toggle-scenarios" aria-expanded="${state.scenarioOpen}">SCENARIOS</button>${state.scenarioOpen ? `<div class="scenario-menu"><h3>DETERMINISTIC PATHS</h3>${scenarios.map(([id, label]) => `<button data-action="scenario" data-scenario="${id}">${label}</button>`).join("")}<button data-action="reset">恢复默认状态</button></div>` : ""}</div>`;
  }

  function render() {
    document.documentElement.dataset.theme = state.theme;
    const content = state.view === "overview" ? overview() : state.view === "episode" ? episodeView() : productionView();
    root.innerHTML = `<div class="creator-app">${masthead()}${layerNav()}<div class="creator-shell">${sidebar()}<div class="creator-main">${content}</div></div>${modal()}${scenarios()}${state.notice ? `<div class="notice" role="status">${esc(state.notice)}</div>` : ""}</div>`;
    if (state.modal) requestAnimationFrame(() => document.querySelector(".modal textarea, .modal button")?.focus());
    else if (focusReturn) requestAnimationFrame(() => { document.querySelector(focusReturn)?.focus(); focusReturn = ""; });
  }

  function openModal(kind, trigger) {
    focusReturn = `[data-action="${trigger.dataset.action}"]`;
    state.modal = kind;
  }

  function closeModal() {
    state.modal = null;
    render();
  }

  function setRoute(route) {
    state.route = route;
    if (route === "idea") { state.front = { source: "故事想法 v1", analysis: "skipped", development: "accepted", developmentVersion: 1, developmentHistory: [{ version: 1, state: "accepted", source: "故事想法 v1" }], map: "current" }; state.episodes = clone(episodeSeed); }
    else if (route === "original") {
      state.front = { source: "合法持有原著快照 v1", analysis: "missing", development: "missing", developmentVersion: 0, developmentHistory: [], map: "missing" };
      state.episodes = clone(episodeSeed).map((episode) => ({ ...episode, docs: Object.fromEntries(Object.keys(docLabels).map((key) => [key, { state: key === "video" && episode.format === "static" ? "skipped" : "missing", version: null, task: null, source: key === "video" && episode.format === "static" ? "静态漫剧不需要" : "等待上游", ...(key === "screenplay" ? { history: [] } : {}) }])), production: productionRecord(), refs: refRecords() }));
    }
    else {
      state.front = { source: "现成多集剧本原文 v1", analysis: "skipped", development: "skipped", developmentVersion: 0, developmentHistory: [], map: "current" };
      state.episodes = clone(episodeSeed);
      state.episodes.forEach((episode) => {
        const screenplay = episode.docs.screenplay;
        if (screenplay.state === "accepted") { screenplay.source = "现成多集剧本原文 v1"; screenplay.history = [{ version: screenplay.version, state: "accepted", source: "导入剧本原文 v1" }]; }
        else if (screenplay.state === "submitted") { screenplay.source = "导入剧本原文 v1 + 规范化修订"; screenplay.history = [{ version: 1, state: "rejected", source: "导入剧本原文 v1", feedback: "主角越界缺少足够代价。" }, { version: screenplay.version, state: "submitted", source: "导入剧本原文 v1 + 退回反馈" }]; }
      });
    }
    state.episode = "EP01";
    state.view = "overview";
    notify("入口路径已切换；未使用的前期阶段没有被创建");
  }

  function staleDownstream(episode, screenplayVersion) {
    ["visual", "image", "storyboard", "video"].forEach((key) => {
      if (episode.docs[key].state !== "missing" && episode.docs[key].state !== "skipped") episode.docs[key].state = "stale";
    });
    invalidateProduction(episode, "剧本来源版本已变化");
    notify(`剧本 v${screenplayVersion} 已接受；所有仍引用旧来源的下游已精确标记 STALE`);
  }

  root.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    const action = button.dataset.action;
    if (action === "theme") state.theme = state.theme === "light" ? "dark" : "light";
    else if (action === "view") { state.view = button.dataset.view; if (state.view === "episode" && currentEpisode().docs.screenplay.state === "missing") state.doc = "screenplay"; }
    else if (action === "route") setRoute(button.dataset.route);
    else if (action === "episode") { state.episode = button.dataset.episode; state.view = "episode"; }
    else if (action === "matrix-doc") { state.episode = button.dataset.episode; state.doc = button.dataset.doc; state.view = "episode"; }
    else if (action === "matrix-production") { state.episode = button.dataset.episode; state.view = "production"; }
    else if (action === "doc") state.doc = button.dataset.doc;
    else if (action === "format") {
      const episode = currentEpisode(), nextFormat = button.dataset.format, video = episode.docs.video;
      if (episode.format !== nextFormat) {
        if (nextFormat === "static") { if (video.state !== "skipped") video.saved = { state: video.state, version: video.version, task: video.task, source: video.source }; video.state = "skipped"; video.task = null; video.source = "静态漫剧不需要"; }
        else if (video.saved) { Object.assign(video, video.saved); delete video.saved; }
        else { video.state = "missing"; video.task = null; video.source = "等待当前分镜"; }
        episode.format = nextFormat;
        notify(`${invalidateProduction(episode, "制作形态已变化")}；${episode.format === "static" ? "视频提示词历史已保留并标记 SKIPPED" : "动态视频提示词状态已恢复"}`);
      }
    }
    else if (action === "analysis-start") { state.front.analysis = "queued"; notify("原著分析 Task 已排队；尚未产生分析文档"); }
    else if (action === "analysis-complete") { state.front.analysis = "current"; state.front.development = "missing"; notify("原著分析已成为可选当前材料；下一步仍由创作者决定"); }
    else if (action === "analysis-skip") { state.front.analysis = "skipped"; state.front.development = "missing"; notify("原著分析已跳过；可以按需创建故事开发"); }
    else if (action === "development-start") { state.front.development = "queued"; notify("故事开发 Task 已排队；当前还没有可审批正文"); }
    else if (action === "development-run") state.front.development = "running";
    else if (action === "development-submit") {
      state.front.developmentVersion = state.front.developmentVersion || 1; state.front.development = "submitted";
      if (!state.front.developmentHistory.some((item) => item.version === state.front.developmentVersion)) state.front.developmentHistory.push({ version: state.front.developmentVersion, state: "submitted", source: state.front.analysis === "current" ? "原著分析当前版本" : state.front.source });
      notify(`故事开发文档 v${state.front.developmentVersion} 已提交，等待整体审批`);
    }
    else if (action === "development-accept") { state.front.development = "accepted"; state.front.developmentHistory.find((item) => item.version === state.front.developmentVersion).state = "accepted"; notify(`故事开发文档 v${state.front.developmentVersion} 已整体接受，可以进入单集写作`); }
    else if (action === "development-reject") openModal("development-reject", button);
    else if (action === "development-new-version") { state.front.developmentVersion += 1; state.front.development = "queued"; notify(`故事开发文档 v${state.front.developmentVersion} 空修订已创建并排队；旧版本保持不变`); }
    else if (action === "create-task") { currentDoc().task = "queued"; notify(`${docLabels[state.doc]} Task 已排队；尚未产生文档版本`); }
    else if (action === "task-run") currentDoc().task = "running";
    else if (action === "task-submit") { const doc = currentDoc(); doc.version = doc.version || 1; doc.task = "submitted"; doc.state = state.doc === "screenplay" ? "submitted" : "proposal"; notify("ChangeSet 已提交；当前事实仍未改变"); }
    else if (action === "task-fail") { currentDoc().task = "failed"; notify("Run 已失败；没有修改当前文档"); }
    else if (action === "task-retry" || action === "task-refresh") { currentDoc().task = "queued"; notify("已创建新的 Run；旧失败记录保留"); }
    else if (action === "task-conflict") currentDoc().task = "conflict";
    else if (action === "apply-changeset") {
      const episode = currentEpisode(), doc = currentDoc(); doc.task = null; doc.state = "current";
      staleDependents(episode, state.doc);
      const invalidated = ["image", "storyboard", "video"].includes(state.doc) ? ` ${invalidateProduction(episode)}。` : state.doc === "visual" ? ` ${invalidateProduction(episode, "视觉来源已变化")}。` : "";
      notify(`${docLabels[state.doc]} v${doc.version} 已成为当前版本；没有第二次接受门槛。${invalidated}`);
    }
    else if (action === "formal-accept") {
      const episode = currentEpisode(), doc = currentDoc(); doc.task = null; doc.state = "accepted";
      if (!doc.history.some((item) => item.version === doc.version)) doc.history.push({ version: doc.version, state: "accepted", source: doc.source || "Codex ChangeSet" });
      else doc.history.find((item) => item.version === doc.version).state = "accepted";
      staleDownstream(episode, doc.version);
    }
    else if (action === "formal-reject") openModal("reject", button);
    else if (action === "confirm-reject") {
      const feedback = document.getElementById("reject-feedback").value.trim();
      if (!feedback) { document.getElementById("reject-feedback").focus(); return; }
      if (state.modal === "development-reject") {
        const item = state.front.developmentHistory.find((entry) => entry.version === state.front.developmentVersion); Object.assign(item, { state: "rejected", feedback }); state.front.development = "rejected";
        focusReturn = "[data-action=\"development-new-version\"]";
        state.modal = null; notify(`故事开发文档 v${state.front.developmentVersion} 已退回；反馈与历史已保留`);
      } else {
        const doc = currentDoc(); doc.task = null; doc.state = "rejected";
        const found = doc.history.find((item) => item.version === doc.version);
        if (found) Object.assign(found, { state: "rejected", feedback }); else doc.history.push({ version: doc.version, state: "rejected", source: doc.source, feedback });
        focusReturn = "[data-action=\"new-version\"]";
        state.modal = null; notify(`剧本 v${doc.version} 已退回；反馈与历史已保留`);
      }
    }
    else if (action === "new-version") { const doc = currentDoc(); doc.version += 1; doc.task = "queued"; doc.source = `被拒版本 v${doc.version - 1} + 退回反馈`; notify(`v${doc.version} 空修订已创建并排队；没有伪造提交正文`); }
    else if (action === "stale-revise") { const doc = currentDoc(); doc.version = (doc.version || 0) + 1; doc.task = "queued"; doc.source = `${currentEpisode().id} 剧本 v${currentEpisode().docs.screenplay.version}`; notify("基于最新上游创建了修订任务；旧版本仍为 STALE"); }
    else if (action === "request-review") { state.reviewReport = true; notify("审查报告已生成；这是按需能力，不是固定阶段"); }
    else if (action === "prepare-production") {
      const episode = currentEpisode(), production = currentProduction(), items = manifestItems(episode);
      if (!productionReady(episode)) { notify(`${episode.id} 创作输入未就绪，不能 Prepare`); }
      else {
        production.prepared = true; production.confirmed = false; production.invalidated = false; production.revision += 1; production.inputHash = fingerprint(items); production.confirmKey = ""; production.jobs = [];
        notify(`${episode.id} manifest 已冻结；等待明确确认，没有调用外部 adapter`);
      }
    }
    else if (action === "retry-production") { notify(invalidateProduction(currentEpisode(), "失败重试需要新的 prepare 与确认")); }
    else if (action === "open-confirm") openModal("production", button);
    else if (action === "confirm-production") {
      const episode = currentEpisode(), production = currentProduction();
      if (!production.confirmed && production.prepared && productionReady(episode)) {
        const items = manifestItems(episode); production.confirmKey = `confirm-${episode.id}-${production.inputHash.slice(-8)}`;
        production.jobs = items.map((item, index) => ({ ...item, id: `JOB-${episode.id}-${String(index + 1).padStart(3, "0")}`, state: item.kind === "asset_ref" ? "review" : item.kind === "frozen_keyframe" || item.kind === "video" ? "blocked" : "queued" }));
        production.confirmed = true;
      }
      state.modal = null; notify(`${episode.id} 的 ${production.jobs.length} 个稳定 Job ID 已创建；重复确认不会新增任务`);
    }
    else if (action === "invalidate") {
      notify(invalidateProduction(currentEpisode(), "输出参数已改变"));
    }
    else if (action === "simulate-production-fail") {
      const jobs = currentProduction().jobs;
      if (!jobs.length) notify("尚无已确认任务；请先 prepare 并确认");
      else { jobs[0].state = "failed"; notify("任务失败；直接重试已被禁止"); }
    }
    else if (action === "approve-job-ref") { const ref = currentEpisode().refs.find((item) => item.id === button.dataset.ref); ref.state = "review"; notify(`${currentEpisode().id} 生产结果已进入 ${ref.id} 审阅槽位`); }
    else if (action === "approve-ref") {
      const episode = currentEpisode(), ref = episode.refs.find((item) => item.id === button.dataset.ref); ref.state = "approved";
      const assetJob = episode.production.jobs.find((job) => job.resultRef === ref.id); if (assetJob) assetJob.state = "current"; reconcileJobs(episode); notify(`${episode.id} ${ref.id} 已成为真实、可引用的 REF 媒体`);
    }
    else if (action === "complete-job") { const episode = currentEpisode(), job = episode.production.jobs.find((item) => item.id === button.dataset.id); job.state = "review"; notify(`${job.id} 已完成，等待结果审阅`); }
    else if (action === "approve-job") { const episode = currentEpisode(), job = episode.production.jobs.find((item) => item.id === button.dataset.id); job.state = "current"; reconcileJobs(episode); notify(`${job.id} 结果已批准`); }
    else if (action === "close-modal") { closeModal(); return; }
    else if (action === "toggle-scenarios") state.scenarioOpen = !state.scenarioOpen;
    else if (action === "reset") state = clone(initialState);
    else if (action === "command") notify(`当前路由：${state.view === "overview" ? "short-drama / develop" : state.view === "episode" ? `short-drama-${state.doc === "visual" ? "assets" : state.doc}` : "short-drama-produce"}`);
    else if (action === "scenario") runScenario(button.dataset.scenario);
    render();
  });

  function runScenario(id) {
    state.scenarioOpen = false;
    if (["idea", "original", "existing"].includes(id)) { setRoute(id); return; }
    state.episode = id === "static" ? "EP02" : "EP01";
    if (id === "parallel") { state.view = "episode"; state.doc = "image"; notify("图片提示词与分镜都从视觉设定分支，可并行创建"); }
    else if (id === "static") { currentEpisode().docs.storyboard.state = "current"; currentEpisode().docs.storyboard.task = null; state.view = "episode"; state.doc = "video"; notify("静态漫剧的视频提示词是 SKIPPED，不是 MISSING"); }
    else if (id === "running") { state.view = "episode"; state.doc = "video"; const doc = currentDoc(); doc.task = "running"; doc.state = "proposal"; }
    else if (id === "failed") { state.view = "episode"; state.doc = "video"; const doc = currentDoc(); doc.task = "failed"; doc.state = "proposal"; }
    else if (id === "reject") { state.episode = "EP03"; state.view = "episode"; state.doc = "screenplay"; }
    else if (id === "stale") { state.episode = "EP01"; const doc = currentEpisode().docs.screenplay; doc.version = 2; doc.state = "submitted"; doc.task = "submitted"; doc.source = "EP01 剧本 v1 修订"; state.view = "episode"; state.doc = "screenplay"; notify("接受剧本 v2 后可观察精确 stale 传播"); }
    else if (id === "reference") { state.view = "production"; currentEpisode().refs[1].state = "review"; }
    else if (id === "production") { const production = currentProduction(), items = manifestItems(); state.view = "production"; production.prepared = true; production.inputHash = fingerprint(items); production.revision = 1; }
    else if (id === "invalidated") { state.view = "production"; invalidateProduction(currentEpisode(), "演示输入变化"); }
  }

  window.addEventListener("keydown", (event) => {
    if (state.modal) {
      if (event.key === "Escape") { event.preventDefault(); closeModal(); return; }
      if (event.key === "Tab") {
        const focusable = [...document.querySelectorAll(".modal button:not([disabled]), .modal textarea")];
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); notify("Skill 路由已根据当前文档准备；仍需用户触发 Task"); render(); }
  });

  render();
})();
