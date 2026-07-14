---
name: vps-proxy-telegram
description: Install, configure, operate, diagnose, and maintain a Telegram-controlled VPS proxy manager for authorized VPS hosts using sing-box TUN, SSH automation, rollback protection, secure subscription parsing, and audited deterministic tasks.
---

# VPS Proxy Telegram

Use this skill when the user wants Codex to install, configure, update, diagnose, or operate the bundled VPS Proxy Manager on a control VPS.

The deployable project is bundled at `assets/vps-proxy-manager/`.

## Operating Rules

- Manage only VPS hosts owned by the user or explicitly authorized by the user.
- Never paste, log, commit, or echo Telegram bot tokens, SSH passwords, private keys, subscription URLs, UUIDs, or full proxy links.
- Do not generate ad hoc remote shell from Telegram input. Use only the project CLI, fixed Python functions, and audited remote payload commands.
- For high-risk operations such as applying global proxy routing, rollback, uninstall, credential deletion, and host deletion, require an explicit confirmation in Telegram or from the operator.
- Prefer SSH keys over passwords. If a password is used, explain that it is encrypted at rest but Telegram is not a high-security password vault.
- For target VPS hosts, prefer Debian/Ubuntu first. Other distros require an adapter extension.

## Common Tasks

### Install on the control VPS

1. Copy or keep this skill folder under the Codex skills directory.
2. Go to `assets/vps-proxy-manager/`.
3. Copy `.env.example` to `.env` and fill:
   - `VPSPM_TELEGRAM_BOT_TOKEN`
   - `VPSPM_ADMIN_USER_IDS`
   - `VPSPM_SECRET_KEY` from `python -m vps_proxy_manager keygen`
4. Run `sudo ./scripts/install.sh`.
5. Start the bot with `sudo systemctl enable --now vps-proxy-manager.service`.

### Operate

Use the project CLI for deterministic operations:

```bash
vps-proxy-manager --help
vps-proxy-manager keygen
vps-proxy-manager init-db
vps-proxy-manager run-bot
vps-proxy-manager doctor
```

### Diagnose

- Read `docs/TROUBLESHOOTING.md` for common Telegram, SSH, subscription, sing-box, DNS, and TUN issues.
- Use `vps-proxy-manager doctor` for local checks.
- Use Telegram "当前状态" and "任务记录" for audited host-level status.
- If a target loses networking, use the target-side script documented in `docs/RECOVERY.md`.

## Architecture Pointers

- Bot UI: `src/vps_proxy_manager/bot/`
- Task queue and locks: `src/vps_proxy_manager/tasks/`
- SSH transport: `src/vps_proxy_manager/ssh/`
- Remote deterministic payload: `src/vps_proxy_manager/remote/payload.py`
- Node/subscription parsing: `src/vps_proxy_manager/proxy/parser.py`
- sing-box config generation: `src/vps_proxy_manager/proxy/singbox.py`
- SSRF protection: `src/vps_proxy_manager/proxy/ssrf.py`
- Tests: `tests/`

## Design Defaults

- Python 3.11+
- `python-telegram-bot` 22.x asynchronous API
- SQLite + SQLAlchemy async + Alembic migrations
- `asyncssh` for SSH and SFTP
- Fernet encryption for stored credentials
- sing-box TUN inbound with `auto_route`, Linux `auto_redirect` when available, DNS hijack, private/management/proxy-server bypass rules, and rollback timer

## Validation

Run from `assets/vps-proxy-manager/`:

```bash
python -m pip install -e ".[dev]"
ruff check .
mypy src
pytest
```

The tests cover VLESS Reality parsing, subscription decoding, SSRF blocking, command-injection guardrails, sing-box config generation, task lock behavior, and rollback command construction.
