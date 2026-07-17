# 故障排查

## 控制端检查顺序

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl is-active vps-proxy-manager.service vps-proxy-codex-worker.service
sudo journalctl -u vps-proxy-manager.service -u vps-proxy-codex-worker.service -n 200 --no-pager
```

日志不得贴出环境文件、私钥、节点链接或订阅 URL。

## Bot 不响应

- `doctor` 报 Token 未配置：编辑 `/etc/vps-proxy-manager/vps-proxy-manager.env`。
- `Unauthorized`：Telegram 数字用户 ID 不在 `VPSPM_ADMIN_USER_IDS`。
- 默认只允许私聊；群组需要关闭 private-only 并配置允许 chat ID。
- `Conflict: terminated by other getUpdates`：同一 Token 有第二个 polling 实例，停止旧实例。
- Telegram API 超时：检查控制端 DNS、IPv4/IPv6 和到 `api.telegram.org` 的访问。

## Codex 初始化或自动诊断不运行

```bash
sudo codex login status
sudo systemctl status vps-proxy-codex-worker.service --no-pager
ls -ld /root/.codex/skills/vps-proxy-target-bootstrap
ls -ld /root/.codex/skills/vps-proxy-task-diagnosis
```

- `codex_unavailable`：`VPSPM_CODEX_CLI` 路径错误。
- `Codex CLI is not logged in`：以 root 完成 Codex 登录；systemd 使用 `/root/.codex`。
- `codex_no_result`：Codex 未调用固定准入命令，检查准入 Skill 是否完整。
- `codex_invalid_result`：诊断没有返回符合 JSON Schema 的结构化结果。
- Worker 重启会把 running 任务标记失败；在 Telegram 待初始化列表手工重试。

## VPS 订阅测速出现 database is locked

0.3.0 已在保存订阅条目后提交事务，再并发测速和更新进度，并启用 SQLite WAL、60 秒 busy timeout。升级后重新执行该 VPS 的订阅测速。旧失败任务不会自动重放，但会保留在任务中心。

## SSH

- `ssh_auth_failed`：用户、密码或私钥错误。
- `ssh_host_key_unverified`：无法获取或验证主机公钥。
- 主机指纹变化：可能是重装，也可能是 MITM；先从云控制台核对，再走“编辑连接”向导，禁止自动接受。
- `sudo` 失败：非 root 用户需要免交互 sudo。当前远端 Agent 不支持交互式 sudo 密码提示。
- 连接超时：确认安全组、端口、控制端出口 IP 和 fail2ban。

## 候选 VPS 无法准入

- `unsupported_system`：当前只自动支持 Debian/Ubuntu。
- `unsupported_arch`：只支持 x86_64/amd64/aarch64/arm64。
- `tun_unavailable`：VPS 套餐未暴露 `/dev/net/tun` 或 `CAP_NET_ADMIN`。
- `singbox_download_failed`：目标无法访问官方 HTTPS 安装源。
- `local_exit_verification_failed`：初始化后本地公网访问失败，先修目标 DNS/路由，不要强行入库。

## 订阅

- 只接受 HTTPS，拒绝 URL 内嵌用户名/密码。
- 私网、localhost、链路本地、保留地址和元数据 IP 默认阻止。
- 重定向目标也必须通过同样检查。
- 空响应、超时、超过大小限制会直接失败。
- Clash/sing-box 中不受当前生成器支持的协议条目会被忽略；完全没有可用条目时导入失败。
- 目标 VPS 订阅测速失败而控制端成功，检查目标到订阅域名的 DNS/HTTPS，不代表控制端缓存可代替目标拉取。

## 测速

- `DNS OK, TCP failed`：节点端口不可达或受目标网络阻断。
- `TCP OK, proxy failed`：凭据、Reality 参数、SNI、时间同步或协议参数错误。
- `proxy OK, access latency high`：节点链路可用但真实 HTTPS 慢。
- 大批超时：降低 `VPSPM_SPEEDTEST_CONCURRENCY`，默认 3。
- 取消不是立即 kill 所有 SSH；当前子测试结束后停止后续项。

## sing-box / TUN

目标控制台：

```bash
sudo sing-box check -c /etc/sing-box/config.json
sudo journalctl -u sing-box.service -n 100 --no-pager
ip rule
ip route show table all
sudo nft list ruleset
```

不要把完整 config 发到公共日志。Reality `pbk`、UUID、密码缺失会在生成或 `sing-box check` 阶段失败。

若代理启动后 SSH 或公网验证失败，保持等待自动回滚，或从控制台执行 `/etc/vps-proxy-manager/rollback-last.sh`。

## 本地出口仍显示代理 IP

1. Telegram 执行 `切回本地出口`。
2. 目标检查 `systemctl is-enabled/is-active sing-box.service`，两者应 disabled/inactive。
3. 检查系统是否另有 Clash、Xray、环境变量代理、Docker gateway 或云厂商 NAT；本系统不会删除未声明的第三方代理。
4. 使用 `curl --noproxy '*' -4 https://ifconfig.co/json`。

## 恢复代理按钮不出现

只有状态为 local 且数据库保留上次当前节点时才显示。首次初始化、从未选节点或已卸载时没有可恢复配置；重新导入/测速并应用节点。

## 数据库迁移

```bash
sudo systemctl stop vps-proxy-manager.service vps-proxy-codex-worker.service
sudo cp -a /opt/vps-proxy-manager/data/app.db /opt/vps-proxy-manager/data/app.db.manual-backup
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager init-db
sudo systemctl start vps-proxy-manager.service vps-proxy-codex-worker.service
```

迁移失败时不要删除数据库。保留错误日志和备份，确认 `VPSPM_DATABASE_URL` 指向预期文件。
