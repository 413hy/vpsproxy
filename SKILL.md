---
name: vps-proxy-telegram
description: Deploy, configure, test, upgrade, diagnose, and safely operate the bundled Telegram VPS Proxy Manager. Use when Codex must manage the control VPS lifecycle, inspect deterministic proxy tasks, or supervise authorized target-VPS admission through the bundled Codex worker and bootstrap skill.
---

# VPS Proxy Telegram

Operate the project bundled under `assets/vps-proxy-manager/`. Read `CODEX_HANDOFF.md` before changing runtime behavior and read the relevant document under `assets/vps-proxy-manager/docs/` before recovery or upgrade work.

## Non-Negotiable Boundaries

- Act only on VPS hosts owned by the user or explicitly authorized by the user.
- Never turn Telegram text into generated shell commands.
- Use only project CLI commands, fixed Python task handlers, the SSH action allowlist, and the installed `$vps-proxy-target-bootstrap` and `$vps-proxy-task-diagnosis` Skills.
- Never print or commit Bot tokens, SSH credentials, private keys, encryption keys, full node links, or subscription URLs.
- Keep SSH host-key verification, per-host mutation locking, high-risk confirmations, and target rollback timers enabled.
- Treat `切回本地出口` as persistent `stop_proxy`; it is separate from rollback, uninstall, and deleting the VPS record.

## Runtime Domains

Do not merge these stores:

1. Controller single-node library: manually imported links only; tests run from the controller.
2. Controller subscription library: whole subscriptions plus private cached entries; entries never become controller single nodes.
3. Target VPS libraries: copies explicitly imported to that VPS; target tests run through that VPS.
4. Target proxy state: one active outbound at most, or persistent local-exit mode.

## Codex Admission Workflow

The Telegram add-VPS wizard creates `VpsCandidate` and `CodexTask` records after SSH host-key confirmation. `vps-proxy-codex-worker.service` invokes Codex non-interactively with numeric IDs only. Codex must use `$vps-proxy-target-bootstrap`, which runs exactly:

```bash
/opt/vps-proxy-manager/venv/bin/vps-proxy-manager provision-candidate \
  --candidate-id <integer> \
  --codex-task-id <integer>
```

Do not inspect credentials or improvise remote commands. A candidate enters `vps_hosts` only after the deterministic command verifies the Agent version, sing-box installation, stopped proxy state, TUN initialization, local public connectivity, and a pinned SSH host key.

## Control VPS Lifecycle

Install:

```bash
cd assets/vps-proxy-manager
sudo ./scripts/install.sh
sudo editor /etc/vps-proxy-manager/vps-proxy-manager.env
sudo codex login status
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service
```

Upgrade:

```bash
cd assets/vps-proxy-manager
sudo ./scripts/upgrade.sh
```

Diagnose:

```bash
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl status vps-proxy-manager.service vps-proxy-codex-worker.service --no-pager
sudo journalctl -u vps-proxy-manager.service -u vps-proxy-codex-worker.service -n 200 --no-pager
```

Do not echo the environment file. Report only whether required values are present and permissions are `0600`.

## Change Validation

Run from `assets/vps-proxy-manager/`:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff format --check src tests migrations
ruff check src tests migrations
mypy src
pytest -q
```

For proxy generator changes, also run generated configs through `sing-box check`. For migrations, test both a fresh database and an upgrade from revision `0001_initial`. For rollback changes, verify that a previously stopped service remains stopped after rollback and an active previous service is restored active.

## Recovery

If target SSH still works, use the confirmed Telegram rollback or local-exit action. If SSH is lost, direct the operator to the cloud console and run:

```bash
sudo /etc/vps-proxy-manager/rollback-last.sh
```

If absent, use `assets/vps-proxy-manager/scripts/emergency_restore.sh` on the target. Do not delete unknown network or sing-box configuration as an unverified recovery step.
