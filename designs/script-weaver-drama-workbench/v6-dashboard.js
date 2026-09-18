(() => {
  const app = document.getElementById("app");
  const mediaRoot = "assets/visual-loop/";
  const docs = ["剧本", "视觉设定", "图片提示词", "分镜", "视频提示词"];
  const episodes = [
    { id: "EP01", title: "没有寄出的声音", state: "制作中", tone: "good", note: "4/5 文档当前 · 1 个本地视频任务" },
    { id: "EP02", title: "被剪断的雨声", state: "分镜中", tone: "", note: "静态漫剧 · 跳过视频提示词" },
    { id: "EP03", title: "第一次篡改", state: "待确认", tone: "warn", note: "剧本 v2 等待整体确认" },
    { id: "EP04", title: "回声借来的脸", state: "未开始", tone: "", note: "等待上一集故事决定" }
  ];

  const state = {
    view: "dashboard",
    episode: "EP03",
    doc: "剧本",
    panel: false,
    decision: "idle",
    selectedCandidate: 0,
    revision: false,
    officialRef: false,
    production: null,
    videoQueued: true,
    notice: ""
  };

  const esc = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
  const currentEpisode = () => episodes.find((episode) => episode.id === state.episode) || episodes[0];

  function topbar() {
    return `<header class="topbar">
      <button class="brand" data-action="home" aria-label="回到项目总览">
        <span class="brand-mark">场</span>
        <span class="brand-copy"><strong>Script Weaver</strong><small>短剧创作台</small></span>
      </button>
      <div class="top-actions">
        <button class="runtime" data-action="runtime">局域网 GPU · H3 Ref2VA 在线</button>
        ${state.view === "workbench" ? `<button class="quiet" data-action="home">项目总览</button>` : `<button class="quiet" data-action="open-workbench" data-episode="EP01" data-doc="剧本">打开创作区</button>`}
      </div>
    </header>`;
  }

  function episodeCard(episode, index) {
    return `<button class="episode-card" data-action="open-workbench" data-episode="${episode.id}" data-doc="剧本">
      <span class="episode-no">${String(index + 1).padStart(2, "0")}</span>
      <span class="episode-copy"><strong>${episode.id} · ${episode.title}</strong><small>${episode.note}</small></span>
      <span class="episode-state ${episode.tone}">${episode.state}</span>
    </button>`;
  }

  function mediaCard(image, type, title, meta, action = "") {
    return `<button class="media-card" ${action ? `data-action="${action}"` : ""}>
      <span class="media-visual">${image ? `<img src="${mediaRoot}${image}" alt="">` : `<span class="media-placeholder">${type === "视频" ? "▶" : "波形"}</span>`}<span class="media-type">${type}</span></span>
      <span class="media-copy"><strong>${title}</strong><small>${meta}</small></span>
    </button>`;
  }

  function docCell(ep, doc, status, detail) {
    const cls = status === "需同步" ? "stale" : status === "未开始" ? "missing" : "";
    return `<button class="doc-cell ${cls}" data-action="open-workbench" data-episode="${ep}" data-doc="${doc}"><b>${doc}</b>${status} · ${detail}</button>`;
  }

  function dashboard() {
    return `<main class="dashboard" data-screen-label="V6 项目 Dashboard">
      <section class="hero">
        <div>
          <span class="eyebrow">Project overview</span>
          <h1>孤身入魔</h1>
          <p class="hero-copy">从故事想法到分集剧本、视觉设定、分镜和制作成果。这里先帮助你找回创作上下文，不要求理解后台工作流。</p>
          <div class="next-card">
            <span class="next-index">续</span>
            <span class="next-copy"><span>最值得继续的一件事</span><strong>确认 EP03 剧本 v2 的越界代价是否成立</strong></span>
            <button class="primary" data-action="open-workbench" data-episode="EP03" data-doc="剧本">继续创作</button>
          </div>
          <div class="stats">
            <div class="stat"><strong>06</strong><span>计划分集</span></div>
            <div class="stat"><strong>03</strong><span>正在创作</span></div>
            <div class="stat"><strong>12</strong><span>正式 REF</span></div>
            <div class="stat"><strong>01</strong><span>本地生成中</span></div>
          </div>
        </div>
        <div class="hero-side">
          <div class="section-head"><div><span class="eyebrow">Episodes</span><h2>分集进度</h2></div><span>按真正的创作状态排序</span></div>
          <div class="episode-list">${episodes.slice(0, 3).map(episodeCard).join("")}</div>
        </div>
      </section>

      <section class="dashboard-section">
        <div class="section-head"><div><span class="eyebrow">Media</span><h2>最近的创作成果</h2></div><span>候选和正式媒体在卡片上直接区分</span></div>
        <div class="media-grid">
          ${mediaCard("shot-kf-004.png", "冻结关键帧", "KF-004 · 母带转移", "正式输入 · EP01", "open-video-doc")}
          ${mediaCard("prop-revision-b1.png", "正式 REF", "REF-PROP-001-v1", "无日期母带 · 已接受", "open-prop")}
          ${mediaCard("prop-candidate-suite.png", "候选组", "PROP-001 · 视觉探索", "3 张 · 尚未接受", "open-prop")}
          ${mediaCard("", "视频", "SHOT-004 · H3 生成中", "本地 Ref2VA · 6 秒", "open-production-status")}
          ${mediaCard("", "声音", "许真 · 声音身份样本", "正式参考 · 全剧", "")}
        </div>
      </section>

      <section class="content-grid">
        <article class="doc-map">
          <div class="section-head"><div><span class="eyebrow">Creator documents</span><h3>每集内容</h3></div><span>不是瀑布步骤，是当前可读成果</span></div>
          <div class="doc-table">
            <div class="doc-row"><strong>EP01</strong>${docCell("EP01", "剧本", "当前", "v1")}${docCell("EP01", "视觉设定", "当前", "v2")}${docCell("EP01", "图片提示词", "当前", "v1")}${docCell("EP01", "分镜", "当前", "v2")}${docCell("EP01", "视频提示词", "当前", "v1")}</div>
            <div class="doc-row"><strong>EP02</strong>${docCell("EP02", "剧本", "当前", "v1")}${docCell("EP02", "视觉设定", "当前", "v1")}${docCell("EP02", "图片提示词", "当前", "v1")}${docCell("EP02", "分镜", "待确认", "v1")}${docCell("EP02", "视频提示词", "已跳过", "静态")}</div>
            <div class="doc-row"><strong>EP03</strong>${docCell("EP03", "剧本", "待确认", "v2")}${docCell("EP03", "视觉设定", "需同步", "v1")}${docCell("EP03", "图片提示词", "需同步", "v1")}${docCell("EP03", "分镜", "需同步", "v1")}${docCell("EP03", "视频提示词", "未开始", "等待分镜")}</div>
          </div>
        </article>
        <article class="activity">
          <div class="section-head"><div><span class="eyebrow">Creative trace</span><h3>最近发生</h3></div><button class="text-button" data-action="notify" data-message="这里只保留创作者看得懂的决定，不展示后台事务日志">为何这样显示？</button></div>
          <div class="activity-list">
            <div class="activity-item"><span class="activity-dot"></span><p><strong>EP03 剧本 v2</strong> 已由 Codex 提交，等待整体确认。</p><time>8 分钟</time></div>
            <div class="activity-item"><span class="activity-dot"></span><p><strong>SHOT-004</strong> 已交给局域网 H3 Ref2VA，输入仍可追溯。</p><time>14 分钟</time></div>
            <div class="activity-item"><span class="activity-dot"></span><p><strong>REF-PROP-001-v1</strong> 已被接受为无日期母带正式参考。</p><time>昨天</time></div>
          </div>
        </article>
      </section>
    </main>`;
  }

  function rail() {
    return `<aside class="content-rail">
      <div class="rail-head"><div class="breadcrumb"><span>孤身入魔</span><span>›</span><span>${state.episode}</span></div><h2>${currentEpisode().title}</h2></div>
      <nav class="rail-nav" aria-label="分集创作内容">
        <button class="rail-link" data-action="home"><span>项目总览</span><small>全剧</small></button>
        ${episodes.map((episode) => `<div class="rail-episode">${episode.id} · ${episode.title}</div>${docs.map((doc) => {
          const active = episode.id === state.episode && doc === state.doc;
          const warn = episode.id === "EP03" && doc !== "剧本";
          const status = episode.id === "EP02" && doc === "视频提示词" ? "跳过" : episode.id === "EP03" && doc === "剧本" ? "待确认" : warn ? "需同步" : "当前";
          return `<button class="rail-link ${active ? "active" : ""}" data-action="open-workbench" data-episode="${episode.id}" data-doc="${doc}"><span>${doc}</span><small class="${warn ? "warn" : ""}">${status}</small></button>`;
        }).join("")}`).join("")}
        <div class="rail-episode">制作成果</div>
        <button class="rail-link" data-action="open-video-doc"><span>图片、视频与声音</span><small>7 项</small></button>
      </nav>
    </aside>`;
  }

  function documentContent() {
    const ep = currentEpisode();
    if (state.doc === "视频提示词") return `<span class="doc-kicker">${ep.id} · Motion document</span><h2>视频提示词</h2>
      <h3 class="scene-heading">MOTION-004 ← SHOT-004 · 6 秒 · 9:16</h3>
      <div class="prompt-block"><strong>可复制正文</strong>冻结关键帧中的许真紧贴档案架，右手将闭合的无日期母带按入外套内袋。她听见门外脚步声后停止动作，视线先落向门缝，再缓慢抬向镜头左侧。镜头以极慢速度向前推进，背景保持压暗，衣料和呼吸产生细微运动。终点是母带完全进入内袋、盒盖仍闭合，人物保持警觉。</div>
      <p>冻结起点来自 <button class="entity" data-action="open-prop">KF-004</button>，正式道具参考为 <button class="entity" data-action="open-prop">REF-PROP-001-v1</button>。</p>
      <div class="document-note">这份正文保持模型无关。选择 H3 后，prepare 阶段会把它编译为 Ref2VA 的文本和媒体输入。</div>`;
    if (state.doc === "视觉设定") return `<span class="doc-kicker">${ep.id} · Visual facts</span><h2>视觉设定</h2><h3>PROP-001 · 无日期母带</h3><p>黑色塑料保护盒，无标签、日期或可见文字。盒盖保持闭合。当前场景状态 <button class="entity" data-action="open-prop">PSTATE-001-B</button>：由许真持有，位于右侧外套内袋。</p><h3>地点 · 档案库 B 区</h3><p>低照度窄纵深空间，金属货架形成重复线条，走道尽头保留一处冷白灯。</p><div class="document-note">视觉事实是可复用约束，不是提示词。选中人物、地点或道具即可让 Codex 生成候选。</div>`;
    if (state.doc === "分镜") return `<span class="doc-kicker">${ep.id} · Storyboard</span><h2>分镜</h2><h3 class="scene-heading">SHOT-004 · 把证据藏进衣袋</h3><p><strong>职责：</strong>第一次把“偷听未来”从能力变成主动越界。</p><p><strong>冻结起点：</strong>许真已取出 <button class="entity" data-action="open-prop">无日期母带</button>，盒盖闭合，右手贴近外套内袋。</p><p><strong>可信终点：</strong>母带完全进入内袋，她听见门外脚步并停止动作。</p><img class="revision-image" src="${mediaRoot}shot-kf-004.png" alt="SHOT-004 冻结关键帧"><div class="document-note">正式 REF 被替换时，这个关键帧只标记需要复核，不自动重新生成。</div>`;
    if (state.doc === "图片提示词") return `<span class="doc-kicker">${ep.id} · Image prompts</span><h2>图片提示词</h2><h3>IMG-PROP-001 · 无日期母带</h3><div class="prompt-block"><strong>可复制正文</strong>一只小型黑色塑料磁带保护盒，表面无标签、无日期、无文字，盒盖闭合，轻微使用痕迹，置于中性深褐色工作台，柔和侧光，正视产品参考图，完整轮廓清晰。</div><button class="primary" data-action="open-prop">生成视觉候选</button>`;
    return `<span class="doc-kicker">${ep.id} · Screenplay candidate v2</span><h2>${ep.title}</h2>
      <h3 class="scene-heading">${ep.id}-SC03　内景 · 档案库 B 区 · 深夜</h3>
      <p class="script-action">门禁灯由绿转红。许真把耳机摘下，仍能听见尚未发生的争吵从货架深处传来。</p>
      <div class="dialogue"><strong>许真</strong><p>如果明天一定会有人说谎，那我今天先听见，算不算救人？</p></div>
      <p class="script-action">她抽出一盒 <button class="entity" data-action="open-prop">无日期母带</button>。盒盖闭合，表面没有任何标签。脚步声逼近，她没有把它放回去。</p>
      <div class="dialogue"><strong>系统录音</strong><p>你每改掉一句，都会有人替你记住原来的版本。</p></div>
      <p class="script-action">许真把母带藏进外套内袋。门外的人停下。她第一次主动切断了监听。</p>
      <div class="document-note"><strong>整体确认前的真实分叉：</strong>这一版把“越界代价”从抽象警告改成可追踪的记忆转移。接受后，视觉设定和旧分镜会提示需要同步，但旧版本不会消失。</div>`;
  }

  function codexPanel() {
    if (!state.panel) return "";
    const generated = state.decision !== "idle";
    return `<aside class="codex-panel" data-screen-label="上下文 Codex 创作面板">
      <header class="panel-head"><div><small>Codex · 当前选择</small><h2>PROP-001 · 无日期母带</h2></div><button class="panel-close" data-action="close-panel" aria-label="关闭">×</button></header>
      <div class="panel-body">
        <section class="panel-section"><h3>当前事实</h3><p>盒盖闭合；无日期、标签或文字；PSTATE-001-B 由许真持有。</p><div class="fact-list"><div class="fact"><span><b>来源</b><small>${state.episode} 剧本 · SC03</small></span><span class="pill">事实</span></div><div class="fact"><span><b>正式参考</b><small>${state.officialRef ? "REF-PROP-001-v2" : "REF-PROP-001-v1"}</small></span><span class="pill good">正式</span></div></div></section>
        ${!generated ? `<section class="panel-section"><h3>你想继续什么？</h3><p>Codex 会基于当前事实生成图片候选。候选不会自动改变正式 REF。</p><div class="button-row" style="margin-top:10px"><button class="primary" data-action="generate-candidates">生成 3 张候选</button></div></section>` : ""}
        ${generated && !state.officialRef ? `<section class="panel-section"><h3>${state.revision ? "基于候选 B 的修订" : "比较候选"}</h3><p>${state.revision ? "新图仍是候选，原图和正式 REF 都没有被覆盖。" : "选择一张继续修改，或直接接受为正式参考。"}</p>${state.revision ? `<img class="revision-image" src="${mediaRoot}prop-revision-b1.png" alt="母带修订候选">` : `<div class="candidate-grid">${[1,2,3].map((number) => `<button class="candidate ${state.selectedCandidate === number ? "selected" : ""}" data-action="select-candidate" data-id="${number}" aria-label="候选 ${number}"><img src="${mediaRoot}prop-candidate-suite.png" alt=""></button>`).join("")}</div>`}
          ${state.selectedCandidate && !state.revision ? `<textarea class="edit-field" id="editPrompt">保留盒盖闭合，削弱表面划痕，保持无文字。</textarea><div class="button-row" style="margin-top:8px"><button class="secondary" data-action="revise-candidate">继续局部编辑</button><button class="primary" data-action="accept-ref">接受为正式 REF</button></div>` : ""}
          ${state.revision ? `<div class="button-row" style="margin-top:10px"><button class="secondary" data-action="back-candidates">返回比较</button><button class="primary" data-action="accept-ref">接受为正式 REF</button></div>` : ""}
        </section>` : ""}
        ${state.officialRef ? `<section class="panel-section"><h3>已登记为 REF-PROP-001-v2</h3><p>旧的 v1 保留。下游不会自动重新生成。</p><ul class="impact-list"><li><span>SHOT-004</span><b>需要复核</b></li><li><span>KF-004</span><b>stale</b></li><li><span>MOTION-004</span><b>stale</b></li></ul></section>` : ""}
      </div>
    </aside>`;
  }

  function nextAction() {
    if (state.doc === "剧本" && state.episode === "EP03") return `<div class="next-inline"><span>下一步</span><strong>整体接受剧本 v2，或继续要求 Codex 修改</strong></div><div class="button-row"><button class="secondary" data-action="notify" data-message="已打开反馈输入，正式剧本仍保持 v1">继续修改</button><button class="primary" data-action="accept-screenplay">整体接受 v2</button></div>`;
    if (state.doc === "视频提示词") return `<div class="next-inline"><span>下一步</span><strong>将当前镜头交给已连接的生成端</strong></div><button class="primary" data-action="open-production">准备本地 H3 生成</button>`;
    return `<div class="next-inline"><span>下一步</span><strong>选中正文中的人物、地点或道具，让 Codex 继续</strong></div><button class="secondary" data-action="open-panel">打开 Codex</button>`;
  }

  function workbench() {
    return `<main class="workbench" data-screen-label="V6 文档创作工作台">${rail()}<section class="workspace-main">
      <header class="workspace-toolbar"><div><small>${state.episode} · ${state.doc}</small><h1>${currentEpisode().title}</h1></div><div class="status-line"><span class="episode-state ${state.episode === "EP03" ? "warn" : "good"}">${state.episode === "EP03" ? "待整体确认" : "当前版本"}</span><button class="quiet" data-action="open-panel">Codex</button></div></header>
      <div class="workspace-stage ${state.panel ? "" : "panel-closed"}"><div class="document-scroll"><article class="document-paper">${documentContent()}</article></div>${codexPanel()}</div>
      <footer class="workspace-next">${nextAction()}</footer>
    </section></main>`;
  }

  function productionSheet() {
    if (!state.production) return "";
    const prepared = state.production === "prepared";
    const running = state.production === "running";
    return `<div class="overlay" role="presentation"><section class="sheet" role="dialog" aria-modal="true" aria-labelledby="production-title" data-screen-label="本地 H3 生产确认">
      <header class="sheet-head"><div><span class="eyebrow">${running ? "Local generation" : "Prepare → confirm"}</span><h2 id="production-title">${running ? "SHOT-004 正在本地生成" : prepared ? "确认交给 MiniMax H3？" : "准备本地视频生成"}</h2></div><button class="panel-close" data-action="close-overlay" aria-label="关闭">×</button></header>
      ${running ? `<div class="result-state"><span class="loader" aria-hidden="true"></span><strong>局域网 GPU 正在运行 H3 Ref2VA</strong><span class="episode-state">任务已经创建，关闭窗口不会中断生成</span></div>` : `<div class="sheet-body">
        <div class="production-route"><div class="route-step active"><span>01 · SOURCE</span><strong>MOTION-004 · 当前</strong></div><div class="route-step ${prepared ? "active" : ""}"><span>02 · ADAPTER</span><strong>minimax_h3_local</strong></div><div class="route-step ${prepared ? "active" : ""}"><span>03 · OUTPUT</span><strong>候选媒体，不自动接受</strong></div></div>
        <div class="manifest">
          <div class="manifest-row"><span>执行端</span><strong>局域网 GPU · http://h3-studio.local:30011</strong></div>
          <div class="manifest-row"><span>模型</span><strong>MiniMax H3-Base Ref2VA · 本地 768p</strong></div>
          <div class="manifest-row"><span>镜头正文</span><strong>MOTION-004 · 6 秒 · 9:16 · 中文对白</strong></div>
          <div class="manifest-row"><span>参考输入</span><strong>KF-004 首帧 + REF-PROP-001-v1 + 人物造型 REF</strong></div>
          <div class="manifest-row"><span>输出</span><strong>EP01 / 制作成果 / SHOT-004-h3-candidate-01.mp4</strong></div>
          <div class="manifest-row"><span>来源追溯</span><strong>EP01 视频提示词 v1 · 分镜 v2 · 当前 REF 字节快照</strong></div>
        </div>
        <div class="sheet-note">这是完全本地的 H3-Base 路径，输出为 768p。不会调用 MiniMax 云端 Context-IR 或 2K Regenerate，也不会把项目素材上传到外网。</div>
      </div><footer class="sheet-foot"><button class="secondary" data-action="close-overlay">返回修改</button>${prepared ? `<button class="danger" data-action="confirm-production">我已核对，创建本地任务</button>` : `<button class="primary" data-action="prepare-production">冻结这份输入预览</button>`}</footer>`}
    </section></div>`;
  }

  function render() {
    app.innerHTML = `<div class="shell">${topbar()}${state.view === "dashboard" ? dashboard() : workbench()}${productionSheet()}${state.notice ? `<div class="notice" role="status">${esc(state.notice)}</div>` : ""}</div>`;
  }

  function notify(message) {
    state.notice = message;
    render();
    window.clearTimeout(notify.timer);
    notify.timer = window.setTimeout(() => { state.notice = ""; render(); }, 3000);
  }

  app.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "home") { state.view = "dashboard"; state.panel = false; state.production = null; render(); }
    else if (action === "open-workbench") { state.view = "workbench"; state.episode = button.dataset.episode || "EP01"; state.doc = button.dataset.doc || "剧本"; state.panel = false; state.production = null; render(); }
    else if (action === "open-video-doc") { state.view = "workbench"; state.episode = "EP01"; state.doc = "视频提示词"; state.panel = false; render(); }
    else if (action === "open-prop" || action === "open-panel") { state.view = "workbench"; state.panel = true; state.decision = action === "open-prop" ? state.decision : "idle"; render(); }
    else if (action === "close-panel") { state.panel = false; render(); }
    else if (action === "generate-candidates") { state.decision = "candidates"; state.selectedCandidate = 0; state.revision = false; notify("Codex 已返回 3 张候选；正式 REF 没有变化"); }
    else if (action === "select-candidate") { state.selectedCandidate = Number(button.dataset.id); render(); }
    else if (action === "revise-candidate") { state.revision = true; notify("已基于候选 B 生成修订图，没有覆盖任何已有媒体"); }
    else if (action === "back-candidates") { state.revision = false; render(); }
    else if (action === "accept-ref") { state.officialRef = true; state.decision = "accepted"; notify("REF-PROP-001-v2 已登记；相关分镜、关键帧和视频提示词已标记需要复核"); }
    else if (action === "accept-screenplay") { notify("EP03 剧本 v2 已成为正式版本；视觉设定和旧分镜保留但需要同步"); }
    else if (action === "open-production") { state.production = "draft"; render(); }
    else if (action === "prepare-production") { state.production = "prepared"; render(); }
    else if (action === "confirm-production") { state.production = "running"; state.videoQueued = true; render(); }
    else if (action === "open-production-status") { state.view = "workbench"; state.episode = "EP01"; state.doc = "视频提示词"; state.production = "running"; render(); }
    else if (action === "close-overlay") { state.production = null; render(); }
    else if (action === "runtime") { notify("本地运行时只负责媒体生成；脚本、提示词和正式资产仍由 Script Weaver daemon 管理"); }
    else if (action === "notify") notify(button.dataset.message || "已完成");
  });

  render();
})();
