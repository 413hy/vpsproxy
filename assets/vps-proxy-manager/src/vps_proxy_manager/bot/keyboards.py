from __future__ import annotations

from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from vps_proxy_manager.models import ProxyNode, VpsHost


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("VPS 管理", callback_data="hosts"), InlineKeyboardButton("添加 VPS", callback_data="add_host")],
            [InlineKeyboardButton("代理节点", callback_data="nodes:0"), InlineKeyboardButton("导入单节点", callback_data="import_node")],
            [InlineKeyboardButton("导入订阅", callback_data="import_sub"), InlineKeyboardButton("节点测速", callback_data="speedtest_menu")],
            [InlineKeyboardButton("当前状态", callback_data="status_menu"), InlineKeyboardButton("任务记录", callback_data="tasks")],
            [InlineKeyboardButton("安全设置", callback_data="security"), InlineKeyboardButton("帮助", callback_data="help")],
        ]
    )


def host_list(hosts: Sequence[VpsHost]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{h.name} ({h.host})", callback_data=f"host:{h.id}")] for h in hosts]
    rows.append([InlineKeyboardButton("添加 VPS", callback_data="add_host"), InlineKeyboardButton("返回", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def host_detail(host: VpsHost) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("测试 SSH", callback_data=f"task:test_ssh:{host.id}"), InlineKeyboardButton("刷新状态", callback_data=f"task:status:{host.id}")],
            [InlineKeyboardButton("测试所有节点", callback_data=f"speedtest:{host.id}:all")],
            [InlineKeyboardButton("切回本地出口", callback_data=f"confirm:stop_proxy:{host.id}:0"), InlineKeyboardButton("启用代理", callback_data=f"confirm:restore_proxy:{host.id}:0")],
            [InlineKeyboardButton("回滚配置", callback_data=f"confirm:rollback:{host.id}:0"), InlineKeyboardButton("彻底卸载", callback_data=f"confirm:uninstall:{host.id}:0")],
            [InlineKeyboardButton("返回", callback_data="hosts")],
        ]
    )


def node_list(nodes: Sequence[ProxyNode], page: int, current_node_id: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for node in nodes:
        marker = "● " if node.id == current_node_id else ""
        latency = f"{node.last_latency_ms}ms" if node.last_latency_ms is not None else "未测"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{marker}{node.name[:28]} | {node.protocol} | {latency}",
                    callback_data=f"node:{node.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("上一页", callback_data=f"nodes:{max(page - 1, 0)}"),
            InlineKeyboardButton("下一页", callback_data=f"nodes:{page + 1}"),
        ]
    )
    rows.append([InlineKeyboardButton("导入单节点", callback_data="import_node"), InlineKeyboardButton("返回", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def node_detail(node: ProxyNode) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("选择并应用到 VPS", callback_data=f"choose_host_for_node:{node.id}")],
            [InlineKeyboardButton("测试此节点", callback_data=f"choose_host_speed:{node.id}")],
            [InlineKeyboardButton("返回", callback_data="nodes:0")],
        ]
    )


def choose_host(hosts: Sequence[VpsHost], prefix: str, item_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(h.name, callback_data=f"{prefix}:{h.id}:{item_id}")] for h in hosts]
    rows.append([InlineKeyboardButton("返回", callback_data="nodes:0")])
    return InlineKeyboardMarkup(rows)


def confirm(action: str, host_id: int, node_id: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("确认执行", callback_data=f"do:{action}:{host_id}:{node_id}"),
                InlineKeyboardButton("取消", callback_data=f"host:{host_id}"),
            ]
        ]
    )
