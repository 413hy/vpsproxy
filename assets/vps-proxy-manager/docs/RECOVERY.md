# Recovery

## Automatic Rollback

During proxy apply, target VPS creates:

- `/etc/vps-proxy-manager/rollback-last.sh`
- `vpspm-rollback.timer`

If the control VPS cannot confirm success before the timer fires, target rollback stops sing-box and restores the previous config when available.

## Manual Recovery While SSH Works

Use Telegram host page:

- `回滚配置`
- `停止代理`
- `彻底卸载`

Or run on target:

```bash
sudo /etc/vps-proxy-manager/rollback-last.sh
```

## Manual Recovery When SSH Is Lost

Use cloud provider VNC/rescue console and run:

```bash
sudo bash /path/to/emergency_restore.sh
```

If the file is not present, run:

```bash
sudo systemctl disable --now sing-box.service
sudo systemctl disable --now vpspm-rollback.timer
sudo rm -f /etc/sing-box/config.json
sudo reboot
```

## Risky Operations

The following can interrupt networking if the provider/kernel behaves unexpectedly:

- applying TUN global proxy
- changing DNS hijack behavior
- rollback during package manager network use
- uninstalling while sing-box is the only working route

Always keep provider console access available for first deployment on a new VPS class.
