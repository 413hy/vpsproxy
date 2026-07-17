# 控制端部署

## 支持环境

推荐 Debian 12 或 Ubuntu 22.04/24.04，root、systemd、2 vCPU、1 GB RAM、至少 2 GB 可用磁盘。Python 必须 3.11+。控制端需要 HTTPS 访问 Telegram、sing-box 官方源、订阅站和 Codex 服务，并能访问目标 SSH。

目标自动化支持 Debian/Ubuntu、systemd、Python 3、root/免交互 sudo、TUN 和 IPv4。

## Telegram 准备

1. 在 BotFather 创建 Bot，取得 Token。
2. 取得自己的 Telegram 数字 user ID。
3. 若 Token 曾粘贴到公开聊天或仓库，在 BotFather 重新生成。

## Codex 准备

以 root 安装 Codex CLI并登录，确认：

```bash
sudo codex --version
sudo codex login status
```

本项目安装脚本会安装准入 Skill，但不会代替用户安装或授权 Codex CLI。

## 安装

```bash
git clone https://github.com/413hy/vpsproxy-skill.git
cd vpsproxy-skill/assets/vps-proxy-manager
sudo ./scripts/install.sh
```

脚本安装 Python 依赖、控制端 sing-box、项目、Alembic 数据库、两个 systemd unit 和准入 Skill。只有 sing-box 是本次新安装时，脚本才会停止其默认服务；已有控制端 sing-box 服务不主动修改。

配置路径：

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager keygen
sudo nano /etc/vps-proxy-manager/vps-proxy-manager.env
```

示例：

```env
VPSPM_TELEGRAM_BOT_TOKEN=实际Token
VPSPM_ADMIN_USER_IDS=实际数字ID
VPSPM_ALLOWED_CHAT_IDS=
VPSPM_REQUIRE_PRIVATE_CHAT=true
VPSPM_SECRET_KEY=实际Fernet密钥
VPSPM_CODEX_ENABLED=true
VPSPM_CODEX_CLI=/usr/local/bin/codex
VPSPM_CODEX_HOME=/root/.codex
```

不要将实际文件复制到仓库。

## 启动前检查

```bash
sudo chown root:root /etc/vps-proxy-manager/vps-proxy-manager.env
sudo chmod 600 /etc/vps-proxy-manager/vps-proxy-manager.env
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager init-db
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
```

启动：

```bash
sudo systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service
sudo systemctl --no-pager --full status vps-proxy-manager.service vps-proxy-codex-worker.service
```

打开 Bot `/start`，确认底部六项菜单出现。

## 升级

先更新仓库，再从仓库项目目录运行：

```bash
git pull --ff-only
cd assets/vps-proxy-manager
sudo ./scripts/upgrade.sh
```

脚本会停止服务、保留 env/data/venv、创建带时间戳的数据库备份、同步代码、升级依赖和准入 Skill、执行 Alembic、再启动服务。升级后执行 doctor 和 Telegram 状态检查。

## 控制端卸载

```bash
sudo ./scripts/uninstall.sh
```

该脚本禁用并移除两个控制端 service，保留 `/opt/vps-proxy-manager/data` 和 `/etc/vps-proxy-manager`，防止误删唯一密钥。确认备份后可由管理员手工清理。

## 防火墙

控制端无需监听公网 Web 端口，Bot 使用 outbound polling。只需允许出站 HTTPS 和到目标 SSH。不要为项目额外开放未经认证的管理端口。

## 已知中断点

- 首次目标 VPS 初始化会安装包，但代理保持停止。
- 应用或恢复代理会修改目标主要出站，必须有云控制台。
- 控制端升级期间 Bot 暂停，running 高风险任务不会自动重放。
- 更换 `VPSPM_SECRET_KEY` 会使历史凭据不可解密。
