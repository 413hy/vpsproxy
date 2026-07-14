# vpsproxy-skill

这是一个 Codex Skill + 可部署 Telegram Bot 项目，用于在一台“控制端 VPS”上通过 Telegram Bot 管理本人拥有或已明确授权的目标 VPS，并在目标 VPS 上安装、配置和管理系统级全局代理。

核心能力：

- 通过 Telegram 按钮菜单添加和管理目标 VPS
- 支持 SSH 密码或 SSH 私钥连接目标 VPS
- SSH 主机指纹校验，避免中间人攻击
- 导入 VLESS Reality 单节点和常见订阅格式
- 从目标 VPS 发起节点可用性/延迟测试
- 使用 `sing-box` TUN 配置系统级全局出站代理
- 自动备份目标 VPS 网络/代理配置
- 应用代理前设置自动回滚保护，降低目标 VPS 失联风险
- 支持停止、恢复、回滚和卸载代理
- Telegram 管理员白名单，默认拒绝未授权用户
- 凭据加密存储，日志脱敏

只允许用于你本人拥有或已获得明确授权管理的 VPS。

## 仓库结构

```text
.
├── SKILL.md
├── agents/openai.yaml
├── CODEX_HANDOFF.md
└── assets/vps-proxy-manager/
    ├── README.md
    ├── .env.example
    ├── pyproject.toml
    ├── src/vps_proxy_manager/
    ├── scripts/
    ├── systemd/
    ├── migrations/
    ├── tests/
    └── docs/
```

说明：

- `SKILL.md`：给 Codex 使用的 Skill 入口说明。
- `assets/vps-proxy-manager/`：真正可安装运行的 Telegram Bot 后端项目。
- `assets/vps-proxy-manager/scripts/install.sh`：控制端 VPS 一键安装脚本。
- `assets/vps-proxy-manager/.env.example`：环境变量模板，不包含真实密钥。
- `assets/vps-proxy-manager/docs/`：架构、安全、Telegram 操作和恢复文档。
- `CODEX_HANDOFF.md`：给另一个 Codex/运维 agent 的交接说明。

## 最低要求

控制端 VPS：

- Debian/Ubuntu
- systemd
- Python 3.11+
- 能访问 Telegram API
- 能 SSH 访问目标 VPS

目标 VPS：

- Debian/Ubuntu 优先支持
- systemd
- root 或具备 sudo 权限的 SSH 用户
- `/dev/net/tun` 可用
- IPv4 可用

代理核心：

- `sing-box`，由程序在目标 VPS 首次测速或应用代理时安装。

## 快速部署

在控制端 VPS 上执行：

```bash
git clone https://github.com/413hy/vpsproxy-skill.git
cd vpsproxy-skill/assets/vps-proxy-manager
sudo ./scripts/install.sh
```

安装后编辑配置：

```bash
sudo nano /etc/vps-proxy-manager/vps-proxy-manager.env
```

至少填写：

```env
VPSPM_TELEGRAM_BOT_TOKEN=你的 Telegram Bot Token
VPSPM_ADMIN_USER_IDS=你的 Telegram 数字用户 ID
VPSPM_SECRET_KEY=用下面命令生成的 Fernet 密钥
```

生成 `VPSPM_SECRET_KEY`：

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager keygen
```

初始化并启动：

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager init-db
sudo systemctl enable --now vps-proxy-manager.service
```

查看状态：

```bash
sudo systemctl status vps-proxy-manager.service --no-pager
sudo journalctl -u vps-proxy-manager.service -f
```

## Telegram Bot 使用流程

1. 在 Telegram 打开你的 Bot，发送 `/start`。
2. 点击 `添加 VPS`。
3. 按向导输入：
   - VPS 名称
   - IP 或域名
   - SSH 端口
   - SSH 用户名
   - 密码或 SSH 私钥
4. Bot 会测试 SSH，并保存目标 VPS 的 SSH 主机指纹。
5. 点击 `导入单节点` 或 `导入订阅`。
6. 在 `代理节点` 页面选择节点。
7. 先选择 `测试此节点`，从目标 VPS 发起测速。
8. 确认节点可用后选择 `选择并应用到 VPS`。
9. 高风险操作会二次确认。
10. 在 `当前状态` 中查看目标 VPS 当前代理状态。

## 支持的代理输入

单节点：

- 已重点支持 VLESS + TCP + Reality + XTLS Vision。
- 示例格式：`vless://...?...security=reality...#name`

订阅：

- 普通节点链接列表
- Base64 编码订阅
- Clash YAML
- sing-box JSON
- 常见 VMess/Trojan/VLESS 节点尽量解析

订阅下载有 SSRF 防护：

- 仅允许 HTTPS
- 限制重定向次数
- 限制响应大小
- 限制超时
- 默认阻止本地地址、私有地址、云元数据地址

## 重要安全说明

- 不要把真实 `.env`、Bot Token、SSH 密码、私钥、订阅链接提交到 Git。
- Telegram 不是高安全级别密码保险库，Bot 会尽量删除敏感消息，但它们已经经过 Telegram 平台传输。
- 首次在一类新 VPS 上应用全局 TUN 代理前，必须确保有云厂商 VNC/救援控制台。
- 如果 Bot Token 曾出现在日志中，请去 BotFather 轮换 Token。
- 本项目不会根据 Telegram 输入拼接任意 shell 命令；远程操作通过固定 Python payload action 执行。

## 常用运维命令

```bash
# 本地检查
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor

# 初始化数据库
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager init-db

# 重启 Bot
sudo systemctl restart vps-proxy-manager.service

# 查看日志
sudo journalctl -u vps-proxy-manager.service -n 100 --no-pager

# 升级
cd vpsproxy-skill/assets/vps-proxy-manager
sudo ./scripts/upgrade.sh

# 卸载控制端服务，保留数据
sudo ./scripts/uninstall.sh
```

## 目标 VPS 失联恢复

代理应用前会在目标 VPS 上创建回滚脚本和 systemd 回滚定时器。若仍然失联：

1. 打开云厂商 VNC/救援控制台。
2. 在目标 VPS 上执行：

```bash
sudo /etc/vps-proxy-manager/rollback-last.sh
```

如果该脚本不存在，可使用仓库中的：

```bash
assets/vps-proxy-manager/scripts/emergency_restore.sh
```

更多说明见：

- `assets/vps-proxy-manager/docs/RECOVERY.md`
- `assets/vps-proxy-manager/docs/TROUBLESHOOTING.md`

## 开发和测试

```bash
cd assets/vps-proxy-manager
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check .
mypy src
pytest
```

当前测试覆盖：

- VLESS Reality 节点解析
- 普通/Base64/Clash/sing-box 订阅解析
- SSRF 防护
- sing-box TUN 配置生成
- 输入校验和脱敏
- 远端回滚脚本生成

## 当前限制

- 目标 VPS 自动化重点支持 Debian/Ubuntu。
- IPv6 配置会生成，但依赖目标 VPS 网络和内核支持。
- 默认实现的是连通性和延迟测试，下载测速属于可扩展项。
- TUN/透明代理依赖 `/dev/net/tun` 和 `CAP_NET_ADMIN`，部分容器型 VPS 可能不可用。

## 给 Codex 的入口

如果把这个仓库交给另一个 Codex，先让它阅读：

1. `CODEX_HANDOFF.md`
2. `SKILL.md`
3. `assets/vps-proxy-manager/README.md`
4. `assets/vps-proxy-manager/docs/SECURITY.md`
5. `assets/vps-proxy-manager/docs/RECOVERY.md`
