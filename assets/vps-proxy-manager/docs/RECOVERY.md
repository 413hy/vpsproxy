# 目标 VPS 恢复手册

## 恢复层级

按以下顺序处理，避免先做破坏性操作：

1. Telegram `切回本地出口`。
2. Telegram `回滚配置`。
3. SSH 执行目标现有回滚脚本。
4. 云厂商 VNC/串口/救援控制台执行回滚脚本。
5. 回滚脚本不存在时，执行仓库紧急恢复脚本，仅停止代理。

## 自动回滚

每次应用或恢复代理前，目标创建：

```text
/etc/vps-proxy-manager/rollback-last.sh
/etc/systemd/system/vpspm-rollback.service
/etc/systemd/system/vpspm-rollback.timer
```

控制端只有在新 SSH 和真实公网访问成功后才解除 timer。否则 timer 到期恢复切换前的配置、systemd 单元以及服务 enabled/active 状态。

查看保护状态：

```bash
sudo systemctl status vpspm-rollback.timer --no-pager
sudo cat /etc/vps-proxy-manager/last-backup
```

第二条只包含备份目录路径，不包含代理凭据。不要输出备份中的 `config.json`。

## SSH 仍可用

优先从 Telegram 执行 `切回本地出口`。手工等价命令：

```bash
sudo systemctl disable --now sing-box.service
sudo systemctl disable --now vpspm-rollback.timer
```

该操作保留配置，之后可由 Bot 恢复。

需要恢复上次切换前状态：

```bash
sudo /etc/vps-proxy-manager/rollback-last.sh
```

## SSH 已失联

打开云厂商控制台，先执行：

```bash
sudo /etc/vps-proxy-manager/rollback-last.sh
```

等待 10-30 秒后测试网络和 SSH：

```bash
ip route
systemctl is-active sing-box.service
curl -4 --max-time 10 https://www.gstatic.com/generate_204 -o /dev/null -w '%{http_code}\n'
```

回滚脚本不存在时，将仓库 `scripts/emergency_restore.sh` 放到目标救援环境并执行：

```bash
sudo bash emergency_restore.sh
```

兜底只停止并禁用 sing-box，不删除 `/etc/sing-box/config.json`，也不盲目重启 NetworkManager/systemd-networkd。

## 恢复初始化前状态

Telegram `卸载代理` 会读取：

```text
/etc/vps-proxy-manager/original-backup
```

它恢复首次初始化前的 sing-box 配置、本地 systemd 单元和服务状态。若 sing-box 原本不存在，本系统安装的包会尝试移除。卸载前另存一份安全备份，路径在任务结果中。

如果 original backup 损坏，不要猜测并覆盖网络配置。先停止 sing-box，再从云镜像、配置管理或人工备份恢复原配置。

## 控制端与目标状态不一致

自动 timer 已回滚但控制端任务失败时，数据库通常仍保留旧节点。进入 VPS 页面点击 `刷新状态`。如果状态仍不一致：

1. 切回本地出口。
2. 重新测试目标资源。
3. 再次选择节点并应用。

不要直接修改 SQLite 当前节点字段。

## 首次上线准备

- 确认 VNC/串口控制台实际可登录。
- 保存 VPS 当前出口 IP、默认路由和 DNS。
- 确认 `/dev/net/tun` 和 `CAP_NET_ADMIN`。
- 不在系统升级、数据库迁移或重要下载期间切换全局代理。
- 首次使用新供应商/内核时先在可重装测试机验收。
