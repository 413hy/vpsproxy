# Architecture

## Roles

- Codex Skill: installs, configures, updates, diagnoses, tests, and calls deterministic project commands on the control VPS.
- Telegram Bot: collects inputs, displays menus/status, and creates predefined tasks.
- Backend task runner: performs SSH, detection, subscription import, speed tests, proxy apply, rollback, and audit logging.
- Target payload: fixed Python actions executed over SSH. It does not receive arbitrary shell.

## Flow

1. Admin uses Telegram inline menus.
2. Bot validates authorization and input.
3. Bot stores encrypted credentials/nodes in SQLite.
4. Bot creates a task row and enqueues the task ID.
5. Task runner loads the task and takes a per-host lock for network-changing operations.
6. SSH client connects with host key verification.
7. Remote payload executes a fixed action.
8. Result is stored as task result and audit log with redaction.

## Proxy Apply

1. Detect target OS, route, tools, and SSH client IP.
2. Generate sing-box TUN config.
3. Remote payload backs up config, route/rule snapshots, nft rules, and resolver state.
4. Remote payload writes rollback script and arms a systemd rollback timer.
5. sing-box config is checked with `sing-box check`.
6. Service starts and enables on boot.
7. Control VPS verifies SSH/status and disarms rollback.

## Task States

`queued`, `running`, `cancel_requested`, `succeeded`, `failed`, `rolled_back`, `canceled`.

Long-running tasks do not block the Telegram polling loop.

## Codex Boundary

Codex is deliberately not called by the Telegram bot for each runtime operation. The bot creates predefined tasks and the backend executes audited code paths. Codex remains the operator for deployment, diagnosis, upgrades, test runs, and code/script repair.

This avoids a production pattern where Telegram text causes an LLM to generate arbitrary shell commands.

## Local Exit Mode

`stop_proxy` means persistent local VPS egress:

```text
systemctl disable --now sing-box.service
```

`restore_proxy` means re-enable the existing proxy config:

```text
systemctl enable --now sing-box.service
```
