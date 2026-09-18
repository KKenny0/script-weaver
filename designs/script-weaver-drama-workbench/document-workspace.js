(function () {
  "use strict";

  const app = document.getElementById("app");
  const docLabels = {
    screenplay: "剧本",
    visual: "视觉设定",
    image: "图片提示词",
    storyboard: "分镜",
    video: "视频提示词"
  };
  const docOrder = Object.keys(docLabels);
  const clone = (value) => JSON.parse(JSON.stringify(value));

  const makeProduction = (episodeId, refState = "approved") => ({
    prepared: false,
    confirmed: false,
    invalidated: false,
    inputHash: "",
    confirmKey: "",
    outputPreset: "vertical-standard",
    jobs: [],
    previousJobs: [],
    refs: [
      { id: "REF-001", label: "许真深夜造型", kind: "character", state: refState, path: `${episodeId}/制作成果/REF-001.png` },
      { id: "REF-002", label: "档案间夜态", kind: "location", state: refState, path: `${episodeId}/制作成果/REF-002.png` },
      { id: "REF-003", label: "无日期母带", kind: "prop", state: refState, path: `${episodeId}/制作成果/REF-003.png` }
    ]
  });

  const episodes = [
    {
      id: "EP01",
      title: "没有寄出的声音",
      format: "dynamic",
      docs: {
        screenplay: { state: "accepted", version: 2, source: "故事开发文档 v2", task: null, history: [{ version: 1, state: "rejected", feedback: "让母带出现得更早。" }, { version: 2, state: "accepted" }] },
        visual: { state: "current", version: 2, source: "EP01 剧本 v2", task: "submitted", proposal: { id: "CS-EP01-VIS-003", version: 3, summary: "明确母带进入右侧内袋后的连续性", before: "许真把母带藏进外套。", after: "许真将盒盖闭合的母带放入右侧外套内袋。" } },
        image: { state: "current", version: 1, source: "视觉设定 v2", task: null },
        storyboard: { state: "current", version: 2, source: "视觉设定 v2", task: null },
        video: { state: "current", version: 1, source: "分镜 v2", task: null }
      },
      production: makeProduction("EP01")
    },
    {
      id: "EP02",
      title: "被剪断的雨声",
      format: "static",
      docs: {
        screenplay: { state: "accepted", version: 1, source: "故事开发文档 v2", task: null, history: [{ version: 1, state: "accepted" }] },
        visual: { state: "current", version: 1, source: "EP02 剧本 v1", task: null },
        image: { state: "current", version: 1, source: "视觉设定 v1", task: null },
        storyboard: { state: "current", version: 1, source: "视觉设定 v1", task: null },
        video: { state: "skipped", version: null, source: "静态漫剧不需要视频提示词", task: null }
      },
      production: makeProduction("EP02")
    },
    {
      id: "EP03",
      title: "第一次篡改",
      format: "dynamic",
      docs: {
        screenplay: { state: "submitted", version: 2, source: "剧本 v1 + 退回反馈", task: "submitted", history: [{ version: 1, state: "rejected", feedback: "主角越界缺少足够代价。" }, { version: 2, state: "submitted" }] },
        visual: { state: "stale", version: 1, source: "EP03 剧本 v1", task: null },
        image: { state: "stale", version: 1, source: "视觉设定 v1", task: null },
        storyboard: { state: "stale", version: 1, source: "视觉设定 v1", task: null },
        video: { state: "missing", version: null, source: "等待当前分镜", task: null }
      },
      production: makeProduction("EP03")
    },
    {
      id: "EP04",
      title: "销毁请求",
      format: "dynamic",
      docs: {
        screenplay: { state: "missing", version: null, source: "等待创建", task: null, history: [] },
        visual: { state: "missing", version: null, source: "等待已接受剧本", task: null },
        image: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        storyboard: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        video: { state: "missing", version: null, source: "等待当前分镜", task: null }
      },
      production: makeProduction("EP04")
    },
    {
      id: "EP05",
      title: "无辜的人",
      format: "dynamic",
      docs: {
        screenplay: { state: "missing", version: null, source: "等待创建", task: null, history: [] },
        visual: { state: "missing", version: null, source: "等待已接受剧本", task: null },
        image: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        storyboard: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        video: { state: "missing", version: null, source: "等待当前分镜", task: null }
      },
      production: makeProduction("EP05")
    },
    {
      id: "EP06",
      title: "她的录音",
      format: "static",
      docs: {
        screenplay: { state: "missing", version: null, source: "等待创建", task: null, history: [] },
        visual: { state: "missing", version: null, source: "等待已接受剧本", task: null },
        image: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        storyboard: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        video: { state: "skipped", version: null, source: "静态漫剧不需要视频提示词", task: null }
      },
      production: makeProduction("EP06")
    }
  ];

  function freshEpisode(id, title, screenplayState = "missing") {
    const imported = screenplayState === "submitted";
    return {
      id,
      title,
      format: "dynamic",
      docs: {
        screenplay: { state: screenplayState, version: imported ? 1 : null, source: imported ? "导入剧本原文 v1" : "等待创建", task: imported ? "submitted" : null, history: imported ? [{ version: 1, state: "submitted" }] : [] },
        visual: { state: "missing", version: null, source: "等待已接受剧本", task: null },
        image: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        storyboard: { state: "missing", version: null, source: "等待当前视觉设定", task: null },
        video: { state: "missing", version: null, source: "等待当前分镜", task: null }
      },
      production: makeProduction(id, "missing")
    };
  }

  let state = {
    theme: "light",
    projectName: "没有寄出的声音",
    sourceType: "idea",
    currentEpisode: "EP01",
    expandedEpisode: "EP01",
    selection: { scope: "doc", key: "visual" },
    railOpen: false,
    overlay: null,
    intakeType: "idea",
    intakeDraft: null,
    notice: "",
    demoContent: true,
    sourceSnapshot: Object.freeze({ version: 1, type: "idea", text: "一名声音修复师在旧母带里听见尚未发生的犯罪。" }),
    front: {
      source: "故事想法 v1",
      analysis: "未使用",
      development: {
        state: "accepted",
        version: 1,
        source: "故事想法 v1",
        task: "submitted",
        proposal: { id: "DOC-DEV-V2", version: 2, summary: "把每集的代价推进写进分集承诺" },
        history: [{ version: 1, state: "accepted" }]
      }
    },
    episodes: clone(episodes)
  };

  let returnFocus = "";
  let noticeTimer = 0;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const currentEpisode = () => state.episodes.find((episode) => episode.id === state.currentEpisode);
  const currentDoc = () => currentEpisode().docs[state.selection.key];

  function plainStatus(item) {
    if (!item) return { label: "尚未开始", tone: "warn" };
    if (item.proposal || item.state === "submitted") return { label: "有内容待确认", tone: "warn" };
    return {
      accepted: { label: "已整体确认", tone: "good" },
      current: { label: "已是当前版本", tone: "good" },
      stale: { label: "上游已更新，需同步", tone: "warn" },
      missing: { label: "尚未开始", tone: "warn" },
      skipped: { label: "本集无需", tone: "good" },
      rejected: { label: "已退回修改", tone: "danger" }
    }[item.state] || { label: "创作中", tone: "warn" };
  }

  function selectionStatus() {
    if (state.selection.scope === "overview") return { label: state.episodes.length ? `${state.episodes.length} 集 · 内容已保存` : "等待故事开发", tone: state.episodes.length ? "good" : "warn" };
    if (state.selection.scope === "front") {
      const development = state.front.development;
      if (development.state === "missing") return { label: "等待故事开发", tone: "warn" };
      if (development.state === "skipped") return { label: "现成剧本直达", tone: "good" };
      return plainStatus(development);
    }
    if (state.selection.scope === "production") {
      const production = currentEpisode().production;
      if (production.invalidated) return { label: "输入变化，需重新确认", tone: "warn" };
      if (production.confirmed) return { label: "任务已经创建", tone: "good" };
      return productionReady(currentEpisode()) ? { label: "内容就绪，可准备生产", tone: "good" } : { label: "等待创作内容", tone: "warn" };
    }
    return plainStatus(currentDoc());
  }

  function productionReady(episode) {
    return episode.docs.image.state === "current" && episode.docs.storyboard.state === "current" && (episode.format === "static" || episode.docs.video.state === "current");
  }

  function canStartDoc(episode, key) {
    if (key === "screenplay") return true;
    if (key === "visual") return episode.docs.screenplay.state === "accepted";
    if (key === "image" || key === "storyboard") return episode.docs.visual.state === "current";
    return episode.format === "dynamic" && episode.docs.storyboard.state === "current";
  }

  function docNavState(doc) {
    if (doc.proposal || doc.state === "submitted" || doc.state === "stale") return "attention";
    if (doc.state === "skipped") return "skipped";
    if (doc.state === "missing") return "waiting";
    return "current";
  }

  function routeLabel(type) {
    return { idea: "故事想法", original: "合法持有的原著", script: "现成单集或多集剧本" }[type];
  }

  function frontProgressLabel() {
    const development = state.front.development;
    if (development.proposal) return "待确认";
    if (development.state === "missing") return "未创建";
    if (development.state === "skipped") return "已跳过";
    return development.state === "accepted" ? "已确认" : "创作中";
  }

  function freshProject(name, type, sourceText) {
    const scriptRoute = type === "script";
    const freshEpisodes = scriptRoute ? [freshEpisode("EP01", "导入剧本", "submitted")] : [];
    state = {
      theme: state.theme,
      projectName: name,
      sourceType: type,
      currentEpisode: scriptRoute ? "EP01" : "",
      expandedEpisode: scriptRoute ? "EP01" : "",
      selection: scriptRoute ? { scope: "doc", key: "screenplay" } : { scope: "front" },
      railOpen: false,
      overlay: null,
      intakeType: type,
      intakeDraft: null,
      notice: "",
      demoContent: false,
      sourceSnapshot: Object.freeze({ version: 1, type, text: sourceText }),
      front: {
        source: `${routeLabel(type)}输入快照 v1`,
        analysis: type === "original" ? "可选，尚未运行" : "未使用",
        development: {
          state: scriptRoute ? "skipped" : "missing",
          version: null,
          source: scriptRoute ? "导入剧本原文 v1" : `${routeLabel(type)}输入快照 v1`,
          task: null,
          proposal: null,
          history: []
        }
      },
      episodes: freshEpisodes
    };
  }

  function contextTitle() {
    if (state.selection.scope === "overview") return "全剧";
    if (state.selection.scope === "front") return "前期材料";
    if (state.selection.scope === "production") return "制作成果";
    return docLabels[state.selection.key];
  }

  function header() {
    const status = selectionStatus();
    const episodeLabel = ["overview", "front"].includes(state.selection.scope) ? "全剧" : state.currentEpisode;
    return `
      <header class="topbar" data-screen-label="项目与当前工作状态">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">SW</span>
          <span class="brand-copy"><strong>Script Weaver</strong><small>短剧创作台</small></span>
        </div>
        <div class="project-context">
          <strong>${escapeHtml(state.projectName)}</strong>
          <span class="context-separator">/</span>
          <span>${episodeLabel} · ${escapeHtml(contextTitle())}</span>
          <span class="plain-state" data-tone="${status.tone}">${escapeHtml(status.label)}</span>
        </div>
        <div class="top-actions">
          <button class="menu-button" data-action="toggle-rail" aria-expanded="${state.railOpen}" aria-label="打开内容目录">目录</button>
          <button class="quiet-button" data-action="open-intake">新建项目</button>
          <button class="theme-button" data-action="theme" aria-label="切换明暗主题">${state.theme === "light" ? "夜" : "昼"}</button>
        </div>
      </header>`;
  }

  function rail() {
    return `
      ${state.railOpen ? `<button class="mobile-rail-backdrop" data-action="close-rail" aria-label="关闭内容目录"></button>` : ""}
      <aside class="content-rail ${state.railOpen ? "open" : ""}" data-screen-label="全剧与分集内容目录">
        <div class="rail-head"><span>CONTENTS</span><strong>本剧内容</strong></div>
        <nav class="rail-nav" aria-label="创作内容">
          <button class="rail-link ${state.selection.scope === "overview" ? "active" : ""}" data-action="select-global" data-scope="overview"><span>全剧</span><small>${state.episodes.length} 集</small></button>
          <button class="rail-link ${state.selection.scope === "front" ? "active" : ""}" data-action="select-global" data-scope="front"><span>前期材料</span><small>${frontProgressLabel()}</small></button>
          <div class="rail-divider"></div>
          ${state.episodes.map((episode) => episodeGroup(episode)).join("")}
          <div class="rail-divider mobile-new-project"></div>
          <button class="rail-link mobile-new-project" data-action="open-intake"><span>新建项目</span><small>选择起点</small></button>
        </nav>
      </aside>`;
  }

  function episodeGroup(episode) {
    const expanded = state.expandedEpisode === episode.id;
    const compactTitle = {
      "没有寄出的声音": "没有寄出",
      "被剪断的雨声": "被剪雨声",
      "第一次篡改": "首次篡改",
      "销毁请求": "销毁请求",
      "无辜的人": "无辜的人",
      "她留下的录音": "她的录音"
    }[episode.title] || episode.title;
    return `
      <section class="episode-group">
        <button class="episode-toggle ${state.currentEpisode === episode.id ? "current" : ""}" data-action="toggle-episode" data-episode="${episode.id}" aria-expanded="${expanded}">
          <span><span class="episode-label-full">${episode.id} · ${escapeHtml(episode.title)}</span><span class="episode-label-compact">${episode.id} · ${compactTitle}</span></span>
          <small>${episode.format === "static" ? "静态" : "动态"}</small>
        </button>
        ${expanded ? `<div class="episode-docs">
          ${docOrder.map((key) => {
            const doc = episode.docs[key];
            const active = state.selection.scope === "doc" && state.currentEpisode === episode.id && state.selection.key === key;
            const note = doc.proposal || doc.state === "submitted" ? "待确认" : doc.state === "stale" ? "需同步" : doc.version ? `v${doc.version}` : doc.state === "skipped" ? "无需" : "未开始";
            return `<button class="doc-link ${active ? "active" : ""}" data-action="select-doc" data-episode="${episode.id}" data-doc="${key}" data-state="${docNavState(doc)}"><span>${docLabels[key]}</span><small>${note}</small></button>`;
          }).join("")}
          <button class="doc-link ${state.selection.scope === "production" && state.currentEpisode === episode.id ? "active" : ""}" data-action="select-production" data-episode="${episode.id}" data-state="${productionReady(episode) ? "current" : "waiting"}"><span>制作成果</span><small>${episode.production.confirmed ? "已创建" : productionReady(episode) ? "可准备" : "等待内容"}</small></button>
        </div>` : ""}
      </section>`;
  }

  function documentIdentity() {
    if (state.selection.scope === "overview") return { location: "全剧", title: state.projectName, version: "系列视图", note: `${state.episodes.length} 集` };
    if (state.selection.scope === "front") {
      const development = state.front.development;
      const absent = development.state === "missing" || development.state === "skipped";
      return { location: `前期材料 · 来源：${routeLabel(state.sourceType)}`, title: absent ? "前期材料" : "故事开发文档", version: development.state === "missing" ? "未创建" : development.state === "skipped" ? "已跳过" : development.version ? `v${development.version}` : development.proposal ? `提案 v${development.proposal.version}` : "创作中", note: absent ? "输入快照" : "整份审批" };
    }
    const episode = currentEpisode();
    if (state.selection.scope === "production") return { location: `${episode.id} · ${episode.format === "static" ? "静态漫剧" : "动态漫剧"}`, title: "制作成果", version: episode.production.confirmed ? "已确认" : "准备中", note: "真实媒体与任务" };
    const doc = currentDoc();
    return { location: `${episode.id} · ${episode.title} · ${doc.source}`, title: docLabels[state.selection.key], version: doc.version ? `v${doc.version}` : "未创建", note: state.selection.key === "screenplay" ? "整份审批" : "应用修改即更新" };
  }

  function overviewDocument() {
    if (!state.episodes.length) return `
      <span class="doc-kicker">Series overview</span>
      <h2>还没有建立分集</h2>
      <p class="lead">${escapeHtml(state.sourceSnapshot.text)}</p>
      <div class="editor-note"><strong>当前起点</strong><p>${escapeHtml(routeLabel(state.sourceType))}输入快照 v${state.sourceSnapshot.version} 已保存。先形成并确认故事开发文档，再展开真正需要的分集。</p></div>`;
    return `
      <span class="doc-kicker">Series overview</span>
      <h2>一盘没有日期的母带，让声音修复师听见未来发生的罪。</h2>
      <p class="lead">许真每修复一段旧录音，就会听见尚未发生的选择。她最初只想阻止伤害，后来却发现每一次干预都会改写另一个无辜者的命运。</p>
      <div class="editor-note"><strong>创作承诺</strong><p>每集完成一次“听见 → 选择 → 付出代价”，母带的位置和持有人承担跨集连续性。</p></div>
      <h3>六集走向</h3>
      <ol class="episode-outline">
        ${state.episodes.map((episode, index) => `<li class="episode-row"><strong>${episode.id}</strong><div><b>${escapeHtml(episode.title)}</b><p>${state.sourceType === "script" && state.episodes.length === 1 ? "导入原文已经保存，当前剧本等待整体确认。" : [
          "许真藏起母带，并第一次改变录音里的未来。",
          "被剪断的雨声证明改变并不会消除代价。",
          "许真越过职业边界，篡改了一份决定命运的录音。",
          "档案馆收到销毁母带的正式请求。",
          "一个无辜的人承担了许真此前选择的后果。",
          "母亲留下的最后一段声音揭开母带来源。"
        ][index] || "本集等待故事开发文档提供正式写作方向。"}</p></div><small>${plainStatus(episode.docs.screenplay).label}</small></li>`).join("")}
      </ol>`;
  }

  function frontDocument() {
    const development = state.front.development;
    if (development.state === "missing") return `
      <span class="doc-kicker">Source snapshot · v${state.sourceSnapshot.version} · immutable</span>
      <h2>前期材料</h2>
      <p class="lead">输入已经保存为不可变快照。${state.sourceType === "original" ? "原著分析可以按需使用，也可以直接开始故事开发。" : "下一步把这个想法发展为可整体确认的故事开发文档。"}</p>
      <div class="editor-note"><strong>${escapeHtml(routeLabel(state.sourceType))}</strong><p>${escapeHtml(state.sourceSnapshot.text)}</p></div>
      <h3>尚未创建故事开发文档</h3>
      <p>系统不会为了填满流程而建立空文档。开始后，Codex 的提案会先进入整体审阅，当前输入快照不会被覆盖。</p>`;
    if (development.state === "skipped") return `
      <span class="doc-kicker">Source snapshot · v${state.sourceSnapshot.version} · immutable</span>
      <h2>前期材料</h2>
      <p class="lead">这个项目从现成剧本进入，原文已经保存为不可变快照，不倒补故事开发阶段。</p>
      <div class="editor-note"><strong>导入剧本原文</strong><p>${escapeHtml(state.sourceSnapshot.text)}</p></div>
      <h3>当前处理方式</h3><p>EP01 剧本 v1 等待整体确认。接受后才会解锁视觉设定、图片提示词和分镜。</p>`;
    if (!state.demoContent) return `
      <span class="doc-kicker">Development document · ${development.proposal ? "proposal" : "current"}</span>
      <h2>故事开发文档</h2>
      <p class="lead">${escapeHtml(state.sourceSnapshot.text)}</p>
      <div class="editor-note"><strong>不可变来源</strong><p>${escapeHtml(routeLabel(state.sourceType))}输入快照 v${state.sourceSnapshot.version}。故事开发版本不会覆盖它。</p></div>
      <h3>${development.proposal ? `v${development.proposal.version} 等待整体确认` : `v${development.version} 已整体确认`}</h3>
      <p>${development.proposal ? escapeHtml(development.proposal.summary) : "当前版本已经提供正式的系列承诺、故事引擎与第一集方向，可以继续进入单集写作。"}</p>`;
    return `
      <span class="doc-kicker">Development document · current</span>
      <h2>故事开发文档</h2>
      <p class="lead">这份文档把来源材料收束为系列承诺、故事引擎和分集方向。它按整份版本确认，不拆成后台字段。</p>
      <div class="editor-note"><strong>来源材料</strong><p>${escapeHtml(state.front.source)}。${escapeHtml(state.sourceSnapshot.text)} 原著分析：${escapeHtml(state.front.analysis)}。</p></div>
      <h3>系列承诺</h3>
      <p>一个擅长修复别人声音、却无法面对自己记忆的年轻人，被迫在“忠实保存过去”与“主动改变未来”之间做选择。</p>
      <h3>故事引擎</h3>
      <p>母带每集释放一段尚未发生的声音。许真可以干预，但她无法同时保住所有人，也不能确定自己听见的是警告、诱导还是已经被她改写过的过去。</p>
      <h3>分集约束</h3>
      <p>每集必须出现一次可见选择和一次不可逆代价。PROP-001 母带跨集复用，状态只随位置、持有人与盒盖状态变化。</p>
      ${development.proposal ? `<div class="editor-note"><strong>待确认版本</strong><p>v${development.proposal.version} 已提交：${escapeHtml(development.proposal.summary)}。当前 v${development.version} 仍是正式版本。</p></div>` : ""}`;
  }

  function screenplayDocument(episode, doc) {
    if (doc.state === "missing") return waitingDocument(episode, "screenplay");
    if (!state.demoContent) return `
      <span class="doc-kicker">${episode.id} · ${state.sourceType === "script" ? "imported screenplay" : "screenplay"}</span>
      <h2>${escapeHtml(episode.title)}</h2>
      ${doc.state === "submitted" ? `<div class="editor-note"><strong>待整体确认</strong><p>剧本 v${doc.version} 已提交。接受后才会解锁视觉设定、图片提示词和分镜。</p></div>` : ""}
      <h3>${state.sourceType === "script" ? "导入原文" : "当前写作来源"}</h3>
      <div class="prompt-copy source-copy">${escapeHtml(state.sourceSnapshot.text)}</div>
      <div class="editor-note"><strong>来源边界</strong><p>${state.sourceType === "script" ? "导入原文保持不变，任何规范化或创作修改都必须成为新的整份剧本版本。" : "这份剧本从已接受的故事开发文档创建，不会改写最初输入快照。"}</p></div>`;
    return `
      <span class="doc-kicker">${episode.id} · screenplay</span>
      <h2>${escapeHtml(episode.title)}</h2>
      ${doc.state === "submitted" ? `<div class="editor-note"><strong>待整体确认</strong><p>剧本 v${doc.version} 已提交。当前下游仍引用上一个已接受版本。</p></div>` : ""}
      <section class="screenplay-block">
        <h3 class="scene-heading">${episode.id}-SC02　内景 · 档案间 · 深夜</h3>
        <p>顶灯在母带架上投下一条窄白光。许真戴着监听耳机，倒带声突然停止。</p>
        <p>她从最里层抽出一盒没有日期的母带。盒盖闭合，封口处有一道新鲜划痕。</p>
        <p class="character-cue">录音中的许真</p>
        <p class="dialogue">别把它交出去。明天的雨会替你回答。</p>
        <p>许真摘下耳机，看向左后方的门。脚步声正在靠近。</p>
        <p>她把母带放进右侧外套内袋，盒盖仍然闭合。</p>
      </section>
      <div class="editor-note"><strong>连续性出口</strong><p>PROP-001 以 PSTATE-001-B 进入 EP02：持有人为许真，位于右侧外套内袋，盒盖闭合。</p></div>`;
  }

  function visualDocument(episode, doc) {
    if (["missing", "stale"].includes(doc.state)) return waitingDocument(episode, "visual", doc.state === "stale");
    return `
      <span class="doc-kicker">${episode.id} · visual facts</span>
      <h2>视觉设定</h2>
      <p class="lead">视觉设定只记录可复用事实及其状态变化。图片提示词和分镜从同一份当前事实出发。</p>
      <dl class="fact-list">
        <div class="fact-row"><dt class="fact-label">Character + Look</dt><dd><b>CHAR-001 / LOOK-001-A · 许真深夜造型</b><p>短发，旧深灰风衣，右侧内袋可以完整容纳母带盒；疲惫但克制。</p></dd></div>
        <div class="fact-row"><dt class="fact-label">Location + View</dt><dd><b>LOC-002 / VIEW-002-A · 档案间夜态</b><p>窄纵深，金属母带架，单点顶灯，门位于画面左后方。</p></dd></div>
        <div class="fact-row"><dt class="fact-label">Prop + State</dt><dd><b>PROP-001 · 无日期母带</b><p>深灰盒体，盒盖闭合。身份不变，只记录位置与持有人变化。</p></dd></div>
        <div class="fact-row"><dt class="fact-label">Continuity</dt><dd><b>EP02 incoming</b><p>继承 PSTATE-001-B，不重新创建母带身份。</p></dd></div>
      </dl>
      <h3>道具连续性</h3>
      <div class="continuity-line" aria-label="PROP-001 状态连续性">
        <div class="continuity-stop"><small>${episode.id}-SC02 · 初始</small><b>PSTATE-001-A</b><span>档案架 · 盒盖闭合</span></div>
        <span class="continuity-arrow" aria-hidden="true">→</span>
        <div class="continuity-stop"><small>${episode.id}-SC02 · 场末</small><b>PSTATE-001-B</b><span>许真右侧内袋 · 盒盖闭合</span></div>
        <span class="continuity-arrow" aria-hidden="true">→</span>
        <div class="continuity-stop"><small>EP02 incoming</small><b>沿用 B</b><span>持有人：许真</span></div>
      </div>
      ${doc.proposal ? `<div class="editor-note"><strong>有 1 处修改</strong><p>${escapeHtml(doc.proposal.summary)}。应用前，当前视觉设定仍为 v${doc.version}。</p></div>` : ""}`;
  }

  function imagePromptDocument(episode, doc) {
    if (["missing", "stale"].includes(doc.state)) return waitingDocument(episode, "image", doc.state === "stale");
    return `
      <span class="doc-kicker">${episode.id} · image prompts</span>
      <h2>图片提示词</h2>
      <p class="lead">这里是可复制的文本规格，不是参考媒体。真实 REF 只会在生产并通过结果审阅后出现。</p>
      <div class="prompt-list">
        <section class="prompt-row"><span class="row-label">IMG-CHAR-001</span><div><b>许真 · 深夜造型参考</b><div class="prompt-copy">中国女性，27 岁，短发，旧深灰风衣，右侧内袋略有重量感；档案间单点顶灯，疲惫但克制，正面与侧面保持同一造型事实。</div></div></section>
        <section class="prompt-row"><span class="row-label">IMG-LOC-002</span><div><b>档案间 · 夜态</b><div class="prompt-copy">狭窄纵深的模拟音频档案间，金属母带架贯穿画面，门在左后方，冷白顶灯只照亮中段，保留可供人物调度的通道。</div></div></section>
        <section class="prompt-row"><span class="row-label">IMG-PROP-001</span><div><b>无日期母带 · PSTATE-001-A</b><div class="prompt-copy">深灰色无日期母带盒，盒盖闭合，封口处一道新鲜划痕；中性纸面背景，正侧顶三种视图，禁止改变盒体身份。</div></div></section>
      </div>`;
  }

  function storyboardDocument(episode, doc) {
    if (["missing", "stale"].includes(doc.state)) return waitingDocument(episode, "storyboard", doc.state === "stale");
    return `
      <span class="doc-kicker">${episode.id} · storyboard</span>
      <h2>分镜</h2>
      <p class="lead">分镜负责戏剧职责、调度边界与冻结关键帧。它和图片提示词并行，不互相等待。</p>
      <div class="shot-list">
        <section class="shot-row"><span class="row-label">SHOT-001</span><div><b>建立档案间的窄纵深</b><p>许真位于画面右前，母带架向左后延伸。门必须留在左后方，给即将靠近的脚步声一个方向。</p></div><small>4 秒 · 固定</small></section>
        <section class="shot-row"><span class="row-label">SHOT-004</span><div><b>冻结关键帧：母带刚离开档案架</b><p>PROP-001 仍为 PSTATE-001-A，盒盖闭合。关键帧不能提前出现“藏入内袋”的动作结果。</p></div><small>3 秒 · 近景</small></section>
        <section class="shot-row"><span class="row-label">SHOT-006</span><div><b>场末状态交接</b><p>许真完成藏入动作，PROP-001 更新为 PSTATE-001-B，门仍在左后方，视线指向声源。</p></div><small>5 秒 · 跟移</small></section>
      </div>`;
  }

  function videoPromptDocument(episode, doc) {
    if (doc.state === "skipped") return `<div class="skip-message"><span class="doc-kicker">${episode.id} · static drama</span><strong>本集不需要视频提示词</strong><p>静态漫剧会从当前分镜的冻结关键帧直接进入配音与画面编排。跳过不是缺失，也不会删除历史版本。</p></div>`;
    if (["missing", "stale"].includes(doc.state)) return waitingDocument(episode, "video", doc.state === "stale");
    return `
      <span class="doc-kicker">${episode.id} · video prompts</span>
      <h2>视频提示词</h2>
      <p class="lead">只翻译当前分镜已经确定的表演、动作、运镜和起止状态，不重新发明视觉事实。</p>
      <div class="prompt-list">
        <section class="prompt-row"><span class="row-label">MOTION-004</span><div><b>从 PSTATE-001-A 到 B</b><div class="prompt-copy">6 秒，竖屏。起点：许真右手持盒盖闭合的 PROP-001，站在母带架前。她听见左后方脚步，短暂停顿，视线先到门，再将母带放入右侧外套内袋。终点：PSTATE-001-B，盒盖仍闭合。镜头缓慢推近，不越轴。</div></div></section>
        <section class="prompt-row"><span class="row-label">SOUND-004</span><div><b>声音与表演</b><div class="prompt-copy">保留倒带停止的机械余响；脚步从左后方接近。许真不说话，只在听见脚步后短促吸气，动作克制，不表现惊恐。</div></div></section>
      </div>`;
  }

  function waitingDocument(episode, key, stale = false) {
    const doc = episode.docs[key];
    const ready = canStartDoc(episode, key);
    return `<div class="waiting-message"><span class="doc-kicker">${episode.id} · ${escapeHtml(docLabels[key])}</span><strong>${stale ? "上游已经更新" : `${docLabels[key]}尚未开始`}</strong><p>${stale ? `当前 v${doc.version} 仍引用 ${escapeHtml(doc.source)}。请基于最新上游创建修订，旧版本会保留。` : ready ? "所需上游已经就绪，可以让 Codex 开始这份内容。" : `现在先${key === "visual" ? "整体确认剧本" : key === "image" || key === "storyboard" ? "更新视觉设定" : "完成当前分镜"}。系统不会创建空文档。`}</p></div>`;
  }

  function productionDocument(episode) {
    const production = episode.production;
    const jobs = production.jobs.length ? production.jobs : production.previousJobs;
    return `
      <section class="production-summary">
        <div><span class="doc-kicker">${episode.id} · production results</span><h2>制作成果</h2><p class="lead">这里展示真实参考媒体与已创建任务。精确参数只在准备生产时出现。</p></div>
        <span class="summary-state">${production.confirmed ? "本批次已确认" : production.invalidated ? "旧确认已失效" : productionReady(episode) ? "内容已就绪" : "等待创作内容"}</span>
      </section>
      <h3>真实 REF 媒体</h3>
      <div class="reference-list">
        ${production.refs.map((ref) => `<section class="reference-row"><div class="ref-preview ${ref.kind} ${ref.state !== "approved" ? "missing" : ""}">${escapeHtml(ref.id)}<br>${escapeHtml(ref.path)}</div><div><b>${escapeHtml(ref.label)}</b><p>${ref.state === "approved" ? ref.kind === "prop" ? "PROP-001，盒盖闭合，身份已经由创作者审阅确认。" : "媒体文件已经通过创作者审阅，可作为后续生成的 reference。" : "尚未生成真实媒体。文本提示词不会被当作 reference。"}</p></div><span class="ref-state">${ref.state === "approved" ? "已通过审阅" : "尚未生成"}</span></section>`).join("")}
      </div>
      <h3>${jobs.length ? "生产任务" : "尚未创建生产任务"}</h3>
      ${jobs.length ? `<div class="job-list">${jobs.map((job) => `<section class="job-row"><div><b>${escapeHtml(job.id)}</b><p>${escapeHtml(job.label)}</p></div><span class="job-kind">${escapeHtml(job.adapter)}<br>${escapeHtml(job.output)}</span><span class="job-state">${jobStateLabel(job.state)}</span></section>`).join("")}</div>` : `<p class="lead">准备生产会先显示本集的精确 manifest。只有再次明确确认后才会创建任务。</p>`}`;
  }

  function jobStateLabel(jobState) {
    return { review: "结果待审阅", blocked: "等待参考结果", queued: "等待执行", current: "结果已确认", failed: "失败，需重新准备" }[jobState] || "进行中";
  }

  function documentBody() {
    if (state.selection.scope === "overview") return overviewDocument();
    if (state.selection.scope === "front") return frontDocument();
    const episode = currentEpisode();
    if (state.selection.scope === "production") return productionDocument(episode);
    const doc = currentDoc();
    return {
      screenplay: () => screenplayDocument(episode, doc),
      visual: () => visualDocument(episode, doc),
      image: () => imagePromptDocument(episode, doc),
      storyboard: () => storyboardDocument(episode, doc),
      video: () => videoPromptDocument(episode, doc)
    }[state.selection.key]();
  }

  function nextAction() {
    if (state.selection.scope === "overview") return state.episodes.length
      ? { copy: "从当前材料继续阅读第一集", label: "打开 EP01 剧本", action: "go-ep01" }
      : { copy: "先把输入发展成可整体确认的故事开发文档", label: "返回前期材料", action: "go-front" };
    if (state.selection.scope === "front") {
      const development = state.front.development;
      if (development.state === "missing") return { copy: "输入快照已经保存，尚未创建故事开发文档", label: "开始故事开发", action: "start-development" };
      if (development.state === "skipped") return { copy: "现成剧本不需要倒补故事开发阶段", label: "审阅导入剧本", action: "go-ep01" };
      return development.proposal
        ? { copy: `故事开发 v${development.proposal.version} 已提交，当前 v${development.version} 未改变`, label: `审阅 v${development.proposal.version}`, action: "open-details" }
        : { copy: "当前开发文档已经整体确认", label: "打开 EP01 剧本", action: "go-ep01" };
    }

    const episode = currentEpisode();
    if (state.selection.scope === "production") {
      const production = episode.production;
      if (!productionReady(episode)) {
        const first = docOrder.find((key) => ["missing", "stale"].includes(episode.docs[key].state));
        return { copy: "本集创作输入尚未就绪，不能准备生产", label: `处理${docLabels[first]}`, action: "go-blocking-doc", key: first };
      }
      if (!production.confirmed) return { copy: production.invalidated ? "输入或输出规格已变化，旧确认不再有效" : "先核对精确输入、输出、参数与计费", label: "准备生产", action: "open-production" };
      const reviewJob = production.jobs.find((job) => job.state === "review");
      if (reviewJob) return { copy: `${reviewJob.id} 的结果等待创作者确认`, label: "审阅下一个结果", action: "review-job", id: reviewJob.id };
      const queuedJob = production.jobs.find((job) => job.state === "queued");
      if (queuedJob) return { copy: `${queuedJob.id} 已满足依赖，等待执行`, label: "查看任务进度", action: "advance-job", id: queuedJob.id };
      return { copy: "本批次任务与结果已经归档", label: "查看确认记录", action: "open-production" };
    }

    const doc = currentDoc();
    const key = state.selection.key;
    if (doc.state === "submitted" && key === "screenplay") return { copy: `剧本 v${doc.version} 已提交，接受后才会更新正式下游来源`, label: `整体接受 v${doc.version}`, action: "accept-screenplay" };
    if (doc.proposal) return { copy: `${doc.proposal.summary}，当前版本还没有改变`, label: "查看 1 处修改", action: "open-details" };
    if (doc.state === "stale") return { copy: `当前版本仍引用 ${doc.source}`, label: "基于最新上游更新", action: "refresh-doc" };
    if (doc.state === "missing") {
      if (canStartDoc(episode, key)) return { copy: "所需上游已经就绪，不会创建空文档", label: `开始${docLabels[key]}`, action: "start-doc" };
      const blocker = key === "visual" ? "screenplay" : key === "image" || key === "storyboard" ? "visual" : "storyboard";
      return { copy: `先完成${docLabels[blocker]}，再开始当前内容`, label: `打开${docLabels[blocker]}`, action: "go-doc", key: blocker };
    }
    if (doc.state === "skipped") return { copy: "静态漫剧从分镜直接进入配音与画面编排", label: "查看制作成果", action: "go-production" };
    const index = docOrder.indexOf(key);
    if (index < docOrder.length - 1) return { copy: `${docLabels[key]}已经是当前版本`, label: `继续到${docLabels[docOrder[index + 1]]}`, action: "go-doc", key: docOrder[index + 1] };
    return { copy: "本集五份创作内容已经就绪", label: "查看制作成果", action: "go-production" };
  }

  function actionBar() {
    const next = nextAction();
    const canReject = state.selection.scope === "doc" && state.selection.key === "screenplay" && currentDoc().state === "submitted";
    return `
      <footer class="context-bar" data-screen-label="唯一上下文动作">
        <div class="next-copy"><span>下一步</span><strong>${escapeHtml(next.copy)}</strong></div>
        <div class="context-actions">
          ${!["overview"].includes(state.selection.scope) ? `<button class="text-action" data-action="request-review">按需审查</button>` : ""}
          ${state.selection.scope === "production" ? `<button class="text-action" data-action="open-production">生产设置</button>` : !["overview"].includes(state.selection.scope) ? `<button class="text-action" data-action="open-details">修改详情</button>` : ""}
          ${canReject ? `<button class="text-action" data-action="reject-formal">退回并说明</button>` : ""}
          <button class="primary-action" data-action="${next.action}" ${next.key ? `data-doc="${next.key}"` : ""} ${next.id ? `data-id="${next.id}"` : ""}>${escapeHtml(next.label)}</button>
        </div>
      </footer>`;
  }

  function workspace() {
    const identity = documentIdentity();
    return `
      <div class="workspace" data-screen-label="Script Weaver 单页短剧创作台">
        <a class="skip-link" href="#main-content">跳到当前正文</a>
        ${header()}
        <div class="desk-shell">
          ${rail()}
          <main class="document-workspace" id="main-content" tabindex="-1" data-screen-label="永久内容阅读面">
            <header class="document-toolbar">
              <div class="document-identity"><span class="document-location">${escapeHtml(identity.location)}</span><h1>${escapeHtml(identity.title)}</h1></div>
              <div class="document-meta"><span>${escapeHtml(identity.note)}</span><span class="version-mark">${escapeHtml(identity.version)}</span></div>
            </header>
            <div class="document-scroll" id="document-scroll"><article class="document-canvas">${documentBody()}</article></div>
            ${actionBar()}
          </main>
        </div>
        ${state.overlay ? overlay() : ""}
        ${state.notice ? `<div class="notice" role="status" aria-live="polite">${escapeHtml(state.notice)}</div>` : ""}
      </div>`;
  }

  function detailsOverlay() {
    const front = state.selection.scope === "front";
    const production = state.selection.scope === "production";
    const episode = currentEpisode();
    const doc = !front && !production && state.selection.scope === "doc" ? currentDoc() : null;
    const proposal = front ? state.front.development.proposal : doc?.proposal;
    const source = front ? state.front.development.source : production ? `${episode.id} 当前创作文档` : doc?.source;
    const version = front ? state.front.development.version : production ? "—" : doc?.version;
    const title = front ? "故事开发的修改详情" : production ? "生产确认与来源" : `${docLabels[state.selection.key]}的修改详情`;
    return `
      <div class="overlay drawer-overlay" data-action="overlay-backdrop">
        <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="details-title" data-screen-label="按需修改详情抽屉">
          <header class="drawer-head"><div><span>Details on demand</span><h2 id="details-title">${escapeHtml(title)}</h2></div><button class="close-button" data-action="close-overlay" aria-label="关闭">×</button></header>
          <div class="drawer-body">
            <section class="drawer-section"><h3>创作者现在需要知道的事</h3><p>${proposal ? escapeHtml(proposal.summary) : production ? "生产只对当前来源版本和输出规格有效。任何输入变化都会让旧确认失效。" : "当前没有等待应用的修改。下面保留完整来源与任务记录，供需要时核对。"}</p></section>
            ${proposal ? `<section class="drawer-section"><h3>${front ? "整份文档提案" : "ChangeSet"}</h3>${front ? `<p>新版本 v${proposal.version} 会在整体接受后取代当前 v${version}。退回不会自动创建下一版。</p>` : `<div class="change-preview"><del>− ${escapeHtml(proposal.before)}</del><ins>+ ${escapeHtml(proposal.after)}</ins></div>`}</section>` : ""}
            <section class="drawer-section"><h3>精确记录</h3><div class="ledger">
              <div class="ledger-row"><span>当前版本</span><code>${escapeHtml(version)}</code></div>
              <div class="ledger-row"><span>直接来源</span><code>${escapeHtml(source)}</code></div>
              <div class="ledger-row"><span>${front ? "文档提案" : "Task"}</span><code>${escapeHtml(proposal?.id || (doc?.task ? `TASK-${episode.id}-${state.selection.key?.toUpperCase()}` : "无运行中任务"))}</code></div>
              <div class="ledger-row"><span>应用规则</span><code>${front || state.selection.key === "screenplay" ? "整份接受或退回" : production ? "prepare → explicit confirm" : "ChangeSet 应用后成为 current"}</code></div>
            </div></section>
            ${doc?.history?.length ? `<section class="drawer-section"><h3>版本历史</h3>${[...doc.history].reverse().map((entry) => `<p>v${entry.version} · ${entry.state === "accepted" ? "已接受" : entry.state === "rejected" ? `已退回：${escapeHtml(entry.feedback)}` : "待确认"}</p>`).join("")}</section>` : ""}
          </div>
          <footer class="drawer-foot">
            ${proposal ? front ? `<button class="secondary-action" data-action="reject-formal">退回并说明</button><button class="primary-action" data-action="accept-development">整体接受 v${proposal.version}</button>` : `<button class="primary-action" data-action="apply-changeset">应用这处修改</button>` : ""}
            ${doc?.state === "submitted" && state.selection.key === "screenplay" ? `<button class="secondary-action" data-action="reject-formal">退回并说明</button><button class="primary-action" data-action="accept-screenplay">整体接受 v${doc.version}</button>` : ""}
            ${!proposal && !(doc?.state === "submitted" && state.selection.key === "screenplay") ? `<button class="secondary-action" data-action="close-overlay">关闭</button>` : ""}
          </footer>
        </aside>
      </div>`;
  }

  function intakeOverlay() {
    const copy = {
      idea: { title: "从一个故事想法开始", body: "保存原始想法，再形成可整体确认的故事开发文档。", field: "故事想法" },
      original: { title: "从合法持有的原著开始", body: "先保存原著快照。原著分析可选，不是永久门禁。", field: "原著摘要或章节" },
      script: { title: "从现成剧本开始", body: "保留导入原文。多集剧本识别分集边界，单集剧本直接进入确认。", field: "剧本文本" }
    }[state.intakeType];
    return `
      <div class="overlay sheet-overlay" data-action="overlay-backdrop">
        <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="intake-title" data-screen-label="一次性新建项目入口">
          <header class="sheet-head"><div><span>New project</span><h2 id="intake-title">创建短剧项目</h2></div><button class="close-button" data-action="close-overlay" aria-label="关闭">×</button></header>
          <div class="sheet-body"><div class="intake-layout">
            <div class="intake-options" role="group" aria-label="选择起点">
              ${[["idea", "故事想法", "发展故事承诺"], ["original", "合法持有的原著", "分析可选"], ["script", "现成剧本", "直接确认或分集"]].map(([id, label, note]) => `<button class="intake-option ${state.intakeType === id ? "active" : ""}" data-action="intake-type" data-type="${id}" aria-pressed="${state.intakeType === id}"><span><strong>${label}</strong><small>${note}</small></span></button>`).join("")}
            </div>
            <div class="intake-form"><h3>${copy.title}</h3><p>${copy.body}</p><label class="field"><span>项目名称</span><input id="intake-name" value="${escapeHtml(state.intakeDraft?.name || state.projectName)}" autocomplete="off"></label><label class="field"><span>${copy.field}</span><textarea id="intake-source">${escapeHtml(state.intakeDraft?.texts?.[state.intakeType] || "")}</textarea></label><div class="route-note">进入项目后，来源类型不会成为导航。创作者只会看到当前内容和下一步。</div></div>
          </div></div>
          <footer class="sheet-foot"><button class="secondary-action" data-action="close-overlay">取消</button><button class="primary-action" data-action="create-project">保存输入并进入项目</button></footer>
        </section>
      </div>`;
  }

  function reviewOverlay() {
    const visual = state.selection.scope === "doc" && state.selection.key === "visual";
    return `
      <div class="overlay sheet-overlay" data-action="overlay-backdrop">
        <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="review-title" data-screen-label="按需审查结果">
          <header class="sheet-head"><div><span>Review on demand</span><h2 id="review-title">${escapeHtml(contextTitle())}审查</h2></div><button class="close-button" data-action="close-overlay" aria-label="关闭">×</button></header>
          <div class="sheet-body">
            <div class="review-result"><h3>${visual ? "道具连续性成立" : "当前内容可以继续"}</h3><p>${visual ? "PROP-001 从 PSTATE-001-A 到 B 的位置、持有人与盒盖状态明确，EP02 incoming 复用 B，没有重新创建道具身份。" : "没有发现会阻断当前创作的事实冲突。审查是按需建议，不会自动成为固定阶段。"}</p></div>
            <div class="review-result"><h3>建议修订</h3><p>${visual ? "图片提示词可在 PROP-001 的顶视图中补充封口划痕方向，避免生成结果左右翻转。" : "把当前段落的戏剧职责写在标题下，后续分镜会更容易保持节奏。"}</p></div>
          </div>
          <footer class="sheet-foot"><button class="primary-action" data-action="close-overlay">知道了</button></footer>
        </section>
      </div>`;
  }

  function rejectOverlay() {
    const development = state.selection.scope === "front";
    const version = development ? state.front.development.proposal?.version : currentDoc().version;
    return `
      <div class="overlay sheet-overlay" data-action="overlay-backdrop">
        <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="reject-title" data-screen-label="整份文档退回">
          <header class="sheet-head"><div><span>Formal review</span><h2 id="reject-title">退回整份${development ? "故事开发文档" : "剧本"} v${version}</h2></div><button class="close-button" data-action="close-overlay" aria-label="关闭">×</button></header>
          <div class="sheet-body"><p>反馈会和被退回的版本一起保留。系统不会自动创建下一版。</p><label class="field reject-field"><span>退回反馈</span><textarea id="reject-feedback" placeholder="说明需要改变的戏剧结果，而不只是改写句子"></textarea></label></div>
          <footer class="sheet-foot"><button class="secondary-action" data-action="close-overlay">取消</button><button class="danger-action" data-action="confirm-reject">确认退回</button></footer>
        </section>
      </div>`;
  }

  function manifestItems(episode) {
    const preset = episode.production.outputPreset === "vertical-hq" ? "1080×1920 · 24fps · high" : "1080×1920 · 24fps";
    const items = [
      { kind: "asset_ref", label: "许真深夜造型参考 v2", source: `图片提示词 v${episode.docs.image.version || "—"} / IMG-CHAR-001`, adapter: "gpt-image-2", params: "1536×2048 · 9:16", output: `${episode.id}/制作成果/REF-001-v2.png`, cost: "1 image unit", resultRef: "REF-001" },
      { kind: "asset_ref", label: "PROP-001 道具参考 v2", source: `图片提示词 v${episode.docs.image.version || "—"} / IMG-PROP-001`, adapter: "gpt-image-2", params: "2048×1536 · 4:3", output: `${episode.id}/制作成果/REF-003-v2.png`, cost: "1 image unit", resultRef: "REF-003" },
      { kind: "frozen_keyframe", label: `${episode.id}-SHOT-004 冻结关键帧`, source: `分镜 v${episode.docs.storyboard.version || "—"} / FROZEN-004`, adapter: "gpt-image-2", params: "1080×1920 · 9:16", output: `${episode.id}/制作成果/KF-004.png`, cost: "1 image unit" },
      { kind: "tts", label: `${episode.id} 对白与声音时间点`, source: `剧本 v${episode.docs.screenplay.version || "—"} + 分镜 v${episode.docs.storyboard.version || "—"}`, adapter: "tts-local", params: "48kHz · mono", output: `${episode.id}/制作成果/AUDIO-004.wav`, cost: "8 audio seconds" }
    ];
    if (episode.format === "dynamic") items.splice(3, 0, { kind: "video", label: `${episode.id}-SHOT-004 动态镜头`, source: `视频提示词 v${episode.docs.video.version || "—"} / MOTION-004`, adapter: "seedance", params: `6s · ${preset}`, output: `${episode.id}/制作成果/SHOT-004.mp4`, cost: "6 video seconds" });
    return items;
  }

  function fingerprint(items) {
    const input = JSON.stringify(items);
    let hash = 2166136261;
    for (let index = 0; index < input.length; index += 1) {
      hash ^= input.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `sha256-demo-${(hash >>> 0).toString(16).padStart(8, "0")}`;
  }

  function productionOverlay() {
    const episode = currentEpisode();
    const production = episode.production;
    const items = manifestItems(episode);
    const preparedHash = production.inputHash || fingerprint(items);
    const ready = productionReady(episode);
    const refsApproved = production.refs.every((ref) => ref.state === "approved");
    return `
      <div class="overlay sheet-overlay" data-action="overlay-backdrop">
        <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="production-title" data-screen-label="一次性精确生产确认">
          <header class="sheet-head"><div><span>Prepare → confirm</span><h2 id="production-title">${episode.id} · ${production.confirmed ? "确认记录" : production.prepared ? "明确确认生产" : "准备生产"}</h2></div><button class="close-button" data-action="close-overlay" aria-label="关闭">×</button></header>
          <div class="sheet-body">
            <div class="confirm-intro"><p>${!ready ? "创作输入尚未就绪，这份清单只能查看，不能冻结或确认。" : production.confirmed ? "相同 confirm key 的重复确认不会创建新任务。" : production.prepared ? "manifest 已冻结，但还没有创建任何外部任务。最后核对后再明确确认。" : "先核对来源版本、adapter、参数、输出和计费。此时不会调用外部服务。"}</p><span class="confirm-total">${items.length} jobs · ${episode.format === "dynamic" ? "3 image + 6 video + 8 audio" : "3 image + 8 audio"}</span></div>
            <div class="manifest-table">
              <div class="manifest-row header"><span>任务</span><span>准确来源</span><span>Adapter / 参数</span><span>计费</span></div>
              ${items.map((item) => `<div class="manifest-row"><div><b>${escapeHtml(item.kind)}</b><span>${escapeHtml(item.label)}</span></div><span>${escapeHtml(item.source)}<br>${escapeHtml(item.output)}</span><code>${escapeHtml(item.adapter)}<br>${escapeHtml(item.params)}</code><span>${escapeHtml(item.cost)}</span></div>`).join("")}
            </div>
            <div class="confirm-controls"><label class="field"><span>输出规格</span><select data-field="output-preset"><option value="vertical-standard" ${production.outputPreset === "vertical-standard" ? "selected" : ""}>竖屏标准 · 1080×1920</option><option value="vertical-hq" ${production.outputPreset === "vertical-hq" ? "selected" : ""}>竖屏高质量 · 1080×1920 high</option></select></label><div class="route-note">更改输入、参考、参数或输出会使已准备和已确认的批次失效，必须重新 prepare。</div></div>
            <div class="confirm-facts">
              <div class="confirm-fact"><span>Input hash</span><code>${escapeHtml(preparedHash)}</code></div>
              <div class="confirm-fact"><span>Confirm key</span><code>${escapeHtml(production.confirmKey || "尚未确认")}</code></div>
              <div class="confirm-fact"><span>References</span><code>REF-001 + REF-002 + REF-003 · ${refsApproved ? "approved" : "尚未生成"}</code></div>
              <div class="confirm-fact"><span>输出根目录</span><code>${episode.id}/制作成果/</code></div>
            </div>
          </div>
          <footer class="sheet-foot">
            <button class="secondary-action" data-action="close-overlay">${production.confirmed ? "关闭" : "返回修改"}</button>
            ${!ready ? `<button class="primary-action" disabled>等待创作输入</button>` : production.confirmed ? "" : production.prepared ? `<button class="danger-action" data-action="confirm-production">我已核对，创建 ${items.length} 个任务</button>` : `<button class="primary-action" data-action="prepare-production">冻结这份预览</button>`}
          </footer>
        </section>
      </div>`;
  }

  function overlay() {
    return {
      details: detailsOverlay,
      intake: intakeOverlay,
      review: reviewOverlay,
      reject: rejectOverlay,
      production: productionOverlay
    }[state.overlay]();
  }

  function focusSelector(button) {
    const pairs = [["data-action", button.dataset.action], ["data-episode", button.dataset.episode], ["data-doc", button.dataset.doc]]
      .filter(([, value]) => value)
      .map(([key, value]) => `[${key}="${CSS.escape(value)}"]`)
      .join("");
    return `button${pairs}`;
  }

  function openOverlay(kind, button) {
    returnFocus = button ? focusSelector(button) : "";
    state.overlay = kind;
    render();
    requestAnimationFrame(() => document.querySelector(".sheet button, .drawer button")?.focus());
  }

  function closeOverlay() {
    state.overlay = null;
    render();
    if (returnFocus) requestAnimationFrame(() => document.querySelector(returnFocus)?.focus());
  }

  function notify(message) {
    state.notice = message;
    clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(() => {
      state.notice = "";
      document.querySelector(".notice")?.remove();
    }, 3600);
  }

  function selectDoc(episodeId, key) {
    state.currentEpisode = episodeId;
    state.expandedEpisode = episodeId;
    state.selection = { scope: "doc", key };
    state.railOpen = false;
  }

  function staleAfterVisualChange(episode) {
    ["image", "storyboard", "video"].forEach((key) => {
      if (episode.docs[key].state !== "skipped" && episode.docs[key].state !== "missing") episode.docs[key].state = "stale";
    });
    invalidateProduction(episode);
  }

  function invalidateProduction(episode) {
    const production = episode.production;
    if (production.jobs.length) production.previousJobs = production.jobs.map((job) => ({ ...job, state: job.state === "current" ? "current" : "failed" }));
    production.prepared = false;
    production.confirmed = false;
    production.invalidated = true;
    production.inputHash = "";
    production.confirmKey = "";
    production.jobs = [];
  }

  function reconcileJobs(episode) {
    const jobs = episode.production.jobs;
    const refsReady = jobs.filter((job) => job.kind === "asset_ref").every((job) => job.state === "current");
    const keyframe = jobs.find((job) => job.kind === "frozen_keyframe");
    if (refsReady && keyframe?.state === "blocked") keyframe.state = "queued";
    const video = jobs.find((job) => job.kind === "video");
    if (keyframe?.state === "current" && video?.state === "blocked") video.state = "queued";
  }

  function render() {
    document.documentElement.dataset.theme = state.theme;
    app.innerHTML = workspace();
  }

  app.addEventListener("click", (event) => {
    if (event.target.matches('[data-action="overlay-backdrop"]')) { closeOverlay(); return; }
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    const action = button.dataset.action;

    if (action === "theme") state.theme = state.theme === "light" ? "dark" : "light";
    else if (action === "toggle-rail") state.railOpen = !state.railOpen;
    else if (action === "close-rail") state.railOpen = false;
    else if (action === "select-global") { state.selection = { scope: button.dataset.scope }; state.railOpen = false; }
    else if (action === "toggle-episode") { state.currentEpisode = button.dataset.episode; state.expandedEpisode = state.expandedEpisode === button.dataset.episode ? "" : button.dataset.episode; }
    else if (action === "select-doc") selectDoc(button.dataset.episode, button.dataset.doc);
    else if (action === "select-production") { state.currentEpisode = button.dataset.episode; state.expandedEpisode = button.dataset.episode; state.selection = { scope: "production" }; state.railOpen = false; }
    else if (action === "open-intake") {
      state.intakeType = state.sourceType;
      state.intakeDraft = { name: state.projectName, texts: { idea: "", original: "", script: "" } };
      state.intakeDraft.texts[state.sourceType] = state.sourceSnapshot.text;
      openOverlay("intake", button);
      return;
    }
    else if (action === "open-details") { openOverlay("details", button); return; }
    else if (action === "request-review") { openOverlay("review", button); return; }
    else if (action === "open-production") { openOverlay("production", button); return; }
    else if (action === "close-overlay") { closeOverlay(); return; }
    else if (action === "intake-type") { state.intakeType = button.dataset.type; render(); requestAnimationFrame(() => document.querySelector(`[data-type="${state.intakeType}"]`)?.focus()); return; }
    else if (action === "create-project") {
      const name = state.intakeDraft.name.trim();
      const source = state.intakeDraft.texts[state.intakeType].trim();
      if (!name || !source) { (name ? document.getElementById("intake-source") : document.getElementById("intake-name")).focus(); return; }
      freshProject(name, state.intakeType, source);
      notify("输入快照已保存；来源类型退出导航，接下来只处理创作内容");
    }
    else if (action === "go-ep01") selectDoc("EP01", "screenplay");
    else if (action === "go-front") state.selection = { scope: "front" };
    else if (action === "go-doc") selectDoc(state.currentEpisode, button.dataset.doc);
    else if (action === "go-production") state.selection = { scope: "production" };
    else if (action === "go-blocking-doc") selectDoc(state.currentEpisode, button.dataset.doc);
    else if (action === "start-doc") {
      const doc = currentDoc();
      doc.task = "submitted";
      doc.version = (doc.version || 0) + 1;
      if (state.selection.key === "screenplay") {
        doc.state = "submitted";
        doc.history.push({ version: doc.version, state: "submitted" });
      } else {
        doc.proposal = { id: `CS-${state.currentEpisode}-${state.selection.key.toUpperCase()}-${String(doc.version).padStart(3, "0")}`, version: doc.version, summary: `${docLabels[state.selection.key]}初稿已提交`, before: "尚无当前内容", after: "Codex 已提交可审阅的第一版内容" };
      }
      notify(`${docLabels[state.selection.key]}已提交为修改建议，当前事实尚未改变`);
    }
    else if (action === "start-development") {
      const development = state.front.development;
      const lastVersion = development.history.reduce((highest, item) => Math.max(highest, item.version), development.version || 0);
      development.state = "submitted";
      development.task = "submitted";
      development.proposal = { id: `DOC-DEV-V${lastVersion + 1}`, version: lastVersion + 1, summary: "系列承诺、故事引擎与第一集方向已经提交" };
      notify(`故事开发文档 v${lastVersion + 1} 已提交，等待整体确认`);
    }
    else if (action === "refresh-doc") {
      const doc = currentDoc();
      doc.task = "submitted";
      doc.proposal = { id: `CS-${state.currentEpisode}-${state.selection.key.toUpperCase()}-${String((doc.version || 0) + 1).padStart(3, "0")}`, version: (doc.version || 0) + 1, summary: `基于最新上游同步${docLabels[state.selection.key]}`, before: `仍引用 ${doc.source}`, after: "改为引用最新接受的直接上游版本" };
      notify("修订已提交；旧版本仍保留，等待应用 ChangeSet");
    }
    else if (action === "apply-changeset") {
      const episode = currentEpisode();
      const doc = currentDoc();
      doc.version = doc.proposal.version;
      doc.state = "current";
      doc.source = state.selection.key === "visual" ? `${episode.id} 剧本 v${episode.docs.screenplay.version}` : state.selection.key === "video" ? `分镜 v${episode.docs.storyboard.version}` : "视觉设定当前版本";
      doc.task = null;
      doc.proposal = null;
      if (state.selection.key === "visual") staleAfterVisualChange(episode);
      else if (state.selection.key === "storyboard") { if (episode.docs.video.state !== "skipped") episode.docs.video.state = "stale"; invalidateProduction(episode); }
      else invalidateProduction(episode);
      state.overlay = null;
      notify(`${docLabels[state.selection.key]}已更新为当前版本；依赖旧来源的内容已标记需要同步`);
    }
    else if (action === "accept-development") {
      const development = state.front.development;
      development.version = development.proposal.version;
      development.state = "accepted";
      development.task = null;
      development.history.push({ version: development.version, state: "accepted" });
      development.proposal = null;
      if (!state.episodes.length) {
        state.episodes = [freshEpisode("EP01", "第 1 集")];
        state.currentEpisode = "EP01";
        state.expandedEpisode = "EP01";
      }
      state.overlay = null;
      notify(`故事开发文档 v${development.version} 已整体接受`);
    }
    else if (action === "accept-screenplay") {
      const episode = currentEpisode();
      const doc = currentDoc();
      doc.state = "accepted";
      doc.task = null;
      const history = doc.history.find((item) => item.version === doc.version);
      if (history) history.state = "accepted";
      else doc.history.push({ version: doc.version, state: "accepted" });
      ["visual", "image", "storyboard", "video"].forEach((key) => { if (!["missing", "skipped"].includes(episode.docs[key].state)) episode.docs[key].state = "stale"; });
      invalidateProduction(episode);
      state.overlay = null;
      notify(`剧本 v${doc.version} 已整体接受；引用旧剧本的下游需要同步`);
    }
    else if (action === "reject-formal") { openOverlay("reject", button); return; }
    else if (action === "confirm-reject") {
      const feedback = document.getElementById("reject-feedback").value.trim();
      if (!feedback) { document.getElementById("reject-feedback").focus(); return; }
      if (state.selection.scope === "front") {
        const development = state.front.development;
        development.history.push({ version: development.proposal.version, state: "rejected", feedback });
        development.task = null;
        development.proposal = null;
        development.state = development.version ? "accepted" : "missing";
        notify("故事开发提案已退回；当前接受版本没有改变");
      } else {
        const doc = currentDoc();
        doc.state = "rejected";
        doc.task = null;
        const history = doc.history.find((item) => item.version === doc.version);
        if (history) Object.assign(history, { state: "rejected", feedback });
        else doc.history.push({ version: doc.version, state: "rejected", feedback });
        notify(`剧本 v${doc.version} 已退回；反馈和版本历史已经保留`);
      }
      state.overlay = null;
    }
    else if (action === "prepare-production") {
      const episode = currentEpisode();
      const production = episode.production;
      const items = manifestItems(episode);
      if (!productionReady(episode)) { notify("创作输入尚未就绪，不能冻结生产清单"); render(); return; }
      production.prepared = true;
      production.confirmed = false;
      production.invalidated = false;
      production.inputHash = fingerprint(items);
      production.confirmKey = "";
      render();
      requestAnimationFrame(() => document.querySelector('[data-action="confirm-production"]')?.focus());
      return;
    }
    else if (action === "confirm-production") {
      const episode = currentEpisode();
      const production = episode.production;
      const items = manifestItems(episode);
      if (production.prepared && production.inputHash === fingerprint(items)) {
        production.confirmKey = `confirm-${episode.id}-${production.inputHash.slice(-8)}`;
        production.jobs = items.map((item, index) => ({ ...item, id: `JOB-${episode.id}-${String(index + 1).padStart(3, "0")}`, state: item.kind === "asset_ref" ? "review" : item.kind === "frozen_keyframe" || item.kind === "video" ? "blocked" : "queued" }));
        production.confirmed = true;
        production.invalidated = false;
        state.overlay = null;
        notify(`${production.jobs.length} 个稳定任务已经创建；重复确认不会新增任务`);
      }
    }
    else if (action === "review-job") {
      const episode = currentEpisode();
      const job = episode.production.jobs.find((item) => item.id === button.dataset.id);
      job.state = "current";
      const ref = episode.production.refs.find((item) => item.id === job.resultRef);
      if (ref) ref.state = "approved";
      reconcileJobs(episode);
      notify(`${job.id} 的结果已通过审阅，可以成为后续任务的真实 reference`);
    }
    else if (action === "advance-job") {
      const episode = currentEpisode();
      const job = episode.production.jobs.find((item) => item.id === button.dataset.id);
      job.state = "review";
      notify(`${job.id} 已完成，结果等待创作者审阅`);
    }

    render();
  });

  app.addEventListener("change", (event) => {
    if (event.target.matches('[data-field="output-preset"]')) {
      const episode = currentEpisode();
      const production = episode.production;
      const changed = production.outputPreset !== event.target.value;
      production.outputPreset = event.target.value;
      if (changed && (production.prepared || production.confirmed)) {
        invalidateProduction(episode);
        notify("输出规格已改变；旧 prepare 与确认已失效，请重新准备");
      }
      render();
      requestAnimationFrame(() => document.querySelector('[data-field="output-preset"]')?.focus());
    }
  });

  app.addEventListener("input", (event) => {
    if (!state.intakeDraft) return;
    if (event.target.id === "intake-name") state.intakeDraft.name = event.target.value;
    else if (event.target.id === "intake-source") state.intakeDraft.texts[state.intakeType] = event.target.value;
  });

  window.addEventListener("keydown", (event) => {
    if (!state.overlay) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeOverlay();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...document.querySelectorAll('.overlay button:not([disabled]), .overlay input:not([disabled]), .overlay textarea:not([disabled]), .overlay select:not([disabled])')];
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  render();
})();
