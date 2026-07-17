from __future__ import annotations

from vps_proxy_manager.bot.keyboards import (
    BTN_NODES,
    BTN_SUBSCRIPTIONS,
    BTN_TASKS,
    BTN_VPS,
    main_reply_keyboard,
    risk_confirm,
    subscription_entries,
    task_detail,
)


def test_main_reply_keyboard_separates_management_domains() -> None:
    markup = main_reply_keyboard()
    labels = {button.text for row in markup.keyboard for button in row}
    assert {BTN_VPS, BTN_NODES, BTN_SUBSCRIPTIONS, BTN_TASKS} <= labels
    assert markup.is_persistent is True
    assert markup.resize_keyboard is True


def test_callback_data_stays_within_telegram_limit() -> None:
    markup = risk_confirm("remove_vps_subscription", 2147483647, 2147483647, "h:v:1")
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_subscription_entry_controls_stay_within_callback_limit() -> None:
    markup = subscription_entries(
        [],
        2147483647,
        2147483647,
        True,
        search_active=True,
        status_filter="offline",
        sort="name",
    )
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_finished_task_links_results_and_codex_diagnosis() -> None:
    markup = task_detail(
        9,
        False,
        result_callback="vse:list:7:0",
        codex_task_id=11,
        resolved_task_id=12,
    )
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert {"vse:list:7:0", "ct:v:11", "t:v:9", "t:v:12"} <= callbacks
