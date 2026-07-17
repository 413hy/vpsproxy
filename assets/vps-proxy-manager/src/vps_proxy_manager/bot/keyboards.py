from __future__ import annotations

from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from vps_proxy_manager.models import (
    ProxyNode,
    Subscription,
    SubscriptionEntry,
    VpsCandidate,
    VpsHost,
    VpsNode,
    VpsProxyState,
    VpsSubscription,
    VpsSubscriptionEntry,
)

BTN_VPS = "🖥 VPS 管理"
BTN_NODES = "🔗 单节点库"
BTN_SUBSCRIPTIONS = "📚 订阅库"
BTN_TASKS = "📋 任务中心"
BTN_STATUS = "📊 控制端状态"
BTN_SETTINGS = "⚙️ 系统设置"


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_VPS), KeyboardButton(BTN_NODES)],
            [KeyboardButton(BTN_SUBSCRIPTIONS), KeyboardButton(BTN_TASKS)],
            [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="选择管理功能",
    )


def home_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("刷新概览", callback_data="home")],
            [InlineKeyboardButton("帮助与安全说明", callback_data="help")],
        ]
    )


def host_list(hosts: Sequence[VpsHost], candidates: Sequence[VpsCandidate]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{_mode_dot(h)} {h.name} · {h.host}", callback_data=f"h:v:{h.id}")]
        for h in hosts
    ]
    rows.append(
        [
            InlineKeyboardButton("添加 VPS", callback_data="h:add"),
            InlineKeyboardButton(f"待初始化 ({len(candidates)})", callback_data="c:list"),
        ]
    )
    rows.append([InlineKeyboardButton("刷新", callback_data="h:list")])
    return InlineKeyboardMarkup(rows)


def host_detail(host: VpsHost, state: VpsProxyState) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("单节点", callback_data=f"vh:n:{host.id}:0"),
            InlineKeyboardButton("订阅", callback_data=f"vh:s:{host.id}:0"),
        ],
        [
            InlineKeyboardButton("导入资源", callback_data=f"vh:import:{host.id}"),
            InlineKeyboardButton("刷新状态", callback_data=f"run:status:{host.id}:0"),
        ],
    ]
    if state.mode.value == "proxy":
        rows.append(
            [InlineKeyboardButton("切回本地出口", callback_data=f"risk:stop_proxy:{host.id}:0")]
        )
    elif state.mode.value == "local" and state.current_display_name:
        rows.append(
            [InlineKeyboardButton("启用上次代理", callback_data=f"risk:restore_proxy:{host.id}:0")]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton("编辑连接", callback_data=f"h:edit:{host.id}"),
                InlineKeyboardButton("回滚配置", callback_data=f"risk:rollback:{host.id}:0"),
            ],
            [
                InlineKeyboardButton("卸载代理", callback_data=f"risk:uninstall:{host.id}:0"),
                InlineKeyboardButton("删除 VPS", callback_data=f"h:delete:{host.id}"),
            ],
            [InlineKeyboardButton("返回 VPS 列表", callback_data="h:list")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def candidate_list(items: Sequence[VpsCandidate]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{item.name} · {item.lifecycle.value}", callback_data=f"c:v:{item.id}"
            )
        ]
        for item in items
    ]
    rows.append([InlineKeyboardButton("添加 VPS", callback_data="h:add")])
    rows.append([InlineKeyboardButton("返回", callback_data="h:list")])
    return InlineKeyboardMarkup(rows)


def candidate_detail(item: VpsCandidate) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if item.lifecycle.value == "failed":
        rows.append(
            [InlineKeyboardButton("重新交给 Codex 初始化", callback_data=f"c:retry:{item.id}")]
        )
        rows.append([InlineKeyboardButton("删除待初始化记录", callback_data=f"c:delete:{item.id}")])
    rows.append([InlineKeyboardButton("返回", callback_data="c:list")])
    return InlineKeyboardMarkup(rows)


def node_list(
    nodes: Sequence[ProxyNode],
    page: int,
    has_next: bool,
    *,
    search_active: bool = False,
    status_filter: str | None = None,
    sort: str = "latency",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for node in nodes:
        latency = f"{node.last_latency_ms}ms" if node.last_latency_ms is not None else "未测"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{_node_dot(node.status.value)} {node.name[:24]} · {latency}",
                    callback_data=f"n:v:{node.id}",
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton("上一页", callback_data=f"n:list:{page - 1}"))
    if has_next:
        navigation.append(InlineKeyboardButton("下一页", callback_data=f"n:list:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton("导入单节点", callback_data="n:add"),
            InlineKeyboardButton("全部本地测速", callback_data="run:local_node_test:0:0"),
        ]
    )
    filter_labels = {None: "全部", "online": "可用", "offline": "不可用", "unknown": "未测"}
    next_filter = {None: "online", "online": "offline", "offline": "unknown", "unknown": "all"}[
        status_filter
    ]
    rows.append(
        [
            InlineKeyboardButton(
                f"筛选：{filter_labels[status_filter]}", callback_data=f"n:filter:{next_filter}"
            ),
            InlineKeyboardButton(
                f"排序：{'延迟' if sort == 'latency' else '名称'}",
                callback_data=f"n:sort:{'name' if sort == 'latency' else 'latency'}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("搜索名称", callback_data="n:search"),
            *(
                [InlineKeyboardButton("清除搜索", callback_data="n:searchclear")]
                if search_active
                else []
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def node_detail(node: ProxyNode) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "控制端测速", callback_data=f"run:local_node_test:0:{node.id}"
                ),
                InlineKeyboardButton("导入指定 VPS", callback_data=f"as:start:n:{node.id}"),
            ],
            [
                InlineKeyboardButton("完整导出", callback_data=f"n:export:{node.id}"),
                InlineKeyboardButton("删除", callback_data=f"n:delete:{node.id}"),
            ],
            [InlineKeyboardButton("返回单节点库", callback_data="n:list:0")],
        ]
    )


def subscription_list(
    items: Sequence[Subscription], page: int, has_next: bool, *, search_active: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"📚 {item.name[:24]} · {item.node_count} 节点", callback_data=f"s:v:{item.id}"
            )
        ]
        for item in items
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton("上一页", callback_data=f"s:list:{page - 1}"))
    if has_next:
        navigation.append(InlineKeyboardButton("下一页", callback_data=f"s:list:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("导入订阅", callback_data="s:add")])
    rows.append(
        [
            InlineKeyboardButton("搜索名称", callback_data="s:search"),
            *(
                [InlineKeyboardButton("清除搜索", callback_data="s:searchclear")]
                if search_active
                else []
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def subscription_detail(item: Subscription) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("查看订阅节点", callback_data=f"se:list:{item.id}:0"),
                InlineKeyboardButton(
                    "本地测速全部", callback_data=f"run:local_subscription_test:0:{item.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "更新并测速", callback_data=f"run:local_subscription_test:1:{item.id}"
                ),
                InlineKeyboardButton("导入指定 VPS", callback_data=f"as:start:s:{item.id}"),
            ],
            [
                InlineKeyboardButton("完整导出", callback_data=f"s:export:{item.id}"),
                InlineKeyboardButton("删除", callback_data=f"s:delete:{item.id}"),
            ],
            [InlineKeyboardButton("返回订阅库", callback_data="s:list:0")],
        ]
    )


def subscription_entries(
    items: Sequence[SubscriptionEntry],
    subscription_id: int,
    page: int,
    has_next: bool,
    *,
    search_active: bool = False,
    status_filter: str | None = None,
    sort: str = "latency",
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{_node_dot(item.status.value)} {item.name[:25]} · {_latency(item.last_latency_ms)}",
                callback_data=f"se:v:{item.id}",
            )
        ]
        for item in items
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("上一页", callback_data=f"se:list:{subscription_id}:{page - 1}")
        )
    if has_next:
        nav.append(
            InlineKeyboardButton("下一页", callback_data=f"se:list:{subscription_id}:{page + 1}")
        )
    if nav:
        rows.append(nav)
    filter_labels = {None: "全部", "online": "可用", "offline": "不可用", "unknown": "未测"}
    next_filter = {None: "online", "online": "offline", "offline": "unknown", "unknown": "all"}[
        status_filter
    ]
    rows.append(
        [
            InlineKeyboardButton(
                f"筛选：{filter_labels[status_filter]}",
                callback_data=f"se:filter:{subscription_id}:{next_filter}",
            ),
            InlineKeyboardButton(
                f"排序：{'延迟' if sort == 'latency' else '名称'}",
                callback_data=f"se:sort:{subscription_id}:{'name' if sort == 'latency' else 'latency'}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("搜索名称", callback_data=f"se:search:{subscription_id}"),
            *(
                [
                    InlineKeyboardButton(
                        "清除搜索", callback_data=f"se:searchclear:{subscription_id}"
                    )
                ]
                if search_active
                else []
            ),
        ]
    )
    rows.append([InlineKeyboardButton("返回订阅", callback_data=f"s:v:{subscription_id}")])
    return InlineKeyboardMarkup(rows)


def assignment_hosts(
    hosts: Sequence[VpsHost], *, kind: str, resource_id: int, selected: set[int]
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'☑' if host.id in selected else '☐'} {host.name}",
                callback_data=f"as:t:{kind}:{resource_id}:{host.id}",
            )
        ]
        for host in hosts
    ]
    rows.append(
        [
            InlineKeyboardButton("确认导入", callback_data=f"as:go:{kind}:{resource_id}"),
            InlineKeyboardButton("取消", callback_data=f"{kind}:v:{resource_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def vps_import_menu(host_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("从单节点库选择", callback_data=f"vh:pickn:{host_id}:0")],
            [InlineKeyboardButton("从订阅库选择", callback_data=f"vh:picks:{host_id}:0")],
            [InlineKeyboardButton("返回 VPS", callback_data=f"h:v:{host_id}")],
        ]
    )


def vps_pick_nodes(
    host_id: int, items: Sequence[ProxyNode], page: int, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(item.name[:28], callback_data=f"vh:syncn:{host_id}:{item.id}")]
        for item in items
    ]
    rows.extend(_pick_nav("vh:pickn", host_id, page, has_next))
    rows.append([InlineKeyboardButton("返回", callback_data=f"vh:import:{host_id}")])
    return InlineKeyboardMarkup(rows)


def vps_pick_subscriptions(
    host_id: int, items: Sequence[Subscription], page: int, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(item.name[:28], callback_data=f"vh:syncs:{host_id}:{item.id}")]
        for item in items
    ]
    rows.extend(_pick_nav("vh:picks", host_id, page, has_next))
    rows.append([InlineKeyboardButton("返回", callback_data=f"vh:import:{host_id}")])
    return InlineKeyboardMarkup(rows)


def vps_node_list(
    host_id: int, items: Sequence[VpsNode], page: int, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{_node_dot(item.status.value)} {item.name[:25]} · {_latency(item.last_latency_ms)}",
                callback_data=f"vn:v:{item.id}",
            )
        ]
        for item in items
    ]
    rows.extend(_pick_nav("vh:n", host_id, page, has_next))
    rows.append(
        [InlineKeyboardButton("测试全部单节点", callback_data=f"run:vps_node_test:{host_id}:0")]
    )
    rows.append([InlineKeyboardButton("返回 VPS", callback_data=f"h:v:{host_id}")])
    return InlineKeyboardMarkup(rows)


def vps_node_detail(item: VpsNode) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "从此 VPS 测速", callback_data=f"run:vps_node_test:{item.host_id}:{item.id}"
                ),
                InlineKeyboardButton(
                    "设为当前出口", callback_data=f"risk:apply_node:{item.host_id}:{item.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "从 VPS 删除", callback_data=f"risk:remove_vps_node:{item.host_id}:{item.id}"
                )
            ],
            [InlineKeyboardButton("返回", callback_data=f"vh:n:{item.host_id}:0")],
        ]
    )


def vps_subscription_list(
    host_id: int, items: Sequence[VpsSubscription], page: int, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"📚 {item.name[:25]} · {item.node_count} 节点", callback_data=f"vs:v:{item.id}"
            )
        ]
        for item in items
    ]
    rows.extend(_pick_nav("vh:s", host_id, page, has_next))
    rows.append([InlineKeyboardButton("返回 VPS", callback_data=f"h:v:{host_id}")])
    return InlineKeyboardMarkup(rows)


def vps_subscription_detail(item: VpsSubscription) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "从此 VPS 测试全部",
                    callback_data=f"run:vps_subscription_test:{item.host_id}:{item.id}",
                )
            ],
            [InlineKeyboardButton("查看测速节点", callback_data=f"vse:list:{item.id}:0")],
            [
                InlineKeyboardButton(
                    "从 VPS 删除",
                    callback_data=f"risk:remove_vps_subscription:{item.host_id}:{item.id}",
                )
            ],
            [InlineKeyboardButton("返回", callback_data=f"vh:s:{item.host_id}:0")],
        ]
    )


def vps_subscription_entries(
    items: Sequence[VpsSubscriptionEntry], sub: VpsSubscription, page: int, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{_node_dot(item.status.value)} {item.name[:24]} · {_latency(item.last_latency_ms)}",
                callback_data=f"vse:v:{item.id}",
            )
        ]
        for item in items
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("上一页", callback_data=f"vse:list:{sub.id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("下一页", callback_data=f"vse:list:{sub.id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("返回订阅", callback_data=f"vs:v:{sub.id}")])
    return InlineKeyboardMarkup(rows)


def vps_subscription_entry_detail(item: VpsSubscriptionEntry, host_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "设为当前出口", callback_data=f"risk:apply_sub_entry:{host_id}:{item.id}"
                )
            ],
            [InlineKeyboardButton("返回", callback_data=f"vse:list:{item.vps_subscription_id}:0")],
        ]
    )


def risk_confirm(
    action: str, host_id: int, item_id: int, cancel_callback: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("确认执行", callback_data=f"do:{action}:{host_id}:{item_id}"),
                InlineKeyboardButton("取消", callback_data=cancel_callback),
            ]
        ]
    )


def delete_host_confirm(host_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("仅移除管理记录", callback_data=f"do:delete_host:{host_id}:0")],
            [InlineKeyboardButton("卸载代理后移除", callback_data=f"do:delete_host:{host_id}:1")],
            [InlineKeyboardButton("取消", callback_data=f"h:v:{host_id}")],
        ]
    )


def source_delete_confirm(kind: str, resource_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "强制删除所有副本", callback_data=f"srcdel:{kind}:{resource_id}"
                )
            ],
            [InlineKeyboardButton("取消", callback_data=f"{kind}:v:{resource_id}")],
        ]
    )


def task_list(rows_data: Sequence[tuple[int, str, str, int]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"#{task_id} {kind} · {status} {progress}%", callback_data=f"t:v:{task_id}"
            )
        ]
        for task_id, kind, status, progress in rows_data
    ]
    rows.append([InlineKeyboardButton("刷新", callback_data="t:list")])
    return InlineKeyboardMarkup(rows)


def task_detail(
    task_id: int,
    active: bool,
    *,
    result_callback: str | None = None,
    codex_task_id: int | None = None,
    resolved_task_id: int | None = None,
    return_callback: str | None = None,
    return_label: str = "返回对应页面",
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("刷新", callback_data=f"t:v:{task_id}")]]
    if result_callback:
        rows.append([InlineKeyboardButton("查看各节点测速结果", callback_data=result_callback)])
    if codex_task_id:
        rows.append(
            [InlineKeyboardButton("查看 Codex 自动诊断", callback_data=f"ct:v:{codex_task_id}")]
        )
    if resolved_task_id:
        rows.append(
            [
                InlineKeyboardButton(
                    f"查看解决任务 #{resolved_task_id}",
                    callback_data=f"t:v:{resolved_task_id}",
                )
            ]
        )
    if active:
        rows.append([InlineKeyboardButton("取消任务", callback_data=f"t:cancel:{task_id}")])
    if return_callback:
        rows.append([InlineKeyboardButton(return_label, callback_data=return_callback)])
    rows.append([InlineKeyboardButton("返回任务中心", callback_data="t:list")])
    return InlineKeyboardMarkup(rows)


def _pick_nav(
    prefix: str, host_id: int, page: int, has_next: bool
) -> list[list[InlineKeyboardButton]]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("上一页", callback_data=f"{prefix}:{host_id}:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton("下一页", callback_data=f"{prefix}:{host_id}:{page + 1}"))
    return [row] if row else []


def _node_dot(status: str) -> str:
    return {"online": "🟢", "offline": "🔴"}.get(status, "⚪")


def _mode_dot(host: VpsHost) -> str:
    return "🟢" if host.last_status.get("singbox_active") == "active" else "⚪"


def _latency(value: int | None) -> str:
    return f"{value}ms" if value is not None else "未测"
