# ADR 0001：Workbench-first

- 状态：Accepted
- 日期：2026-08-28

Script-Weaver 的主产品是结构化视频工作台，不是聊天页。项目、剧集、场次、分段、镜头、资产、提示词和媒体版本由 daemon 持久化；聊天记录和浏览器内存都不是事实源。首页进入分镜工作台，语义任务通过 `Ctrl+K` 或行内动作创建 Task。

后果：旧 `ProjectState` 只保留为导入输入和旧 CLI 兼容模型，不继续扩展为工作台核心。
