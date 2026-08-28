# 四镜头垂直切片

自动化测试 `tests/integration/test_workbench_vertical_slice.py` 覆盖：

1. 创建 Project、Episode、ScriptScene、Segment。
2. TaskSnapshot 冻结上下文和 revision。
3. ChangeSet 追加四个 `shot.create`，validate、submit、UI apply。
4. 重复 apply 不重复创建镜头；daemon 重启后事实、ChangeSet 和事件仍在。
5. stale revision 使整个事务冲突，未冲突镜头也不发生部分写入。
6. Asset v2 只标记 `follow_latest`；`frozen` 不变。
7. 同步、冻结、恢复旧 AssetVersion、恢复旧 Shot revision 都创建新 revision/version，历史不删除。
8. Shot preview、asset board、version compare 和 Segment contact sheet 从当前事实重建。
9. Generation fingerprint 覆盖 prompt、数量、模型、参数、引用版本、adapter、输出规格；确认令牌只消费一次，Fake 不访问网络，失败不自动重试。
10. SSE 以 `domain_events.id` 续传；MCP 工具白名单明确排除 apply/confirm/run。

手工视觉验收以 `target-video-workbench-reference.png` 为结构目标：72–80px 主导航、260–280px 分段导航、可滚动 Shot 卡、320–360px Inspector；窄屏 Inspector 为右侧 drawer，所有控件保持键盘焦点可见。
