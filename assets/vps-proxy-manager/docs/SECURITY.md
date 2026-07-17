# 安全设计与威胁模型

## 信任边界

- Telegram 平台只用于交互，不视为密码保险库。
- 管理员 Telegram ID 是第一层授权；默认只允许私聊。
- 控制端 root、环境文件和数据库属于高信任域。
- 目标 VPS 通过 SSH 主机公钥固定建立信任。
- 节点、订阅和目标网络均视为不可信输入。
- Codex 只能处理数字任务 ID，不应看到 SSH 或代理凭据。

## Telegram 授权

`VPSPM_ADMIN_USER_IDS` 是明确白名单，未匹配用户默认拒绝。默认 `VPSPM_REQUIRE_PRIVATE_CHAT=true`。如允许群组，还必须配置 `VPSPM_ALLOWED_CHAT_IDS`。

应用代理、恢复代理、回滚、卸载、删除 VPS、删除目标副本和强制删除源资源都要求 Inline Keyboard 二次确认。Callback 数据只包含固定短动作和整数 ID。

## 凭据

Bot Token 和 Fernet 密钥只放在 `/etc/vps-proxy-manager/vps-proxy-manager.env`，权限 `root:root 0600`。SSH 密码、私钥、节点链接、订阅 URL/内容在数据库中使用 Fernet 加密。

Telegram 输入消息读取后尽量删除。导出是用户明确要求的完整导出，Bot 以文件发送；用户应自行管理 Telegram 历史和导出文件。

日志、任务结果和审计 detail 经过脱敏。不得在异常日志中加入原始 JSON stdin、配置或环境文件；异常记录只写错误类型和脱敏摘要，不输出包含局部变量的完整 traceback。

## SSH 与命令注入

首次连接通过 `ssh-keyscan` 获取公钥并展示 SHA256 指纹，用户确认后保存完整 known_hosts 行；后续 AsyncSSH 必须匹配。指纹变化不自动接受。

主机、端口、用户名和名称均严格校验。远端 command 只有固定 Python 入口和 allowlist action；认证内容及任务参数通过临时权限 `0600` 文件或 JSON stdin 传递。资源路径只使用整数 ID。

没有 Telegram 自由命令、远端 Shell 控制台或拼接脚本入口。

## 订阅 SSRF

控制端和目标端订阅下载均：

- 只允许无内嵌用户名/密码的 HTTPS URL。
- DNS 解析结果必须全部是全局地址；阻止 loopback、私网、链路本地、保留地址和云元数据地址。
- 使用解析后 IP 固定连接，降低 DNS rebinding 风险。
- 每次重定向重新校验，限制重定向次数。
- 限制连接/总超时和响应字节数。
- 忽略环境代理，避免下载路径被未知 `HTTP_PROXY` 改写。

控制端只有明确设置 `VPSPM_ALLOW_PRIVATE_SUBSCRIPTION_URLS=true` 才允许内网订阅。目标 Agent 当前始终阻止私网订阅，避免控制端开关意外扩大目标端权限。

## 网络失联防护

- 代理服务器 IP 必须 direct，避免套娃回路。
- 控制端 SSH 来源 IP direct，保留管理连接回程。
- 管理来源、代理服务器与私网 CIDR 同时写入 TUN `route_exclude_address`，在进入 TUN 前绕过。
- 私网、本地和链路地址 direct。
- 写配置前备份；`sing-box check` 后才替换。
- 启动前 arm 独立 systemd rollback timer。
- 原 SSH 命令先返回，再由公开的 systemd activation timer 延迟启动 TUN。
- 新 SSH 会话和真实公网访问成功后才 disarm。
- 控制端重启不自动重放高风险任务。
- 切回本地出口会 disable 服务，重启后不自动恢复代理。

首次在新的云厂商、内核或 VPS 虚拟化类型上启用 TUN 时，仍必须具备 VNC/串口/救援控制台。没有软件能在内核或供应商路由完全异常时保证 SSH 永不失联。

## Codex Worker 风险

Worker 以 root 调用 Codex CLI。准入命令需要网络和文件权限，自动诊断使用只读沙箱。这是用户要求的运行模型，也是高权限边界。缓解措施：

- 提示词只含整数 ID。
- 准入 Skill 的命令固定。
- CLI 校验 CodexTask 必须处于 running 且匹配 candidate。
- 凭据只由 CLI 解密，不进入提示词和输出。
- 失败或重启不自动重放。
- 普通操作仍由固定 TaskRunner 执行；只有系统级失败会自动创建 Codex 诊断。
- 诊断只读取权限 `0600` 的脱敏上下文和项目源码，不读取环境文件、数据库、SSH 材料、订阅内容或节点链接。
- 诊断不能自动重试代理切换、恢复、回滚、卸载或删除等操作。

控制端必须是专用、受信任 VPS，不应与不可信租户共享 root 或 Codex HOME。

## 审计

审计记录包含操作者 Telegram ID、目标 VPS ID、动作、时间和结果，不包含明文凭据。systemd 日志用于技术诊断。数据库和环境文件备份本身属于高敏感数据，必须加密传输和限制权限。

## 安全发布检查

```bash
ruff check src tests migrations
mypy src
pytest -q
rg -n --hidden --glob '!.git/**' --glob '!.venv/**' \
  'BEGIN (OPENSSH|RSA|EC) PRIVATE KEY|[0-9]{6,12}:[A-Za-z0-9_-]{20,}' .
```

Token 曾公开时，仅从仓库删除不够，必须在 BotFather 吊销并生成新 Token。
