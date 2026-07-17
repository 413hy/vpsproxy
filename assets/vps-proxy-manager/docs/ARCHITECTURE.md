# 架构设计

## 组件

```text
Telegram
   |
   v
Bot Application ---- SQLAlchemy/Alembic ---- SQLite
   |                                      |
   | ordinary Task                        | CodexTask
   v                                      v
TaskRunner                         Codex Worker -> codex exec
   |                                      |
   | AsyncSSH fixed action                | bootstrap Skill -> fixed CLI
   v                                      v
Target Agent <---------------------- candidate provisioning
   |
   v
sing-box TUN + systemd rollback timer
```

`python-telegram-bot` 使用单线程 update 顺序处理和异步长任务，避免 ConversationHandler 状态竞争。TaskRunner 内部不阻塞 Bot 轮询；任务进度写数据库，Bot 通过编辑同一条状态消息展示。

## 数据域

### 控制端单节点

`proxy_nodes` 仅存手工导入的单节点。它可以在控制端测速、完整导出、导入指定 VPS。列表按可测延迟优先排序。

### 控制端订阅

`subscriptions` 保存完整 URL 和缓存内容。`subscription_entries` 是该订阅的解析/测速缓存，生命周期依附订阅；它绝不写入单节点库。更新订阅只更新缓存，不自动切换任何 VPS。

### 目标 VPS 副本

`vps_nodes` 和 `vps_subscriptions` 是明确导入到目标的副本。删除控制端源资源前可以查出所有副本。目标订阅测速时由目标 Agent 安全拉取订阅，再由控制端解析并把固定测速配置逐项发给目标 Agent。

### 当前出口

`vps_proxy_states` 每个 host 一行：

- `local`：sing-box 停止且禁用，走 VPS 本地出口。
- `proxy`：只有一个当前单节点或订阅条目。
- `uninstalled`：本系统配置已卸载并恢复初始化前状态。

## 候选 VPS 与 Codex

添加向导先捕获主机公钥、展示 SHA256 指纹、测试认证和系统信息。确认后只创建 `vps_candidates`，不创建 `vps_hosts`。

Codex Worker 处理两类记录：

- `provision`：以 `danger-full-access` 调用准入 Skill。提示词只包含候选和任务整数 ID，Skill 只能执行 `provision-candidate` 固定 CLI。
- `diagnose`：普通后台任务系统级失败时自动创建。Worker 先生成权限 `0600` 的脱敏上下文，再以 `read-only` 调用诊断 Skill；Codex 只读源码和上下文，返回结构化根因、证据、建议与 `retry_safe`。它不能自动重试或修改网络。

两类任务都显式使用配置的 Codex 模型和推理强度。诊断完成或失败后，Worker 主动向管理员发送 Telegram 消息。

初始化准入检查：

1. Debian/Ubuntu 与 CPU 架构。
2. root/免交互 sudo 执行能力。
3. `/dev/net/tun`。
4. sing-box 安装及版本。
5. 远端 Agent 持久化及版本。
6. 初始化后 sing-box 必须停止。
7. VPS 本地公网访问成功。
8. 使用固定 known_hosts 的新 SSH 会话成功。

成功后事务性创建 `vps_hosts` 和 `vps_proxy_states`；失败留在候选列表，重启不会自动重放。

## 普通任务状态

`queued -> running -> succeeded|failed|canceled`，运行中可变为 `cancel_requested`。控制端重启后，只自动重新排队只读检测/测速类 queued 任务；网络修改任务标记失败并要求手工确认重试。

同一 VPS 的网络修改任务在创建时检查活动任务，执行时再获取 host lock。控制端源资源强制删除逐台获取 host lock。

## 代理应用

1. 解析已导入的 VPS 资源副本。
2. 生成 sing-box TUN 配置。
3. 路由绕过回环、链路本地、私网、组播、代理服务器 IP 和 SSH 管理来源 IP。
4. DNS 请求由路由规则劫持，远端 DoH 经代理，必要的本地解析经 direct。
5. 备份原配置、systemd 单元、service active/enabled 状态以及 route/rule/nft/resolver 快照。
6. 写独立回滚脚本和 systemd timer。
7. `sing-box check` 后原子替换配置。
8. 原 SSH 命令返回后，由 `vpspm-activate.timer` 延迟启动 TUN，避免切换动作占用管理通道。
9. 控制端建立全新 SSH 会话，等待服务稳定，并以出口 IP 请求或 HTTPS 204 任一成功作为公网可用证明。
10. 成功后解除 rollback timer、更新唯一当前出站和配置版本。

失败时数据库当前出站保持原值。回滚脚本按备份恢复服务状态，因此“旧配置存在但原服务已停止”不会被错误启动。

## 远端 Agent

Agent 位于 `/usr/local/lib/vpspm-agent/agent.py`，入口为 `/usr/local/sbin/vpspm-agent`。它只接受 SSH 模块 allowlist 中的固定 action，JSON 从 stdin 读取。资源文件名来自正整数 library ID，用户文本不进入 Shell。

Agent 使用目标端目录：

```text
/etc/vps-proxy-manager/library/nodes
/etc/vps-proxy-manager/library/subscriptions
/etc/vps-proxy-manager/backups
/etc/vps-proxy-manager/original-backup
/etc/vps-proxy-manager/rollback-last.sh
/etc/vps-proxy-manager/last-activation.json
```

目标端还会公开创建 `vpspm-activate.timer/service` 和 `vpspm-rollback.timer/service`。前者延迟两秒启动并检查 sing-box 是否稳定，后者在控制端未确认时恢复备份；它们都不是隐藏定时任务。

首次初始化备份指针永久保留，用于卸载时恢复初始化前 sing-box 状态。每次切换另存 `last-backup`，用于自动或显式回滚。

成功任务会按 VPS、任务类型和完整参数匹配旧失败任务，并写入 `resolved_by_task_id`。任务历史仍保留失败事实，但 Telegram 会明确链接到验证解决它的成功任务。

## 当前限制

- 自动系统适配仅 Debian/Ubuntu。
- 目标 SSH 用户需要 root 或免交互 sudo。
- 每个 VPS 同时只支持一个出站，不做负载均衡或自动故障切换。
- 定时订阅更新字段已预留，当前 UI 采用手工“更新并测速”，不会后台自动切换。
- 下载带宽测试默认未启用，当前实现 DNS/TCP/代理握手/真实访问延迟。
- IPv6 配置可生成，但是否可用取决于目标网络和代理节点。
