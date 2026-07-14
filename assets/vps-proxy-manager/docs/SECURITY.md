# Security Design

## Controls

- Telegram admin user ID whitelist.
- Optional allowed chat ID list and private-chat-only mode.
- Sensitive operations require confirmation.
- SSH host fingerprint capture and verification.
- Credentials encrypted at rest with Fernet.
- Strict input validation for host, port, username, URLs, and names.
- Subscription fetcher allows only HTTPS, limits redirects/size/time, and blocks local/private/metadata addresses by default.
- Logs and task payloads are redacted.
- Remote actions are fixed payload actions, not shell built from Telegram text.
- Per-host network mutation lock prevents conflicting operations.

## Sensitive Data

Never store Bot tokens, SSH passwords, private keys, full node links, UUIDs, or subscription URLs in Git. The `.env.example` is safe; real values belong in `/etc/vps-proxy-manager/vps-proxy-manager.env` with mode `0600`.

Telegram is not a high-security password vault. The bot deletes sensitive messages after reading when possible, but they have already passed through Telegram.

## Threats

- Unauthorized Telegram user: denied by whitelist before menu/task handling.
- SSRF via subscription URL: blocked by URL validation, DNS resolution checks, metadata/private IP blocking, redirect limits, response limits.
- SSH MITM: first connection captures host key; later changes fail.
- Command injection: Telegram text is validated and never interpolated into remote shell commands.
- Target lockout: proxy apply arms rollback timer before route changes and disarms only after successful confirmation.

## Operator Duties

- Use SSH keys where possible.
- Restrict the bot token and rotate it if exposed.
- Keep `/etc/vps-proxy-manager/vps-proxy-manager.env` and backups private.
- Manage only authorized VPS hosts.
