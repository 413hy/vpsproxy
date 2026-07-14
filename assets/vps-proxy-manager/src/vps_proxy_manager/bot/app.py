from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from vps_proxy_manager.bot.keyboards import (
    choose_host,
    confirm,
    host_detail,
    host_list,
    main_menu,
    node_detail,
    node_list,
)
from vps_proxy_manager.bot.security import is_authorized
from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import AuthMethod, TaskKind
from vps_proxy_manager.proxy.parser import ParseError, parse_node_link, parse_subscription_text
from vps_proxy_manager.proxy.ssrf import SSRFError, fetch_subscription
from vps_proxy_manager.services.repository import Repository
from vps_proxy_manager.ssh.client import SSHClient, SSHCredentials, SSHError
from vps_proxy_manager.tasks.runner import TaskRunner
from vps_proxy_manager.utils.redact import mask
from vps_proxy_manager.utils.validators import (
    validate_host,
    validate_https_url,
    validate_port,
    validate_username,
)

ADD_NAME, ADD_HOST, ADD_PORT, ADD_USER, ADD_AUTH, ADD_SECRET, IMPORT_NODE, IMPORT_SUB_NAME, IMPORT_SUB_URL = range(9)


@dataclass
class BotDeps:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    secret_box: SecretBox
    runner: TaskRunner
    ssh: SSHClient


def build_application(deps: BotDeps) -> Application:
    app = ApplicationBuilder().token(deps.settings.telegram_bot_token).concurrent_updates(False).build()
    app.bot_data["deps"] = deps

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_host_start, pattern="^add_host$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_name)],
            ADD_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_addr)],
            ADD_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_port)],
            ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_user)],
            ADD_AUTH: [CallbackQueryHandler(add_host_auth, pattern="^auth:(password|private_key)$")],
            ADD_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host_secret)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    import_node_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(import_node_start, pattern="^import_node$")],
        states={IMPORT_NODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_node_text)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    import_sub_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(import_sub_start, pattern="^import_sub$")],
        states={
            IMPORT_SUB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_sub_name)],
            IMPORT_SUB_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_sub_url)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(add_conv)
    app.add_handler(import_node_conv)
    app.add_handler(import_sub_conv)
    app.add_handler(CallbackQueryHandler(callback_router))
    return app


def deps(context: ContextTypes.DEFAULT_TYPE) -> BotDeps:
    return context.application.bot_data["deps"]


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_authorized(update, deps(context).settings):
        if update.effective_message:
            await update.effective_message.reply_text("未授权。")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    await update.effective_message.reply_text("VPS 代理管理主菜单", reply_markup=main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    await update.effective_message.reply_text(
        "通过按钮添加 VPS、导入节点/订阅、测速并应用全局代理。敏感消息读取后会尽量删除；Telegram 本身不应视为高安全密码库。",
        reply_markup=main_menu(),
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    if data == "main":
        await query.edit_message_text("VPS 代理管理主菜单", reply_markup=main_menu())
    elif data == "hosts":
        await show_hosts(update, context)
    elif data.startswith("host:"):
        await show_host(update, context, int(data.split(":")[1]))
    elif data.startswith("nodes:"):
        await show_nodes(update, context, int(data.split(":")[1]))
    elif data.startswith("node:"):
        await show_node(update, context, int(data.split(":")[1]))
    elif data.startswith("task:"):
        _, kind, host_id = data.split(":")
        await enqueue_task(update, context, TaskKind(kind), int(host_id), {})
    elif data.startswith("confirm:"):
        _, action, host_id, node_id = data.split(":")
        await query.edit_message_text(f"高风险操作：{action}\n请确认。", reply_markup=confirm(action, int(host_id), int(node_id)))
    elif data.startswith("do:"):
        _, action, host_id, node_id = data.split(":")
        payload = {"node_id": int(node_id)} if int(node_id) else {}
        await enqueue_task(update, context, TaskKind(action), int(host_id), payload)
    elif data.startswith("choose_host_for_node:"):
        await choose_host_for_node(update, context, int(data.split(":")[1]))
    elif data.startswith("apply_node:"):
        _, host_id, node_id = data.split(":")
        await query.edit_message_text("切换系统级全局代理属于高风险操作，请确认。", reply_markup=confirm("apply_proxy", int(host_id), int(node_id)))
    elif data.startswith("choose_host_speed:"):
        await choose_host_for_speed(update, context, int(data.split(":")[1]))
    elif data.startswith("speed_node:"):
        _, host_id, node_id = data.split(":")
        await enqueue_task(update, context, TaskKind.speedtest, int(host_id), {"node_ids": [int(node_id)]})
    elif data.startswith("speedtest:"):
        _, host_id, _scope = data.split(":")
        await enqueue_task(update, context, TaskKind.speedtest, int(host_id), {})
    elif data == "tasks":
        await show_tasks(update, context)
    elif data in {"help", "security"}:
        await query.edit_message_text(
            "安全模型：管理员 ID 白名单、私聊限制、敏感操作二次确认、SSH 指纹校验、凭据本地加密、订阅 SSRF 防护、脱敏审计日志。",
            reply_markup=main_menu(),
        )
    else:
        await query.edit_message_text("暂不支持的操作。", reply_markup=main_menu())


async def show_hosts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        hosts = await repo.list_hosts()
    await update.callback_query.edit_message_text("VPS 列表", reply_markup=host_list(hosts))


async def show_host(update: Update, context: ContextTypes.DEFAULT_TYPE, host_id: int) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        host = await repo.get_host(host_id)
        sysinfo = host.system_info.get("os_release", {})
        node = host.current_node.name if host.current_node else "未选择"
        text = f"{host.name}\n地址：{host.host}:{host.port}\n系统：{sysinfo.get('PRETTY_NAME', '未知')}\n当前节点：{node}"
    await update.callback_query.edit_message_text(text, reply_markup=host_detail(host))


async def show_nodes(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        nodes = await repo.list_nodes(limit=8, offset=page * 8)
    await update.callback_query.edit_message_text("节点列表", reply_markup=node_list(nodes, page))


async def show_node(update: Update, context: ContextTypes.DEFAULT_TYPE, node_id: int) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        node = await repo.get_node(node_id)
        text = (
            f"{node.name}\n协议：{node.protocol}\n服务器：{mask(node.server)}:{node.port}\n"
            f"状态：{node.status.value}\n延迟：{node.last_latency_ms or '未测'}"
        )
    await update.callback_query.edit_message_text(text, reply_markup=node_detail(node))


async def choose_host_for_node(update: Update, context: ContextTypes.DEFAULT_TYPE, node_id: int) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        hosts = await repo.list_hosts()
    await update.callback_query.edit_message_text("选择要应用节点的 VPS", reply_markup=choose_host(hosts, "apply_node", node_id))


async def choose_host_for_speed(update: Update, context: ContextTypes.DEFAULT_TYPE, node_id: int) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        hosts = await repo.list_hosts()
    await update.callback_query.edit_message_text("选择从哪台 VPS 发起测速", reply_markup=choose_host(hosts, "speed_node", node_id))


async def enqueue_task(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: TaskKind, host_id: int | None, payload: dict[str, Any]) -> None:
    actor = update.effective_user.id
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        task = await repo.create_task(kind=kind, actor_user_id=actor, host_id=host_id, payload=payload)
        await session.commit()
    await deps(context).runner.enqueue(task.id)
    await update.callback_query.edit_message_text(f"任务已创建：#{task.id} {kind.value}", reply_markup=main_menu())


async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        tasks = await repo.recent_tasks(10)
    lines = [f"#{t.id} {t.kind.value} {t.status.value} {t.message}" for t in tasks]
    await update.callback_query.edit_message_text("\n".join(lines) or "暂无任务", reply_markup=main_menu())


async def add_host_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    context.user_data["add_host"] = {}
    await update.callback_query.edit_message_text("输入 VPS 名称或备注：", reply_markup=None)
    return ADD_NAME


async def add_host_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["add_host"]["name"] = update.message.text.strip()
    await update.message.reply_text("输入 IP 地址或域名：", reply_markup=ForceReply(selective=True))
    return ADD_HOST


async def add_host_addr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["add_host"]["host"] = validate_host(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ADD_HOST
    await update.message.reply_text("输入 SSH 端口：", reply_markup=ForceReply(selective=True))
    return ADD_PORT


async def add_host_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["add_host"]["port"] = validate_port(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ADD_PORT
    await update.message.reply_text("输入 SSH 用户名：", reply_markup=ForceReply(selective=True))
    return ADD_USER


async def add_host_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["add_host"]["username"] = validate_username(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ADD_USER
    await update.message.reply_text(
        "选择认证方式：",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("密码", callback_data="auth:password"),
                InlineKeyboardButton("SSH 私钥", callback_data="auth:private_key"),
            ]]
        ),
    )
    return ADD_AUTH


async def add_host_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["add_host"]["auth_method"] = update.callback_query.data.split(":")[1]
    await update.callback_query.edit_message_text("发送密码或私钥内容。读取后 Bot 会尽量删除该消息。")
    return ADD_SECRET


async def add_host_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    secret = update.message.text
    data = context.user_data["add_host"]
    try:
        await update.message.delete()
    except Exception:
        pass
    auth = AuthMethod(data["auth_method"])
    creds = SSHCredentials(data["host"], data["port"], data["username"], auth, secret, None)
    ssh = deps(context).ssh
    try:
        known_host = await ssh.capture_host_key(creds)
        creds = SSHCredentials(data["host"], data["port"], data["username"], auth, secret, known_host)
        system = (await ssh.run_payload(creds, "detect", sudo=False, timeout=45)).get("system", {})
    except SSHError as exc:
        await update.effective_chat.send_message(f"SSH 测试失败：{exc}")
        return ConversationHandler.END
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        host = await repo.add_host(
            name=data["name"],
            host=data["host"],
            port=data["port"],
            username=data["username"],
            auth_method=auth,
            secret=secret,
            known_host=known_host,
            system_info=system,
        )
        await repo.audit(actor_user_id=update.effective_user.id, action="add_host", result="ok", host_id=host.id)
        await session.commit()
    pretty = system.get("os_release", {}).get("PRETTY_NAME", "未知系统")
    await update.effective_chat.send_message(f"已保存 VPS：{data['name']}\n检测到：{pretty}", reply_markup=main_menu())
    return ConversationHandler.END


async def import_node_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("粘贴单节点链接。保存后消息会尽量删除。")
    return IMPORT_NODE


async def import_node_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    try:
        spec = parse_node_link(link)
        await update.message.delete()
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"解析失败：{exc}")
        return ConversationHandler.END
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        saved = await repo.save_nodes([spec])
        await repo.audit(actor_user_id=update.effective_user.id, action="import_node", result="ok", detail={"node": spec.name})
        await session.commit()
    await update.effective_chat.send_message(
        f"已导入节点：{spec.name}\n协议：{spec.protocol}\n服务器：{mask(spec.server)}:{spec.port}",
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
    context.user_data["import_sub"]["name"] = update.message.text.strip()
    await update.message.reply_text("粘贴 https 订阅链接。读取后 Bot 会尽量删除。")
    return IMPORT_SUB_URL


async def import_sub_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings = deps(context).settings
    try:
        url = validate_https_url(update.message.text)
        text = await fetch_subscription(
            url,
            timeout_seconds=settings.subscription_timeout_seconds,
            max_bytes=settings.subscription_max_bytes,
            max_redirects=settings.subscription_max_redirects,
            allow_private=settings.allow_private_subscription_urls,
        )
        specs = parse_subscription_text(text)
        await update.message.delete()
    except (ValueError, SSRFError, ParseError) as exc:
        await update.message.reply_text(f"订阅导入失败：{exc}")
        return ConversationHandler.END
    async with deps(context).session_factory() as session:
        repo = Repository(session, deps(context).secret_box)
        sub = await repo.create_subscription(context.user_data["import_sub"]["name"], url)
        await repo.save_nodes(specs, subscription_id=sub.id)
        await repo.audit(actor_user_id=update.effective_user.id, action="import_subscription", result="ok", detail={"count": len(specs)})
        await session.commit()
    await update.effective_chat.send_message(f"订阅已导入，节点数：{len(specs)}。不会自动切换当前节点。", reply_markup=main_menu())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("已取消。", reply_markup=main_menu())
    return ConversationHandler.END
