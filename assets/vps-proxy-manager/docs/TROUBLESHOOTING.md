# Troubleshooting

## Bot Does Not Respond

- Check `systemctl status vps-proxy-manager.service`.
- Confirm token and admin ID in `/etc/vps-proxy-manager/vps-proxy-manager.env`.
- Run `vps-proxy-manager doctor`.

## Unauthorized

Confirm your numeric Telegram user ID is in `VPSPM_ADMIN_USER_IDS`. If `VPSPM_REQUIRE_PRIVATE_CHAT=true`, use private chat.

## SSH Fails

- `ssh_auth_failed`: password/key/user is wrong.
- `ssh_host_key_unverified`: first host key capture failed.
- `ssh_host_key_changed`: possible rebuild or MITM. Verify manually before updating the saved host.
- `sudo` failure: target user must run `sudo python3` without an interactive password for privileged actions.

## Subscription Fails

- URL must be HTTPS.
- Private and metadata IPs are blocked by default.
- Response bigger than `VPSPM_SUBSCRIPTION_MAX_BYTES` is rejected.
- Empty or unsupported content returns a parse error.

## sing-box Fails

- Check target: `journalctl -u sing-box -n 100`.
- Run rollback from Telegram.
- If SSH is lost, use `scripts/emergency_restore.sh` from rescue console or provider VNC.

## TUN Unavailable

Some container VPS plans do not expose `/dev/net/tun` or `CAP_NET_ADMIN`. Use a full VM plan or ask the provider to enable TUN.

## DNS Leak

The generated config hijacks DNS into sing-box and uses remote DNS detoured through proxy. Verify with a DNS leak test from target after apply.
