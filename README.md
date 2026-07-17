# VPS Proxy Manager Codex Skill

这是一个可直接部署到控制端 VPS 的 Codex Skill 和 Telegram Bot 项目。它只用于管理本人拥有或已明确授权的 Debian/Ubuntu VPS，并通过 sing-box TUN 管理目标 VPS 的系统级出站代理。

## 当前架构

系统刻意分成四个边界：

| 边界 | 职责 |
| --- | --- |
| 控制端单节点库 | 只保存手工导入的单节点，在控制端本地测速 |
| 控制端订阅库 | 保存完整订阅及其独立缓存条目，在控制端本地更新和测速；条目不会进入单节点库 |
| 每台目标 VPS | 保存导入到该 VPS 的单节点/订阅副本，并从该 VPS 发起测速；任一时刻只允许一个代理出站 |
| Codex Worker | 对待准入 VPS 执行受控初始化，并自动只读诊断失败任务和状态漂移；不把 Telegram 文本转换成任意 Shell |

新 VPS 的流程是：Telegram 收集连接参数并确认 SSH 主机指纹，创建候选记录，Codex Worker 调用内置 `$vps-proxy-target-bootstrap` Skill，Skill 再调用固定 CLI。只有远端 Agent、TUN、sing-box、本地出口和 SSH 验证全部通过后，VPS 才进入正式管理库。

## 功能

- Telegram Reply Keyboard 主菜单和 Inline Keyboard 列表、分页、确认、返回、取消
- SSH 密码或私钥认证及 SSH 主机指纹固定
- VLESS Reality/XTLS Vision、VMess、Trojan、Shadowsocks、Hysteria2 节点解析
- 普通链接列表、Base64、Clash YAML 和 sing-box JSON 订阅解析
- 控制端测速与目标 VPS 本机测速相互独立
- DNS、TCP、代理握手和真实 HTTPS 访问延迟分项记录
- 测速详情显示测试代理出口；切换结果显示目标系统实际出口
- sing-box TUN、DNS 劫持、私网/管理连接/代理服务器绕过
- 自动回滚定时器、配置版本备份、持久本地出口、恢复代理和卸载
- 活动配置 SHA-256/资源指纹验收及控制端到目标机的一致性巡检
- 管理员白名单、SSRF 防护、加密凭据、结构化脱敏日志和审计记录
- SQLite/SQLAlchemy/Alembic、异步任务、每 VPS 网络修改互斥锁

## 目录

```text
.
├── SKILL.md                         # 控制端 Codex Skill
├── CODEX_HANDOFF.md                 # 给另一个 Codex 的完整交接
├── agents/openai.yaml
└── assets/vps-proxy-manager/
    ├── codex-skills/vps-proxy-target-bootstrap/  # Codex 准入子 Skill
    ├── codex-skills/vps-proxy-task-diagnosis/    # 失败任务只读诊断 Skill
    ├── src/vps_proxy_manager/       # Bot、任务、SSH、解析、配置、Codex Worker
    ├── migrations/                  # Alembic 迁移
    ├── scripts/                     # 安装、升级、卸载、紧急恢复
    ├── systemd/                     # Bot 与 Codex Worker 服务
    ├── tests/
    └── docs/
```

## 最低要求

控制端：Debian 12 或 Ubuntu 22.04/24.04、systemd、root、Python 3.11+、Codex CLI 已安装并登录、可访问 Telegram API 和目标 SSH。

目标端：Debian/Ubuntu、systemd、root 或免交互 sudo、Python 3、`/dev/net/tun`、IPv4；IPv6 取决于目标网络。容器型 VPS 必须提供 TUN 和 `CAP_NET_ADMIN`。

## 部署

```bash
git clone https://github.com/413hy/vpsproxy-skill.git
cd vpsproxy-skill/assets/vps-proxy-manager
sudo ./scripts/install.sh
```

安装脚本会创建：

```text
/opt/vps-proxy-manager
/etc/vps-proxy-manager/vps-proxy-manager.env
/root/.codex/skills/vps-proxy-target-bootstrap
/root/.codex/skills/vps-proxy-task-diagnosis
```

生成加密密钥并编辑配置：

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager keygen
sudo nano /etc/vps-proxy-manager/vps-proxy-manager.env
```

至少填写：

```env
VPSPM_TELEGRAM_BOT_TOKEN=BotFather提供的Token
VPSPM_ADMIN_USER_IDS=你的Telegram数字用户ID
VPSPM_SECRET_KEY=上一步生成的Fernet密钥
```

验证并启动两个服务：

```bash
sudo codex login status
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service
sudo systemctl --no-pager --full status vps-proxy-manager.service vps-proxy-codex-worker.service
```

## Telegram 使用

1. 私聊 Bot，发送 `/start`。
2. `VPS 管理` -> `添加 VPS`，完成名称、地址、端口、用户、认证方式和主机指纹确认。
3. 等待 Codex 初始化任务成功；成功前不会出现在正式 VPS 列表。
4. 在 `单节点库` 导入单链接，或在 `订阅库` 导入完整 HTTPS 订阅。
5. 可先在控制端本地测速；选择资源后点击 `导入指定 VPS`。
6. 进入目标 VPS 的 `单节点` 或 `订阅`，从该 VPS 测速。
7. 选择一个可用节点并二次确认，设为该 VPS 唯一当前出站。
8. `切回本地出口` 只停用代理，资源和上次配置保留；`启用上次代理` 可恢复。

切换完成后任务消息提供 `查看此 VPS`。VPS 详情中的“配置一致性”只有在数据库选择、远端活动配置哈希、资源指纹和 sing-box 服务均匹配时才显示“已核对”。连续状态漂移会自动创建失败任务并唤醒 Codex 诊断。

删除控制端资源时，Bot 会列出正在使用它的 VPS。只有再次确认“强制删除所有副本”后，才会先让相关 VPS 切回本地出口并移除副本。

## 出口与恢复语义

- `切回本地出口`：`disable --now sing-box`，重启后仍使用 VPS 原出口，资源不删除。
- `启用上次代理`：先设置自动回滚，再启动上次配置；新 SSH 与公网访问验证成功后解除保护。
- `回滚配置`：恢复上一次配置文件和当时的 systemd 启用/运行状态。
- `卸载代理`：恢复初始化前的 sing-box 状态，删除本系统在目标 VPS 上的资源库，保留 Agent 和恢复备份以便重新初始化。
- `删除 VPS`：可仅删除控制端记录，或先远端卸载再删除。

目标失联时通过云厂商控制台执行：

```bash
sudo /etc/vps-proxy-manager/rollback-last.sh
```

脚本不存在时使用仓库中的 `assets/vps-proxy-manager/scripts/emergency_restore.sh`。兜底脚本只停止 sing-box，不删除未知的原有配置。

## 运维

```bash
# 日志
sudo journalctl -u vps-proxy-manager.service -u vps-proxy-codex-worker.service -n 200 --no-pager

# 升级
cd vpsproxy-skill/assets/vps-proxy-manager
sudo ./scripts/upgrade.sh

# 控制端卸载服务，保留数据库与环境文件
sudo ./scripts/uninstall.sh
```

备份时必须同时保存 `/opt/vps-proxy-manager/data/` 和 `/etc/vps-proxy-manager/vps-proxy-manager.env`。丢失 `VPSPM_SECRET_KEY` 后，数据库中的 SSH 凭据和代理资源无法解密。

## 验证

```bash
cd assets/vps-proxy-manager
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check src tests migrations
mypy src
pytest -q
```

详细资料见 [部署文档](assets/vps-proxy-manager/docs/DEPLOYMENT.md)、[架构](assets/vps-proxy-manager/docs/ARCHITECTURE.md)、[安全设计](assets/vps-proxy-manager/docs/SECURITY.md)、[Telegram 操作](assets/vps-proxy-manager/docs/TELEGRAM.md)、[恢复](assets/vps-proxy-manager/docs/RECOVERY.md)和[故障排查](assets/vps-proxy-manager/docs/TROUBLESHOOTING.md)。

本项目不得用于未授权主机。首次在新 VPS 类型上启用全局 TUN 前，必须确认云厂商控制台可用。
