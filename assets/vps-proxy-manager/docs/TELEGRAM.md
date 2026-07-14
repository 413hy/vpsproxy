# Telegram Operation

## Main Menu

- VPS 管理
- 添加 VPS
- 代理节点
- 导入单节点
- 导入订阅
- 节点测速
- 切换节点
- 当前状态
- 任务记录
- 安全设置
- 帮助

## Add VPS Wizard

1. VPS name.
2. IP or domain.
3. SSH port.
4. SSH username.
5. Auth method: password or SSH private key.
6. Bot tests SSH and detects OS.
7. Host key and encrypted credential are saved.

## Node Import

Single-node import validates the link and shows a masked server. Subscription import fetches from the control VPS, parses plain link lists, base64 subscriptions, Clash YAML, and sing-box JSON, then saves nodes without switching automatically.

## Node Pages

Each page shows node name, protocol, status, latency, and current-node marker. Node detail provides:

- select and apply to VPS
- test from VPS
- back

## High Risk Confirmation

The bot asks for confirmation before apply, stop, restore, rollback, uninstall, or host credential deletion.
