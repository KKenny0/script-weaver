window.SW_V8 = {
  episodes: [
    { id:"EP01", title:"未通过的好友申请", state:"accepted", freshness:"current", progress:84, scene:"SC02 · 空影院", note:"REF 与冻结关键帧已就绪" },
    { id:"EP02", title:"空影院", state:"accepted", freshness:"current", progress:56, scene:"SC01 · 第七排", note:"等待视觉事实" },
    { id:"EP03", title:"磨到不刮人为止", state:"submitted", freshness:"current", progress:38, scene:"SC02 · 调查报告", note:"剧本 v2 待审" },
    { id:"EP04", title:"黑胶带", state:"accepted", freshness:"stale", progress:47, scene:"SC01 · 海边公寓", note:"开发文档更新影响 3 项" },
    { id:"EP05", title:"所有数字都在上涨", state:"draft", freshness:"current", progress:22, scene:"未拆场", note:"剧本草稿" },
    { id:"EP06", title:"缺席的求婚", state:"draft", freshness:"current", progress:16, scene:"未拆场", note:"等待写作任务" },
    { id:"EP07", title:"让我想想", state:"draft", freshness:"current", progress:8, scene:"未开始", note:"只有分集梗概" },
    { id:"EP08", title:"十一秒", state:"draft", freshness:"current", progress:8, scene:"未开始", note:"只有分集梗概" }
  ],
  sources: {
    idea:"一个习惯用数字解决一切的男人，在第一次说‘让我想想’之后，才发现自己从未真正听见身边的人。",
    novel:"粘贴你有权改编的原著正文，或选择本地 Markdown / PDF。",
    single:"EP01-SC01　内景 · 旧电脑房 · 夜\n\n屏幕上的好友申请停在‘等待验证’。",
    multi:"# EP01 未通过的好友申请\n……\n# EP02 空影院\n……"
  },
  development: [
    ["创作承诺","不是复述一段关系，而是让观众看见：给予如何逐渐变成控制。"],
    ["故事引擎","每集用一个物件与一个数字推动关系变化；镜头逐步拆穿第一人称叙述者。"],
    ["人物冲突","周屿相信支付就是负责；沈禾要的不是更多，而是被真正听见。"],
    ["分集地图","EP01 好友申请 · EP02 空影院 · EP03 指甲锉 · EP04 黑胶带 · EP05 金额梯度 · EP06 剩余针剂 · EP07 第一次停顿 · EP08 十一秒。"]
  ],
  scenes: [
    { id:"EP01-SC01", heading:"内景 · 旧电脑房 · 夜（2007）", body:"屏幕上的好友申请停在‘等待验证’。模糊头像只有一块过曝的白。\n\n周屿（旁白）\n我以为没有通过，是网络延迟。后来才知道，有些拒绝需要十九年才能加载完成。" },
    { id:"EP01-SC02", heading:"内景 · 空影院 · 夜（现在）", body:"银幕已经熄灭。沈禾坐在第七排，用一把小指甲锉慢慢磨掉指甲边缘。\n\n沈禾\n磨到不刮人为止。\n\n周屿看着她，开始计算包下整场影院花了多少钱。" },
    { id:"EP01-SC03", heading:"内景 · 影院走廊 · 连续", body:"沈禾把指甲锉放回口袋。周屿的手机亮起，又一个转账页面。她没有回头。" }
  ],
  assets: [
    { id:"CHAR-001", name:"沈禾", kind:"人物身份", variant:"LOOK-001-A · 深色长外套", range:"EP01–EP04", flow:"进入：克制 / 离开：开始拒绝" },
    { id:"LOC-001", name:"空影院", kind:"地点身份", variant:"LVIEW-001-B · 第七排", range:"EP01–EP02", flow:"进入：银幕冷光 / 离开：走道灯亮" },
    { id:"PROP-001", name:"指甲锉", kind:"道具身份", variant:"PSTATE-001-B · 边缘磨钝", range:"EP01–EP03", flow:"进入：右手持有 / 离开：放回外套" }
  ],
  candidates: [
    { id:"A", crop:"left", prompt:"干净、产品设定感更强", asset:"MEDIA-CAND-A" },
    { id:"B", crop:"center", prompt:"细齿与使用痕迹平衡", asset:"MEDIA-CAND-B" },
    { id:"C", crop:"right", prompt:"更强戏剧侧光", asset:"MEDIA-CAND-C" }
  ],
  statusFixtures: [
    ["故事开发 v2","accepted","current","succeeded"],
    ["EP04 剧本 v1","accepted","stale","succeeded"],
    ["EP03 剧本 v2","submitted","current","succeeded"],
    ["EP06 写作任务","draft","current","failed"],
    ["SHOT-004 生成","accepted","stale","inputs-changed"],
    ["B1 图片候选","submitted","current","succeeded"],
    ["REF-PROP-001-v0","accepted","stale","succeeded"]
  ],
  batch: [
    ["EP05 · short-drama-write","succeeded"],
    ["EP06 · short-drama-write","failed"],
    ["EP07 · short-drama-write","running"],
    ["EP08 · short-drama-write","queued"]
  ]
};
