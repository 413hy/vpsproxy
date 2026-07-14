# VPS Proxy Manager

Telegram-controlled manager for authorized VPS hosts. It connects to target VPS hosts over SSH, installs `sing-box`, imports VLESS Reality nodes or subscriptions, tests nodes from the target VPS, and applies system-level global outbound proxy routing with rollback protection.

Only use it on VPS hosts you own or are explicitly authorized to manage.

## Requirements

- Control VPS: Debian/Ubuntu, Python 3.11+, systemd, outbound access to Telegram and target SSH.
- Target VPS: Debian/Ubuntu first-class support, root or sudo-capable SSH user, systemd, TUN available, IPv4.
- Python packages: see `pyproject.toml`.
- Proxy core: `sing-box`, installed on target VPS during first apply/speedtest.

## Telegram Setup

1. Create a bot with BotFather and get the token.
2. Get your numeric Telegram user ID.
3. Generate an encryption key:

   ```bash
   vps-proxy-manager keygen
   ```

4. Edit `/etc/vps-proxy-manager/vps-proxy-manager.env`:

   ```bash
   VPSPM_TELEGRAM_BOT_TOKEN=...
   VPSPM_ADMIN_USER_IDS=6977085303
   VPSPM_SECRET_KEY=...
   ```

Do not commit `.env` or the system env file.

## Install

```bash
cd assets/vps-proxy-manager
sudo ./scripts/install.sh
sudo editor /etc/vps-proxy-manager/vps-proxy-manager.env
sudo systemctl enable --now vps-proxy-manager.service
```

Check:

```bash
sudo systemctl status vps-proxy-manager.service
journalctl -u vps-proxy-manager.service -f
```

## First VPS

Open the Telegram bot and press:

`添加 VPS` -> name -> host/IP -> SSH port -> SSH username -> password or private key.

The bot captures and stores the SSH host fingerprint. If the fingerprint changes later, it refuses the connection.

## Import Nodes

- `导入单节点`: paste a node link such as VLESS + TCP + Reality + XTLS Vision.
- `导入订阅`: paste an HTTPS subscription URL. The fetcher limits redirects, response size, timeout, and blocks private/metadata IPs unless explicitly enabled.

Sensitive Telegram messages are deleted after reading when Telegram permits it. Telegram still transported them, so prefer SSH keys and rotate exposed secrets if needed.

## Test And Apply

1. Go to `代理节点`.
2. Select a node.
3. Choose `测试此节点` and pick the target VPS.
4. Choose `选择并应用到 VPS`.
5. Confirm the high-risk operation.

The target VPS applies `sing-box` TUN routing, bypasses private addresses, the management SSH port, and the proxy server IP when it is an IP literal. A rollback timer is armed before service start and disarmed only after the control VPS confirms SSH still works.

## Verify

Use `当前状态` in Telegram. It shows SSH status, sing-box service status, version, current node, outbound probe, and backup availability. You can also run on the target:

```bash
curl https://ifconfig.co/json
systemctl status sing-box
```

## Stop, Rollback, Uninstall

Telegram host page:

- `停止代理`: stops sing-box.
- `恢复代理`: starts sing-box with current config.
- `回滚配置`: runs target rollback script.
- `彻底卸载`: stops sing-box and removes managed config while keeping backups.

Each high-risk action requires confirmation.

## Backup And Migration

- Control data: `/opt/vps-proxy-manager/data`
- Control env: `/etc/vps-proxy-manager/vps-proxy-manager.env`
- Target backups: `/etc/vps-proxy-manager/backups`

Back up the database and env file together. Without the same `VPSPM_SECRET_KEY`, encrypted credentials cannot be decrypted.

## Known Limits

- Debian/Ubuntu are the only target systems automated in this version.
- Clash and sing-box subscription imports are parsed, but generated transparent-routing configs are best-tested for VLESS Reality and common VMess/Trojan nodes.
- IPv6 is enabled in config generation but depends on target kernel/network support.
- Download speed tests are not enabled by default; latency and proxy reachability are implemented.
