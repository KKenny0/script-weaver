(function () {
  "use strict";

  const root = document.getElementById("root");
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const statusLabel = { planned: "PLANNED", queued: "QUEUED", running: "RUNNING", submitted: "SUBMITTED", accepted: "ACCEPTED", stale: "STALE", locked: "LOCKED", ready: "READY" };
  const stages = [
    ["development", "故事开发", "01"], ["screenplay", "多集剧本", "02"], ["visual", "视觉设定", "03"],
    ["storyboard", "分镜", "04"], ["motion", "动态提示词", "05"], ["production", "生产交接", "06"]
  ];

  const episodes = [
    { id: "EP01", title: "没有寄出的声音", promise: "陌生遗言指向失联父亲", screenplay: "accepted", visual: "accepted", storyboard: "accepted", motion: "submitted", production: "locked", scenes: 2, shots: 6, duration: "01:46" },
    { id: "EP02", title: "被剪断的波形", promise: "缺口证明录音曾被人为处理", screenplay: "accepted", visual: "submitted", storyboard: "locked", motion: "locked", production: "locked", scenes: 3, shots: 0, duration: "02:05" },
    { id: "EP03", title: "第一次篡改", promise: "许真为了保护委托人删除一句话", screenplay: "submitted", visual: "locked", storyboard: "locked", motion: "locked", production: "locked", scenes: 3, shots: 0, duration: "01:58" },
    { id: "EP04", title: "销毁母带", promise: "父亲出现并要求她永久删除证据", screenplay: "running", visual: "locked", storyboard: "locked", motion: "locked", production: "locked", scenes: 0, shots: 0, duration: "—" },
    { id: "EP05", title: "无辜的人", promise: "公开真相将伤害另一个家庭", screenplay: "queued", visual: "locked", storyboard: "locked", motion: "locked", production: "locked", scenes: 0, shots: 0, duration: "—" },
    { id: "EP06", title: "她自己的录音", promise: "许真留下自己的版本并承担代价", screenplay: "planned", visual: "locked", storyboard: "locked", motion: "locked", production: "locked", scenes: 0, shots: 0, duration: "—" }
  ];

  const assets = [
    { id: "CHAR-001", type: "人物身份", name: "许真", scope: "全剧共享", status: "accepted", anchor: "窄长脸、左眉尾浅疤、齐肩黑发；工作时总戴旧银色监听耳机。", variant: "EP01 夜班：深灰针织衫、袖口卷起；无伤。", occurs: "EP01-SC01 · EP01-SC02", lineage: "剧本 EP01 v1" },
    { id: "LOOK-001-A", type: "造型变体", name: "许真 · 夜班", scope: "EP01", status: "accepted", anchor: "继承 CHAR-001 的脸、发型与监听耳机。", variant: "深灰针织衫，右侧外套内袋在 SC02 后装有母带。", occurs: "EP01-SC01 → EP01-SC02", lineage: "CHAR-001 + EP01 v1" },
    { id: "LOC-001", type: "地点身份", name: "深夜录音室", scope: "EP01–EP06", status: "accepted", anchor: "狭长控制室、胡桃木台面、磨损的红色录音灯、后墙三层母带架。", variant: "EP01 暴雨夜；窗面有连续水痕，室内仅工作灯。", occurs: "EP01-SC01", lineage: "剧本 EP01 v1" },
    { id: "PROP-001", type: "功能道具", name: "无日期母带", scope: "EP01–EP04", status: "accepted", anchor: "透明塑料盒，米白纸套，蓝黑钢笔字：不要让真真听。", variant: "EP01 末：由档案架转移至许真右侧外套内袋，盒盖闭合。", occurs: "EP01-SC02", lineage: "剧本 EP01 v1" },
    { id: "LOOK-001-B", type: "造型变体", name: "许真 · 修复日", scope: "EP02", status: "submitted", anchor: "继承 CHAR-001 的脸、发型与监听耳机。", variant: "米灰衬衫，袖口有磁粉污迹；母带仍在右侧内袋。", occurs: "EP02-SC01 → EP02-SC03", lineage: "CHAR-001 + 剧本 EP02 v1" }
  ];

  const imagePrompts = [
    { id: "IMG-CHAR-001", name: "许真身份板", purpose: "身份板", status: "accepted", controls: "人物身份、发型、面部锚点", prompt: "Character identity sheet for Xu Zhen, a Chinese woman in her early thirties, narrow long face, a faint scar at the tail of her left eyebrow, shoulder-length black hair, neutral front portrait and two profile views, restrained documentary lighting, plain warm-gray background, no text, no logo, no costume variation." },
    { id: "IMG-LOC-001", name: "深夜录音室地点板", purpose: "地点板", status: "submitted", controls: "空间地理、固定锚点、雨夜光态", prompt: "Location plate for a narrow late-night audio control room: walnut console desk, worn red recording lamp, three-tier analog tape archive on the rear wall, rain trails on the single window, practical work lights only, clear geography, empty room, vertical-drama framing, no people, no readable brand marks." },
    { id: "IMG-PROP-001", name: "无日期母带道具板", purpose: "道具板", status: "accepted", controls: "尺度、材质、盒盖状态", prompt: "Prop reference plate of one transparent compact cassette case with an aged ivory paper sleeve and blue-black handwritten Chinese note, closed state, front/back/side scale comparison, neutral evidence-table lighting, no hands, no extra labels, no logo." }
  ];

  const references = [
    { id: "REF-001", name: "许真身份参考", source: "创作者提供", status: "approved", controls: "脸型、眉尾浅疤、发型", excludes: "服装、构图、情绪", tone: "portrait" },
    { id: "REF-002", name: "录音室地理参考", source: "待生成并确认", status: "missing", controls: "尚未建立", excludes: "不可作为生产输入", tone: "room" },
    { id: "REF-003", name: "母带道具参考", source: "生产结果待审", status: "review", controls: "盒体比例、纸套材质", excludes: "手写文字内容", tone: "prop" }
  ];

  const shots = [
    { id: "SHOT-001", duty: "建立许真的夜班秩序", duration: "7s", frame: "中近景 · 固定机位", start: "许真独自在控制台前，红灯未亮。", end: "她按下播放，红灯亮起。", refs: "REF-001 控制身份；地点直接引用 LOC-001", prompt: "Vertical cinematic keyframe, Xu Zhen seated alone at the walnut audio console before playback, worn red recording lamp still dark, rain streaks on the narrow window, restrained work light, medium close shot, right hand resting near the space bar, no cassette visible." },
    { id: "SHOT-002", duty: "遗言第一次越过职业边界", duration: "12s", frame: "耳机侧面特写 · 缓慢推进", start: "许真听见普通底噪。", end: "录音提到父亲，她的手停住。", refs: "REF-001；IMG-CHAR-001", prompt: "Frozen opening frame: close side profile of Xu Zhen wearing old silver monitoring headphones, waveform glow reflected faintly on her cheek, fingers still moving over the keyboard, calm professional posture before the triggering line is heard." },
    { id: "SHOT-003", duty: "把线索变成可见物证", duration: "9s", frame: "俯拍插入镜头", start: "档案架中的盒脊排列整齐。", end: "她抽出母亲笔迹的母带。", refs: "IMG-PROP-001；LOC-001", prompt: "Top-down frozen frame of an orderly analog tape shelf before Xu Zhen touches it, one transparent cassette case with an aged ivory sleeve barely visible among numbered spines, low practical light, no hand yet in frame." },
    { id: "SHOT-004", duty: "建立跨集持物连续性", duration: "6s", frame: "腰部近景 · 手持", start: "母带在她右手，盒盖闭合。", end: "母带进入右侧外套内袋。", refs: "REF-001；IMG-PROP-001", prompt: "Waist-level vertical keyframe, Xu Zhen holding the closed cassette case in her right hand beside her dark coat, right inner pocket open and empty at the starting instant, archive room doorway soft in the background." }
  ];

  const motions = [
    { id: "MOTION-001", shot: "SHOT-001", mode: "文生视频", status: "accepted", change: "她按下播放键，红灯由暗转亮。", end: "灯亮后保持 1 秒，人物尚未听见异常。" },
    { id: "MOTION-002", shot: "SHOT-002", mode: "图生视频", status: "submitted", change: "波形继续，她的手从敲击变为完全静止，眼神第一次偏离屏幕。", end: "手悬停，呼吸停半拍，不转头。" },
    { id: "MOTION-003", shot: "SHOT-003", mode: "文生视频", status: "accepted", change: "右手从画外进入，越过两个盒脊，抽出目标母带。", end: "目标槽位空出，盒盖仍闭合。" },
    { id: "MOTION-004", shot: "SHOT-004", mode: "图生视频", status: "accepted", change: "母带从右手进入右侧内袋，左手压住外套。", end: "右手离开口袋，母带不可见。" }
  ];

  const state = { theme: "light", stage: "screenplay", episode: "EP01", visualTab: "assets", selectedAsset: "CHAR-001", selectedPrompt: "IMG-CHAR-001", selectedRef: "REF-001", selectedShot: "SHOT-001", selectedMotion: "MOTION-001", modal: null, notice: "" };
  let noticeTimer;

  const currentEpisode = () => episodes.find((episode) => episode.id === state.episode);
  const selected = (collection, id) => collection.find((item) => item.id === id);
  const notify = (message) => { state.notice = message; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { state.notice = ""; render(); }, 2400); };
  const stageStatus = (id) => {
    if (id === "development") return "ACCEPTED";
    if (id === "screenplay") return `${episodes.filter((e) => e.screenplay === "accepted").length}/6 ACCEPTED`;
    const episode = currentEpisode();
    return statusLabel[episode[id]] || "READY";
  };

  function masthead() {
    return `<header class="masthead"><div class="wordmark"><b>Script Weaver</b><span>Drama desk</span></div><div class="project-name">没有寄出的声音 · 6 集</div><div class="mast-actions"><span class="project-health"><i></i>2 集剧本已接受</span><button class="icon-button" data-action="theme" aria-label="切换主题">${state.theme === "light" ? "夜" : "昼"}</button></div></header>`;
  }

  function stageSpine() {
    return `<nav class="stage-spine" aria-label="创作阶段">${stages.map(([id, label, mark]) => `<button class="stage ${state.stage === id ? "active" : ""}" data-action="stage" data-stage="${id}" aria-current="${state.stage === id ? "step" : "false"}"><small>${mark}</small><span><strong>${label}</strong><span class="stage-status">${stageStatus(id)}</span></span></button>`).join("")}</nav>`;
  }

  function episodeRail() {
    return `<aside class="episode-rail"><div class="rail-intro"><span class="eyebrow">Series ledger</span><h2>剧集矩阵</h2><p>每集独立审批；共享资产只复用身份，不继承瞬态。</p></div><div class="episode-list">${episodes.map((episode) => `<button class="episode-row ${state.episode === episode.id ? "active" : ""}" data-action="episode" data-episode="${episode.id}"><span><b>${episode.id}</b><small>${esc(episode.title)}</small></span><i class="status-dot ${episode[state.stage] || episode.screenplay}" aria-label="${statusLabel[episode[state.stage]] || statusLabel[episode.screenplay]}"></i></button>`).join("")}</div><div class="rail-key"><span><i class="status-dot accepted"></i>已接受</span><span><i class="status-dot submitted"></i>待审批</span><span><i class="status-dot locked"></i>未解锁</span></div></aside>`;
  }

  function developmentView() {
    return `<article class="flow-page" data-screen-label="故事开发文档"><header class="page-head"><div><span class="eyebrow">Development document · accepted v2</span><h1>一份文档，驱动六集</h1></div><span class="version-stamp">v2</span></header><div class="development-grid"><section class="editorial-block feature"><span>01 / 创作承诺</span><p>故事不是关于轻易原谅，而是关于一个人是否愿意让真相被完整听见。</p></section><section class="editorial-block"><span>02 / 故事引擎</span><p>每集由一段未完成的录音推动；每次交付都产生选择、代价与不可逆的新事实。</p></section></div><section class="episode-map"><header><div><span class="eyebrow">Episode map</span><h2>六集的递进不是六次重复</h2></div><span>整体接受 · 逐集生产</span></header>${episodes.map((episode, index) => `<button data-action="open-episode" data-episode="${episode.id}"><b>${episode.id}</b><span><strong>${esc(episode.title)}</strong><small>${esc(episode.promise)}</small></span><em>0${index + 1}</em></button>`).join("")}</section></article>`;
  }

  function screenplayView() {
    const episode = currentEpisode();
    const accepted = episode.screenplay === "accepted";
    const submitted = episode.screenplay === "submitted";
    return `<article class="flow-page" data-screen-label="多集剧本工作台"><header class="page-head"><div><span class="eyebrow">Screenplay slate · ${stageStatus("screenplay")}</span><h1>多集不是一个长文档</h1><p>先看整季节奏，再进入每集的独立版本、场次和审批。</p></div><span class="version-stamp">6 EPS</span></header><div class="season-board">${episodes.map((item) => `<button class="episode-card ${state.episode === item.id ? "selected" : ""}" data-action="episode" data-episode="${item.id}"><header><b>${item.id}</b><span class="pill ${item.screenplay}">${statusLabel[item.screenplay]}</span></header><h2>${esc(item.title)}</h2><p>${esc(item.promise)}</p><footer><span>${item.scenes || "—"} 场</span><span>${item.duration}</span></footer></button>`).join("")}</div><section class="episode-reader"><header><div><span class="eyebrow">${episode.id} · screenplay ${episode.screenplay}</span><h2>${esc(episode.title)}</h2></div>${submitted ? `<div class="inline-actions"><button class="secondary" data-action="reject-episode">退回</button><button class="primary" data-action="accept-episode">整体接受</button></div>` : `<span class="pill ${episode.screenplay}">${statusLabel[episode.screenplay]}</span>`}</header>${accepted || submitted ? episodeScript(episode) : `<div class="gate-card"><b>${statusLabel[episode.screenplay]}</b><h3>${episode.screenplay === "running" ? "Codex 正在写这一集" : "等待创建写作任务"}</h3><p>当前集没有可审批正文；其他集的 accepted 事实不受影响。</p>${episode.screenplay === "queued" ? `<button class="primary" data-action="start-writing">模拟 Codex 接手</button>` : ""}</div>`}</section></article>`;
  }

  function episodeScript(episode) {
    const alternate = episode.id !== "EP01";
    return `<div class="script-pages"><section><h3>${episode.id}-SC01 · 内 · ${alternate ? "修复室" : "深夜录音室"} · 夜</h3><p>${alternate ? "许真把被剪断的波形放大。缺口的长度，刚好够藏下一句话。" : "红色录音灯亮着。许真戴上耳机，替一个已经去世的女人清理遗言里的杂音。"}</p><p class="dialogue">许真：${alternate ? "不是噪声，是有人动过它。" : "再放一次。"}</p></section><section><h3>${episode.id}-SC02 · 内 · 档案间 · 连续</h3><p>${alternate ? "她在登记本上找到同一批母带。父亲的签名出现在处理人一栏。" : "许真找到没有日期的母带。盒脊上只有母亲的笔迹：不要让真真听。"}</p><div class="continuity-note">连续性 · ${alternate ? "上一集母带仍在右侧内袋；父亲尚不知道。" : "本场末母带进入右侧外套内袋，盒盖闭合。"}</div></section></div>`;
  }

  function visualView() {
    const episode = currentEpisode();
    if (episode.visual === "locked") return gateView("视觉设定", `${episode.id} 剧本尚未接受`, "只有当前集 accepted screenplay 才能成为资产事实的正式来源。", "screenplay");
    if (episode.visual === "queued" || episode.visual === "ready") return taskReadyView("视觉设定", `${episode.id} 已具备正式上游`, "accepted screenplay 已冻结；创建任务后，Codex 才会拆分身份、变体、地点、道具和跨集 outgoing。", "start-visual", "创建视觉设定任务");
    if (episode.visual === "running") return taskRunningView("视觉设定", `${episode.id} 正在拆解视觉事实`, "Codex 正在比较新身份、复用项与状态变体；现有 accepted 事实尚未改变。");
    const tabs = [["assets", "视觉事实"], ["prompts", "图片提示词"], ["references", "真实参考图"]];
    return `<article class="flow-page visual-workbench" data-screen-label="视觉设定工作台"><header class="page-head"><div><span class="eyebrow">Visual development · ${episode.id}</span><h1>先固定事实，再生成图片</h1><p>身份、变体和镜头瞬态分开；提示词条目不冒充真实参考图。</p></div><span class="pill ${episode.visual}">${statusLabel[episode.visual]}</span></header><nav class="subtabs" aria-label="视觉设定视图">${tabs.map(([id, label]) => `<button class="${state.visualTab === id ? "active" : ""}" data-action="visual-tab" data-tab="${id}">${label}<small>${id === "assets" ? assets.length : id === "prompts" ? imagePrompts.length : references.length}</small></button>`).join("")}</nav>${state.visualTab === "assets" ? assetPanel() : state.visualTab === "prompts" ? promptPanel() : referencePanel()}${episode.visual === "submitted" ? `<div class="bottom-approval"><span>EP02 视觉设定 v1 已提交，整体审批后才解锁分镜。</span><button class="primary" data-action="accept-visual">整体接受视觉设定</button></div>` : ""}</article>`;
  }

  function assetPanel() {
    const asset = selected(assets, state.selectedAsset);
    return `<div class="catalog-layout"><section class="catalog-list"><header><span class="eyebrow">Asset registry</span><button class="text-button" data-action="simulate-asset">＋ 新建变体</button></header>${assets.map((item) => `<button class="catalog-item ${item.id === asset.id ? "active" : ""}" data-action="asset" data-id="${item.id}"><span class="asset-mark ${item.type.includes("人物") || item.type.includes("造型") ? "human" : item.type.includes("地点") ? "place" : "prop"}">${item.type.slice(0, 1)}</span><span><b>${esc(item.name)}</b><small>${item.id} · ${item.scope}</small></span><i class="status-dot ${item.status}"></i></button>`).join("")}</section><section class="catalog-detail"><header><div><span class="eyebrow">${asset.id} · ${asset.type}</span><h2>${esc(asset.name)}</h2></div><span class="pill ${asset.status}">${statusLabel[asset.status]}</span></header><dl><div><dt>识别锚点</dt><dd>${esc(asset.anchor)}</dd></div><div><dt>本集变体</dt><dd>${esc(asset.variant)}</dd></div><div><dt>出现证据</dt><dd>${esc(asset.occurs)}</dd></div><div><dt>版本 lineage</dt><dd>${esc(asset.lineage)}</dd></div></dl><div class="continuity-strip"><span>INCOMING</span><b>${asset.id === "PROP-001" ? "档案架 · 盒盖闭合" : "身份锚点保持"}</b><span>OUTGOING</span><b>${asset.id === "PROP-001" ? "右侧内袋 · 盒盖闭合" : "传递至 EP02"}</b></div></section></div>`;
  }

  function promptPanel() {
    const prompt = selected(imagePrompts, state.selectedPrompt);
    return `<div class="catalog-layout"><section class="catalog-list"><header><span class="eyebrow">Prompt entries</span><button class="text-button" data-action="notify" data-message="图片提示词任务已排队">＋ 创建提示词</button></header>${imagePrompts.map((item) => `<button class="catalog-item ${item.id === prompt.id ? "active" : ""}" data-action="prompt" data-id="${item.id}"><span class="asset-mark prompt">P</span><span><b>${esc(item.name)}</b><small>${item.id} · ${item.purpose}</small></span><i class="status-dot ${item.status}"></i></button>`).join("")}</section><section class="catalog-detail prompt-detail"><header><div><span class="eyebrow">${prompt.id} · ${prompt.purpose}</span><h2>${esc(prompt.name)}</h2></div><button class="secondary" data-action="copy-prompt">复制正文</button></header><div class="control-scope"><span>控制范围</span><b>${esc(prompt.controls)}</b></div><pre>${esc(prompt.prompt)}</pre><p class="boundary-note">这是稳定提示词条目，不代表图片已经生成；只有经确认的生产结果才能进入 REF 槽位。</p></section></div>`;
  }

  function referencePanel() {
    const ref = selected(references, state.selectedRef);
    return `<div class="reference-grid">${references.map((item) => `<button class="reference-card ${item.id === ref.id ? "active" : ""} ${item.status}" data-action="reference" data-id="${item.id}"><div class="reference-art ${item.tone}"><span>${item.status === "missing" ? "NO IMAGE" : item.id}</span><i></i></div><header><div><b>${esc(item.name)}</b><small>${item.source}</small></div><span class="ref-status">${item.status.toUpperCase()}</span></header></button>`).join("")}</div><section class="reference-ledger"><header><span class="eyebrow">Reference boundary</span><h2>${ref.id} · ${esc(ref.name)}</h2></header><dl><div><dt>允许控制</dt><dd>${esc(ref.controls)}</dd></div><div><dt>不得控制</dt><dd>${esc(ref.excludes)}</dd></div></dl>${ref.status === "missing" ? `<button class="primary" data-action="generate-reference">审阅任务后创建参考图</button>` : ref.status === "review" ? `<div class="inline-actions"><button class="secondary" data-action="notify" data-message="REF-003 已退回，不改变 PROP-001 事实">退回结果</button><button class="primary" data-action="approve-reference">确认进入 REF 槽位</button></div>` : `<span class="accepted-line">已确认，可作为分镜输入参考图。</span>`}</section>`;
  }

  function storyboardView() {
    const episode = currentEpisode();
    if (episode.storyboard === "locked") return gateView("分镜", `${episode.id} 视觉设定尚未接受`, "分镜需要 accepted screenplay 与必要视觉事实；图片提示词不是前置门槛。", "visual");
    if (episode.storyboard === "queued" || episode.storyboard === "ready") return taskReadyView("分镜", `${episode.id} 已具备分镜输入`, "accepted screenplay 与视觉事实已冻结；创建任务后才会产生镜头职责和冻结关键帧。", "start-storyboard", "创建分镜任务");
    if (episode.storyboard === "running") return taskRunningView("分镜", `${episode.id} 正在设计镜头`, "Codex 正在建立镜头职责、空间连续性与冻结关键帧；尚无可审批正文。");
    const shot = selected(shots, state.selectedShot);
    return `<article class="flow-page" data-screen-label="分镜工作台"><header class="page-head"><div><span class="eyebrow">Storyboard · ${episode.id}</span><h1>每个镜头只承担一个变化</h1><p>冻结关键帧只描述起点；终点动作留给动态提示词。</p></div><span class="pill ${episode.storyboard}">${statusLabel[episode.storyboard]}</span></header><div class="shot-workbench"><section class="shot-strip">${shots.map((item, index) => `<button class="shot-tile ${item.id === shot.id ? "active" : ""}" data-action="shot" data-id="${item.id}"><div class="shot-frame"><span>0${index + 1}</span><i></i><b>${item.frame.split("·")[0]}</b></div><strong>${item.id}</strong><small>${item.duration} · ${esc(item.duty)}</small></button>`).join("")}</section><section class="shot-detail"><div class="frozen-frame"><span>FROZEN START</span><div><b>${shot.id}</b><p>${esc(shot.start)}</p></div></div><div class="shot-copy"><header><span class="eyebrow">${shot.id} · ${shot.duration}</span><h2>${esc(shot.duty)}</h2></header><dl><div><dt>景别 / 机位</dt><dd>${esc(shot.frame)}</dd></div><div><dt>可信终点</dt><dd>${esc(shot.end)}</dd></div><div><dt>视觉依据</dt><dd>${esc(shot.refs)}</dd></div></dl><details><summary>冻结关键帧提示词</summary><p>${esc(shot.prompt)}</p></details></div></section></div></article>`;
  }

  function motionView() {
    const episode = currentEpisode();
    if (episode.motion === "locked") return gateView("动态提示词", `${episode.id} 分镜尚未接受`, "动态只拥有从冻结起点到可信终点的变化，不回写分镜或视觉事实。", "storyboard");
    const motion = selected(motions, state.selectedMotion);
    return `<article class="flow-page" data-screen-label="动态提示词工作台"><header class="page-head"><div><span class="eyebrow">Motion document · ${episode.id}</span><h1>让运动发生在正确的边界里</h1><p>有真实 REF 才走图生视频；没有参考图时，提示词必须携带静态视觉锚点。</p></div><span class="pill ${episode.motion}">${statusLabel[episode.motion]}</span></header><div class="motion-layout"><section class="motion-list">${motions.map((item) => `<button class="motion-row ${item.id === motion.id ? "active" : ""}" data-action="motion" data-id="${item.id}"><span><b>${item.id}</b><small>${item.shot}</small></span><em>${item.mode}</em><i class="status-dot ${item.status}"></i></button>`).join("")}</section><section class="motion-detail"><header><div><span class="eyebrow">${motion.id} ← ${motion.shot}</span><h2>${motion.mode}</h2></div><span class="pill ${motion.status}">${statusLabel[motion.status]}</span></header><div class="motion-timeline"><div><span>00:00</span><b>冻结起点</b></div><i></i><div><span>TRIGGER</span><b>${esc(motion.change)}</b></div><i></i><div><span>END</span><b>${esc(motion.end)}</b></div></div><div class="prompt-box"><span>可复制正文结构</span><p>静态锚点 → 起点 → 触发 → 主体动作 → 次级反应 → 运镜 → 声音 → 可验证终点</p></div>${episode.motion === "submitted" ? `<div class="inline-actions"><button class="secondary" data-action="notify" data-message="动态文档已退回；已接受分镜不变">退回整份文档</button><button class="primary" data-action="accept-motion">整体接受动态提示词</button></div>` : ""}</section></div></article>`;
  }

  function productionView() {
    const episode = currentEpisode();
    if (episode.production === "locked") return gateView("生产交接", `${episode.id} 动态提示词尚未接受`, "生产任务不会自动创建；必须先展示精确 job，再由创作者显式确认。", "motion");
    return `<article class="flow-page" data-screen-label="生产交接"><header class="page-head"><div><span class="eyebrow">Production handoff · ${episode.id}</span><h1>最后一步仍然不是自动执行</h1><p>四个镜头、两种生成方式、明确输入引用；确认后才建立外部生产任务。</p></div><span class="pill ready">READY</span></header><section class="job-manifest"><header><span>JOB MANIFEST</span><b>EP01 · 4 shots</b></header>${motions.map((item) => `<div><b>${item.id}</b><span>${item.mode}</span><span>${item.mode === "图生视频" ? "REF-001 + frozen frame" : "static anchors embedded"}</span><em>未创建</em></div>`) .join("")}</section><button class="danger production-cta" data-action="open-production">检查并确认创建 4 个生产任务</button></article>`;
  }

  function gateView(stage, title, copy, upstream) {
    return `<article class="flow-page gate-page" data-screen-label="${stage}未解锁"><div class="gate-seal">LOCKED</div><span class="eyebrow">Stage gate · ${currentEpisode().id}</span><h1>${esc(title)}</h1><p>${esc(copy)}</p><button class="primary" data-action="stage" data-stage="${upstream}">返回上游处理</button></article>`;
  }

  function taskReadyView(stage, title, copy, action, label) {
    return `<article class="flow-page gate-page ready-page" data-screen-label="${stage}可创建任务"><div class="gate-seal ready">READY</div><span class="eyebrow">Task boundary · ${currentEpisode().id}</span><h1>${esc(title)}</h1><p>${esc(copy)}</p><button class="primary" data-action="${action}">${label}</button></article>`;
  }

  function taskRunningView(stage, title, copy) {
    return `<article class="flow-page gate-page" data-screen-label="${stage}任务运行中"><div class="running-mark"><i></i><span>CODEX RUNNING</span></div><span class="eyebrow">No fact mutation · ${currentEpisode().id}</span><h1>${esc(title)}</h1><p>${esc(copy)}</p><button class="secondary" data-action="stage" data-stage="screenplay">查看其他已接受内容</button></article>`;
  }

  function productionModal() {
    if (state.modal !== "production") return "";
    return `<div class="modal-backdrop"><section class="modal production-modal" role="dialog" aria-modal="true" aria-labelledby="production-title"><span class="eyebrow">External production confirmation</span><h2 id="production-title">创建 4 个生产任务？</h2><p>这会把已接受的提示词与明确 REF 输入交给外部执行端。当前原型只模拟创建，不调用供应商。</p><div class="job-summary"><span>4 SHOTS</span><span>2 TEXT-TO-VIDEO</span><span>2 IMAGE-TO-VIDEO</span></div><div class="modal-actions"><button class="secondary" data-action="close-modal">取消</button><button class="danger" data-action="confirm-production">确认模拟创建</button></div></section></div>`;
  }

  function mainView() {
    if (state.stage === "development") return developmentView();
    if (state.stage === "screenplay") return screenplayView();
    if (state.stage === "visual") return visualView();
    if (state.stage === "storyboard") return storyboardView();
    if (state.stage === "motion") return motionView();
    return productionView();
  }

  function render() {
    document.documentElement.dataset.theme = state.theme;
    root.innerHTML = `<div class="app complete-app">${masthead()}${stageSpine()}<div class="complete-workspace">${episodeRail()}<main class="complete-main">${mainView()}</main></div>${productionModal()}${state.notice ? `<div class="notice" role="status">${esc(state.notice)}</div>` : ""}</div>`;
  }

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.action;
    if (action === "theme") state.theme = state.theme === "light" ? "dark" : "light";
    else if (action === "stage") state.stage = button.dataset.stage;
    else if (action === "episode" || action === "open-episode") { state.episode = button.dataset.episode; if (action === "open-episode") state.stage = "screenplay"; }
    else if (action === "visual-tab") state.visualTab = button.dataset.tab;
    else if (action === "asset") state.selectedAsset = button.dataset.id;
    else if (action === "prompt") state.selectedPrompt = button.dataset.id;
    else if (action === "reference") state.selectedRef = button.dataset.id;
    else if (action === "shot") state.selectedShot = button.dataset.id;
    else if (action === "motion") state.selectedMotion = button.dataset.id;
    else if (action === "accept-episode") { currentEpisode().screenplay = "accepted"; currentEpisode().visual = "ready"; notify(`${state.episode} 剧本已接受；视觉设定可创建任务`); }
    else if (action === "reject-episode") { currentEpisode().screenplay = "queued"; notify(`${state.episode} 已退回；下游仍保持锁定`); }
    else if (action === "start-writing") { currentEpisode().screenplay = "running"; notify(`${state.episode} 写作任务已领取`); }
    else if (action === "start-visual") { currentEpisode().visual = "running"; notify(`${state.episode} 视觉设定任务已领取`); }
    else if (action === "start-storyboard") { currentEpisode().storyboard = "running"; notify(`${state.episode} 分镜任务已领取`); }
    else if (action === "accept-visual") { currentEpisode().visual = "accepted"; currentEpisode().storyboard = "queued"; notify(`${state.episode} 视觉设定已接受；分镜解锁`); }
    else if (action === "accept-motion") { currentEpisode().motion = "accepted"; currentEpisode().production = "ready"; notify("动态提示词已接受；生产交接解锁"); }
    else if (action === "approve-reference") { const ref = selected(references, state.selectedRef); ref.status = "approved"; ref.source = "已确认生产结果"; notify(`${ref.id} 已进入真实参考图槽位`); }
    else if (action === "generate-reference") notify("已打开生产任务清单；需要显式确认后才会创建");
    else if (action === "copy-prompt") { const prompt = selected(imagePrompts, state.selectedPrompt); try { await navigator.clipboard.writeText(prompt.prompt); notify(`${prompt.id} 正文已复制`); } catch { notify("浏览器未授权剪贴板；正文仍可手动选择"); } }
    else if (action === "open-production") state.modal = "production";
    else if (action === "close-modal") state.modal = null;
    else if (action === "confirm-production") { state.modal = null; notify("已模拟创建 4 个生产任务；没有调用外部服务"); }
    else if (action === "notify" || action === "simulate-asset") notify(button.dataset.message || "新变体任务已排队；原资产身份不变");
    render();
  });

  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && state.modal) { state.modal = null; render(); } });
  render();
})();
