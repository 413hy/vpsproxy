# Architecture

## Roles

- Codex Skill: installs, configures, updates, diagnoses, and calls deterministic project commands.
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
