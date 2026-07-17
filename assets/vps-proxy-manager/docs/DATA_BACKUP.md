# 数据备份与迁移

## 必须一起备份

```text
/opt/vps-proxy-manager/data/
/etc/vps-proxy-manager/vps-proxy-manager.env
```

数据库包含 Fernet 加密后的 SSH、节点和订阅内容；环境文件中的 `VPSPM_SECRET_KEY` 是唯一解密密钥。只备份数据库无法恢复。

停止服务后备份：

```bash
sudo systemctl stop vps-proxy-manager.service vps-proxy-codex-worker.service
sudo tar -C / -czf /root/vps-proxy-manager-backup.tgz \
  opt/vps-proxy-manager/data \
  etc/vps-proxy-manager/vps-proxy-manager.env
sudo chmod 600 /root/vps-proxy-manager-backup.tgz
sudo systemctl start vps-proxy-manager.service vps-proxy-codex-worker.service
```

备份文件含高敏感数据，应使用受控渠道传输并加密离线保存。

## 恢复到新控制端

1. 安装相同或更新版本项目，但先不启动服务。
2. 恢复 data 目录和 env 文件。
3. 设置 owner/mode。
4. 执行 Alembic 和 doctor。
5. 确认 Codex root 登录和准入 Skill。
6. 启动两个服务。

```bash
sudo chown -R root:root /opt/vps-proxy-manager/data /etc/vps-proxy-manager
sudo chmod 700 /opt/vps-proxy-manager/data /etc/vps-proxy-manager
sudo chmod 600 /etc/vps-proxy-manager/vps-proxy-manager.env
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager init-db
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service
```

目标 VPS known_hosts 绑定主机名/IP 和公钥。迁移控制端不需要重新接受目标指纹；若目标地址或主机公钥改变，使用 Telegram 编辑连接并重新确认。

## 目标端备份

目标自动备份位于 `/etc/vps-proxy-manager/backups`，`original-backup` 指向初始化前快照，`last-backup` 指向最近切换快照。它们包含代理配置，权限必须保持 root-only，不应上传到公开工单。

控制端数据库备份不代替目标恢复备份，目标备份也不代替控制端数据库。
