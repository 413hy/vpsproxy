from __future__ import annotations

from vps_proxy_manager.bot.keyboards import main_menu


def test_main_menu_callbacks_have_router_entries() -> None:
    markup = main_menu()
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "status_menu" in callbacks
    assert "speedtest_menu" in callbacks
    assert "hosts" in callbacks
    assert "nodes:0" in callbacks
