# ADR 0003：Single-writer daemon

- 状态：Accepted
- 日期：2026-08-28

只有绑定 `127.0.0.1` 的 `script-weaverd` 读写 SQLite。浏览器通过 Next 服务端代理访问相对 `/api`；MCP 通过 daemon HTTP API。启动令牌为 256-bit 随机值，写入用户应用数据目录，重启轮换，浏览器不可见。

SQLite 为每个线程一条独立连接（WAL 快照供本地读并发），启用 `foreign_keys=ON`、`busy_timeout`；写入由单进程锁串行化，并在 `BEGIN IMMEDIATE` 事务内完成状态/fingerprint/revision 检查与事实写入。这是本地单用户上限；只有 profiling 证明吞吐受限时才拆细粒度锁。

信任边界：这是本机单用户产品，`127.0.0.1` 上的同一 OS 用户被视为可信。Next dev/start 显式绑定 `127.0.0.1`；代理只接受 loopback 的 Host/Origin，请求体上限 2 MiB（按实际字节计数，不信任 Content-Length），并剥除客户端的 `authorization`/`cookie`。`SCRIPT_WEAVER_DAEMON_URL` 只允许 loopback 主机。若未来要开放远程访问或同机不可信用户共享端口，必须先引入用户身份、CSRF/session 与授权模型。
