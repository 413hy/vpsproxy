from __future__ import annotations

from telegram import Update

from vps_proxy_manager.config import Settings


def is_authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or user.id not in settings.admin_user_ids:
        return False
    if chat is None:
        return False
    if settings.require_private_chat and chat.type != "private":
        return False
    if settings.allowed_chat_ids and chat.id not in settings.allowed_chat_ids:
        return False
    return True
