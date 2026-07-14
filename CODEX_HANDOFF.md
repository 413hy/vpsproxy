# Codex Handoff

这个文件是给接手本仓库的 Codex 或运维自动化 agent 看的。目标是让接手者不需要读完整聊天记录，也能知道项目是什么、在哪里、如何部署、如何验证、哪些地方不能乱改。

## 项目定位

本仓库是一个 Codex Skill，内含一个可部署的 Telegram Bot 后端。它运行在控制端 VPS 上，通过 Telegram 菜单管理用户本人拥有或明确授权的目标 VPS，并在目标 VPS 上安装/配置/管理 `sing-box` 系统级全局代理。

不要把它理解成“Codex 临时根据 Telegram 消息生成 shell 命令”。生产路径是：

```text
Telegram Bot -> 受控任务 -> SSH 客户端 -> 固定远端 payload action -> 目标 VPS
```

Telegram 输入必须经过校验，不能拼接成任意 shell。

## 关键路径

仓库根：

```text
/root/vpsvpn/vpsproxy-skill-repo
```

Skill 入口：

```text
SKILL.md
agents/openai.yaml
```

可部署项目：

```text
assets/vps-proxy-manager
```

控制端安装后路径：

```text
/opt/vps-proxy-manager
/etc/vps-proxy-manager/vps-proxy-manager.env
/etc/systemd/system/vps-proxy-manager.service
```

## 当前部署方式

在控制端 VPS 上：

```bash
cd /root/vpsvpn/vpsproxy-skill-repo/assets/vps-proxy-manager
sudo ./scripts/install.sh
sudo nano /etc/vps-proxy-manager/vps-proxy-manager.env
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager init-db
sudo systemctl enable --now vps-proxy-manager.service
```

`vps-proxy-manager.service` 当前按用户要求以 root 运行。不要在文档里声称它是低权限用户运行。

## 必填环境变量

配置文件：

```text
/etc/vps-proxy-manager/vps-proxy-manager.env
```

必填：

```env
VPSPM_TELEGRAM_BOT_TOKEN=...
VPSPM_ADMIN_USER_IDS=6977085303
VPSPM_SECRET_KEY=...
```

生成密钥：

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager keygen
```

不要打印、提交或写入日志：

- Telegram Bot Token
- SSH 密码
- SSH 私钥
- 订阅链接
- 完整代理节点链接
- UUID

## 验证部署

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl is-active vps-proxy-manager.service
sudo systemctl status vps-proxy-manager.service --no-pager
```

安全检查：

```bash
rg -n 'BEGIN (OPENSSH|RSA|EC) PRIVATE KEY|[0-9]{6,12}:[A-Za-z0-9_-]{20,}' .
```

开发检查：

```bash
cd assets/vps-proxy-manager
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check .
mypy src
pytest
```

## Telegram 使用流程

用户侧：

1. `/start`
2. `添加 VPS`
3. 输入名称、IP/域名、端口、用户名、密码或私钥
4. Bot 测试 SSH 并保存 SSH 指纹
5. `导入单节点` 或 `导入订阅`
6. `代理节点`
7. 选择节点，先测速
8. 选择应用到 VPS
9. 二次确认高风险操作
10. 查看 `当前状态`

高风险操作必须保留确认：

- 应用全局代理
- 停止代理
- 恢复代理
- 回滚
- 卸载
- 删除 VPS/凭据

## 代码模块说明

```text
src/vps_proxy_manager/bot/
```

Telegram 菜单、向导、callback 路由和管理员鉴权。

```text
src/vps_proxy_manager/tasks/runner.py
```

后台任务执行器。对同一 VPS 的网络修改任务加锁。

```text
src/vps_proxy_manager/ssh/client.py
```

SSH 连接、主机指纹采集、远端 payload 执行。

```text
src/vps_proxy_manager/remote/payload.py
```

目标 VPS 上通过 `python3 - action` 执行的固定动作集合。这里是最敏感区域，改动后必须重新检查回滚逻辑。

```text
src/vps_proxy_manager/proxy/parser.py
```

节点和订阅解析。

```text
src/vps_proxy_manager/proxy/singbox.py
```

sing-box TUN 配置生成。

```text
src/vps_proxy_manager/proxy/ssrf.py
```

订阅下载 SSRF 防护。

```text
src/vps_proxy_manager/models.py
```

SQLAlchemy 数据模型。

## 远端代理应用流程

1. 生成 sing-box TUN 配置。
2. SSH 到目标 VPS。
3. 备份 `/etc/sing-box/config.json`、路由、防火墙、resolver 状态。
4. 写入 `/etc/vps-proxy-manager/rollback-last.sh`。
5. 启动 `vpspm-rollback.timer`。
6. `sing-box check -c config.json`。
7. 启动并 enable `sing-box.service`。
8. 控制端确认 SSH/status 仍可用。
9. 关闭 rollback timer。

不要移除自动回滚保护。

## 常见故障点

Bot 不启动：

- 检查 env 文件是否存在。
- 检查 Token 和 admin ID。
- `journalctl -u vps-proxy-manager.service -n 100 --no-pager`

Telegram API 能通但无菜单：

- 用户 ID 不在 `VPSPM_ADMIN_USER_IDS`。
- 如果 `VPSPM_REQUIRE_PRIVATE_CHAT=true`，必须私聊 Bot。

SSH 失败：

- 密码/私钥错误。
- SSH 主机指纹变化。
- 目标用户没有 sudo 权限。

应用代理后目标异常：

- Telegram 里执行回滚。
- 如果 SSH 失联，用云厂商 VNC 执行 `/etc/vps-proxy-manager/rollback-last.sh`。

## 不要做的事

- 不要把真实 `/etc/vps-proxy-manager/vps-proxy-manager.env` 复制进仓库。
- 不要在日志里打印 token、密码、私钥、完整节点链接。
- 不要把 Telegram 输入拼接进 shell。
- 不要强推覆盖 GitHub 远端，除非用户明确要求。
- 不要移除 SSH 指纹校验。
- 不要移除高风险操作二次确认。
- 不要移除目标 VPS 自动回滚定时器。

## 推送仓库

当前远端：

```bash
git remote -v
```

如果机器上已有 `~/.ssh/codex_tg_manager` 且能认证 GitHub：

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/codex_tg_manager -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new' git push
```

推送前必须先跑敏感扫描：

```bash
rg -n 'BEGIN (OPENSSH|RSA|EC) PRIVATE KEY|[0-9]{6,12}:[A-Za-z0-9_-]{20,}|VPSPM_TELEGRAM_BOT_TOKEN=.*[A-Za-z0-9_-]{20,}' .
```
