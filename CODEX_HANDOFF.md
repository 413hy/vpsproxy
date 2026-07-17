# Codex Handoff

这份文件供接手仓库或控制端 VPS 的另一个 Codex 使用。不要依赖历史聊天；以下内容是当前运行模型。

## 项目定位

仓库是一个 Codex Skill，内含可部署的异步 Telegram Bot 后端。它管理用户本人拥有或明确授权的 Debian/Ubuntu VPS，并在目标 VPS 上通过 sing-box TUN 管理系统级主要出站。

生产链路不是“Telegram 文本 -> LLM 生成 Shell”。当前链路为：

```text
Telegram Bot
  ├─ 控制端资源操作 -> 固定 TaskRunner -> 控制端 sing-box 测速
  ├─ 目标 VPS 操作  -> 固定 TaskRunner -> AsyncSSH -> 远端 Agent 固定 action
  ├─ 新 VPS 准入    -> CodexTask -> Codex Worker -> bootstrap Skill -> 固定 CLI
  ├─ 系统任务失败   -> CodexTask -> Codex Worker -> read-only diagnosis Skill
  └─ 状态一致性巡检 -> consistency_check -> Codex Worker -> read-only diagnosis Skill
```

Codex 介入两类流程。新 VPS 初始化只收到候选 ID 和任务 ID，SSH 凭据由固定 CLI 从加密数据库读取。普通任务系统级失败时会自动创建只读诊断任务，只向 Codex 提供 Worker 生成的脱敏上下文和项目源码。

## 不能混合的数据域

- `proxy_nodes`：控制端手工单节点库。只包含单独导入的节点。
- `subscriptions`：控制端完整订阅对象。
- `subscription_entries`：订阅的私有解析/测速缓存，不属于单节点库。
- `vps_nodes`：明确导入到某台 VPS 的单节点副本。
- `vps_subscriptions`：明确导入到某台 VPS 的完整订阅副本。
- `vps_subscription_entries`：从该 VPS 拉取订阅后形成的测速缓存。
- `vps_proxy_states`：每台 VPS 唯一当前出站状态。
- `vps_candidates` / `codex_tasks`：Codex 准入工作区；验收前不能出现在正式 VPS 列表。

删除控制端节点或订阅时必须先显示 `node_usage` / `subscription_usage`。强制删除任务会逐台停止正在使用的代理、删除远端副本，最后删除控制端源对象。

## 仓库和部署路径

```text
仓库: /root/vpsvpn/vpsproxy-skill-repo
项目: /root/vpsvpn/vpsproxy-skill-repo/assets/vps-proxy-manager
部署: /opt/vps-proxy-manager
环境: /etc/vps-proxy-manager/vps-proxy-manager.env
数据库: /opt/vps-proxy-manager/data/app.db
主 Skill: 仓库根 SKILL.md
准入 Skill: /root/.codex/skills/vps-proxy-target-bootstrap
诊断 Skill: /root/.codex/skills/vps-proxy-task-diagnosis
```

systemd 服务均按用户要求以 root 运行：

```text
vps-proxy-manager.service          Telegram Bot + TaskRunner
vps-proxy-codex-worker.service    Codex 候选 VPS 准入与失败任务诊断
```

Bot 服务有 `NoNewPrivileges`、`ProtectSystem=strict` 和受限写目录。Codex Worker 需要访问 `/root/.codex`、数据库、Codex CLI 和目标网络。

## 安装与验证

```bash
cd /root/vpsvpn/vpsproxy-skill-repo/assets/vps-proxy-manager
sudo ./scripts/install.sh
sudo editor /etc/vps-proxy-manager/vps-proxy-manager.env
sudo codex login status
sudo /opt/vps-proxy-manager/venv/bin/vps-proxy-manager doctor
sudo systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service
```

环境文件必须是 `root:root 0600`。不要执行 `cat` 或把值带进回复。只检查变量是否存在：

```bash
sudo stat -c '%U:%G %a %n' /etc/vps-proxy-manager/vps-proxy-manager.env
```

必填变量：

```text
VPSPM_TELEGRAM_BOT_TOKEN
VPSPM_ADMIN_USER_IDS
VPSPM_SECRET_KEY
```

Codex 相关变量默认指向 `/usr/local/bin/codex`、`/root/.codex` 和 `/opt/vps-proxy-manager`。`doctor` 会校验 Fernet 密钥、Codex CLI 和登录状态。

当前生产模型已实际调用验证为 `VPSPM_CODEX_MODEL=gpt-5.6-sol`、`VPSPM_CODEX_REASONING_EFFORT=high`。当前 ChatGPT 账号直接调用 `gpt-5.6` 会返回“不支持”错误，不要未经烟雾测试改回该 slug。

## 新 VPS 准入

1. Bot 向导验证名称、主机、端口、用户和认证内容。
2. `ssh-keyscan` 获取主机公钥并显示 SHA256 指纹，用户确认后固定 known_hosts 内容。
3. Bot 测试 SSH 与系统信息，创建 `VpsCandidate` 和 `CodexTask`。
4. Worker 将任务标为 running，然后调用 `codex exec`，提示词只有数字 ID。
5. Codex 使用 `$vps-proxy-target-bootstrap`，执行固定 `provision-candidate` 命令。
6. CLI 通过 SSH 安装并持久化远端 Agent、检查 Debian/Ubuntu、TUN 和 sing-box。
7. 初始化完成后代理保持停止，VPS 使用本地出口。
8. 新 SSH 会话验证 Agent 版本、sing-box 版本和本地公网访问。
9. 全部通过后才从候选域提升为正式 `VpsHost`。

Worker 或 Codex 重启后，running 任务会失败，不自动重放高风险动作。

失败任务无需用户主动请求排查。`TaskRunner` 在失败事务中创建一对一 `CodexTask(operation=diagnose)`；Worker 使用只读沙箱分析并主动发送 Telegram 结论。诊断本身不重试原任务。

TaskRunner 还会周期核对数据库期望出口、远端活动配置 SHA-256、资源指纹、sing-box 状态和公网探测。巡检避开正在进行网络修改的 VPS；连续异常达到阈值后创建 `consistency_check` 失败任务并自动排队 Codex 诊断。出于断网风险控制，诊断不能未经确认自动重放切换、回滚或卸载。

## 目标 VPS 固定 action

`src/vps_proxy_manager/ssh/client.py` 的 `REMOTE_ACTIONS` 是唯一远端动作名单：

```text
detect initialize store_node store_subscription remove_node remove_subscription
fetch_subscription apply_proxy confirm_proxy rollback stop_proxy restore_proxy
uninstall status speedtest
```

动作名不能来自 Telegram 自由文本。参数通过 JSON stdin 传给固定 Python Agent，资源文件名只允许正整数 ID。

## 代理切换与回滚

应用配置时：

1. 根据 VPS 资源副本生成 sing-box 配置。
2. 绕过私网、本地网、控制端 SSH 来源 IP 和代理服务器 IP。
3. 远端备份配置、systemd 单元、服务 active/enabled 状态、路由/rule/nft/resolver 快照。
4. 写入 `/etc/vps-proxy-manager/rollback-last.sh` 并启动 `vpspm-rollback.timer`。
5. `sing-box check` 成功后写入配置与 `pending-config.json`，内容包含配置 SHA-256 和不可逆资源指纹。
6. 延迟 activation 必须执行 `systemctl restart sing-box.service`；`start` 无法让已运行进程加载新配置，禁止改回。
7. 进程稳定后 Agent 才把待加载标识提升为 `active-config.json`。
8. 控制端建立新的 SSH 会话，核对服务、活动哈希、资源指纹和真实公网访问。
9. 仅在全部验证成功后调用 `confirm_proxy` 解除定时器并更新数据库当前节点。

回滚必须恢复备份时的服务启用和运行状态。旧配置存在并不代表旧服务原本处于运行状态；不要重新引入“有配置就启动”的错误。

每个 VPS 相关任务的 Telegram 详情必须提供 `查看此 VPS`。应用当前已核对节点时应直接提示无需重复切换；不得再次创建网络修改任务。测速详情应显示 DNS、TCP、代理握手、真实访问延迟和测试代理返回的出口。

## 出口操作语义

- `stop_proxy`：停止并禁用服务，持久使用 VPS 本地出口；节点、订阅、上次配置保留。
- `restore_proxy`：保护性恢复上次代理；验证失败时定时回滚到启动前的本地状态。
- `rollback`：恢复上次切换前的配置和当时出口模式，不等同于强制本地出口。
- `uninstall`：恢复首次初始化前的 sing-box 配置/单元/服务状态，删除目标 VPS 资源库；保留远端 Agent 和备份。
- `delete_host`：可以仅删控制端记录，或先卸载再删。

## 测试要求

```bash
cd /root/vpsvpn/vpsproxy-skill-repo/assets/vps-proxy-manager
. .venv/bin/activate
ruff format --check src tests migrations
ruff check src tests migrations
mypy src
pytest -q
```

代理解析或生成器改动后，对每种协议运行 `sing-box check`。远端逻辑测试必须 monkeypatch 所有 `/etc`、`/usr/local` 和 systemd 路径；禁止测试触碰宿主机真实配置。

发布前执行敏感扫描，管理员 ID 也不要写死进公开文档：

```bash
rg -n --hidden --glob '!.git/**' --glob '!.venv/**' \
  'BEGIN (OPENSSH|RSA|EC) PRIVATE KEY|[0-9]{6,12}:[A-Za-z0-9_-]{20,}|VPSPM_TELEGRAM_BOT_TOKEN=.+|VPSPM_SECRET_KEY=.+[A-Za-z0-9_-]{30,}' .
```

## 升级、备份、恢复

升级脚本会先停两个服务、创建带时间戳的 `app.db.before-upgrade.*`、在同步时排除 data/venv/env、安装依赖、更新准入 Skill、执行 Alembic，再启动服务：

```bash
sudo ./scripts/upgrade.sh
```

正式备份必须同时包含数据库目录和环境文件。恢复时保持原 `VPSPM_SECRET_KEY`，执行 `init-db` 后再启动服务。

目标失联时首先使用云厂商控制台运行 `/etc/vps-proxy-manager/rollback-last.sh`。脚本不存在时运行仓库 `scripts/emergency_restore.sh`；它只停 sing-box 并保留未知配置。

## GitHub

远端仓库：`https://github.com/413hy/vpsproxy-skill`。推送前检查 `git status`、完整测试和敏感扫描。不要覆盖用户未提交的更改，不要强推。
