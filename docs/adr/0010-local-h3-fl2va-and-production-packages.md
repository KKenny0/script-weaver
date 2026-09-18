# ADR 0010：本地 H3 FL2VA 与版本化生产包

- 状态：Accepted for phase 5 implementation; pending product review
- 日期：2026-08-30

第一版视频生成只支持用户自行部署的 `MiniMaxAI/MiniMax-H3` H3-Base 768p FL2VA：
`sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va`。Script Weaver 固定
`task=fl2va`、`target.short_edge=768`、1–2 张关键帧和 4–15 秒时长；不实现 T2VA、Ref2VA、
2K、Hosted API 或权重分发。

为避免服务端默认值随部署漂移，提交 fingerprint 与请求体固定记录 cookbook 的采样参数：
`num_outputs_per_prompt=1`、`num_inference_steps=50`、`flow_shift=12`、
`audio_flow_shift=3`，并由 daemon 为每个新 GenerationJob 冻结一个 seed。同一任务重启后沿用原 seed，
新一轮获得新 seed；旧任务和候选都不会被覆盖。

H3 endpoint 必须是字面量 loopback IP，禁止 DNS 名、凭据、query、fragment、路径和重定向。
daemon 只把已接受且在镜头上 frozen 的 PNG REF 从 Media Store 复制到配置的 canonical shared
root，并向 H3 提供其中的 `file:` URI。H3 返回的任意本地路径均不受信任；视频只能由
`GET /v1/videos/{id}/content` 流入 daemon staging，经 MIME、大小和 MP4 box 校验后进入现有
content-addressed Media Store。

GenerationJob 保存 H3 job ID、提交 fingerprint 与远端状态。daemon 重启后只查询已经保存的
job ID，不重新提交；SUBMITTING 状态在重启时失败关闭。提交前输入变化使任务 STALE；提交后
输入变化仍保留下载结果，但只登记为 stale candidate，不能成为当前事实。所有 H3 输出沿用
ADR 0009 的候选审批；图片接受后创建 AssetVersion / 正式 REF，而 shot-owned 视频只在同一
`shot + kind=video` 槽内成为 current，不创建 AssetVersion，也不替换共享图片 REF。

生产包是 daemon 管理目录下的不可变、版本化 manifest，只列 accepted documents、assets、
shots 和正式 media 引用，同时显式列出 missing、failed、stale 与 excluded。相同事实 fingerprint
幂等返回同一包；写入先在同父目录完成，再原子 rename，失败不登记数据库事实。生产包不复制
模型权重，也不宣称完成剪辑、配音、字幕或发行。

“下一季”仍创建一个正常的新项目和新的 idea source，但在项目 metadata 中保存
`source_project_id` 与 `continuation_kind=season`；不存在来源项目时整个 intake 被拒绝。
这提供最小可追溯关系，不引入独立 Season 聚合。

本 ADR 延续 ADR 0003 的单写 daemon、ADR 0004 的单向投影、ADR 0009 的 candidate-first
边界。H3 服务仍运行在独立进程；同一 OS 用户恶意代码的隔离需要外部进程/容器沙箱。
