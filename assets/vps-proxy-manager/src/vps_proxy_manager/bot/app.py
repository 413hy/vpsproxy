from __future__ import annotations

import asyncio
import json
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import (
    Bot,
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from vps_proxy_manager.bot.keyboards import (
    BTN_NODES,
    BTN_SETTINGS,
    BTN_STATUS,
    BTN_SUBSCRIPTIONS,
    BTN_TASKS,
    BTN_VPS,
    assignment_hosts,
    candidate_detail,
    candidate_list,
    delete_host_confirm,
    home_inline,
    host_detail,
    host_list,
    main_reply_keyboard,
    node_detail,
    node_list,
    risk_confirm,
    source_delete_confirm,
    subscription_detail,
    subscription_entries,
    subscription_list,
    task_detail,
    vps_import_menu,
    vps_node_detail,
    vps_node_list,
    vps_pick_nodes,
    vps_pick_subscriptions,
    vps_subscription_detail,
    vps_subscription_entries,
    vps_subscription_entry_detail,
    vps_subscription_list,
)
from vps_proxy_manager.bot.security import is_authorized
from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import (
    AuthMethod,
    CodexTaskStatus,
    HostLifecycle,
    ProxyMode,
    Task,
    TaskKind,
    TaskStatus,
    VpsCandidate,
)
from vps_proxy_manager.proxy.parser import ParseError, parse_node_link, parse_subscription_text
from vps_proxy_manager.proxy.ssrf import SSRFError, fetch_subscription
from vps_proxy_manager.services.repository import Repository
from vps_proxy_manager.ssh.client import SSHClient, SSHCredentials, SSHError
from vps_proxy_manager.tasks.runner import NETWORK_MUTATING, TaskRunner
from vps_proxy_manager.utils.redact import mask, redact_text
from vps_proxy_manager.utils.validators import (
    validate_host,
    validate_https_url,
    validate_name,
    validate_port,
    validate_username,
)

log = structlog.get_logger()

# These conversations intentionally start from an inline button and continue in later text messages.
warnings.filterwarnings(
    "ignore",
    message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked.*",
    category=PTBUserWarning,
)

PAGE_SIZE = 8

(
    ADD_NAME,
    ADD_HOST,
    ADD_PORT,
    ADD_USER,
    ADD_AUTH,
    ADD_SECRET,
    ADD_CONFIRM,
    IMPORT_NODE,
    IMPORT_SUB_NAME,
    IMPORT_SUB_URL,
    RENAME_HOST,
    EDIT_HOST,
    EDIT_PORT,
    EDIT_USER,
    EDIT_AUTH,
    EDIT_SECRET,
    EDIT_CONFIRM,
    SEARCH_NODE,
    SEARCH_SUBSCRIPTION,
    SEARCH_SUBSCRIPTION_ENTRY,
) = range(20)


@dataclass
class BotDeps:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    secret_box: SecretBox
    runner: TaskRunner
    ssh: SSHClient


def build_application(dependencies: BotDeps) -> Application:
    app = (
        ApplicationBuilder()
        .token(dependencies.settings.telegram_bot_token)
        .concurrent_updates(False)
        .build()
    )
    app.bot_data["deps"] = dependencies

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_host_start, pattern=r"^h:add$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_name)],
            ADD_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_addr)],
            ADD_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_port)],
            ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_user)],
            ADD_AUTH: [
                CallbackQueryHandler(add_host_auth, pattern=r"^auth:(password|private_key)$")
            ],
            ADD_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_secret)],
            ADD_CONFIRM: [CallbackQueryHandler(add_host_confirm, pattern=r"^addsave:(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    import_node_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(import_node_start, pattern=r"^n:add$")],
        states={IMPORT_NODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_node_text)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    import_sub_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(import_sub_start, pattern=r"^s:add$")],
        states={
            IMPORT_SUB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_sub_name)],
            IMPORT_SUB_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_sub_url)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(rename_host_start, pattern=r"^h:rename:\d+$"),
            CallbackQueryHandler(edit_host_start, pattern=r"^h:reconn:\d+$"),
        ],
        states={
            RENAME_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_host_save)],
            EDIT_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_host_addr)],
            EDIT_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_host_port)],
            EDIT_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_host_user)],
            EDIT_AUTH: [
                CallbackQueryHandler(edit_host_auth, pattern=r"^eauth:(password|private_key)$")
            ],
            EDIT_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_host_secret)],
            EDIT_CONFIRM: [CallbackQueryHandler(edit_host_confirm, pattern=r"^editsave:(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    search_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(node_search_start, pattern=r"^n:search$"),
            CallbackQueryHandler(subscription_search_start, pattern=r"^s:search$"),
            CallbackQueryHandler(subscription_entry_search_start, pattern=r"^se:search:\d+$"),
        ],
        states={
            SEARCH_NODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, node_search_save)],
            SEARCH_SUBSCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, subscription_search_save)
            ],
            SEARCH_SUBSCRIPTION_ENTRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, subscription_entry_search_save)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(add_conv)
    app.add_handler(import_node_conv)
    app.add_handler(import_sub_conv)
    app.add_handler(edit_conv)
    app.add_handler(search_conv)
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_menu_router))
    app.add_error_handler(error_handler)
    return app


async def configure_bot(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "打开管理菜单"),
            BotCommand("menu", "重新显示主菜单"),
            BotCommand("help", "查看帮助和安全说明"),
            BotCommand("cancel", "取消当前输入向导"),
        ]
    )
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def deps(context: ContextTypes.DEFAULT_TYPE) -> BotDeps:
    return context.application.bot_data["deps"]


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_authorized(update, deps(context).settings):
        return True
    if update.callback_query:
        await update.callback_query.answer("未授权", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("未授权。")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    await show_home(update, context, new_message=True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    text = (
        "操作入口都在底部键盘。单节点库和订阅库属于控制端；进入某台 VPS 后看到的是该 VPS 自己的资源副本。\n\n"
        "新 VPS 只有在 Codex 初始化、安装远端 Agent、验证 sing-box 和本地出口后才会正式入库。切回本地出口不会删除节点或订阅。\n\n"
        "密码、私钥和订阅地址会加密保存，输入消息读取后会尽量删除；Telegram 不应被当作密码保险库。"
    )
    await update.effective_message.reply_text(text, reply_markup=main_reply_keyboard())


async def reply_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    text = update.effective_message.text
    if text == BTN_VPS:
        await show_hosts(update, context, new_message=True)
    elif text == BTN_NODES:
        await show_nodes(update, context, 0, new_message=True)
    elif text == BTN_SUBSCRIPTIONS:
        await show_subscriptions(update, context, 0, new_message=True)
    elif text == BTN_TASKS:
        await show_tasks(update, context, new_message=True)
    elif text == BTN_STATUS:
        await show_controller_status(update, context, new_message=True)
    elif text == BTN_SETTINGS:
        await show_settings(update, context, new_message=True)
    else:
        await update.effective_message.reply_text(
            "请使用底部菜单选择功能。", reply_markup=main_reply_keyboard()
        )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    try:
        if data == "home":
            await show_home(update, context)
        elif data in {"help", "settings"}:
            await show_settings(update, context)
        elif data == "h:list":
            await show_hosts(update, context)
        elif data.startswith("h:v:"):
            await show_host(update, context, _last_int(data))
        elif data == "c:list":
            await show_candidates(update, context)
        elif data.startswith("c:v:"):
            await show_candidate(update, context, _last_int(data))
        elif data.startswith("c:retry:"):
            await retry_candidate(update, context, _last_int(data))
        elif data.startswith("c:delete:"):
            await confirm_candidate_delete(update, context, _last_int(data))
        elif data.startswith("c:dodel:"):
            await delete_candidate(update, context, _last_int(data))
        elif data.startswith("h:edit:"):
            await show_host_edit_menu(update, context, _last_int(data))
        elif data.startswith("h:delete:"):
            await show_host_delete(update, context, _last_int(data))
        elif data == "n:searchclear":
            context.user_data.pop("node_search", None)
            await show_nodes(update, context, 0)
        elif data.startswith("n:filter:"):
            value = data.rsplit(":", 1)[1]
            context.user_data["node_filter"] = None if value == "all" else value
            await show_nodes(update, context, 0)
        elif data.startswith("n:sort:"):
            context.user_data["node_sort"] = data.rsplit(":", 1)[1]
            await show_nodes(update, context, 0)
        elif data.startswith("n:list:"):
            await show_nodes(update, context, _last_int(data))
        elif data.startswith("n:v:"):
            await show_node(update, context, _last_int(data))
        elif data.startswith("n:export:"):
            await export_node(update, context, _last_int(data))
        elif data.startswith("n:delete:"):
            await confirm_source_delete(update, context, "n", _last_int(data))
        elif data == "s:searchclear":
            context.user_data.pop("subscription_search", None)
            await show_subscriptions(update, context, 0)
        elif data.startswith("s:list:"):
            await show_subscriptions(update, context, _last_int(data))
        elif data.startswith("s:v:"):
            await show_subscription(update, context, _last_int(data))
        elif data.startswith("s:export:"):
            await export_subscription(update, context, _last_int(data))
        elif data.startswith("s:delete:"):
            await confirm_source_delete(update, context, "s", _last_int(data))
        elif data.startswith("se:searchclear:"):
            subscription_id = _last_int(data)
            view = _subscription_entry_view(context, subscription_id)
            view["search"] = None
            await show_subscription_entries(update, context, subscription_id, 0)
        elif data.startswith("se:filter:"):
            _, _, sub_id_text, value = data.split(":")
            subscription_id = int(sub_id_text)
            view = _subscription_entry_view(context, subscription_id)
            view["status"] = None if value == "all" else value
            await show_subscription_entries(update, context, subscription_id, 0)
        elif data.startswith("se:sort:"):
            _, _, sub_id_text, value = data.split(":")
            subscription_id = int(sub_id_text)
            view = _subscription_entry_view(context, subscription_id)
            view["sort"] = value
            await show_subscription_entries(update, context, subscription_id, 0)
        elif data.startswith("se:list:"):
            _, _, sub_id, page = data.split(":")
            await show_subscription_entries(update, context, int(sub_id), int(page))
        elif data.startswith("se:v:"):
            await show_subscription_entry(update, context, _last_int(data))
        elif data.startswith("as:start:"):
            _, _, kind, resource_id = data.split(":")
            await start_assignment(update, context, kind, int(resource_id))
        elif data.startswith("as:t:"):
            _, _, kind, resource_id, host_id = data.split(":")
            await toggle_assignment(update, context, kind, int(resource_id), int(host_id))
        elif data.startswith("as:go:"):
            _, _, kind, resource_id = data.split(":")
            await execute_assignment(update, context, kind, int(resource_id))
        elif data.startswith("vh:import:"):
            await show_vps_import(update, context, _last_int(data))
        elif data.startswith("vh:pickn:"):
            _, _, host_id, page = data.split(":")
            await show_vps_pick_nodes(update, context, int(host_id), int(page))
        elif data.startswith("vh:picks:"):
            _, _, host_id, page = data.split(":")
            await show_vps_pick_subscriptions(update, context, int(host_id), int(page))
        elif data.startswith("vh:syncn:"):
            _, _, host_id, node_id = data.split(":")
            await enqueue_task(
                update, context, TaskKind.sync_node, int(host_id), {"node_id": int(node_id)}
            )
        elif data.startswith("vh:syncs:"):
            _, _, host_id, sub_id = data.split(":")
            await enqueue_task(
                update,
                context,
                TaskKind.sync_subscription,
                int(host_id),
                {"subscription_id": int(sub_id)},
            )
        elif data.startswith("vh:n:"):
            _, _, host_id, page = data.split(":")
            await show_vps_nodes(update, context, int(host_id), int(page))
        elif data.startswith("vh:s:"):
            _, _, host_id, page = data.split(":")
            await show_vps_subscriptions(update, context, int(host_id), int(page))
        elif data.startswith("vn:v:"):
            await show_vps_node(update, context, _last_int(data))
        elif data.startswith("vs:v:"):
            await show_vps_subscription(update, context, _last_int(data))
        elif data.startswith("vse:list:"):
            _, _, sub_id, page = data.split(":")
            await show_vps_subscription_entries(update, context, int(sub_id), int(page))
        elif data.startswith("vse:v:"):
            await show_vps_subscription_entry(update, context, _last_int(data))
        elif data.startswith("run:"):
            await handle_run_callback(update, context, data)
        elif data.startswith("risk:"):
            await show_risk_confirmation(update, context, data)
        elif data.startswith("do:"):
            await handle_confirmed_action(update, context, data)
        elif data.startswith("srcdel:"):
            _, kind, resource_id = data.split(":")
            task_kind = (
                TaskKind.delete_source_node if kind == "n" else TaskKind.delete_source_subscription
            )
            payload_key = "node_id" if kind == "n" else "subscription_id"
            await enqueue_task(update, context, task_kind, None, {payload_key: int(resource_id)})
        elif data == "t:list":
            await show_tasks(update, context)
        elif data.startswith("t:v:"):
            await show_task(update, context, _last_int(data))
        elif data.startswith("t:cancel:"):
            await cancel_task(update, context, _last_int(data))
        elif data.startswith("ct:v:"):
            await show_codex_task(update, context, _last_int(data))
        else:
            await _edit(update, "此按钮已经失效，请重新打开对应菜单。", home_inline())
    except (KeyError, ValueError) as exc:
        await _edit(update, f"操作无法完成：{exc}", home_inline())


async def show_home(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool = False
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        hosts = await repo.list_hosts()
        candidates = await repo.list_candidates()
        node_count = await repo.count_nodes()
        sub_count = await repo.count_subscriptions()
    proxy_hosts = sum(h.last_status.get("singbox_active") == "active" for h in hosts)
    text = (
        "VPS Proxy Manager\n\n"
        f"VPS：{len(hosts)} 台（代理出口 {proxy_hosts} 台）\n"
        f"待 Codex 初始化：{len(candidates)} 台\n"
        f"控制端单节点：{node_count} 个\n"
        f"控制端订阅：{sub_count} 个\n\n"
        "使用底部键盘进入对应管理域。"
    )
    if new_message or not update.callback_query:
        await update.effective_message.reply_text(text, reply_markup=main_reply_keyboard())
        await update.effective_message.reply_text("控制面板", reply_markup=home_inline())
    else:
        await _edit(update, text, home_inline())


async def show_hosts(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool = False
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        hosts = await repo.list_hosts()
        candidates = await repo.list_candidates()
    text = "VPS 管理\n\n只有通过 Codex 初始化和准入验证的 VPS 才显示在正式列表中。"
    markup = host_list(hosts, candidates)
    await _send_or_edit(update, text, markup, new_message)


async def show_host(update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        host = await repo.get_host(host_id)
        state = await repo.get_proxy_state(host.id)
        nodes = await repo.list_vps_nodes(host.id)
        subscriptions = await repo.list_vps_subscriptions(host.id)
        pretty = host.system_info.get("os_release", {}).get("PRETTY_NAME", "未知系统")
        last_status = host.last_status or {}
        exit_ip, exit_region = _outbound_info(last_status)
        mode_label = {
            ProxyMode.proxy: "代理出口",
            ProxyMode.local: "本地出口",
            ProxyMode.uninstalled: "已卸载/原始出口",
            ProxyMode.unknown: "未知",
        }[state.mode]
    text = (
        f"{host.name}\n\n"
        f"地址：{host.host}:{host.port}\n"
        f"系统：{pretty}\n"
        f"远端 Agent：{host.remote_agent_version or '未知'}\n"
        f"SSH/公网：{'可用' if last_status.get('connectivity_ok') else '未验证或不可用'}\n"
        f"sing-box：{last_status.get('singbox_version') or '未知'}\n"
        f"出口模式：{mode_label}\n"
        f"当前节点：{state.current_display_name or '未选择'}\n"
        f"出口 IP：{exit_ip}\n"
        f"出口地区：{exit_region}\n"
        f"DNS 路由：{'TUN 劫持并经代理' if last_status.get('dns_mode') == 'tun_hijack_to_proxy' else '系统本地'}\n"
        f"已导入：{len(nodes)} 个单节点，{len(subscriptions)} 个订阅\n"
        f"最近切换：{_date(state.last_switch_at)}\n"
        f"配置版本：{host.config_version}\n"
        f"可恢复备份：{'有' if last_status.get('has_backup') else '未知或无'}"
    )
    await _edit(update, text, host_detail(host, state))


async def show_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with deps(context).session_factory() as session:
        items = await Repository(session, deps(context).secret_box).list_candidates()
    await _edit(update, "待初始化 VPS", candidate_list(items))


async def show_candidate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, candidate_id: int
) -> None:
    async with deps(context).session_factory() as session:
        item = await Repository(session, deps(context).secret_box).get_candidate(candidate_id)
    text = (
        f"{item.name}\n\n地址：{item.host}:{item.port}\n用户：{item.username}\n"
        f"状态：{item.lifecycle.value}\n结果：{item.message}"
    )
    await _edit(update, text, candidate_detail(item))


async def retry_candidate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, candidate_id: int
) -> None:
    if not deps(context).settings.codex_enabled:
        await _edit(
            update,
            "Codex Worker 当前已禁用。",
            candidate_detail(await _get_candidate(context, candidate_id)),
        )
        return
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        candidate = await repo.get_candidate(candidate_id)
        active = await repo.active_codex_task(candidate.id)
        if active:
            await _edit(
                update,
                f"该候选已有 Codex 初始化任务 #{active.id} 正在执行。",
                candidate_detail(candidate),
            )
            return
        candidate.lifecycle = HostLifecycle.pending
        candidate.error_code = None
        candidate.message = "等待 Codex Worker 重试"
        task = await repo.create_codex_task(candidate.id)
        await session.commit()
    await _edit(update, f"Codex 初始化任务 #{task.id} 已重新创建。", candidate_detail(candidate))
    _start_codex_monitor(update, context, task.id)


async def confirm_candidate_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, candidate_id: int
) -> None:
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("确认删除", callback_data=f"c:dodel:{candidate_id}")],
            [InlineKeyboardButton("取消", callback_data=f"c:v:{candidate_id}")],
        ]
    )
    await _edit(
        update, "删除待初始化记录不会清理目标 VPS 上可能残留的初始化文件。确认删除？", markup
    )


async def delete_candidate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, candidate_id: int
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        candidate = await repo.get_candidate(candidate_id)
        if candidate.lifecycle != HostLifecycle.failed or await repo.active_codex_task(
            candidate.id
        ):
            await _edit(update, "初始化活动期间不能删除候选记录。", candidate_detail(candidate))
            return
        await session.execute(delete(VpsCandidate).where(VpsCandidate.id == candidate_id))
        await session.commit()
    await show_candidates(update, context)


async def show_nodes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, *, new_message: bool = False
) -> None:
    page = max(page, 0)
    search = str(context.user_data.get("node_search") or "") or None
    status_filter = context.user_data.get("node_filter")
    sort = str(context.user_data.get("node_sort") or "latency")
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        items = list(
            await repo.list_nodes(
                search=search,
                status=status_filter,
                sort=sort,
                limit=PAGE_SIZE + 1,
                offset=page * PAGE_SIZE,
            )
        )
    markup = node_list(
        items[:PAGE_SIZE],
        page,
        len(items) > PAGE_SIZE,
        search_active=bool(search),
        status_filter=status_filter,
        sort=sort,
    )
    view = f"搜索：{search or '无'} · 筛选：{status_filter or '全部'} · 排序：{sort}"
    await _send_or_edit(
        update,
        f"控制端单节点库\n\n只保存手动导入的单节点。\n{view}",
        markup,
        new_message,
    )


async def show_node(update: Update, context: ContextTypes.DEFAULT_TYPE, node_id: int) -> None:
    async with deps(context).session_factory() as session:
        node = await Repository(session, deps(context).secret_box).get_node(node_id)
    result = node.last_test or {}
    text = (
        f"{node.name}\n\n协议：{node.protocol}\n服务器：{mask(node.server)}:{node.port}\n"
        f"状态：{node.status.value}\nTCP：{_ms(result.get('tcp_latency_ms'))}\n"
        f"代理握手：{_ms(result.get('proxy_handshake_ms'))}\n真实访问：{_ms(result.get('access_latency_ms'))}"
    )
    await _edit(update, text, node_detail(node))


async def export_node(update: Update, context: ContextTypes.DEFAULT_TYPE, node_id: int) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        node = await repo.get_node(node_id)
        content = repo.decrypt_node_link(node)
    await _send_document(update, content, f"node-{node.id}.txt", "完整节点已导出。")


async def show_subscriptions(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, *, new_message: bool = False
) -> None:
    page = max(page, 0)
    search = str(context.user_data.get("subscription_search") or "") or None
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        items = list(
            await repo.list_subscriptions(
                search=search, limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE
            )
        )
    markup = subscription_list(
        items[:PAGE_SIZE], page, len(items) > PAGE_SIZE, search_active=bool(search)
    )
    await _send_or_edit(
        update,
        f"控制端订阅库\n\n订阅独立保存，不会写入单节点库。\n搜索：{search or '无'}",
        markup,
        new_message,
    )


async def node_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "输入单节点名称关键词，或使用 /cancel 取消：",
    )
    return SEARCH_NODE


async def node_search_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.effective_message.text.strip()
    if not value or len(value) > 80:
        await update.effective_message.reply_text("关键词长度必须为 1-80 个字符。")
        return SEARCH_NODE
    context.user_data["node_search"] = value
    await show_nodes(update, context, 0)
    return ConversationHandler.END


async def subscription_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "输入订阅名称关键词，或使用 /cancel 取消：",
    )
    return SEARCH_SUBSCRIPTION


async def subscription_search_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.effective_message.text.strip()
    if not value or len(value) > 80:
        await update.effective_message.reply_text("关键词长度必须为 1-80 个字符。")
        return SEARCH_SUBSCRIPTION
    context.user_data["subscription_search"] = value
    await show_subscriptions(update, context, 0)
    return ConversationHandler.END


async def subscription_entry_search_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    sub_id = _last_int(query.data or "")
    _subscription_entry_view(context, sub_id)
    await query.edit_message_text(
        "输入此订阅内的节点名称关键词，或使用 /cancel 取消：",
    )
    return SEARCH_SUBSCRIPTION_ENTRY


async def subscription_entry_search_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.effective_message.text.strip()
    if not value or len(value) > 80:
        await update.effective_message.reply_text("关键词长度必须为 1-80 个字符。")
        return SEARCH_SUBSCRIPTION_ENTRY
    view = context.user_data.get("subscription_entry_view")
    if not isinstance(view, dict) or not isinstance(view.get("subscription_id"), int):
        await update.effective_message.reply_text("搜索上下文已失效，请重新打开订阅。")
        return ConversationHandler.END
    view["search"] = value
    await show_subscription_entries(update, context, view["subscription_id"], 0)
    return ConversationHandler.END


async def show_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int
) -> None:
    async with deps(context).session_factory() as session:
        item = await Repository(session, deps(context).secret_box).get_subscription(subscription_id)
    summary = item.last_test or {}
    text = (
        f"{item.name}\n\n订阅节点：{item.node_count}\n"
        f"最近更新：{_date(item.last_update_at)}\n"
        f"最近测速：{summary.get('online', 0)}/{summary.get('total', item.node_count)} 可用\n"
        "订阅节点仅保存在此订阅自己的解析缓存中。"
    )
    await _edit(update, text, subscription_detail(item))


async def show_subscription_entries(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, page: int
) -> None:
    page = max(page, 0)
    view = _subscription_entry_view(context, sub_id)
    search = str(view.get("search") or "") or None
    status_filter = str(view.get("status") or "") or None
    sort = str(view.get("sort") or "latency")
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        sub = await repo.get_subscription(sub_id)
        items = list(
            await repo.list_subscription_entries(
                sub.id,
                search=search,
                status=status_filter,
                sort=sort,
                limit=PAGE_SIZE + 1,
                offset=page * PAGE_SIZE,
            )
        )
    display_sort = "延迟" if sort == "latency" else "名称"
    display_status = {
        None: "全部",
        "online": "可用",
        "offline": "不可用",
        "unknown": "未测",
    }[status_filter]
    await _edit(
        update,
        f"订阅节点：{sub.name}\n\n"
        f"搜索：{search or '无'} · 筛选：{display_status} · 排序：{display_sort}",
        subscription_entries(
            items[:PAGE_SIZE],
            sub.id,
            page,
            len(items) > PAGE_SIZE,
            search_active=bool(search),
            status_filter=status_filter,
            sort=sort,
        ),
    )


async def show_subscription_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE, entry_id: int
) -> None:
    async with deps(context).session_factory() as session:
        item = await Repository(session, deps(context).secret_box).get_subscription_entry(entry_id)
    result = item.last_test or {}
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("返回订阅节点", callback_data=f"se:list:{item.subscription_id}:0")]]
    )
    text = (
        f"{item.name}\n\n协议：{item.protocol}\n服务器：{mask(item.server)}:{item.port}\n"
        f"TCP：{_ms(result.get('tcp_latency_ms'))}\n代理握手：{_ms(result.get('proxy_handshake_ms'))}\n"
        f"真实访问：{_ms(result.get('access_latency_ms'))}"
    )
    await _edit(update, text, markup)


async def export_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        sub = await repo.get_subscription(sub_id)
        url = repo.decrypt_subscription_url(sub)
        cached = repo.decrypt_subscription_content(sub) if sub.encrypted_content else ""
    content = f"# Subscription URL\n{url}\n\n# Cached content\n{cached}\n"
    await _send_document(
        update, content, f"subscription-{sub.id}.txt", "完整订阅地址和缓存内容已导出。"
    )


async def confirm_source_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, resource_id: int
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        if kind == "n":
            host_ids = [item.host_id for item in await repo.node_usage(resource_id)]
        else:
            host_ids = [item.host_id for item in await repo.subscription_usage(resource_id)]
        host_names = [(await repo.get_host(host_id)).name for host_id in host_ids]
    used = "、".join(host_names) if host_names else "无"
    text = (
        f"正在使用此资源的 VPS：{used}\n\n"
        "强制删除会从这些 VPS 同步删除对应副本；若它正作为当前出口，会先切回本地出口。"
    )
    await _edit(update, text, source_delete_confirm(kind, resource_id))


async def start_assignment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, resource_id: int
) -> None:
    context.user_data["assignment"] = {"kind": kind, "resource_id": resource_id, "selected": set()}
    await render_assignment(update, context, kind, resource_id)


async def toggle_assignment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, resource_id: int, host_id: int
) -> None:
    assignment = context.user_data.get("assignment")
    if (
        not assignment
        or assignment.get("kind") != kind
        or assignment.get("resource_id") != resource_id
    ):
        assignment = {"kind": kind, "resource_id": resource_id, "selected": set()}
        context.user_data["assignment"] = assignment
    selected: set[int] = assignment["selected"]
    if host_id in selected:
        selected.remove(host_id)
    else:
        selected.add(host_id)
    await render_assignment(update, context, kind, resource_id)


async def render_assignment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, resource_id: int
) -> None:
    async with deps(context).session_factory() as session:
        hosts = await Repository(session, deps(context).secret_box).list_hosts()
    selected = context.user_data.get("assignment", {}).get("selected", set())
    await _edit(
        update,
        "选择要导入的 VPS，可多选。",
        assignment_hosts(hosts, kind=kind, resource_id=resource_id, selected=selected),
    )


async def execute_assignment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, resource_id: int
) -> None:
    selected = set(context.user_data.get("assignment", {}).get("selected", set()))
    if not selected:
        await render_assignment(update, context, kind, resource_id)
        return
    created: list[int] = []
    skipped: list[int] = []
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        active_tasks = await repo.recent_tasks(500)
        for host_id in sorted(selected):
            if any(
                active.host_id == host_id
                and active.kind in NETWORK_MUTATING
                and active.status
                in {TaskStatus.queued, TaskStatus.running, TaskStatus.cancel_requested}
                for active in active_tasks
            ):
                skipped.append(host_id)
                continue
            task_kind = TaskKind.sync_node if kind == "n" else TaskKind.sync_subscription
            payload = {"node_id": resource_id} if kind == "n" else {"subscription_id": resource_id}
            task = await repo.create_task(
                kind=task_kind,
                actor_user_id=update.effective_user.id,
                host_id=host_id,
                payload=payload,
            )
            created.append(task.id)
        await session.commit()
    for task_id in created:
        await deps(context).runner.enqueue(task_id)
    context.user_data.pop("assignment", None)
    await _edit(
        update,
        f"已创建 {len(created)} 个导入任务：{', '.join(f'#{i}' for i in created) or '无'}"
        + (f"\n跳过忙碌 VPS：{', '.join(map(str, skipped))}" if skipped else ""),
        home_inline(),
    )


async def show_vps_import(update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int) -> None:
    await _edit(update, "选择控制端资源类型，再导入到这台 VPS。", vps_import_menu(host_id))


async def show_vps_pick_nodes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int, page: int
) -> None:
    async with deps(context).session_factory() as session:
        items = list(
            await Repository(session, deps(context).secret_box).list_nodes(
                limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE
            )
        )
    await _edit(
        update,
        "选择要导入的单节点",
        vps_pick_nodes(host_id, items[:PAGE_SIZE], page, len(items) > PAGE_SIZE),
    )


async def show_vps_pick_subscriptions(
    update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int, page: int
) -> None:
    async with deps(context).session_factory() as session:
        items = list(
            await Repository(session, deps(context).secret_box).list_subscriptions(
                limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE
            )
        )
    await _edit(
        update,
        "选择要完整导入的订阅",
        vps_pick_subscriptions(host_id, items[:PAGE_SIZE], page, len(items) > PAGE_SIZE),
    )


async def show_vps_nodes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int, page: int
) -> None:
    async with deps(context).session_factory() as session:
        items = list(await Repository(session, deps(context).secret_box).list_vps_nodes(host_id))
    start_index = page * PAGE_SIZE
    page_items = items[start_index : start_index + PAGE_SIZE]
    await _edit(
        update,
        "此 VPS 的单节点库",
        vps_node_list(host_id, page_items, page, len(items) > start_index + PAGE_SIZE),
    )


async def show_vps_node(update: Update, context: ContextTypes.DEFAULT_TYPE, item_id: int) -> None:
    async with deps(context).session_factory() as session:
        item = await Repository(session, deps(context).secret_box).get_vps_node(item_id)
    result = item.last_test or {}
    text = (
        f"{item.name}\n\n协议：{item.protocol}\n服务器：{mask(item.server)}:{item.port}\n"
        f"TCP：{_ms(result.get('tcp_latency_ms'))}\n代理握手：{_ms(result.get('proxy_handshake_ms'))}\n"
        f"真实访问：{_ms(result.get('access_latency_ms'))}"
    )
    await _edit(update, text, vps_node_detail(item))


async def show_vps_subscriptions(
    update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int, page: int
) -> None:
    async with deps(context).session_factory() as session:
        items = list(
            await Repository(session, deps(context).secret_box).list_vps_subscriptions(host_id)
        )
    start_index = page * PAGE_SIZE
    page_items = items[start_index : start_index + PAGE_SIZE]
    await _edit(
        update,
        "此 VPS 的订阅库",
        vps_subscription_list(host_id, page_items, page, len(items) > start_index + PAGE_SIZE),
    )


async def show_vps_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE, item_id: int
) -> None:
    async with deps(context).session_factory() as session:
        item = await Repository(session, deps(context).secret_box).get_vps_subscription(item_id)
    text = (
        f"{item.name}\n\n订阅节点：{item.node_count}\n最近更新：{_date(item.last_update_at)}\n"
        "测速时由该 VPS 自己拉取订阅并从该 VPS 发起所有连接。"
    )
    await _edit(update, text, vps_subscription_detail(item))


async def show_vps_subscription_entries(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, page: int
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        sub = await repo.get_vps_subscription(sub_id)
        items = list(
            await repo.list_vps_subscription_entries(
                sub.id, limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE
            )
        )
    await _edit(
        update,
        f"{sub.name} 的 VPS 测速结果",
        vps_subscription_entries(items[:PAGE_SIZE], sub, page, len(items) > PAGE_SIZE),
    )


async def show_vps_subscription_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE, entry_id: int
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        item = await repo.get_vps_subscription_entry(entry_id)
        sub = await repo.get_vps_subscription(item.vps_subscription_id)
    result = item.last_test or {}
    text = (
        f"{item.name}\n\n协议：{item.protocol}\n服务器：{mask(item.server)}:{item.port}\n"
        f"TCP：{_ms(result.get('tcp_latency_ms'))}\n代理握手：{_ms(result.get('proxy_handshake_ms'))}\n"
        f"真实访问：{_ms(result.get('access_latency_ms'))}"
    )
    await _edit(update, text, vps_subscription_entry_detail(item, sub.host_id))


async def handle_run_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    _, kind_text, scope, item = data.split(":")
    kind = TaskKind(kind_text)
    host_id: int | None = (
        int(scope) if int(scope) > 1 and kind not in {TaskKind.local_subscription_test} else None
    )
    payload: dict[str, Any] = {}
    if kind == TaskKind.local_node_test and int(item):
        payload = {"node_ids": [int(item)]}
    elif kind == TaskKind.local_subscription_test:
        payload = {"subscription_id": int(item), "refresh": bool(int(scope))}
    elif kind == TaskKind.vps_node_test:
        host_id = int(scope)
        if int(item):
            payload = {"vps_node_ids": [int(item)]}
    elif kind == TaskKind.vps_subscription_test:
        host_id = int(scope)
        payload = {"vps_subscription_id": int(item)}
    elif kind == TaskKind.status:
        host_id = int(scope)
    await enqueue_task(update, context, kind, host_id, payload)


async def show_risk_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    _, action, host_id_text, item_id_text = data.split(":")
    host_id = int(host_id_text)
    item_id = int(item_id_text)
    cancel = f"h:v:{host_id}"
    if action == "remove_vps_node":
        cancel = f"vn:v:{item_id}"
    elif action == "remove_vps_subscription":
        cancel = f"vs:v:{item_id}"
    elif action == "apply_node":
        cancel = f"vn:v:{item_id}"
    elif action == "apply_sub_entry":
        cancel = f"vse:v:{item_id}"
    descriptions = {
        "stop_proxy": "切回本地出口后，代理服务将停止并禁用开机启动；节点和订阅保留。",
        "restore_proxy": "将重新启用上一次已确认的代理配置。",
        "rollback": "将恢复上一次网络配置及其当时的出口模式。",
        "uninstall": "将移除本系统的代理配置和 VPS 资源库，并恢复初始化前的 sing-box 服务状态。",
        "remove_vps_node": "将从这台 VPS 删除节点；若正在使用，会先切回本地出口。",
        "remove_vps_subscription": "将从这台 VPS 删除订阅；若正在使用，会先切回本地出口。",
        "apply_node": "将把目标 VPS 的主要出站切换到此节点，并启用自动回滚保护。",
        "apply_sub_entry": "将把目标 VPS 的主要出站切换到此订阅节点，并启用自动回滚保护。",
    }
    await _edit(
        update,
        descriptions.get(action, "确认执行高风险操作？"),
        risk_confirm(action, host_id, item_id, cancel),
    )


async def handle_confirmed_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    _, action, host_id_text, item_id_text = data.split(":")
    host_id = int(host_id_text)
    item_id = int(item_id_text)
    if action == "delete_host":
        await enqueue_task(
            update, context, TaskKind.delete_host, host_id, {"uninstall": bool(item_id)}
        )
        return
    mapping: dict[str, tuple[TaskKind, dict[str, Any]]] = {
        "stop_proxy": (TaskKind.stop_proxy, {}),
        "restore_proxy": (TaskKind.restore_proxy, {}),
        "rollback": (TaskKind.rollback, {}),
        "uninstall": (TaskKind.uninstall, {}),
        "remove_vps_node": (TaskKind.remove_vps_node, {"vps_node_id": item_id}),
        "remove_vps_subscription": (
            TaskKind.remove_vps_subscription,
            {"vps_subscription_id": item_id},
        ),
        "apply_node": (TaskKind.apply_proxy, {"vps_node_id": item_id}),
        "apply_sub_entry": (
            TaskKind.apply_proxy,
            {"vps_subscription_entry_id": item_id},
        ),
    }
    kind, payload = mapping[action]
    await enqueue_task(update, context, kind, host_id, payload)


async def show_host_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int
) -> None:
    await _edit(
        update,
        "选择删除方式。仅移除记录不会修改目标 VPS；卸载后移除会先恢复本地出口并清理代理配置。",
        delete_host_confirm(host_id),
    )


async def enqueue_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: TaskKind,
    host_id: int | None,
    payload: dict[str, Any],
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        recent = await repo.recent_tasks(500)
        duplicate = next(
            (
                item
                for item in recent
                if item.kind == kind
                and item.host_id == host_id
                and item.payload == payload
                and item.status
                in {TaskStatus.queued, TaskStatus.running, TaskStatus.cancel_requested}
            ),
            None,
        )
        if duplicate:
            await _edit(
                update,
                f"相同任务 #{duplicate.id} 已在执行。",
                task_detail(duplicate.id, True),
            )
            return
        if host_id and kind in NETWORK_MUTATING:
            active = [
                item
                for item in recent
                if item.host_id == host_id
                and item.kind in NETWORK_MUTATING
                and item.status
                in {TaskStatus.queued, TaskStatus.running, TaskStatus.cancel_requested}
            ]
            if active:
                await _edit(
                    update,
                    f"该 VPS 已有网络任务 #{active[0].id} 正在执行。",
                    task_detail(active[0].id, True),
                )
                return
        task = await repo.create_task(
            kind=kind,
            actor_user_id=update.effective_user.id,
            host_id=host_id,
            payload=payload,
        )
        await session.commit()
    await deps(context).runner.enqueue(task.id)
    await _edit(update, _task_text(task), task_detail(task.id, True))
    _start_task_monitor(update, context, task.id)


async def show_tasks(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool = False
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        tasks = await repo.recent_tasks(8)
        codex_tasks = await repo.recent_codex_tasks(5)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"#{item.id} {item.kind.value} · {item.status.value} {item.progress}%",
                callback_data=f"t:v:{item.id}",
            )
        ]
        for item in tasks
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                f"Codex #{item.id} · {item.status.value} {item.progress}%",
                callback_data=f"ct:v:{item.id}",
            )
        ]
        for item in codex_tasks
    )
    rows.append([InlineKeyboardButton("刷新", callback_data="t:list")])
    await _send_or_edit(update, "任务中心", InlineKeyboardMarkup(rows), new_message)


async def show_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    async with deps(context).session_factory() as session:
        item = await session.get(Task, task_id)
        if item is None:
            raise KeyError("task not found")
    active = item.status in {TaskStatus.queued, TaskStatus.running, TaskStatus.cancel_requested}
    await _edit(update, _task_text(item), task_detail(item.id, active))


async def show_codex_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    async with deps(context).session_factory() as session:
        item = await Repository(session, deps(context).secret_box).get_codex_task(task_id)
    text = (
        f"Codex 任务 #{item.id}\n操作：{item.operation}\n状态：{item.status.value}\n"
        f"进度：{item.progress}%\n结果：{item.message}"
    )
    await _edit(
        update, text, InlineKeyboardMarkup([[InlineKeyboardButton("返回", callback_data="t:list")]])
    )


async def cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    canceled = await deps(context).runner.request_cancel(task_id)
    await _edit(
        update,
        "已请求取消，当前远端子操作结束后生效。" if canceled else "任务已经结束，无法取消。",
        task_detail(task_id, canceled),
    )


async def show_controller_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool = False
) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        nodes = await repo.count_nodes()
        subs = await repo.count_subscriptions()
        hosts = await repo.list_hosts()
    settings = deps(context).settings
    text = (
        "控制端状态\n\n"
        f"数据库：正常\n单节点：{nodes}\n订阅：{subs}\nVPS：{len(hosts)}\n"
        f"Codex Worker：{'启用' if settings.codex_enabled else '停用'}\n"
        f"本地测速并发：{settings.speedtest_concurrency}\n"
        f"远端自动回滚：{settings.remote_rollback_seconds} 秒"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("刷新", callback_data="home")]])
    await _send_or_edit(update, text, markup, new_message)


async def show_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool = False
) -> None:
    text = (
        "系统设置与安全\n\n"
        "仅管理员白名单可操作；默认仅允许私聊。SSH 首次添加时显示 SHA256 指纹，后续连接强制校验。\n\n"
        "所有远端动作来自固定白名单。Codex 只能接收两个数字 ID，并调用受控初始化入口。高风险任务不会在控制端重启后自动重放。\n\n"
        "完整导出会包含节点或订阅敏感内容，请自行删除 Telegram 中的导出文件。"
    )
    await _send_or_edit(update, text, home_inline(), new_message)


async def add_host_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    context.user_data["add_host"] = {}
    await update.callback_query.edit_message_text("输入 VPS 名称或备注：")
    return ADD_NAME


async def add_host_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["add_host"]["name"] = validate_name(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=ForceReply(selective=True))
        return ADD_NAME
    await update.message.reply_text("输入 IP 地址或域名：", reply_markup=ForceReply(selective=True))
    return ADD_HOST


async def add_host_addr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["add_host"]["host"] = validate_host(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=ForceReply(selective=True))
        return ADD_HOST
    await update.message.reply_text("输入 SSH 端口：", reply_markup=ForceReply(selective=True))
    return ADD_PORT


async def add_host_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["add_host"]["port"] = validate_port(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=ForceReply(selective=True))
        return ADD_PORT
    await update.message.reply_text("输入 SSH 用户名：", reply_markup=ForceReply(selective=True))
    return ADD_USER


async def add_host_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["add_host"]["username"] = validate_username(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=ForceReply(selective=True))
        return ADD_USER
    await update.message.reply_text(
        "选择认证方式：",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("密码", callback_data="auth:password"),
                    InlineKeyboardButton("SSH 私钥", callback_data="auth:private_key"),
                ]
            ]
        ),
    )
    return ADD_AUTH


async def add_host_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["add_host"]["auth_method"] = update.callback_query.data.split(":")[1]
    await update.callback_query.edit_message_text(
        "发送 SSH 密码或私钥内容。读取后会尽量删除此消息。"
    )
    return ADD_SECRET


async def add_host_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    secret = update.message.text
    data = context.user_data["add_host"]
    await _try_delete(update.message)
    auth = AuthMethod(data["auth_method"])
    loose_creds = SSHCredentials(data["host"], data["port"], data["username"], auth, secret, None)
    try:
        known_host = await deps(context).ssh.capture_host_key(loose_creds)
        fingerprint = await deps(context).ssh.host_key_fingerprint(known_host)
        pinned = SSHCredentials(
            data["host"], data["port"], data["username"], auth, secret, known_host
        )
        system = (await deps(context).ssh.run_payload(pinned, "detect", sudo=True, timeout=45)).get(
            "system", {}
        )
    except SSHError as exc:
        await update.effective_chat.send_message(
            f"SSH 预检失败：{exc}", reply_markup=main_reply_keyboard()
        )
        return ConversationHandler.END
    data.update(secret=secret, known_host=known_host, fingerprint=fingerprint, system=system)
    pretty = system.get("os_release", {}).get("PRETTY_NAME", "未知系统")
    await update.effective_chat.send_message(
        f"SSH 预检成功\n\n系统：{pretty}\n架构：{system.get('arch', '未知')}\n主机指纹：{fingerprint}\n\n"
        "请通过云厂商控制台或可信渠道核对指纹。确认后将创建 Codex 初始化任务，初始化成功前不会进入正式 VPS 列表。",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("指纹正确，开始初始化", callback_data="addsave:yes"),
                    InlineKeyboardButton("取消", callback_data="addsave:no"),
                ]
            ]
        ),
    )
    return ADD_CONFIRM


async def add_host_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    if update.callback_query.data == "addsave:no":
        context.user_data.pop("add_host", None)
        await update.callback_query.edit_message_text("已取消。")
        return ConversationHandler.END
    if not deps(context).settings.codex_enabled:
        await update.callback_query.edit_message_text("Codex Worker 未启用，不能创建初始化任务。")
        return ConversationHandler.END
    data = context.user_data["add_host"]
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        candidate = await repo.add_candidate(
            name=data["name"],
            host=data["host"],
            port=data["port"],
            username=data["username"],
            auth_method=AuthMethod(data["auth_method"]),
            secret=data["secret"],
            known_host=data["known_host"],
            system_info=data["system"],
        )
        codex_task = await repo.create_codex_task(candidate.id)
        await repo.audit(
            actor_user_id=update.effective_user.id,
            action="create_vps_candidate",
            result="ok",
            detail={"candidate_id": candidate.id},
        )
        await session.commit()
    context.user_data.pop("add_host", None)
    await update.callback_query.edit_message_text(
        f"Codex 初始化任务 #{codex_task.id} 已创建。\n目标通过全部验证后才会出现在 VPS 管理列表。"
    )
    _start_codex_monitor(update, context, codex_task.id)
    return ConversationHandler.END


async def import_node_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("粘贴单节点链接。保存后会尽量删除输入消息。")
    return IMPORT_NODE


async def import_node_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    try:
        spec = parse_node_link(link)
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"解析失败：{exc}")
        return IMPORT_NODE
    await _try_delete(update.message)
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        saved = await repo.save_nodes([spec])
        await repo.audit(
            actor_user_id=update.effective_user.id,
            action="import_node",
            result="ok",
            detail={"node": spec.name},
        )
        await session.commit()
    await update.effective_chat.send_message(
        f"已导入单节点：{spec.name}\n协议：{spec.protocol}\n服务器：{mask(spec.server)}:{spec.port}",
        reply_markup=node_detail(saved[0]),
    )
    return ConversationHandler.END


async def import_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    context.user_data["import_sub"] = {}
    await update.callback_query.edit_message_text("输入订阅名称：")
    return IMPORT_SUB_NAME


async def import_sub_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["import_sub"]["name"] = validate_name(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=ForceReply(selective=True))
        return IMPORT_SUB_NAME
    await update.message.reply_text("粘贴 HTTPS 订阅链接。读取后会尽量删除。")
    return IMPORT_SUB_URL


async def import_sub_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings = deps(context).settings
    try:
        url = validate_https_url(update.message.text)
        content = await fetch_subscription(
            url,
            timeout_seconds=settings.subscription_timeout_seconds,
            max_bytes=settings.subscription_max_bytes,
            max_redirects=settings.subscription_max_redirects,
            allow_private=settings.allow_private_subscription_urls,
        )
        specs = parse_subscription_text(content)
    except (ValueError, SSRFError, ParseError) as exc:
        await update.message.reply_text(f"订阅导入失败：{exc}")
        return IMPORT_SUB_URL
    await _try_delete(update.message)
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        sub = await repo.create_subscription(
            context.user_data["import_sub"]["name"], url, content, specs
        )
        await repo.audit(
            actor_user_id=update.effective_user.id,
            action="import_subscription",
            result="ok",
            detail={"count": len(specs)},
        )
        await session.commit()
    context.user_data.pop("import_sub", None)
    await update.effective_chat.send_message(
        f"订阅已独立保存：{sub.name}\n可解析节点：{len(specs)}\n没有写入单节点库。",
        reply_markup=subscription_detail(sub),
    )
    return ConversationHandler.END


async def show_host_edit_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int
) -> None:
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("修改名称", callback_data=f"h:rename:{host_id}")],
            [InlineKeyboardButton("重设 SSH 连接", callback_data=f"h:reconn:{host_id}")],
            [InlineKeyboardButton("返回", callback_data=f"h:v:{host_id}")],
        ]
    )
    await _edit(update, "编辑 VPS\n\n重设 SSH 连接会重新校验主机指纹和远端 Agent。", markup)


async def rename_host_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["rename_host_id"] = _last_int(update.callback_query.data)
    await update.callback_query.edit_message_text("输入新的 VPS 名称：")
    return RENAME_HOST


async def rename_host_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        name = validate_name(update.message.text)
        async with deps(context).session_factory() as session:
            repo = Repository(session, deps(context).secret_box)
            host = await repo.get_host(int(context.user_data["rename_host_id"]))
            host.name = name
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"名称修改失败：{exc}")
        return RENAME_HOST
    context.user_data.pop("rename_host_id", None)
    await update.message.reply_text("VPS 名称已更新。", reply_markup=main_reply_keyboard())
    return ConversationHandler.END


async def edit_host_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["edit_host"] = {"host_id": _last_int(update.callback_query.data)}
    await update.callback_query.edit_message_text("输入新的 IP 地址或域名：")
    return EDIT_HOST


async def edit_host_addr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["edit_host"]["host"] = validate_host(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return EDIT_HOST
    await update.message.reply_text("输入新的 SSH 端口：", reply_markup=ForceReply(selective=True))
    return EDIT_PORT


async def edit_host_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["edit_host"]["port"] = validate_port(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return EDIT_PORT
    await update.message.reply_text(
        "输入新的 SSH 用户名：", reply_markup=ForceReply(selective=True)
    )
    return EDIT_USER


async def edit_host_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["edit_host"]["username"] = validate_username(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return EDIT_USER
    await update.message.reply_text(
        "选择认证方式：",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("密码", callback_data="eauth:password"),
                    InlineKeyboardButton("SSH 私钥", callback_data="eauth:private_key"),
                ]
            ]
        ),
    )
    return EDIT_AUTH


async def edit_host_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["edit_host"]["auth_method"] = update.callback_query.data.split(":")[1]
    await update.callback_query.edit_message_text("发送新的密码或私钥。读取后会尽量删除。")
    return EDIT_SECRET


async def edit_host_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data["edit_host"]
    secret = update.message.text
    await _try_delete(update.message)
    auth = AuthMethod(data["auth_method"])
    loose = SSHCredentials(data["host"], data["port"], data["username"], auth, secret, None)
    try:
        known_host = await deps(context).ssh.capture_host_key(loose)
        fingerprint = await deps(context).ssh.host_key_fingerprint(known_host)
        pinned = SSHCredentials(
            data["host"], data["port"], data["username"], auth, secret, known_host
        )
        status = await deps(context).ssh.run_agent(pinned, "status", {}, timeout=45)
    except SSHError as exc:
        await update.effective_chat.send_message(f"新连接验证失败：{exc}")
        return ConversationHandler.END
    data.update(secret=secret, known_host=known_host, fingerprint=fingerprint, status=status)
    await update.effective_chat.send_message(
        f"新连接已验证，主机指纹：{fingerprint}\n远端 Agent：{status.get('status', {}).get('agent_version', '未知')}\n确认替换连接信息？",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("确认替换", callback_data="editsave:yes"),
                    InlineKeyboardButton("取消", callback_data="editsave:no"),
                ]
            ]
        ),
    )
    return EDIT_CONFIRM


async def edit_host_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    data = context.user_data.pop("edit_host", None)
    if update.callback_query.data == "editsave:no" or not data:
        await update.callback_query.edit_message_text("已取消修改。")
        return ConversationHandler.END
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        host = await repo.get_host(int(data["host_id"]))
        host.host = data["host"]
        host.port = data["port"]
        host.username = data["username"]
        host.auth_method = AuthMethod(data["auth_method"])
        host.encrypted_secret = deps(context).secret_box.encrypt(data["secret"])
        host.known_host = data["known_host"]
        host.remote_agent_version = data["status"].get("status", {}).get("agent_version")
        await repo.audit(
            actor_user_id=update.effective_user.id,
            action="edit_host_connection",
            result="ok",
            host_id=host.id,
        )
        await session.commit()
    await update.callback_query.edit_message_text("SSH 连接信息已更新。")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ["add_host", "import_sub", "rename_host_id", "edit_host", "assignment"]:
        context.user_data.pop(key, None)
    if update.effective_message:
        await update.effective_message.reply_text(
            "已取消当前输入。", reply_markup=main_reply_keyboard()
        )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    log.exception(
        "telegram_update_failed",
        error_type=type(error).__name__,
        error=redact_text(str(error)),
        update_id=update.update_id if isinstance(update, Update) else None,
    )
    if isinstance(error, TelegramError):
        return
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "操作失败，请在任务中心查看状态或检查脱敏日志。"
            )
        except TelegramError:
            pass


def _start_task_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    context.application.create_task(
        _monitor_task(
            deps(context),
            context.bot,
            update.effective_chat.id,
            update.effective_message.id,
            task_id,
        ),
        update=update,
    )


async def _monitor_task(
    dependencies: BotDeps, bot: Bot, chat_id: int, message_id: int, task_id: int
) -> None:
    last_text = ""
    while True:
        async with dependencies.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            text = _task_text(task)
            active = task.status in {
                TaskStatus.queued,
                TaskStatus.running,
                TaskStatus.cancel_requested,
            }
        if text != last_text:
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=task_detail(task_id, active),
                )
            except BadRequest as exc:
                if "Message is not modified" not in str(exc):
                    return
            except TelegramError:
                return
            last_text = text
        if not active:
            return
        await asyncio.sleep(2)


def _start_codex_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    context.application.create_task(
        _monitor_codex_task(
            deps(context),
            context.bot,
            update.effective_chat.id,
            update.effective_message.id,
            task_id,
        ),
        update=update,
    )


async def _monitor_codex_task(
    dependencies: BotDeps, bot: Bot, chat_id: int, message_id: int, task_id: int
) -> None:
    last_text = ""
    while True:
        async with dependencies.session_factory() as session:
            item = await Repository(session, dependencies.secret_box).get_codex_task(task_id)
            text = f"Codex 初始化任务 #{item.id}\n状态：{item.status.value}\n进度：{item.progress}%\n{item.message}"
            active = item.status in {CodexTaskStatus.queued, CodexTaskStatus.running}
        if text != last_text:
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("查看任务中心", callback_data="t:list")]]
                    ),
                )
            except BadRequest as exc:
                if "Message is not modified" not in str(exc):
                    return
            except TelegramError:
                return
            last_text = text
        if not active:
            return
        await asyncio.sleep(3)


async def _get_candidate(context: ContextTypes.DEFAULT_TYPE, candidate_id: int) -> VpsCandidate:
    async with deps(context).session_factory() as session:
        return await Repository(session, deps(context).secret_box).get_candidate(candidate_id)


async def _send_document(update: Update, content: str, filename: str, caption: str) -> None:
    stream = BytesIO(content.encode("utf-8"))
    stream.name = filename
    await update.effective_chat.send_document(document=stream, filename=filename, caption=caption)


async def _try_delete(message: Any) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def _send_or_edit(
    update: Update,
    text: str,
    markup: InlineKeyboardMarkup,
    new_message: bool,
) -> None:
    if new_message or not update.callback_query:
        await update.effective_message.reply_text(text, reply_markup=markup)
    else:
        await _edit(update, text, markup)


async def _edit(update: Update, text: str, markup: InlineKeyboardMarkup) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup)


def _subscription_entry_view(
    context: ContextTypes.DEFAULT_TYPE, subscription_id: int
) -> dict[str, Any]:
    current = context.user_data.get("subscription_entry_view")
    if not isinstance(current, dict) or current.get("subscription_id") != subscription_id:
        current = {
            "subscription_id": subscription_id,
            "search": None,
            "status": None,
            "sort": "latency",
        }
        context.user_data["subscription_entry_view"] = current
    return current


def _last_int(data: str) -> int:
    return int(data.rsplit(":", 1)[1])


def _outbound_info(status: dict[str, Any]) -> tuple[str, str]:
    raw = status.get("outbound_probe")
    if not isinstance(raw, str) or not raw:
        return "未检测", "未检测"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return "未检测", "未检测"
    ip = str(data.get("ip") or "未检测")
    region = str(data.get("country") or data.get("country_iso") or "未检测")
    return ip, region


def _task_text(task: Task) -> str:
    return (
        f"任务 #{task.id}\n类型：{task.kind.value}\n状态：{task.status.value}\n"
        f"进度：{task.progress}%\n结果：{task.message}"
        + (f"\n错误代码：{task.error_code}" if task.error_code else "")
    )


def _ms(value: Any) -> str:
    return f"{int(value)}ms" if value is not None else "未测"


def _date(value: Any) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "未更新"
