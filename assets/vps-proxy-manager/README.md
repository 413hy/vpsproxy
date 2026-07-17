# VPS Proxy Manager

控制端 Telegram Bot 后端。它通过受控 Codex 准入流程添加目标 VPS，并以确定性任务管理 sing-box TUN 全局出站。仅用于本人拥有或已明确授权的主机。

## 技术栈

- Python 3.11+
- python-telegram-bot 22.x 异步 API
- SQLAlchemy 2.x async + SQLite + Alembic
- AsyncSSH
- Pydantic Settings
- Fernet 加密
- structlog
- sing-box 1.13 系列配置语义
- systemd

## 管理模型

控制端单节点与订阅是两个独立库。订阅解析结果只存在于 `subscription_entries`，用于展示和测速，不会写进 `proxy_nodes`。导入目标 VPS 后会生成目标自己的 `vps_nodes` 或 `vps_subscriptions` 副本；目标订阅节点由该 VPS 拉取订阅并缓存到 `vps_subscription_entries`。

控制端资源在控制端测速，VPS 资源从相应 VPS 测速。每台 VPS 只保存一个当前代理出站状态，也可处于本地出口或已卸载状态。

## 安装

```bash
sudo ./scripts/install.sh
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager keygen
sudo editor /etc/vps-proxy-manager/vps-proxy-manager.env
sudo codex login status
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service
```

环境文件路径固定为：

```text
/etc/vps-proxy-manager/vps-proxy-manager.env
```

最少配置：

```env
VPSPM_TELEGRAM_BOT_TOKEN=BotFather提供的Token
VPSPM_ADMIN_USER_IDS=你的Telegram数字用户ID
VPSPM_SECRET_KEY=keygen生成的值
```

多个管理员 ID 用逗号分隔。默认只允许私聊；如启用群组，设置 `VPSPM_REQUIRE_PRIVATE_CHAT=false` 并在 `VPSPM_ALLOWED_CHAT_IDS` 填明确的 chat ID。

## 服务

```text
vps-proxy-manager.service          Bot 与普通任务执行器
vps-proxy-codex-worker.service    候选 VPS 准入和失败任务自动诊断
```

两个服务均按当前部署要求以 root 运行。普通测速、切换和删除仍由固定任务处理器执行；任务发生系统级失败时，Worker 会自动让 Codex 只读分析脱敏上下文并主动发送 Telegram 诊断结果。Codex 不会自动重放网络修改任务。

## 第一次使用

1. 私聊 Bot 并发送 `/start`。
2. 打开 `VPS 管理` -> `添加 VPS`。
3. 输入名称、地址、端口、用户名，选择密码或私钥。
4. 核对 Bot 展示的 SSH SHA256 主机指纹。
5. 确认后等待 Codex 准入任务；失败时在待初始化列表查看原因并手动重试。

测速完成后，任务消息中的 `查看各节点测速结果` 会跳到对应列表。节点详情分别显示 DNS、TCP、代理握手、真实访问延迟和失败原因。

后台任务失败时无需主动请求排查：系统自动创建 `diagnose` Codex 任务，使用 `VPSPM_CODEX_MODEL` 和 `VPSPM_CODEX_REASONING_EFFORT`，完成后主动通知管理员并在任务中心保留结构化结论。
6. 在 `单节点库` 或 `订阅库` 导入资源。
7. 在控制端测速后，将资源导入一台或多台 VPS。
8. 进入指定 VPS 的单节点/订阅页，从目标本机测速。
9. 选择一个节点，确认设为唯一当前出站。

Bot 读取密码、私钥、节点或订阅后会尽量删除输入消息，但 Telegram 已经传输过该内容。优先使用专用 SSH 私钥，并在必要时轮换凭据。

## 代理与本地出口

`切回本地出口` 会停止并禁用本系统管理的 sing-box，重启后仍走 VPS 自身出口，但不会删除资源或上次配置。`启用上次代理` 设置回滚保护后重新启用配置，并在新 SSH 与公网访问都成功后确认。

应用新节点时目标先保存备份和服务状态，再启动自动回滚 timer。配置检查、服务启动、SSH 重连或 HTTPS 访问任一失败时，不更新数据库当前节点，timer 会恢复切换前状态。

## CLI

```bash
vps-proxy-manager --help
vps-proxy-manager keygen
vps-proxy-manager init-db
vps-proxy-manager doctor
vps-proxy-manager run-bot
vps-proxy-manager run-codex-worker
```

`provision-candidate` 是准入 Skill 专用入口，不应手工绕过 CodexTask 状态调用。

## 开发验证

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff format --check src tests migrations
ruff check src tests migrations
mypy src
pytest -q
```

## 数据与权限

```text
/opt/vps-proxy-manager/data/app.db                         0600/受0700目录保护
/etc/vps-proxy-manager/vps-proxy-manager.env              0600
/root/.codex/skills/vps-proxy-target-bootstrap            仅root
目标:/etc/vps-proxy-manager                               0700
目标:/etc/sing-box/config.json                            0600
```

备份或迁移必须同时保存数据库和环境文件，否则加密内容不可恢复。

详细文档：

- `docs/DEPLOYMENT.md`
- `docs/ARCHITECTURE.md`
- `docs/TELEGRAM.md`
- `docs/SECURITY.md`
- `docs/RECOVERY.md`
- `docs/DATA_BACKUP.md`
- `docs/TROUBLESHOOTING.md`
