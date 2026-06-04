"""Discord diagnostics and history message builders."""

from __future__ import annotations


def format_discord_id_list(values: set[int], *, limit: int = 8) -> str:
    if not values:
        return "ALL"
    sorted_values = sorted(values)
    rendered = ",".join(str(value) for value in sorted_values[:limit])
    if len(sorted_values) > limit:
        rendered += f",+{len(sorted_values) - limit} more"
    return rendered


def build_discord_doctor_message(
    bot: object,
    channel_id: int | None,
    *,
    empty_content_notice_count: int,
    get_mirrored_codex_thread_id_func,
    get_mirror_project_for_channel_func,
    get_busy_choice_counts_func,
    get_persistent_component_claim_counts_func,
    build_mirror_check_func,
    get_discord_log_markers_func,
    get_recent_discord_hook_events_func,
    discord_qa_commands_enabled_func,
) -> str:
    target_thread_id = get_mirrored_codex_thread_id_func(channel_id)
    project = get_mirror_project_for_channel_func(channel_id)
    active_busy_choices, stale_busy_choices = get_busy_choice_counts_func()
    active_component_claims, stale_component_claims = get_persistent_component_claim_counts_func()
    mirror_lines = build_mirror_check_func().splitlines()
    log_markers = get_discord_log_markers_func()
    history_poll_task = getattr(bot, "_history_poll_task", None)
    history_poll_alive = bool(history_poll_task and not history_poll_task.done())
    recent_events = get_recent_discord_hook_events_func()
    recent_user_events = get_recent_discord_hook_events_func(user_or_control_only=True)
    lines = [
        "Discord adapter diagnostics",
        f"channel_id: {channel_id or '-'}",
        f"mapped_thread_id: {target_thread_id or '-'}",
        f"project_channel: {project[1] if project else '-'}",
        f"message_content_enabled: {bool(getattr(bot, 'enable_prefix_commands', False))}",
        f"intent_message_content: {bool(getattr(getattr(bot, 'intents', None), 'message_content', False))}",
        f"raw_debug_events: {bool(getattr(bot, '_enable_debug_events', False))}",
        f"qa_commands_enabled: {discord_qa_commands_enabled_func()}",
        f"history_poll_seconds: {getattr(bot, 'history_poll_seconds', '-')}",
        f"history_poll_bootstrap_lookback_seconds: {getattr(bot, 'history_poll_bootstrap_lookback_seconds', '-')}",
        f"history_poll_bootstrap_after: {getattr(bot, '_history_poll_bootstrap_after', '-')}",
        f"history_poll_alive: {history_poll_alive}",
        f"history_poll_last_at: {getattr(bot, '_history_poll_last_at', '-')}",
        f"history_poll_primed_channels: {len(getattr(bot, '_history_poll_primed_channels', set()))}",
        f"slash_sync_status: {getattr(bot, '_slash_sync_status', '-')}",
        f"slash_sync_last_at: {getattr(bot, '_slash_sync_last_at', '-')}",
        f"slash_sync_commands: {getattr(bot, '_slash_sync_commands', '-')}",
        f"allowed_channels: {format_discord_id_list(getattr(bot, 'allowed_channel_ids', set()))}",
        f"allowed_users: {format_discord_id_list(getattr(bot, 'allowed_user_ids', set()))}",
        f"startup_channel_id: {getattr(bot, 'startup_channel_id', None) or '-'}",
        f"empty_content_notice_channels: {empty_content_notice_count}",
        f"busy_choices_active: {active_busy_choices}",
        f"busy_choices_stale: {stale_busy_choices}",
        f"persistent_component_claims_active: {active_component_claims}",
        f"persistent_component_claims_stale: {stale_component_claims}",
        f"last_ready_at: {log_markers['last_ready_at']}",
        f"last_gateway_event_at: {log_markers['last_gateway_event_at']}",
        f"last_raw_interaction_at: {log_markers['last_raw_interaction_at']}",
        f"last_interaction_at: {log_markers['last_interaction_at']}",
        f"last_component_at: {log_markers['last_component_at']}",
        f"last_user_or_control_hook_at: {log_markers['last_user_or_control_hook_at']}",
        f"last_button_qa_at: {log_markers['last_button_qa_at']}",
        f"last_button_qa_result: {log_markers['last_button_qa_result']}",
        f"last_steering_button_at: {log_markers['last_steering_button_at']}",
        f"last_steering_button_exit: {log_markers['last_steering_button_exit']}",
        f"last_steering_button_elapsed_sec: {log_markers['last_steering_button_elapsed_sec']}",
        "",
        *mirror_lines,
        "",
        "Expected live log sequence:",
        "message: socket_message_create -> message_received",
        "slash/button: socket_interaction_create -> interaction_received",
        "",
        "Recent user/control hook events:",
        *(recent_user_events or ["-"]),
        "",
        "Recent hook events:",
        *(recent_events or ["-"]),
    ]
    return "\n".join(lines)


def format_discord_message_type(message: object) -> str:
    message_type = getattr(message, "type", "-")
    return str(getattr(message_type, "name", message_type) or "-")


def format_discord_message_created_at(message: object) -> str:
    created_at = getattr(message, "created_at", None)
    if hasattr(created_at, "isoformat"):
        return str(created_at.isoformat())
    return "-"


async def build_discord_channel_history_lines(
    channel: object | None,
    *,
    limit: int = 5,
    format_log_text_len_func,
) -> list[str]:
    lines = ["Recent channel history:"]
    if channel is None or not hasattr(channel, "history"):
        return [*lines, "history_unavailable: no_channel"]
    try:
        messages = []
        async for message in channel.history(limit=limit):  # type: ignore[attr-defined]
            author = getattr(message, "author", None)
            messages.append(
                f"{format_discord_message_created_at(message)} "
                f"bot={bool(getattr(author, 'bot', False))} "
                f"content_len={format_log_text_len_func(getattr(message, 'content', '') or '')} "
                f"type={format_discord_message_type(message)}"
            )
    except Exception as exc:
        return [*lines, f"history_error: {type(exc).__name__}"]
    return [*lines, *(messages or ["-"])]


async def resolve_discord_history_channel(bot: object, channel_id: int) -> tuple[object | None, str]:
    getter = getattr(bot, "get_cached_channel_or_thread", None)
    if callable(getter):
        channel, source = getter(channel_id)
    else:
        channel = None
        source = "-"
        get_channel = getattr(bot, "get_channel", None)
        if callable(get_channel):
            channel = get_channel(channel_id)
            source = "client_channel_cache" if channel is not None else "-"
    if channel is None:
        fetch_channel = getattr(bot, "fetch_channel", None)
        if callable(fetch_channel):
            try:
                channel = await fetch_channel(channel_id)
                source = "fetch"
            except Exception as exc:
                return None, f"fetch_error:{type(exc).__name__}"
    return channel, source


async def build_discord_tracked_target_user_history_lines(
    bot: object,
    *,
    get_startup_probe_targets_func,
    format_log_text_len_func,
    per_target_limit: int = 5,
    target_limit: int = 50,
) -> list[str]:
    lines = ["Recent tracked target user history:"]
    targets = get_startup_probe_targets_func(
        getattr(bot, "allowed_channel_ids", set()),
        getattr(bot, "startup_channel_id", None),
        limit=target_limit,
    )
    if not targets:
        return [*lines, "-"]
    for label, channel_id in targets:
        channel, source = await resolve_discord_history_channel(bot, channel_id)
        prefix = f"{label} channel={channel_id} source={source}"
        if channel is None or not hasattr(channel, "history"):
            lines.append(f"{prefix} latest_user=-")
            continue
        latest_user_message = None
        try:
            async for message in channel.history(limit=per_target_limit):  # type: ignore[attr-defined]
                if getattr(getattr(message, "author", None), "bot", False):
                    continue
                latest_user_message = message
                break
        except Exception as exc:
            lines.append(f"{prefix} latest_user=history_error:{type(exc).__name__}")
            continue
        if latest_user_message is None:
            lines.append(f"{prefix} latest_user=-")
            continue
        author = getattr(latest_user_message, "author", None)
        lines.append(
            f"{prefix} latest_user_at={format_discord_message_created_at(latest_user_message)} "
            f"user={getattr(author, 'id', '-')} "
            f"content_len={format_log_text_len_func(getattr(latest_user_message, 'content', '') or '')} "
            f"type={format_discord_message_type(latest_user_message)}"
        )
    return lines


async def build_discord_doctor_message_with_history(
    bot: object,
    channel_id: int | None,
    channel: object | None,
    *,
    build_discord_doctor_message_func,
    build_discord_channel_history_lines_func,
    build_discord_tracked_target_user_history_lines_func,
) -> str:
    history_lines = await build_discord_channel_history_lines_func(channel)
    tracked_history_lines = await build_discord_tracked_target_user_history_lines_func(bot)
    return (
        build_discord_doctor_message_func(bot, channel_id)
        + "\n\n"
        + "\n".join(history_lines)
        + "\n\n"
        + "\n".join(tracked_history_lines)
    )
