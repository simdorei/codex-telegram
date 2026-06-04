"""
Discord adapter for codex_desktop_bridge.py.

This keeps Discord-specific behavior separate from the Telegram adapter while
reusing the same local Codex Desktop bridge.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands

import codex_desktop_bridge as bridge
import codex_discord_bridge_process as bridge_process
import codex_discord_busy as discord_busy
import codex_discord_commands as discord_commands
import codex_discord_context as discord_context
import codex_discord_diagnostics as discord_diagnostics
import codex_discord_help as discord_help
import codex_discord_interactive as discord_interactive
import codex_discord_mirror_status as discord_mirror_status
import codex_discord_projects as discord_projects
import codex_discord_runner as discord_runner
import codex_discord_runtime as discord_runtime
import codex_discord_steering as discord_steering
import codex_discord_store as discord_store
import codex_discord_stream as discord_stream
import codex_discord_thread_state as discord_thread_state
from codex_discord_components import (
    format_approval_custom_id,
    format_busy_choice_custom_id,
    format_input_choice_custom_id,
    get_busy_choice_custom_ids_from_message,
    get_component_children,
    get_persistent_component_claim_key,
    is_safe_persistent_input_value,
    parse_approval_custom_id,
    parse_busy_choice_custom_id,
    parse_input_choice_custom_id,
)
from codex_discord_logging import (
    get_discord_log_markers,
    get_log_field,
    get_log_path,
    get_recent_discord_hook_events,
    is_user_or_control_hook_summary,
    log_line,
    parse_log_line,
    summarize_discord_hook_log_line,
)
from codex_discord_text import (
    DISCORD_MAX_LEN,
    build_ask_start_message,
    env_flag,
    extract_prompt_first_sentence,
    fit_single_message,
    format_discord_command_label,
    format_log_argv,
    format_log_text_len,
    format_percent,
    normalize_discord_name,
    parse_bounded_float_env,
    parse_bounded_int_arg,
    parse_int_set,
    split_message,
    truncate_discord_title,
)


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
LOG_PATH = SCRIPT_DIR / "codex_discord_bot.log"
MIRROR_DB_PATH = SCRIPT_DIR / "discord_mirror.sqlite"
THREAD_RUNNERS_LOCK = discord_runner.THREAD_RUNNERS_LOCK
THREAD_RUNNERS = discord_runner.THREAD_RUNNERS
STEERING_HANDOFFS: dict[str, float] = {}
ACTIVE_DISCORD_RELAY_GENERATIONS: dict[str, int] = {}
UI_FALLBACK_LOCK = threading.Lock()
STREAM_REDIRECT_LOCK = threading.RLock()
INTERACTIVE_INPUT_TAG = "[choice_required]"
INTERACTIVE_APPROVAL_TAG = "[approval_required]"
INTERACTIVE_STATE_NONE = ""
INTERACTIVE_STATE_INPUT = "waiting-input"
INTERACTIVE_STATE_APPROVAL = "waiting-approval"
CODEX_PROJECTLESS_CHAT_KEY = "codex:chats"
BUSY_CHOICE_CUSTOM_ID_PREFIX = "codex_busy"
APPROVAL_CUSTOM_ID_PREFIX = "codex_approval"
INPUT_CHOICE_CUSTOM_ID_PREFIX = "codex_input"
BUSY_CHOICE_TTL_SECONDS = 1800
BUSY_CHOICE_COMPONENT_CLEANUP_HISTORY_LIMIT = 50
EMPTY_CONTENT_NOTICE_COOLDOWN_SECONDS = 300
EMPTY_CONTENT_NOTICE_LAST_SENT: dict[int, float] = {}
HISTORY_POLL_DEFAULT_SECONDS = 15.0
HISTORY_POLL_HISTORY_LIMIT = 10
HISTORY_POLL_BOOTSTRAP_LOOKBACK_DEFAULT_SECONDS = 120.0
PROCESSED_MESSAGE_ID_LIMIT = 2000
PROCESSED_MESSAGE_RETENTION_SECONDS = 86400.0
QUIET_PROGRESS_NOTICE_DELAY_SECONDS = -1.0
STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS = 25.0
STEERING_PENDING_WATCH_TIMEOUT_SECONDS = 120.0


def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def discord_qa_commands_enabled() -> bool:
    return env_flag("DISCORD_ENABLE_QA_COMMANDS", default=False)


def is_discord_user_allowed(user_id: int | None) -> bool:
    allowed_user_ids = parse_int_set(os.environ.get("DISCORD_ALLOWED_USER_IDS", ""))
    if not allowed_user_ids:
        return True
    return user_id in allowed_user_ids


def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_steering_delivery_confirm_timeout() -> float:
    return parse_bounded_float_env(
        "DISCORD_STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS",
        default=STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS,
        minimum=3.0,
        maximum=120.0,
    )


def get_steering_pending_watch_timeout() -> float:
    return parse_bounded_float_env(
        "DISCORD_STEERING_PENDING_WATCH_TIMEOUT_SECONDS",
        default=STEERING_PENDING_WATCH_TIMEOUT_SECONDS,
        minimum=10.0,
        maximum=600.0,
    )


def resolve_discord_thread_target_args(
    discord_channel_id: int | None,
    ref: str | None,
) -> list[str]:
    normalized = str(ref or "").strip()
    if normalized:
        thread = bridge.resolve_thread_ref(normalized)
        return ["--thread-id", thread.id]
    target_thread_id = get_mirrored_codex_thread_id(discord_channel_id)
    if target_thread_id:
        return ["--thread-id", target_thread_id]
    return []


def format_interaction_type(interaction: discord.Interaction) -> str:
    interaction_type = getattr(interaction, "type", None)
    return str(getattr(interaction_type, "name", None) or interaction_type or "-")


def get_interaction_custom_id(interaction: discord.Interaction) -> str:
    data = getattr(interaction, "data", None)
    if not isinstance(data, dict):
        return "-"
    custom_id = data.get("custom_id")
    if custom_id is None:
        return "-"
    return format_discord_command_label(str(custom_id), limit=100)


def format_raw_interaction_command(data: dict[str, object]) -> str:
    interaction_data = data.get("data")
    if not isinstance(interaction_data, dict):
        return "-"
    name = interaction_data.get("name")
    if name:
        return format_discord_command_label(str(name), limit=80)
    custom_id = interaction_data.get("custom_id")
    if custom_id:
        return format_discord_command_label(str(custom_id), limit=100)
    return "-"


def format_discord_user_id_for_log(user: object) -> str:
    return str(getattr(user, "id", None) or "-")


def run_bridge_command(argv: list[str]) -> tuple[int, str]:
    return bridge_process.run_bridge_command(
        argv,
        bridge_module=bridge,
        stream_redirect_lock=STREAM_REDIRECT_LOCK,
    )


def parse_bridge_output_value(output: str, key: str) -> str | None:
    return bridge_process.parse_bridge_output_value(output, key)


def resolve_selected_target() -> tuple[str | None, str]:
    return discord_thread_state.resolve_selected_target(bridge_module=bridge)


def get_selected_interactive_state() -> tuple[str, str | None, str]:
    return discord_thread_state.get_selected_interactive_state(
        bridge_module=bridge,
        resolve_selected_target_func=resolve_selected_target,
        state_none=INTERACTIVE_STATE_NONE,
        state_input=INTERACTIVE_STATE_INPUT,
        state_approval=INTERACTIVE_STATE_APPROVAL,
    )


def parse_interactive_notice(text: str) -> tuple[str, str, list[tuple[str, str]]]:
    return discord_interactive.parse_interactive_notice(
        text,
        state_none=INTERACTIVE_STATE_NONE,
        state_input=INTERACTIVE_STATE_INPUT,
        state_approval=INTERACTIVE_STATE_APPROVAL,
        input_tag=INTERACTIVE_INPUT_TAG,
        approval_tag=INTERACTIVE_APPROVAL_TAG,
    )


def normalize_interactive_text_reply(state: str, answer: str) -> str | None:
    return discord_interactive.normalize_interactive_text_reply(
        state,
        answer,
        state_input=INTERACTIVE_STATE_INPUT,
        state_approval=INTERACTIVE_STATE_APPROVAL,
    )


def init_mirror_db() -> None:
    discord_store.init_mirror_db(MIRROR_DB_PATH)


def cleanup_expired_busy_choices(now: float | None = None) -> int:
    return discord_store.cleanup_expired_busy_choices(MIRROR_DB_PATH, now=now)


def cleanup_expired_persistent_component_claims(now: float | None = None) -> int:
    return discord_store.cleanup_expired_persistent_component_claims(MIRROR_DB_PATH, now=now)


def create_busy_choice_record(
    message: discord.Message,
    prompt: str,
    target_thread_id: str | None,
    *,
    allow_steer: bool,
) -> str:
    return discord_store.create_busy_choice_record(
        MIRROR_DB_PATH,
        message,
        prompt,
        target_thread_id,
        allow_steer=allow_steer,
        ttl_seconds=BUSY_CHOICE_TTL_SECONDS,
    )


def get_busy_choice_record(choice_id: str) -> dict[str, object] | None:
    return discord_store.get_busy_choice_record(MIRROR_DB_PATH, choice_id)


def claim_busy_choice_record(choice_id: str) -> bool:
    return discord_store.claim_busy_choice_record(MIRROR_DB_PATH, choice_id)


def claim_persistent_component_interaction(
    interaction: discord.Interaction,
    custom_id: str,
    *,
    ttl_seconds: float = 86400.0,
) -> bool:
    return discord_store.claim_persistent_component_interaction(
        MIRROR_DB_PATH,
        interaction,
        custom_id,
        ttl_seconds=ttl_seconds,
    )


def has_active_busy_choice_custom_id(custom_id: str) -> bool:
    parsed = parse_busy_choice_custom_id(custom_id)
    if not parsed:
        return False
    choice_id, _action = parsed
    return get_busy_choice_record(choice_id) is not None


async def clear_stale_busy_choice_message_components(message: object) -> bool:
    custom_ids = get_busy_choice_custom_ids_from_message(message)
    if not custom_ids:
        return False
    if any(has_active_busy_choice_custom_id(custom_id) for custom_id in custom_ids):
        return False
    try:
        await message.edit(view=None)
        log_line(
            f"stale_busy_choice_components_cleared "
            f"message={getattr(message, 'id', '-')} "
            f"channel={getattr(getattr(message, 'channel', None), 'id', '-')}"
        )
        return True
    except Exception:
        log_line("stale_busy_choice_components_clear_failed\n" + traceback.format_exc())
        return False


def get_startup_probe_targets(
    allowed_channel_ids: set[int],
    startup_channel_id: int | None,
    *,
    limit: int = 30,
) -> list[tuple[str, int]]:
    return discord_store.get_startup_probe_targets(
        MIRROR_DB_PATH,
        allowed_channel_ids,
        startup_channel_id,
        limit=limit,
    )


async def cleanup_stale_busy_choice_components_in_channel(
    channel: discord.abc.Messageable,
    *,
    limit: int = BUSY_CHOICE_COMPONENT_CLEANUP_HISTORY_LIMIT,
) -> int:
    history_factory = getattr(channel, "history", None)
    if not callable(history_factory):
        return 0
    cleared = 0
    async for message in history_factory(limit=limit):
        if not getattr(getattr(message, "author", None), "bot", False):
            continue
        if await clear_stale_busy_choice_message_components(message):
            cleared += 1
    return cleared


def get_project_key(thread: bridge.ThreadInfo) -> str:
    return discord_projects.get_project_key(
        thread,
        bridge_module=bridge,
        projectless_chat_key=CODEX_PROJECTLESS_CHAT_KEY,
    )


def get_project_name(thread: bridge.ThreadInfo) -> str:
    return discord_projects.get_project_name(thread, bridge_module=bridge)


def get_saved_workspace_project_keys() -> set[str]:
    return discord_projects.get_saved_workspace_project_keys(bridge_module=bridge)


def is_thread_mirrorable(
    thread: bridge.ThreadInfo,
    saved_project_keys: set[str] | None = None,
) -> bool:
    return discord_projects.is_thread_mirrorable(
        thread,
        saved_project_keys,
        bridge_module=bridge,
        projectless_chat_key=CODEX_PROJECTLESS_CHAT_KEY,
    )


def filter_mirrorable_threads(threads: list[bridge.ThreadInfo]) -> list[bridge.ThreadInfo]:
    return discord_projects.filter_mirrorable_threads(
        threads,
        bridge_module=bridge,
        projectless_chat_key=CODEX_PROJECTLESS_CHAT_KEY,
    )


def is_codex_projectless_chat_cwd(cwd: str) -> bool:
    return discord_projects.is_codex_projectless_chat_cwd(cwd, bridge_module=bridge)


def get_mirrored_codex_thread_id(discord_channel_id: int | None) -> str | None:
    return discord_store.get_mirrored_codex_thread_id(MIRROR_DB_PATH, discord_channel_id)


def describe_mirrored_project_channel(discord_channel_id: int | None) -> str:
    return discord_store.describe_mirrored_project_channel(MIRROR_DB_PATH, discord_channel_id)


def get_mirror_project_for_channel(discord_channel_id: int | None) -> tuple[str, str] | None:
    return discord_store.get_mirror_project_for_channel(MIRROR_DB_PATH, discord_channel_id)


def get_thread_cwd(thread_id: str | None) -> str | None:
    return discord_projects.get_thread_cwd(thread_id, bridge_module=bridge)


def find_projectless_new_chat_cwd() -> str | None:
    return discord_projects.find_projectless_new_chat_cwd()


def resolve_discord_new_thread_cwd(discord_channel_id: int | None) -> str | None:
    return discord_projects.resolve_discord_new_thread_cwd(
        discord_channel_id,
        bridge_module=bridge,
        projectless_chat_key=CODEX_PROJECTLESS_CHAT_KEY,
        get_mirrored_codex_thread_id_func=get_mirrored_codex_thread_id,
        get_thread_cwd_func=get_thread_cwd,
        get_mirror_project_for_channel_func=get_mirror_project_for_channel,
        find_projectless_new_chat_cwd_func=find_projectless_new_chat_cwd,
    )


def project_keys_match(left: str | None, right: str | None) -> bool:
    return discord_projects.project_keys_match(
        left,
        right,
        bridge_module=bridge,
        projectless_chat_key=CODEX_PROJECTLESS_CHAT_KEY,
    )


def resolve_discord_new_thread_project_channel_id(
    discord_channel_id: int | None,
    project_key: str | None,
) -> int | None:
    return discord_projects.resolve_discord_new_thread_project_channel_id(
        discord_channel_id,
        project_key,
        db_path=MIRROR_DB_PATH,
        init_mirror_db_func=init_mirror_db,
        project_keys_match_func=project_keys_match,
    )


def is_mirrored_channel_id(discord_channel_id: int | None) -> bool:
    return discord_store.is_mirrored_channel_id(MIRROR_DB_PATH, discord_channel_id)


def get_discord_message_id(message: object) -> int | None:
    raw_id = getattr(message, "id", None)
    if raw_id is None:
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def cleanup_processed_discord_messages(now: float | None = None) -> int:
    return discord_store.cleanup_processed_discord_messages(
        MIRROR_DB_PATH,
        retention_seconds=PROCESSED_MESSAGE_RETENTION_SECONDS,
        now=now,
    )


def claim_persistent_discord_message_id(message_id: int, now: float | None = None) -> bool:
    try:
        return discord_store.claim_persistent_discord_message_id(MIRROR_DB_PATH, message_id, now=now)
    except Exception as exc:
        log_line(f"processed_message_persist_failed message={message_id} error_type={type(exc).__name__}")
        return True


def claim_discord_message(owner: object, message: object) -> bool:
    message_id = get_discord_message_id(message)
    if message_id is None:
        return True
    processed = getattr(owner, "_processed_message_ids", None)
    if not isinstance(processed, dict):
        processed = {}
        try:
            setattr(owner, "_processed_message_ids", processed)
        except Exception:
            return True
    if message_id in processed:
        return False
    if not claim_persistent_discord_message_id(message_id):
        return False
    processed[message_id] = time.monotonic()
    if len(processed) > PROCESSED_MESSAGE_ID_LIMIT:
        for stale_id, _seen_at in sorted(processed.items(), key=lambda item: item[1])[
            : len(processed) - PROCESSED_MESSAGE_ID_LIMIT
        ]:
            processed.pop(stale_id, None)
    return True


def is_history_bootstrap_user_message(owner: object, message: object) -> bool:
    if getattr(getattr(message, "author", None), "bot", False):
        return False
    cutoff = getattr(owner, "_history_poll_bootstrap_after", None)
    created_at = getattr(message, "created_at", None)
    if not isinstance(cutoff, datetime) or not isinstance(created_at, datetime):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return created_at >= cutoff


LineStream = bridge_process.LineStream


def get_bridge_script_path() -> Path:
    return bridge_process.get_bridge_script_path(SCRIPT_DIR)


def build_bridge_subprocess_env() -> dict[str, str]:
    return bridge_process.build_bridge_subprocess_env()


def run_bridge_command_stream(argv: list[str], on_line) -> tuple[int, str]:
    return bridge_process.run_bridge_command_stream(
        argv,
        on_line,
        script_path=get_bridge_script_path(),
        cwd=SCRIPT_DIR,
        env=build_bridge_subprocess_env(),
    )


def run_ask(
    prompt: str,
    *,
    force_while_busy: bool = False,
    wait: bool = True,
    target_thread_id: str | None = None,
) -> tuple[int, str]:
    argv = [
        "ask",
        "--ipc",
        "--ipc-recover-ui",
        "--foreground",
        "--timeout",
        "0",
    ]
    if target_thread_id:
        argv.extend(["--thread-id", target_thread_id])
    if force_while_busy:
        argv.append("--force-while-busy")
    if not wait:
        argv.append("--no-wait")
    argv.append(prompt)
    exit_code, output = run_bridge_command(argv)
    if should_retry_ask_with_ui(exit_code, output):
        ui_argv = build_ui_ask_argv(
            prompt,
            target_thread_id=target_thread_id,
            force_while_busy=True,
            wait=wait,
        )
        with UI_FALLBACK_LOCK:
            ui_exit_code, ui_output = run_bridge_command(ui_argv)
        return ui_exit_code, "\n\n".join(
            part
            for part in [
                "Retried with Codex UI fallback after IPC attach failed.",
                ui_output,
            ]
            if part
        )
    return exit_code, output


def should_retry_ask_with_ui(exit_code: int, output: str) -> bool:
    if exit_code == 0:
        return False
    text = (output or "").lower()
    return (
        "local sidecar could not attach" in text
        or "ipc owner client for the selected thread was not discovered" in text
        or "winerror 2" in text
        or "winerror 5" in text
    )


def build_ui_ask_argv(
    prompt: str,
    *,
    target_thread_id: str | None,
    force_while_busy: bool,
    wait: bool,
) -> list[str]:
    argv = [
        "ask",
        "--ui",
        "--switch-thread",
        "--foreground",
        "--timeout",
        "0",
    ]
    if target_thread_id:
        argv.extend(["--thread-id", target_thread_id])
    if force_while_busy:
        argv.append("--force-while-busy")
    if not wait:
        argv.append("--no-wait")
    argv.append(prompt)
    return argv


def submit_approval_reply(target_thread_id: str, answer: str) -> tuple[int, str]:
    return run_bridge_command(["approval_reply", answer, target_thread_id])


def submit_input_reply(target_thread_id: str, answer: str) -> tuple[int, str]:
    try:
        thread = bridge.choose_thread(target_thread_id, None)
        result = bridge.reply_to_pending_user_input(thread, answer, timeout_sec=8.0)
        answers_by_question = result.get("answers_by_question") or {}
        lines = [
            f"thread_id: {thread.id}",
            f"thread_ref: {bridge.get_thread_workspace_ref(thread)}",
        ]
        if isinstance(answers_by_question, dict):
            for question_id, values in answers_by_question.items():
                if isinstance(values, list):
                    lines.append(f"{question_id}: {' | '.join(str(value) for value in values)}")
        return 0, "\n".join(lines)
    except Exception as exc:
        return 1, f"ERROR: {exc}"


SteeringPromptResult = discord_steering.SteeringPromptResult


def make_steering_prompt_result(
    exit_code: int,
    output: str,
    *,
    target_thread: bridge.ThreadInfo | None,
    target_ref: str,
    recent_offsets: dict[str, tuple[bridge.ThreadInfo, Path, int]],
    delivery_pending: bool = False,
) -> SteeringPromptResult:
    return discord_steering.make_steering_prompt_result(
        exit_code,
        output,
        target_thread=target_thread,
        target_ref=target_ref,
        recent_offsets=recent_offsets,
        delivery_pending=delivery_pending,
    )


def is_ipc_delivery_confirmation_timeout(output: str) -> bool:
    return discord_steering.is_ipc_delivery_confirmation_timeout(output)


def format_pending_ipc_delivery_output(output: str) -> str:
    return discord_steering.format_pending_ipc_delivery_output(output)


def run_steering_prompt(prompt: str, target_thread_id: str | None) -> SteeringPromptResult:
    return discord_steering.run_steering_prompt(
        prompt,
        target_thread_id,
        bridge_module=bridge,
        resolve_target_ref_func=resolve_target_ref,
        run_ask_func=run_ask,
        get_steering_delivery_confirm_timeout_func=get_steering_delivery_confirm_timeout,
        log_func=log_line,
        format_log_text_len_func=format_log_text_len,
    )


class DiscordAskRelay(discord_stream.DiscordAskRelay):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        channel: discord.abc.Messageable,
        target_thread_id: str | None,
        target_ref: str,
        quiet_notice_delay_sec: float = QUIET_PROGRESS_NOTICE_DELAY_SECONDS,
        suppress_after_steering_since: float | None = None,
        send_timeout_blocks: bool = True,
    ) -> None:
        super().__init__(
            loop,
            channel,
            target_thread_id,
            target_ref,
            quiet_notice_delay_sec=quiet_notice_delay_sec,
            suppress_after_steering_since=suppress_after_steering_since,
            send_timeout_blocks=send_timeout_blocks,
            send_chunks_func=send_chunks,
            parse_interactive_notice_func=parse_interactive_notice,
            send_interactive_prompt_func=send_interactive_prompt,
            register_discord_relay_func=register_discord_relay,
            is_discord_relay_stale_func=is_discord_relay_stale,
            had_steering_handoff_since_func=had_steering_handoff_since,
            log_func=log_line,
            format_log_text_len_func=format_log_text_len,
        )


def run_ask_stream(
    prompt: str,
    relay: DiscordAskRelay,
    *,
    force_while_busy: bool = False,
    wait: bool = True,
    target_thread_id: str | None = None,
) -> tuple[int, str]:
    return discord_stream.run_ask_stream(
        prompt,
        relay,
        force_while_busy=force_while_busy,
        wait=wait,
        target_thread_id=target_thread_id,
        run_bridge_command_stream_func=run_bridge_command_stream,
        should_retry_ask_with_ui_func=should_retry_ask_with_ui,
        build_ui_ask_argv_func=build_ui_ask_argv,
        ui_fallback_lock=UI_FALLBACK_LOCK,
    )


def run_steering_watch_stream(
    steering_result: SteeringPromptResult,
    relay: DiscordAskRelay,
    *,
    timeout_sec: float = 0,
) -> tuple[int, str]:
    return discord_stream.run_steering_watch_stream(
        steering_result,
        relay,
        timeout_sec=timeout_sec,
        watch_for_final_answer_func=bridge.watch_for_final_answer,
    )


async def stream_steering_prompt_result_to_channel(
    channel: discord.abc.Messageable,
    steering_result: object,
    target_thread_id: str | None,
) -> bool:
    if not isinstance(steering_result, SteeringPromptResult):
        return False
    if not steering_result.session_path or steering_result.start_offset is None:
        log_line(f"steer_watch_unavailable target={target_thread_id or '-'}")
        return False
    started_at = time.monotonic()
    relay = DiscordAskRelay(
        asyncio.get_running_loop(),
        channel,
        steering_result.target_thread_id or target_thread_id,
        steering_result.target_ref or target_thread_id or "-",
        suppress_after_steering_since=started_at,
        send_timeout_blocks=False,
    )
    timeout_sec = get_steering_pending_watch_timeout()
    async with channel_typing(channel, context="steer_watch"):
        exit_code, output = await asyncio.to_thread(
            run_steering_watch_stream,
            steering_result,
            relay,
            timeout_sec=timeout_sec,
        )
    log_line(
        f"steer_watch_done exit={exit_code} target={target_thread_id or '-'} "
        f"sent_live={relay.sent_live} final={relay.saw_final} aborted={relay.saw_aborted} "
        f"timeout={relay.saw_timeout} suppressed={relay.suppressed_after_steering} "
        f"pending={steering_result.delivery_pending} output_len={format_log_text_len(output)}"
    )
    if relay.suppressed_after_steering:
        log_line(f"steer_watch_suppressed_after_newer_handoff target={target_thread_id or '-'}")
        return True
    if relay.sent_live:
        if exit_code == 0 and not relay.saw_aborted:
            if relay.saw_final:
                return True
            else:
                log_line(
                    f"steer_watch_no_final_fallback target={target_thread_id or '-'} "
                    f"output_len={format_log_text_len(output)}"
                )
                await send_chunks(channel, f"Steering finished\n\n{output or '(no final answer captured)'}")
        elif not relay.saw_aborted and not relay.saw_timeout:
            await send_chunks(channel, f"Steering watch failed (exit {exit_code})\n\n{output or '(no output)'}")
        return True
    if exit_code != 0 and relay.saw_timeout:
        log_line(
            f"steer_watch_timeout_suppressed target={target_thread_id or '-'} "
            f"exit={exit_code} pending={steering_result.delivery_pending} "
            f"output_len={format_log_text_len(output)}"
        )
        return True
    if exit_code != 0 and not output:
        log_line(
            f"steer_watch_empty_failure_suppressed target={target_thread_id or '-'} "
            f"exit={exit_code} pending={steering_result.delivery_pending}"
        )
        return True
    if exit_code == 0 and not output:
        log_line(
            f"steer_watch_empty_success_suppressed target={target_thread_id or '-'} "
            f"pending={steering_result.delivery_pending}"
        )
        return True
    title = "Steering finished" if exit_code == 0 else f"Steering watch failed (exit {exit_code})"
    await send_chunks(channel, f"{title}\n\n{output or '(no output)'}")
    return True


def make_post_approval_watch_result(target_thread_id: str) -> SteeringPromptResult | None:
    try:
        thread = bridge.choose_thread(target_thread_id, None)
        session_path = Path(thread.rollout_path)
        if not session_path.exists():
            log_line(
                f"approval_followup_watch_unavailable target={target_thread_id} "
                f"reason=session_missing path={session_path}"
            )
            return None
        return SteeringPromptResult(
            0,
            "[approval_submitted]",
            target_thread_id=thread.id,
            target_ref=bridge.get_thread_workspace_ref(thread),
            session_path=str(session_path),
            start_offset=session_path.stat().st_size,
        )
    except Exception as exc:
        log_line(
            f"approval_followup_watch_unavailable target={target_thread_id} "
            f"error_type={type(exc).__name__}"
        )
        return None


async def stream_post_approval_result_to_channel(
    channel: discord.abc.Messageable,
    watch_result: SteeringPromptResult | None,
    target_thread_id: str,
) -> bool:
    if watch_result is None:
        return False
    if not watch_result.session_path or watch_result.start_offset is None:
        log_line(f"approval_followup_watch_unavailable target={target_thread_id} reason=no_session")
        return False
    relay = DiscordAskRelay(
        asyncio.get_running_loop(),
        channel,
        watch_result.target_thread_id or target_thread_id,
        watch_result.target_ref or target_thread_id,
        send_timeout_blocks=False,
    )
    timeout_sec = get_steering_pending_watch_timeout()
    async with channel_typing(channel, context="approval_followup_watch"):
        exit_code, output = await asyncio.to_thread(
            run_steering_watch_stream,
            watch_result,
            relay,
            timeout_sec=timeout_sec,
        )
    log_line(
        f"approval_followup_watch_done exit={exit_code} target={target_thread_id} "
        f"sent_live={relay.sent_live} final={relay.saw_final} aborted={relay.saw_aborted} "
        f"timeout={relay.saw_timeout} output_len={format_log_text_len(output)}"
    )
    if relay.sent_live:
        if exit_code == 0 and not relay.saw_aborted:
            if relay.saw_final:
                return True
            log_line(
                f"approval_followup_watch_no_final_fallback target={target_thread_id} "
                f"output_len={format_log_text_len(output)}"
            )
            await send_chunks(channel, f"Approval follow-up finished\n\n{output or '(no final answer captured)'}")
        elif not relay.saw_aborted and not relay.saw_timeout:
            await send_chunks(channel, f"Approval follow-up watch failed (exit {exit_code})\n\n{output or '(no output)'}")
        return True
    if exit_code != 0 and relay.saw_timeout:
        log_line(
            f"approval_followup_watch_timeout_suppressed target={target_thread_id} "
            f"exit={exit_code} output_len={format_log_text_len(output)}"
        )
        return True
    if exit_code != 0 and not output:
        log_line(f"approval_followup_watch_empty_failure_suppressed target={target_thread_id} exit={exit_code}")
        return True
    if exit_code == 0 and not output:
        log_line(f"approval_followup_watch_empty_success_suppressed target={target_thread_id}")
        return True
    title = "Approval follow-up finished" if exit_code == 0 else f"Approval follow-up watch failed (exit {exit_code})"
    await send_chunks(channel, f"{title}\n\n{output or '(no output)'}")
    return True


async def resolve_approval_followup_channel(interaction: discord.Interaction) -> object | None:
    channel = getattr(interaction, "channel", None)
    if channel is not None and hasattr(channel, "send"):
        return channel
    message = getattr(interaction, "message", None)
    message_channel = getattr(message, "channel", None)
    if message_channel is not None and hasattr(message_channel, "send"):
        return message_channel
    channel_id = int(getattr(interaction, "channel_id", 0) or 0)
    client = getattr(interaction, "client", None)
    if channel_id and client is not None:
        try:
            fetched = await client.fetch_channel(channel_id)
            if hasattr(fetched, "send"):
                return fetched
        except Exception as exc:
            log_line(
                f"approval_followup_channel_fetch_failed target_channel={channel_id} "
                f"error_type={type(exc).__name__}"
            )
    return None


async def stream_post_approval_result_for_interaction(
    interaction: discord.Interaction,
    watch_result: SteeringPromptResult | None,
    target_thread_id: str,
) -> bool:
    channel = await resolve_approval_followup_channel(interaction)
    if channel is None:
        log_line(f"approval_followup_watch_channel_unavailable target={target_thread_id}")
        return False
    return await stream_post_approval_result_to_channel(channel, watch_result, target_thread_id)


class LoggingCommandTree(app_commands.CommandTree):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        command_name = get_interaction_command_name(interaction)
        log_line(
            f"slash_command_error command={command_name} "
            f"channel={interaction.channel_id} user={getattr(interaction.user, 'id', '-')} "
            f"error={type(error).__name__}: {error}"
        )
        try:
            message = "Discord slash command error. Check codex_discord_bot.log."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                log_line(f"slash_command_error_sent command={command_name} response=followup")
            else:
                await interaction.response.send_message(message, ephemeral=True)
                log_line(f"slash_command_error_sent command={command_name} response=initial")
        except Exception:
            log_line("slash_command_error_report_failed\n" + traceback.format_exc())


class CodexDiscordBot(discord.Client):
    def __init__(
        self,
        *,
        allowed_channel_ids: set[int],
        allowed_user_ids: set[int],
        startup_channel_id: int | None,
        guild_id: int | None,
        enable_prefix_commands: bool,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = enable_prefix_commands
        super().__init__(intents=intents, enable_debug_events=True)
        self.tree = LoggingCommandTree(self)
        self.allowed_channel_ids = allowed_channel_ids
        self.allowed_user_ids = allowed_user_ids
        self.startup_channel_id = startup_channel_id
        self.guild_id = guild_id
        self.enable_prefix_commands = enable_prefix_commands
        self.history_poll_seconds = parse_bounded_float_env(
            "DISCORD_HISTORY_POLL_SECONDS",
            default=HISTORY_POLL_DEFAULT_SECONDS,
            minimum=0.0,
            maximum=300.0,
        )
        self.history_poll_bootstrap_lookback_seconds = parse_bounded_float_env(
            "DISCORD_HISTORY_BOOTSTRAP_LOOKBACK_SECONDS",
            default=HISTORY_POLL_BOOTSTRAP_LOOKBACK_DEFAULT_SECONDS,
            minimum=0.0,
            maximum=600.0,
        )
        self._history_poll_task: asyncio.Task[None] | None = None
        self._history_poll_primed_channels: set[int] = set()
        self._history_poll_last_at = "-"
        self._history_poll_bootstrap_after = datetime.now(timezone.utc) - timedelta(
            seconds=self.history_poll_bootstrap_lookback_seconds
        )
        self._processed_message_ids: dict[int, float] = {}
        self._slash_sync_last_at = "-"
        self._slash_sync_status = "-"
        self._slash_sync_commands = "-"

    def is_allowed_channel(self, channel_id: int | None) -> bool:
        if not self.allowed_channel_ids:
            return True
        return channel_id in self.allowed_channel_ids

    def is_allowed_message_channel(self, channel: discord.abc.GuildChannel | discord.Thread | discord.abc.PrivateChannel) -> bool:
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        if self.is_allowed_channel(channel_id) or self.is_allowed_channel(parent_id):
            return True
        if is_mirrored_channel_id(channel_id) or is_mirrored_channel_id(parent_id):
            return True
        parent = getattr(channel, "parent", None)
        category = getattr(channel, "category", None) or getattr(parent, "category", None)
        return getattr(category, "name", None) == "Codex"

    def is_allowed_user(self, user_id: int | None) -> bool:
        if self.allowed_user_ids:
            return user_id in self.allowed_user_ids
        return is_discord_user_allowed(user_id)

    async def setup_hook(self) -> None:
        log_line("setup_hook_start")
        register_commands(self)
        try:
            if self.guild_id:
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                log_line(f"setup_hook_sync_guild guild_id={self.guild_id}")
                synced = await asyncio.wait_for(self.tree.sync(guild=guild), timeout=20)
            else:
                log_line("setup_hook_sync_global")
                synced = await asyncio.wait_for(self.tree.sync(), timeout=20)
            command_names = sorted(command.name for command in synced)
            self._slash_sync_last_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._slash_sync_status = "ok"
            self._slash_sync_commands = ",".join(command_names) or "-"
            log_line(f"setup_hook_synced commands={','.join(command_names) or '-'}")
        except Exception as exc:
            self._slash_sync_last_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._slash_sync_status = f"skipped:{type(exc).__name__}"
            self._slash_sync_commands = "-"
            log_line(f"setup_hook_sync_skipped error={exc}")
        log_line("setup_hook_done")

    def get_cached_channel_or_thread(self, channel_id: int) -> tuple[object | None, str]:
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel, "client_channel_cache"
        for guild in self.guilds:
            thread = guild.get_thread(channel_id)
            if thread is not None:
                return thread, "guild_thread_cache"
            guild_channel = guild.get_channel(channel_id)
            if guild_channel is not None:
                return guild_channel, "guild_channel_cache"
        return None, "-"

    async def probe_channel_access(self, label: str, channel_id: int) -> None:
        channel, source = self.get_cached_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
                source = "fetch"
            except Exception as exc:
                log_line(
                    f"startup_channel_probe label={label} channel={channel_id} "
                    f"status=failed source=fetch error_type={type(exc).__name__}"
                )
                return
        try:
            allowed_message = self.is_allowed_message_channel(channel)  # type: ignore[arg-type]
        except Exception:
            allowed_message = False
        log_line(
            f"startup_channel_probe label={label} channel={channel_id} status=ok "
            f"source={source} type={type(channel).__name__} "
            f"parent={getattr(channel, 'parent_id', '-')} "
            f"messageable={isinstance(channel, discord.abc.Messageable)} "
            f"allowed_message={allowed_message}"
        )

    async def cleanup_stale_busy_choice_components(self) -> None:
        cleared_total = 0
        for label, channel_id in get_startup_probe_targets(self.allowed_channel_ids, self.startup_channel_id):
            channel, _source = self.get_cached_channel_or_thread(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except Exception as exc:
                    log_line(
                        f"stale_busy_choice_component_cleanup_skipped label={label} "
                        f"channel={channel_id} reason=fetch_failed error_type={type(exc).__name__}"
                    )
                    continue
            if not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                cleared = await cleanup_stale_busy_choice_components_in_channel(channel)
            except Exception:
                log_line(
                    f"stale_busy_choice_component_cleanup_failed label={label} "
                    f"channel={channel_id}\n" + traceback.format_exc()
                )
                continue
            if cleared:
                cleared_total += cleared
                log_line(
                    f"stale_busy_choice_component_cleanup_deleted "
                    f"label={label} channel={channel_id} count={cleared}"
                )
        if cleared_total:
            log_line(f"stale_busy_choice_component_cleanup_done count={cleared_total}")

    async def log_startup_diagnostics(self) -> None:
        try:
            targets = get_startup_probe_targets(self.allowed_channel_ids, self.startup_channel_id)
            log_line(f"startup_diagnostics_start targets={len(targets)}")
            for label, channel_id in targets:
                await self.probe_channel_access(label, channel_id)
            log_line("startup_diagnostics_done")
        except Exception:
            log_line("startup_diagnostics_failed\n" + traceback.format_exc())

    async def start_history_polling(self) -> None:
        if self.history_poll_seconds <= 0:
            log_line("history_poll_disabled")
            return
        if self._history_poll_task and not self._history_poll_task.done():
            log_line("history_poll_already_running")
            return
        self._history_poll_task = asyncio.create_task(self.history_poll_loop())
        log_line(f"history_poll_started seconds={self.history_poll_seconds:g}")

    async def history_poll_loop(self) -> None:
        while not self.is_closed():
            try:
                self._history_poll_last_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                targets = get_startup_probe_targets(
                    self.allowed_channel_ids,
                    self.startup_channel_id,
                    limit=50,
                )
                for label, channel_id in targets:
                    await self.poll_history_channel(label, channel_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log_line("history_poll_cycle_failed\n" + traceback.format_exc())
            await asyncio.sleep(self.history_poll_seconds)

    async def poll_history_channel(self, label: str, channel_id: int) -> None:
        channel, source = self.get_cached_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
                source = "fetch"
            except Exception as exc:
                log_line(
                    f"history_poll_channel_failed label={label} channel={channel_id} "
                    f"error_type={type(exc).__name__}"
                )
                return
        if not hasattr(channel, "history"):
            log_line(f"history_poll_channel_skipped label={label} channel={channel_id} reason=no_history")
            return
        is_primed = int(channel_id) in self._history_poll_primed_channels
        claimed_messages = []
        try:
            async for message in channel.history(limit=HISTORY_POLL_HISTORY_LIMIT):  # type: ignore[attr-defined]
                if claim_discord_message(self, message):
                    claimed_messages.append(message)
        except Exception as exc:
            log_line(
                f"history_poll_channel_failed label={label} channel={channel_id} "
                f"source={source} error_type={type(exc).__name__}"
            )
            return
        if not is_primed:
            self._history_poll_primed_channels.add(int(channel_id))
            bootstrap_messages = [
                message
                for message in reversed(claimed_messages)
                if is_history_bootstrap_user_message(self, message)
            ]
            log_line(
                f"history_poll_primed label={label} channel={channel_id} "
                f"source={source} messages={len(claimed_messages)} "
                f"bootstrap_user_messages={len(bootstrap_messages)}"
            )
            for message in bootstrap_messages:
                await CodexDiscordBot.process_history_poll_message(self, message, channel_id)
            return
        for message in reversed(claimed_messages):
            await CodexDiscordBot.process_history_poll_message(self, message, channel_id)

    async def process_history_poll_message(self, message: object, channel_id: int) -> None:
        if getattr(getattr(message, "author", None), "bot", False):
            return
        log_line(
            f"history_poll_message channel={getattr(getattr(message, 'channel', None), 'id', channel_id)} "
            f"user={getattr(getattr(message, 'author', None), 'id', '-')} "
            f"content_len={format_log_text_len(getattr(message, 'content', '') or '')}"
        )
        await self.process_discord_message(message, source="history_poll")

    async def on_ready(self) -> None:
        log_line(f"ready user_id={format_discord_user_id_for_log(self.user)} guilds={len(self.guilds)}")
        try:
            deleted_busy_choices = await asyncio.to_thread(cleanup_expired_busy_choices)
            if deleted_busy_choices:
                log_line(f"busy_choice_cleanup_deleted count={deleted_busy_choices}")
        except Exception:
            log_line("busy_choice_cleanup_failed\n" + traceback.format_exc())
        try:
            deleted_component_claims = await asyncio.to_thread(cleanup_expired_persistent_component_claims)
            if deleted_component_claims:
                log_line(f"persistent_component_claim_cleanup_deleted count={deleted_component_claims}")
        except Exception:
            log_line("persistent_component_claim_cleanup_failed\n" + traceback.format_exc())
        try:
            deleted_processed_messages = await asyncio.to_thread(cleanup_processed_discord_messages)
            if deleted_processed_messages:
                log_line(f"processed_message_cleanup_deleted count={deleted_processed_messages}")
        except Exception:
            log_line("processed_message_cleanup_failed\n" + traceback.format_exc())
        if hasattr(self, "cleanup_stale_busy_choice_components"):
            try:
                await self.cleanup_stale_busy_choice_components()
            except Exception:
                log_line("stale_busy_choice_component_cleanup_failed\n" + traceback.format_exc())
        await self.log_startup_diagnostics()
        if hasattr(self, "start_history_polling"):
            await self.start_history_polling()
        if env_flag("DISCORD_STARTUP_NOTIFY", default=False) and self.startup_channel_id:
            channel = self.get_channel(self.startup_channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(self.startup_channel_id)
                except Exception:
                    log_line("startup_channel_fetch_failed\n" + traceback.format_exc())
                    return
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await send_chunks(channel, "Codex Discord bot online. Try `!list` or `/list`.")
                    log_line(f"startup_notify_sent channel={self.startup_channel_id}")
                except Exception:
                    log_line("startup_notify_failed\n" + traceback.format_exc())
            else:
                log_line(f"startup_notify_skipped channel={self.startup_channel_id} reason=not_messageable")

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        interaction_type = format_interaction_type(interaction)
        command_name = get_interaction_command_name(interaction)
        custom_id = get_interaction_custom_id(interaction)
        log_line(
            f"interaction_received type={interaction_type} command={command_name} "
            f"custom_id={custom_id} channel={interaction.channel_id} "
            f"user={getattr(interaction.user, 'id', '-')}"
        )
        if getattr(interaction, "type", None) == discord.InteractionType.component:
            asyncio.create_task(report_unhandled_component_interaction(interaction))

    async def on_socket_raw_receive(self, message: str | bytes) -> None:
        try:
            if isinstance(message, bytes):
                raw_text = message.decode("utf-8", errors="replace")
            else:
                raw_text = str(message)
            payload = json.loads(raw_text)
        except Exception:
            return
        if isinstance(payload, dict):
            await self.log_socket_payload(payload)

    async def on_socket_response(self, payload: dict[str, object]) -> None:
        await self.log_socket_payload(payload)

    def is_tracked_socket_message_channel(self, channel_id: int | None) -> tuple[bool, str]:
        if channel_id is None:
            return False, "missing_channel"
        channel, source = self.get_cached_channel_or_thread(channel_id)
        if channel is not None:
            try:
                if self.is_allowed_message_channel(channel):  # type: ignore[arg-type]
                    return True, source
            except Exception:
                return False, "cache_error"
        if self.is_allowed_channel(channel_id):
            return True, "allowed_channel_id"
        if is_mirrored_channel_id(channel_id):
            return True, "mirror_channel_id"
        return False, source

    async def log_socket_payload(self, payload: dict[str, object]) -> None:
        event_type = str(payload.get("t") or "")
        data = payload.get("d")
        if not isinstance(data, dict):
            return
        if event_type == "MESSAGE_CREATE":
            channel_id_raw = data.get("channel_id")
            try:
                channel_id = int(str(channel_id_raw))
            except (TypeError, ValueError):
                channel_id = None
            author = data.get("author")
            author_id = "-"
            author_bot = "-"
            if isinstance(author, dict):
                author_id = str(author.get("id") or "-")
                author_bot = str(bool(author.get("bot", False)))
            tracked, track_source = self.is_tracked_socket_message_channel(channel_id)
            if not tracked:
                log_line(
                    f"socket_message_create_untracked channel={channel_id or '-'} "
                    f"guild={data.get('guild_id') or '-'} source={track_source}"
                )
                return
            log_line(
                f"socket_message_create channel={channel_id or '-'} tracked={tracked} "
                f"source={track_source} guild={data.get('guild_id') or '-'} "
                f"author={author_id} bot={author_bot} content_len={format_log_text_len(data.get('content'))}"
            )
            return
        if event_type == "INTERACTION_CREATE":
            channel_id = data.get("channel_id") or "-"
            log_line(
                f"socket_interaction_create channel={channel_id} guild={data.get('guild_id') or '-'} "
                f"user={self.format_socket_interaction_user(data)} "
                f"type={data.get('type') or '-'} command={format_raw_interaction_command(data)}"
            )

    def format_socket_interaction_user(self, data: dict[str, object]) -> str:
        user = data.get("user")
        if isinstance(user, dict) and user.get("id"):
            return str(user.get("id"))
        member = data.get("member")
        if isinstance(member, dict):
            member_user = member.get("user")
            if isinstance(member_user, dict) and member_user.get("id"):
                return str(member_user.get("id"))
        return "-"

    async def on_message(self, message: discord.Message) -> None:
        if getattr(getattr(message, "author", None), "bot", False):
            return
        if not claim_discord_message(self, message):
            log_line(
                f"duplicate_message_skipped source=gateway "
                f"chat={getattr(getattr(message, 'channel', None), 'id', '-')} "
                f"message={get_discord_message_id(message) or '-'}"
            )
            return
        await CodexDiscordBot.process_discord_message(self, message, source="gateway")

    async def process_discord_message(self, message: discord.Message, *, source: str) -> None:
        try:
            log_line(
                f"message_received chat={getattr(message.channel, 'id', '-')} "
                f"parent={getattr(message.channel, 'parent_id', '-')} "
                f"user={message.author.id} "
                f"content_len={format_log_text_len(message.content or '')} "
                f"source={source}"
            )
            if not self.enable_prefix_commands:
                log_line("ignored_message reason=message_content_disabled")
                return
            if not self.is_allowed_message_channel(message.channel):
                parent = getattr(message.channel, "parent", None)
                category = getattr(message.channel, "category", None) or getattr(parent, "category", None)
                log_line(
                    f"ignored_message reason=channel_not_allowed chat={getattr(message.channel, 'id', '-')} "
                    f"parent={getattr(message.channel, 'parent_id', '-')} "
                    f"category={getattr(category, 'name', '-')}"
                )
                return
            if not self.is_allowed_user(message.author.id):
                log_line(f"ignored_message reason=user_not_allowed user={message.author.id}")
                return
            content = (message.content or "").strip()
            if not content:
                log_line(
                    f"ignored_message reason=empty_content chat={message.channel.id} "
                    f"user={message.author.id}"
                )
                await maybe_send_empty_content_notice(message)
                return
            target_thread_id = get_mirrored_codex_thread_id(message.channel.id)
            target_source = "mirror" if target_thread_id else "selected"
            runner_busy = await is_thread_runner_busy(target_thread_id)
            codex_busy_state, _busy_thread_id, _busy_ref = await asyncio.to_thread(
                get_busy_state_for_thread,
                target_thread_id,
            )
            log_line(
                f"message chat={message.channel.id} user={message.author.id} "
                f"prefix={content.startswith('!')} runner_busy={runner_busy} "
                f"codex_busy={codex_busy_state} "
                f"target_source={target_source} target={target_thread_id or '-'} "
                f"text_len={format_log_text_len(content)}"
            )
            if content.startswith("!"):
                await handle_prefix_command(self, message, content[1:].strip())
                return
            if target_thread_id is None:
                project_message = describe_mirrored_project_channel(message.channel.id)
                if project_message:
                    await send_chunks(message.channel, project_message)
                    return
            await handle_plain_ask(message, content, target_thread_id=target_thread_id)
        except Exception:
            log_line("on_message_error\n" + traceback.format_exc())
            try:
                await message.channel.send("Discord bot error. Check codex_discord_bot.log.")
            except Exception:
                log_line("on_message_error_report_failed\n" + traceback.format_exc())


async def send_chunks(target: discord.abc.Messageable, text: str) -> None:
    for chunk in split_message(text):
        await target.send(chunk)


@asynccontextmanager
async def channel_typing(target: object, *, context: str = ""):
    typing_factory = getattr(target, "typing", None)
    if not callable(typing_factory):
        yield
        return

    manager = None
    try:
        manager = typing_factory()
        await manager.__aenter__()
    except Exception as exc:
        log_line(
            f"typing_start_failed context={context or '-'} "
            f"error_type={type(exc).__name__}"
        )
        yield
        return

    exc_info = (None, None, None)
    try:
        yield
    except BaseException:
        exc_info = sys.exc_info()
        raise
    finally:
        try:
            await manager.__aexit__(*exc_info)
        except Exception as exc:
            log_line(
                f"typing_stop_failed context={context or '-'} "
                f"error_type={type(exc).__name__}"
            )


def message_has_non_text_payload(message: discord.Message) -> bool:
    return bool(
        getattr(message, "attachments", None)
        or getattr(message, "embeds", None)
        or getattr(message, "stickers", None)
    )


def should_send_empty_content_notice(channel_id: int | None, *, now: float | None = None) -> bool:
    if not channel_id:
        return False
    current = time.monotonic() if now is None else now
    last_sent = EMPTY_CONTENT_NOTICE_LAST_SENT.get(int(channel_id))
    if last_sent is not None and current - last_sent < EMPTY_CONTENT_NOTICE_COOLDOWN_SECONDS:
        return False
    EMPTY_CONTENT_NOTICE_LAST_SENT[int(channel_id)] = current
    return True


async def maybe_send_empty_content_notice(message: discord.Message) -> None:
    if message_has_non_text_payload(message):
        log_line(
            f"empty_content_notice_skipped reason=non_text_payload "
            f"chat={getattr(message.channel, 'id', '-')}"
        )
        return
    channel_id = getattr(message.channel, "id", None)
    if not should_send_empty_content_notice(channel_id):
        log_line(
            f"empty_content_notice_skipped reason=cooldown "
            f"chat={channel_id or '-'}"
        )
        return
    await send_chunks(
        message.channel,
        "I received a Discord message, but Discord did not provide the text content. "
        "Use `/ask` or enable the bot's Message Content Intent in the Discord developer portal.",
    )
    log_line(f"empty_content_notice_sent chat={channel_id or '-'}")


async def clear_interaction_message_components(
    interaction: discord.Interaction,
    *,
    context: str,
) -> None:
    message = getattr(interaction, "message", None)
    if message is None or not hasattr(message, "edit"):
        return
    try:
        await message.edit(view=None)
        log_line(
            f"component_message_components_cleared context={context} "
            f"channel={interaction.channel_id} user={getattr(interaction.user, 'id', '-')}"
        )
    except Exception:
        log_line(
            f"component_message_components_clear_failed context={context}\n"
            + traceback.format_exc()
        )


async def report_unhandled_component_interaction(
    interaction: discord.Interaction,
    *,
    delay_sec: float = 0.75,
) -> None:
    await asyncio.sleep(delay_sec)
    if interaction.response.is_done():
        return
    custom_id = get_interaction_custom_id(interaction)
    try:
        if await handle_persistent_approval_interaction(interaction, custom_id):
            return
        if await handle_persistent_input_choice_interaction(interaction, custom_id):
            return
        if await handle_persistent_busy_choice_interaction(interaction, custom_id):
            return
    except Exception:
        log_line("component_interaction_persistent_handler_failed\n" + traceback.format_exc())
        if interaction.response.is_done():
            return
    try:
        await clear_interaction_message_components(interaction, context="unhandled_component")
        await interaction.response.send_message(
            "This Discord button is no longer active. Send the message again to get fresh controls.",
            ephemeral=True,
        )
        log_line(
            f"component_interaction_unhandled_reported custom_id={custom_id} "
            f"channel={interaction.channel_id} user={getattr(interaction.user, 'id', '-')}"
        )
    except Exception:
        log_line("component_interaction_unhandled_report_failed\n" + traceback.format_exc())


async def resolve_interaction_channel(interaction: discord.Interaction, channel_id: int) -> object | None:
    channel = getattr(interaction, "channel", None)
    if channel is not None and hasattr(channel, "send"):
        return channel
    client = getattr(interaction, "client", None)
    if client is not None:
        try:
            fetched = await client.fetch_channel(channel_id)
            if hasattr(fetched, "send"):
                return fetched
        except Exception as exc:
            log_line(
                f"busy_choice_persistent_channel_fetch_failed channel={channel_id} "
                f"error_type={type(exc).__name__}"
            )
    return None


async def handle_persistent_approval_interaction(
    interaction: discord.Interaction,
    custom_id: str,
    *,
    approval_submitter=None,
) -> bool:
    approval_submitter = approval_submitter or submit_approval_reply
    parsed = parse_approval_custom_id(custom_id)
    if not parsed:
        return False
    target_thread_id, answer = parsed
    user_id = int(getattr(interaction.user, "id", 0) or 0)
    if not is_discord_user_allowed(user_id):
        await interaction.response.send_message("This user is not allowed.", ephemeral=True)
        log_line(f"approval_persistent_denied user={user_id} target={target_thread_id}")
        return True
    if not claim_persistent_component_interaction(interaction, custom_id):
        await clear_interaction_message_components(interaction, context="approval_persistent_already_handled")
        await interaction.response.send_message("This approval choice was already handled.", ephemeral=True)
        log_line(f"approval_persistent_already_handled user={user_id} target={target_thread_id}")
        return True
    await interaction.response.defer(thinking=True)
    await clear_interaction_message_components(interaction, context="approval_persistent")
    log_line(
        f"approval_persistent user={user_id} target={target_thread_id} "
        f"answer_len={format_log_text_len(answer)}"
    )
    watch_result = make_post_approval_watch_result(target_thread_id)
    exit_code, output = await asyncio.to_thread(approval_submitter, target_thread_id, answer)
    log_line(
        f"approval_persistent_done exit={exit_code} target={target_thread_id} "
        f"answer_len={format_log_text_len(answer)}"
    )
    title = "Approval submitted" if exit_code == 0 else f"Approval failed (exit {exit_code})"
    await send_followup_chunks(
        interaction,
        f"{title}\n\n{output or '(no output)'}",
        title="Approval",
        exit_code=exit_code,
        log_prefix="button_response",
    )
    if exit_code == 0:
        await stream_post_approval_result_for_interaction(interaction, watch_result, target_thread_id)
    return True


async def handle_persistent_input_choice_interaction(
    interaction: discord.Interaction,
    custom_id: str,
    *,
    input_submitter=None,
) -> bool:
    input_submitter = input_submitter or submit_input_reply
    parsed = parse_input_choice_custom_id(custom_id)
    if not parsed:
        return False
    target_thread_id, value = parsed
    user_id = int(getattr(interaction.user, "id", 0) or 0)
    if not is_discord_user_allowed(user_id):
        await interaction.response.send_message("This user is not allowed.", ephemeral=True)
        log_line(f"input_choice_persistent_denied user={user_id} target={target_thread_id}")
        return True
    if not claim_persistent_component_interaction(interaction, custom_id):
        await clear_interaction_message_components(interaction, context="input_choice_persistent_already_handled")
        await interaction.response.send_message("This input choice was already handled.", ephemeral=True)
        log_line(f"input_choice_persistent_already_handled user={user_id} target={target_thread_id}")
        return True
    await interaction.response.defer(thinking=True)
    await clear_interaction_message_components(interaction, context="input_choice_persistent")
    log_line(
        f"input_choice_persistent user={user_id} target={target_thread_id} "
        f"value_len={format_log_text_len(value)}"
    )
    exit_code, output = await asyncio.to_thread(input_submitter, target_thread_id, value)
    log_line(
        f"input_choice_persistent_done exit={exit_code} target={target_thread_id} "
        f"value_len={format_log_text_len(value)}"
    )
    title = "Input submitted" if exit_code == 0 else f"Input failed (exit {exit_code})"
    await send_followup_chunks(
        interaction,
        f"{title}\n\n{output or '(no output)'}",
        title="Input",
        exit_code=exit_code,
        log_prefix="button_response",
    )
    return True


def make_persistent_busy_source_message(record: dict[str, object], channel: object) -> SimpleNamespace:
    return SimpleNamespace(
        author=SimpleNamespace(id=int(record["owner_user_id"])),
        channel=channel,
    )


async def handle_persistent_busy_choice_interaction(
    interaction: discord.Interaction,
    custom_id: str,
    *,
    steering_runner=None,
    steering_streamer=None,
) -> bool:
    steering_runner = steering_runner or run_steering_prompt
    steering_streamer = steering_streamer or stream_steering_prompt_result_to_channel
    parsed = parse_busy_choice_custom_id(custom_id)
    if not parsed:
        return False
    choice_id, action = parsed
    record = get_busy_choice_record(choice_id)
    user_id = int(getattr(interaction.user, "id", 0) or 0)
    if record is None:
        await clear_interaction_message_components(interaction, context="busy_choice_missing")
        await interaction.response.send_message(
            "This Discord button is no longer active. Send the message again to get fresh controls.",
            ephemeral=True,
        )
        log_line(
            f"busy_choice_persistent_missing action={action} choice={choice_id} "
            f"channel={interaction.channel_id} user={user_id}"
        )
        return True
    if user_id != int(record["owner_user_id"]):
        await interaction.response.send_message("Only the original sender can choose this.", ephemeral=True)
        log_line(
            f"busy_choice_persistent_denied action={action} choice={choice_id} "
            f"user={user_id} owner={record['owner_user_id']} target={record['target_thread_id'] or '-'}"
        )
        return True
    if action == "steer" and not bool(record["allow_steer"]):
        await interaction.response.send_message(
            "This message targets a different Codex thread. Queue it instead.",
            ephemeral=True,
        )
        log_line(
            f"busy_choice_persistent_steer_rejected user={user_id} choice={choice_id} "
            f"target={record['target_thread_id'] or '-'} reason=not_allowed"
        )
        return True
    if not claim_busy_choice_record(choice_id):
        await clear_interaction_message_components(interaction, context="busy_choice_already_handled")
        await interaction.response.send_message("This busy choice was already handled.", ephemeral=True)
        log_line(
            f"busy_choice_persistent_already_handled action={action} choice={choice_id} "
            f"user={user_id} target={record['target_thread_id'] or '-'}"
        )
        return True

    prompt = str(record["prompt"] or "")
    target_thread_id = str(record["target_thread_id"] or "") or None
    if action == "ignore":
        log_line(
            f"busy_choice_persistent_ignore user={user_id} choice={choice_id} "
            f"target={target_thread_id or '-'}"
        )
        await clear_interaction_message_components(interaction, context="busy_choice_ignore")
        await interaction.response.send_message("Ignored.")
        return True

    await interaction.response.defer(thinking=True, ephemeral=(action == "steer"))
    await clear_interaction_message_components(interaction, context=f"busy_choice_{action}")
    channel = await resolve_interaction_channel(interaction, int(record["channel_id"]))
    if channel is None:
        await send_direct_followup(
            interaction,
            "Discord channel is unavailable. Send the message again to get fresh controls.",
            log_prefix="button_followup",
            context="persistent_channel_unavailable",
        )
        log_line(
            f"busy_choice_persistent_channel_unavailable action={action} choice={choice_id} "
            f"target={target_thread_id or '-'}"
        )
        return True

    source_message = make_persistent_busy_source_message(record, channel)
    if action == "steer":
        log_line(
            f"busy_choice_persistent_steer user={user_id} choice={choice_id} "
            f"target={target_thread_id or '-'} prompt_len={format_log_text_len(prompt)}"
        )
        started_at = time.monotonic()
        async with channel_typing(channel, context="persistent_steer_now"):
            steering_result = await asyncio.to_thread(steering_runner, prompt, target_thread_id)
        exit_code, output = steering_result
        if exit_code == 0:
            mark_steering_handoff(target_thread_id)
        log_line(
            f"busy_choice_persistent_steer_done exit={exit_code} choice={choice_id} "
            f"target={target_thread_id or '-'} elapsed_sec={time.monotonic() - started_at:.2f} "
            f"output_len={format_log_text_len(output)}"
        )
        if is_selected_thread_busy_error(exit_code, output):
            content, view = make_busy_choice_payload(
                source_message,
                prompt,
                target_thread_id=target_thread_id,
                allow_steer=True,
            )
            await send_direct_followup(
                interaction,
                content,
                view=view,
                log_prefix="button_followup",
                context="persistent_steer_busy_failure",
            )
            log_busy_choice_sent("persistent_steer_busy_failure", target_thread_id, prompt)
            return True
        title = "Steering sent" if exit_code == 0 else f"Steering failed (exit {exit_code})"
        await send_followup_chunks(
            interaction,
            f"{title}\n\n{output or '(no output)'}",
            title="Steering",
            exit_code=exit_code,
            log_prefix="button_response",
            ephemeral=True,
        )
        if exit_code == 0:
            await steering_streamer(channel, steering_result, target_thread_id)
        return True

    busy_state, _busy_thread_id, _busy_ref = await asyncio.to_thread(
        get_busy_state_for_thread,
        target_thread_id,
    )
    if busy_state == "idle" and not await is_thread_runner_busy(target_thread_id):
        await send_direct_followup(
            interaction,
            "No active job now. Starting this message.",
            log_prefix="button_followup",
            context="persistent_queue_next_immediate",
        )
        position = await enqueue_thread_ask(
            channel,  # type: ignore[arg-type]
            prompt,
            target_thread_id,
            queued=False,
            ack_sent=True,
            source_message=source_message,  # type: ignore[arg-type]
        )
        log_line(
            f"busy_choice_persistent_queue_immediate user={user_id} choice={choice_id} "
            f"position={position} target={target_thread_id or '-'} "
            f"prompt_len={format_log_text_len(prompt)}"
        )
        return True

    position = await enqueue_thread_ask(
        channel,  # type: ignore[arg-type]
        prompt,
        target_thread_id,
        queued=True,
        source_message=source_message,  # type: ignore[arg-type]
    )
    await send_direct_followup(
        interaction,
        f"Queued at position {position}.",
        log_prefix="button_followup",
        context="persistent_queue_next",
    )
    log_line(
        f"busy_choice_persistent_queue user={user_id} choice={choice_id} "
        f"position={position} target={target_thread_id or '-'} "
        f"prompt_len={format_log_text_len(prompt)}"
    )
    return True


async def send_interactive_prompt(
    channel: discord.abc.Messageable,
    target_thread_id: str,
    target_ref: str,
    state: str,
    prompt: str,
    options: list[tuple[str, str]],
) -> None:
    if state == INTERACTIVE_STATE_APPROVAL:
        lines = ["Waiting approval", f"thread: {target_ref or target_thread_id}", ""]
        if prompt:
            lines.extend([prompt, ""])
        await channel.send(fit_single_message("\n".join(lines)), view=ApprovalView(target_thread_id))
        return

    if state == INTERACTIVE_STATE_INPUT:
        lines = ["Waiting input", f"thread: {target_ref or target_thread_id}", ""]
        if prompt:
            lines.extend([prompt, ""])
        if options:
            await channel.send(
                fit_single_message("\n".join(lines)),
                view=InputChoiceView(target_thread_id, options),
            )
        else:
            lines.append("Reply with plain text to answer this prompt.")
            await send_chunks(channel, "\n".join(lines))
        return


async def run_bridge_and_send(
    target: discord.abc.Messageable,
    argv: list[str],
    title: str,
    failure_title: str | None = None,
) -> tuple[int, str]:
    exit_code, output = await asyncio.to_thread(run_bridge_command, argv)
    prefix = title if exit_code == 0 else f"{failure_title or title} failed (exit {exit_code})"
    chunks = split_message(f"{prefix}\n\n{output or '(no output)'}")
    log_line(
        f"bridge_command_done title={title!r} exit={exit_code} "
        f"chunks={len(chunks)} argv={format_log_argv(argv)}"
    )
    for chunk in chunks:
        await target.send(chunk)
    log_line(f"bridge_command_sent title={title!r} exit={exit_code} chunks={len(chunks)}")
    return exit_code, output


def get_interaction_command_name(interaction: discord.Interaction) -> str:
    command = getattr(interaction, "command", None)
    return str(getattr(command, "name", None) or "-")


async def send_interaction_chunks(
    interaction: discord.Interaction,
    text: str,
    *,
    title: str,
    exit_code: int | None = None,
) -> None:
    await send_followup_chunks(
        interaction,
        text,
        title=title,
        exit_code=exit_code,
        log_prefix="slash_response",
    )


async def send_followup_chunks(
    interaction: discord.Interaction,
    text: str,
    *,
    title: str,
    exit_code: int | None = None,
    log_prefix: str = "followup_response",
    ephemeral: bool = False,
) -> None:
    chunks = split_message(text)
    command_name = get_interaction_command_name(interaction)
    exit_part = "-" if exit_code is None else str(exit_code)
    log_line(
        f"{log_prefix}_start command={command_name} title={title!r} "
        f"exit={exit_part} chunks={len(chunks)} channel={interaction.channel_id}"
    )
    sent_count = 0
    try:
        for chunk in chunks:
            if ephemeral:
                await interaction.followup.send(chunk, ephemeral=True)
            else:
                await interaction.followup.send(chunk)
            sent_count += 1
    except Exception as exc:
        log_line(
            f"{log_prefix}_failed command={command_name} title={title!r} "
            f"exit={exit_part} sent={sent_count} chunks={len(chunks)} "
            f"error_type={type(exc).__name__}"
        )
        channel = getattr(interaction, "channel", None)
        if channel is not None and hasattr(channel, "send"):
            remaining = "\n\n".join(chunks[sent_count:]) or "(no output)"
            prefix = (
                "Discord follow-up delivery failed; posting remaining response here."
                if sent_count
                else "Discord follow-up delivery failed; posting response here."
            )
            try:
                await send_chunks(channel, f"{prefix}\n\n{remaining}")
                log_line(
                    f"{log_prefix}_fallback_sent command={command_name} title={title!r} "
                    f"exit={exit_part} sent={sent_count} chunks={len(chunks)}"
                )
                return
            except Exception:
                log_line(f"{log_prefix}_fallback_failed\n" + traceback.format_exc())
        raise
    log_line(
        f"{log_prefix}_sent command={command_name} title={title!r} "
        f"exit={exit_part} chunks={len(chunks)}"
    )


async def send_direct_followup(
    interaction: discord.Interaction,
    content: str,
    *,
    view=None,
    log_prefix: str = "direct_followup",
    context: str = "",
) -> None:
    command_name = get_interaction_command_name(interaction)
    safe_context = format_discord_command_label(context, limit=80)
    has_view = view is not None
    failure: Exception | None = None
    try:
        await interaction.followup.send(content, view=view)
        log_line(
            f"{log_prefix}_sent command={command_name} context={safe_context or '-'} "
            f"has_view={has_view} content_len={format_log_text_len(content)}"
        )
        return
    except Exception as exc:
        failure = exc
        log_line(
            f"{log_prefix}_failed command={command_name} context={safe_context or '-'} "
            f"has_view={has_view} content_len={format_log_text_len(content)} "
            f"error_type={type(exc).__name__}"
        )
    channel = getattr(interaction, "channel", None)
    if channel is None or not hasattr(channel, "send"):
        raise failure
    prefix = "Discord follow-up delivery failed; posting response here."
    try:
        if has_view:
            await channel.send(fit_single_message(f"{prefix}\n\n{content}"), view=view)
        else:
            await send_chunks(channel, f"{prefix}\n\n{content}")
        log_line(
            f"{log_prefix}_fallback_sent command={command_name} context={safe_context or '-'} "
            f"has_view={has_view}"
        )
    except Exception:
        log_line(f"{log_prefix}_fallback_failed\n" + traceback.format_exc())
        raise


class SyntheticQAResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deferred = False
        self.done = False
        self.defer_kwargs: list[dict[str, object]] = []

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append(content)
        self.done = True

    async def defer(self, thinking: bool = False, **kwargs) -> None:
        self.deferred = True
        self.done = True
        self.defer_kwargs.append({"thinking": thinking, **kwargs})

    def is_done(self) -> bool:
        return self.done


class SyntheticQAFollowup:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.kwargs: list[dict[str, object]] = []

    async def send(self, content: str, view=None, **kwargs) -> None:
        self.messages.append(content if view is None else (content, view))
        self.kwargs.append(kwargs)


class SyntheticQAInteraction:
    def __init__(
        self,
        *,
        bot: "CodexDiscordBot",
        channel: discord.abc.Messageable,
        message: object,
        user: object,
        custom_id: str,
    ) -> None:
        self.client = bot
        self.channel = channel
        self.channel_id = getattr(channel, "id", None)
        self.command = SimpleNamespace(name="-")
        self.data = {"custom_id": custom_id}
        self.followup = SyntheticQAFollowup()
        self.message = message
        self.response = SyntheticQAResponse()
        self.type = discord.InteractionType.component
        self.user = user


async def run_discord_button_qa(bot: "CodexDiscordBot", message: discord.Message) -> str:
    channel = message.channel
    user = message.author
    lines = ["Discord button QA"]

    async def send_case_button(prompt: str) -> tuple[object, dict[str, str], str]:
        content, view = make_busy_choice_payload(
            message,
            prompt,
            target_thread_id=get_mirrored_codex_thread_id(getattr(channel, "id", None)),
            allow_steer=True,
        )
        sent_message = await channel.send(content, view=view)
        custom_ids = {
            str(getattr(item, "label", "")): str(getattr(item, "custom_id", ""))
            for item in view.children
            if isinstance(item, discord.ui.Button)
        }
        choice_id, _action = parse_busy_choice_custom_id(custom_ids["Ignore"]) or ("", "")
        return sent_message, custom_ids, choice_id

    log_line(f"button_qa_start channel={getattr(channel, 'id', '-')} user={getattr(user, 'id', '-')}")

    sent_message, custom_ids, choice_id = await send_case_button("QA button ignore smoke")
    ignore_interaction = SyntheticQAInteraction(
        bot=bot,
        channel=channel,
        message=sent_message,
        user=user,
        custom_id=custom_ids["Ignore"],
    )
    ignore_handled = await handle_persistent_busy_choice_interaction(ignore_interaction, custom_ids["Ignore"])
    ignore_record_cleared = get_busy_choice_record(choice_id) is None
    lines.append(
        "ignore: "
        + (
            "ok"
            if ignore_handled and ignore_record_cleared and ignore_interaction.response.messages == ["Ignored."]
            else "failed"
        )
    )

    sent_message, custom_ids, choice_id = await send_case_button("QA button claimed-record smoke")
    claim_busy_choice_record(choice_id)
    handled_interaction = SyntheticQAInteraction(
        bot=bot,
        channel=channel,
        message=sent_message,
        user=user,
        custom_id=custom_ids["Queue next"],
    )
    already_handled = await handle_persistent_busy_choice_interaction(
        handled_interaction,
        custom_ids["Queue next"],
    )
    lines.append(
        "claimed_record: "
        + (
            "ok"
            if already_handled
            and handled_interaction.response.messages
            == ["This Discord button is no longer active. Send the message again to get fresh controls."]
            else "failed"
        )
    )

    sent_message, custom_ids, choice_id = await send_case_button("QA button missing-record smoke")
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        conn.execute("DELETE FROM busy_choices WHERE choice_id = ?", (choice_id,))
    missing_interaction = SyntheticQAInteraction(
        bot=bot,
        channel=channel,
        message=sent_message,
        user=user,
        custom_id=custom_ids["Steer now"],
    )
    missing_handled = await handle_persistent_busy_choice_interaction(
        missing_interaction,
        custom_ids["Steer now"],
    )
    lines.append(
        "missing_record: "
        + (
            "ok"
            if missing_handled
            and missing_interaction.response.messages
            == ["This Discord button is no longer active. Send the message again to get fresh controls."]
            else "failed"
        )
    )

    sent_message, custom_ids, choice_id = await send_case_button("QA button stale cleanup smoke")
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        conn.execute("DELETE FROM busy_choices WHERE choice_id = ?", (choice_id,))
    stale_cleanup_done = await clear_stale_busy_choice_message_components(sent_message)
    lines.append(
        "stale_cleanup: "
        + (
            "ok"
            if stale_cleanup_done and not get_busy_choice_record(choice_id)
            else "failed"
        )
    )

    sent_message, custom_ids, choice_id = await send_case_button("QA button steer success smoke")
    steer_interaction = SyntheticQAInteraction(
        bot=bot,
        channel=channel,
        message=sent_message,
        user=user,
        custom_id=custom_ids["Steer now"],
    )
    watched: list[tuple[str | None, str | None]] = []

    def fake_run_steering_prompt(prompt: str, target_thread_id: str | None) -> SteeringPromptResult:
        return SteeringPromptResult(
            0,
            "[qa_delivery_verified]",
            target_thread_id=target_thread_id,
            target_ref=target_thread_id or "-",
            session_path="qa-session.jsonl",
            start_offset=0,
        )

    async def fake_stream_steering_prompt_result_to_channel(
        stream_channel: discord.abc.Messageable,
        steering_result: object,
        target_thread_id: str | None,
    ) -> bool:
        watched.append(
            (
                target_thread_id,
                getattr(steering_result, "target_thread_id", None),
            )
        )
        return True

    steer_handled = await handle_persistent_busy_choice_interaction(
        steer_interaction,
        custom_ids["Steer now"],
        steering_runner=fake_run_steering_prompt,
        steering_streamer=fake_stream_steering_prompt_result_to_channel,
    )
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        conn.execute("DELETE FROM busy_choices WHERE choice_id = ?", (choice_id,))
    lines.append(
        "steer_success: "
        + (
            "ok"
            if steer_handled
            and steer_interaction.response.deferred
            and steer_interaction.response.defer_kwargs
            and steer_interaction.response.defer_kwargs[-1].get("ephemeral") is True
            and steer_interaction.followup.messages
            and str(steer_interaction.followup.messages[0]).startswith("Steering sent")
            and watched == [
                (
                    get_mirrored_codex_thread_id(getattr(channel, "id", None)),
                    get_mirrored_codex_thread_id(getattr(channel, "id", None)),
                )
            ]
            else "failed"
        )
    )

    approval_view = ApprovalView("qa-thread")
    approval_message = await channel.send("QA approval persistent smoke", view=approval_view)
    approval_custom_ids = {
        str(getattr(item, "label", "")): str(getattr(item, "custom_id", ""))
        for item in approval_view.children
        if isinstance(item, discord.ui.Button)
    }
    approval_interaction = SyntheticQAInteraction(
        bot=bot,
        channel=channel,
        message=approval_message,
        user=user,
        custom_id=approval_custom_ids["Approve session"],
    )
    approval_submitted: list[tuple[str, str]] = []

    def fake_submit_approval(target_thread_id: str, answer: str) -> tuple[int, str]:
        approval_submitted.append((target_thread_id, answer))
        return 0, "approved"

    approval_handled = await handle_persistent_approval_interaction(
        approval_interaction,
        approval_custom_ids["Approve session"],
        approval_submitter=fake_submit_approval,
    )
    lines.append(
        "approval_persistent: "
        + (
            "ok"
            if approval_handled
            and approval_interaction.response.deferred
            and approval_interaction.followup.messages == ["Approval submitted\n\napproved"]
            and approval_submitted == [("qa-thread", "2")]
            else "failed"
        )
    )

    input_view = InputChoiceView("qa-thread", [("choice-1", "Choice one")])
    input_message = await channel.send("QA input persistent smoke", view=input_view)
    input_custom_ids = {
        str(getattr(item, "label", "")): str(getattr(item, "custom_id", ""))
        for item in input_view.children
        if isinstance(item, discord.ui.Button)
    }
    input_interaction = SyntheticQAInteraction(
        bot=bot,
        channel=channel,
        message=input_message,
        user=user,
        custom_id=input_custom_ids["Choice one"],
    )
    input_submitted: list[tuple[str, str]] = []

    def fake_submit_input(target_thread_id: str, value: str) -> tuple[int, str]:
        input_submitted.append((target_thread_id, value))
        return 0, "answered"

    input_handled = await handle_persistent_input_choice_interaction(
        input_interaction,
        input_custom_ids["Choice one"],
        input_submitter=fake_submit_input,
    )
    lines.append(
        "input_choice_persistent: "
        + (
            "ok"
            if input_handled
            and input_interaction.response.deferred
            and input_interaction.followup.messages == ["Input submitted\n\nanswered"]
            and input_submitted == [("qa-thread", "choice-1")]
            else "failed"
        )
    )

    passed = all(line.endswith(": ok") for line in lines[1:])
    lines.append(f"result: {'ok' if passed else 'failed'}")
    log_line(
        f"button_qa_done channel={getattr(channel, 'id', '-')} "
        f"user={getattr(user, 'id', '-')} result={'ok' if passed else 'failed'}"
    )
    return "\n".join(lines)


async def run_interaction_bridge_and_send(
    interaction: discord.Interaction,
    argv: list[str],
    title: str,
    failure_title: str | None = None,
) -> tuple[int, str]:
    exit_code, output = await asyncio.to_thread(run_bridge_command, argv)
    prefix = title if exit_code == 0 else f"{failure_title or title} failed (exit {exit_code})"
    log_line(
        f"slash_bridge_done command={get_interaction_command_name(interaction)} "
        f"title={title!r} exit={exit_code} argv={format_log_argv(argv)}"
    )
    await send_interaction_chunks(
        interaction,
        f"{prefix}\n\n{output or '(no output)'}",
        title=title,
        exit_code=exit_code,
    )
    return exit_code, output


async def run_discord_new_thread(
    bot: "CodexDiscordBot",
    discord_channel_id: int | None,
    prompt: str,
) -> tuple[int, str]:
    argv = ["new"]
    target_cwd = resolve_discord_new_thread_cwd(discord_channel_id)
    if target_cwd:
        argv.extend(["--cwd", target_cwd])
        log_line(f"new_thread_cwd channel={discord_channel_id} cwd={target_cwd}")
    else:
        log_line(f"new_thread_cwd channel={discord_channel_id} cwd=default")
    argv.append(prompt)

    exit_code, output = await asyncio.to_thread(run_bridge_command, argv)
    prefix = "New" if exit_code == 0 else f"New failed (exit {exit_code})"
    parts = [f"{prefix}\n\n{output or '(no output)'}"]
    if exit_code == 0:
        new_thread_id = (
            parse_bridge_output_value(output, "target_thread")
            or parse_bridge_output_value(output, "selected_thread")
        )
        if new_thread_id:
            try:
                preferred_project_channel_id = None
                try:
                    codex_thread = await asyncio.to_thread(bridge.choose_thread, new_thread_id, None)
                    preferred_project_channel_id = resolve_discord_new_thread_project_channel_id(
                        discord_channel_id,
                        get_project_key(codex_thread),
                    )
                except Exception:
                    log_line("new_thread_preferred_channel_resolve_failed\n" + traceback.format_exc())
                discord_thread = await mirror_single_codex_thread(
                    bot,
                    new_thread_id,
                    preferred_project_channel_id=preferred_project_channel_id,
                )
                log_line(
                    f"new_thread_mirrored codex_thread={new_thread_id} "
                    f"discord_thread={discord_thread.id}"
                )
                parts.append(f"Mirrored Discord thread: <#{discord_thread.id}>")
            except Exception as exc:
                log_line("new_thread_mirror_failed\n" + traceback.format_exc())
                parts.append(f"Mirror update failed: {exc}\nRun `!mirror sync` to repair.")
        else:
            log_line("new_thread_mirror_skipped reason=no_thread_id")
            parts.append("Mirror update skipped: new thread id was not found in bridge output.")
    return exit_code, "\n\n".join(parts)


async def handle_slash_new(
    bot: "CodexDiscordBot",
    interaction: discord.Interaction,
    prompt: str,
) -> None:
    log_line(
        f"slash_new_dispatch channel={interaction.channel_id} "
        f"user={interaction.user.id} prompt_len={format_log_text_len(prompt)}"
    )
    exit_code, output = await run_discord_new_thread(bot, interaction.channel_id, prompt)
    log_line(f"slash_new_done channel={interaction.channel_id} exit={exit_code}")
    await send_interaction_chunks(interaction, output, title="New", exit_code=exit_code)


async def handle_slash_ask(interaction: discord.Interaction, prompt: str) -> None:
    channel = interaction.channel
    if channel is None or not hasattr(channel, "send"):
        await send_interaction_chunks(
            interaction,
            "This Discord interaction has no messageable channel.",
            title="Ask",
        )
        return

    target_thread_id = get_mirrored_codex_thread_id(interaction.channel_id)
    target_source = "mirror" if target_thread_id else "selected"
    if target_thread_id is None:
        project_message = describe_mirrored_project_channel(interaction.channel_id)
        if project_message:
            log_line(
                f"slash_ask_blocked command={get_interaction_command_name(interaction)} "
                f"channel={interaction.channel_id} user={interaction.user.id} "
                f"reason=project_parent prompt_len={format_log_text_len(prompt)}"
            )
            await send_interaction_chunks(interaction, project_message, title="Ask")
            return

    log_line(
        f"slash_ask_dispatch command={get_interaction_command_name(interaction)} "
        f"channel={interaction.channel_id} user={interaction.user.id} "
        f"target_source={target_source} target={target_thread_id or '-'} "
        f"prompt_len={format_log_text_len(prompt)}"
    )
    await interaction.followup.send("Ask handling posted in this channel.", ephemeral=True)
    log_line(
        f"slash_ask_ack_sent command={get_interaction_command_name(interaction)} "
        f"channel={interaction.channel_id}"
    )
    source_message = SimpleNamespace(channel=channel, author=interaction.user)
    await handle_plain_ask(source_message, prompt, target_thread_id=target_thread_id)  # type: ignore[arg-type]


async def get_mirror_guild(bot: CodexDiscordBot) -> discord.Guild:
    guild = bot.get_guild(bot.guild_id) if bot.guild_id else (bot.guilds[0] if bot.guilds else None)
    if guild is None:
        raise RuntimeError("Discord guild is not available yet.")
    return guild


async def get_or_create_mirror_category(guild: discord.Guild) -> discord.CategoryChannel:
    for category in guild.categories:
        if category.name == "Codex":
            return category
    return await guild.create_category("Codex", reason="Codex mirror setup")


async def get_or_create_project_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    project_key: str,
    project_name: str,
) -> discord.TextChannel:
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        row = conn.execute(
            "SELECT discord_channel_id FROM mirror_projects WHERE project_key = ?",
            (project_key,),
        ).fetchone()

    if row:
        channel = guild.get_channel(int(row[0]))
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await guild.fetch_channel(int(row[0]))
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except Exception:
            pass

    base_name = normalize_discord_name(project_name, prefix="codex-", max_len=80)
    channel_name = base_name
    digest = hashlib.sha1(project_key.encode("utf-8", errors="ignore")).hexdigest()[:6]
    existing_names = {channel.name for channel in guild.text_channels}
    if channel_name in existing_names:
        channel_name = normalize_discord_name(f"{base_name}-{digest}", max_len=90)

    channel = await guild.create_text_channel(
        channel_name,
        category=category,
        topic=f"Codex project mirror: {project_name}",
        reason="Codex project mirror sync",
    )
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO mirror_projects
                (project_key, project_name, discord_channel_id, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (project_key, project_name, int(channel.id), time.time()),
        )
    return channel


async def get_or_create_thread_channel(
    codex_thread: bridge.ThreadInfo,
    project_key: str,
    project_channel: discord.TextChannel,
) -> discord.Thread:
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        row = conn.execute(
            "SELECT discord_channel_id, discord_thread_id FROM mirror_threads WHERE codex_thread_id = ?",
            (codex_thread.id,),
        ).fetchone()

    if row:
        channel_id = int(row[0])
        thread_id = int(row[1])
        if channel_id == int(project_channel.id):
            cached = project_channel.guild.get_thread(thread_id)
            if isinstance(cached, discord.Thread):
                return cached
            try:
                fetched = await project_channel.guild.fetch_channel(thread_id)
                if isinstance(fetched, discord.Thread):
                    return fetched
            except Exception:
                pass

    title = bridge.get_thread_ui_name(codex_thread.id, codex_thread) or codex_thread.title
    thread_name = truncate_discord_title(title, f"codex-{codex_thread.id[:8]}", max_len=90)
    discord_thread = await project_channel.create_thread(
        name=thread_name,
        type=discord.ChannelType.public_thread,
        auto_archive_duration=10080,
        reason="Codex thread mirror sync",
    )
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO mirror_threads
                (codex_thread_id, project_key, thread_title, discord_channel_id, discord_thread_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                codex_thread.id,
                project_key,
                thread_name,
                int(project_channel.id),
                int(discord_thread.id),
                time.time(),
            ),
        )
    return discord_thread


async def delete_stale_discord_threads(
    guild: discord.Guild,
    stale_rows: list[tuple[object, object, object]],
) -> dict[str, object]:
    deleted = 0
    missing = 0
    failed = 0
    errors: list[str] = []

    for codex_thread_id, discord_thread_id, thread_title in stale_rows:
        try:
            thread_id = int(discord_thread_id)
        except (TypeError, ValueError):
            missing += 1
            continue

        try:
            channel = guild.get_thread(thread_id)
            if channel is None:
                fetched = await guild.fetch_channel(thread_id)
                channel = fetched if isinstance(fetched, discord.Thread) else None
            if channel is None:
                missing += 1
                continue
            await channel.delete(
                reason=f"Codex mirror cleanup for stale thread {str(codex_thread_id)[:8]}"
            )
            deleted += 1
        except discord.NotFound:
            missing += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            failed += 1
            if len(errors) < 3:
                label = str(thread_title or codex_thread_id or thread_id)[:80]
                errors.append(f"{label}: {exc}")

    return {
        "deleted": deleted,
        "missing": missing,
        "failed": failed,
        "errors": errors,
    }


async def cleanup_orphan_discord_threads(
    project_channels: list[discord.TextChannel],
    known_thread_ids: set[int],
    bot_user_id: int | None,
) -> dict[str, object]:
    deleted = 0
    skipped = 0
    failed = 0
    seen_thread_ids: set[int] = set()
    errors: list[str] = []

    async def maybe_delete_thread(thread: discord.Thread) -> None:
        nonlocal deleted, skipped, failed
        if int(thread.id) in seen_thread_ids:
            return
        seen_thread_ids.add(int(thread.id))
        if int(thread.id) in known_thread_ids:
            skipped += 1
            return
        if bot_user_id is not None and thread.owner_id not in {None, bot_user_id}:
            skipped += 1
            return
        try:
            await thread.delete(reason="Codex mirror cleanup for orphan Discord thread")
            deleted += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            failed += 1
            if len(errors) < 3:
                errors.append(f"{thread.name}: {exc}")

    for channel in project_channels:
        for thread in list(channel.threads):
            await maybe_delete_thread(thread)
        try:
            async with asyncio.timeout(5):
                async for thread in channel.archived_threads(limit=50):
                    await maybe_delete_thread(thread)
        except TimeoutError:
            failed += 1
            if len(errors) < 3:
                errors.append(f"{channel.name}/archived_threads: timed out")
        except (discord.Forbidden, discord.HTTPException) as exc:
            failed += 1
            if len(errors) < 3:
                errors.append(f"{channel.name}/archived_threads: {exc}")

    return {
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    }


async def delete_stale_project_channels(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    stale_rows: list[tuple[object, object, object]],
) -> dict[str, object]:
    deleted = 0
    missing = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    for project_key, project_name, discord_channel_id in stale_rows:
        try:
            channel_id = int(discord_channel_id)
        except (TypeError, ValueError):
            missing += 1
            continue

        try:
            channel = guild.get_channel(channel_id)
            if channel is None:
                fetched = await guild.fetch_channel(channel_id)
                channel = fetched if isinstance(fetched, discord.TextChannel) else None
            if channel is None:
                missing += 1
                continue
            if not isinstance(channel, discord.TextChannel):
                skipped += 1
                continue

            topic = getattr(channel, "topic", "") or ""
            parent_id = getattr(channel, "category_id", None)
            is_mirror_channel = parent_id == int(category.id) or topic.startswith("Codex project mirror:")
            if not is_mirror_channel:
                skipped += 1
                continue

            await channel.delete(
                reason=f"Codex mirror cleanup for stale project {str(project_key)[:80]}"
            )
            deleted += 1
        except discord.NotFound:
            missing += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            failed += 1
            if len(errors) < 3:
                label = str(project_name or project_key or channel_id)[:80]
                errors.append(f"{label}: {exc}")

    return {
        "deleted": deleted,
        "missing": missing,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    }


async def sync_codex_mirror(bot: CodexDiscordBot, *, limit: int = 30) -> str:
    log_line(f"mirror_sync_start limit={limit}")
    guild = await get_mirror_guild(bot)
    category = await get_or_create_mirror_category(guild)
    threads = await asyncio.to_thread(bridge.load_recent_threads, limit)
    threads = filter_mirrorable_threads(threads)
    if not threads:
        return "No Codex threads found."
    all_active_threads = await asyncio.to_thread(bridge.load_recent_threads, 0)
    all_active_threads = filter_mirrorable_threads(all_active_threads)

    created_or_seen_projects: dict[str, discord.TextChannel] = {}
    mirrored = 0
    for codex_thread in reversed(threads):
        project_key = get_project_key(codex_thread)
        project_name = get_project_name(codex_thread)
        channel = created_or_seen_projects.get(project_key)
        if channel is None:
            channel = await get_or_create_project_channel(guild, category, project_key, project_name)
            created_or_seen_projects[project_key] = channel
        await get_or_create_thread_channel(codex_thread, project_key, channel)
        mirrored += 1

    valid_thread_ids = {thread.id for thread in all_active_threads}
    valid_project_keys = {get_project_key(thread) for thread in all_active_threads}
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        if valid_thread_ids:
            stale_threads = conn.execute(
                """
                SELECT codex_thread_id, discord_thread_id, thread_title
                FROM mirror_threads
                WHERE codex_thread_id NOT IN ({})
                """.format(",".join("?" for _ in valid_thread_ids)),
                tuple(valid_thread_ids),
            ).fetchall()
        else:
            stale_threads = conn.execute(
                """
                SELECT codex_thread_id, discord_thread_id, thread_title
                FROM mirror_threads
                """
            ).fetchall()
        if valid_project_keys:
            stale_projects = conn.execute(
                """
                SELECT project_key, project_name, discord_channel_id FROM mirror_projects
                WHERE project_key NOT IN ({})
                """.format(",".join("?" for _ in valid_project_keys)),
                tuple(valid_project_keys),
            ).fetchall()
        else:
            stale_projects = conn.execute(
                "SELECT project_key, project_name, discord_channel_id FROM mirror_projects"
            ).fetchall()

    stale_cleanup = await delete_stale_discord_threads(guild, stale_threads)
    stale_project_cleanup = await delete_stale_project_channels(guild, category, stale_projects)

    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        if valid_thread_ids:
            conn.execute(
                """
                DELETE FROM mirror_threads
                WHERE codex_thread_id NOT IN ({})
                """.format(",".join("?" for _ in valid_thread_ids)),
                tuple(valid_thread_ids),
            )
        if valid_project_keys:
            conn.execute(
                """
                DELETE FROM mirror_projects
                WHERE project_key NOT IN ({})
                """.format(",".join("?" for _ in valid_project_keys)),
                tuple(valid_project_keys),
            )
        known_thread_ids = {
            int(row[0])
            for row in conn.execute("SELECT discord_thread_id FROM mirror_threads").fetchall()
            if row[0]
        }
        project_channel_ids = [
            int(row[0])
            for row in conn.execute("SELECT discord_channel_id FROM mirror_projects").fetchall()
            if row[0]
        ]

    project_channels: list[discord.TextChannel] = []
    for channel_id in project_channel_ids:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                channel = None
        if isinstance(channel, discord.TextChannel):
            project_channels.append(channel)

    orphan_cleanup = await cleanup_orphan_discord_threads(
        project_channels,
        known_thread_ids,
        bot.user.id if bot.user else None,
    )
    log_line(
        "mirror_sync_done "
        f"mirrored={mirrored} stale_rows={len(stale_threads)} "
        f"stale_deleted={stale_cleanup['deleted']} orphan_deleted={orphan_cleanup['deleted']} "
        f"orphan_failed={orphan_cleanup['failed']} "
        f"stale_projects_deleted={stale_project_cleanup['deleted']}"
    )

    return "\n".join(
        [
            "Mirror sync complete.",
            f"projects: {len(created_or_seen_projects)}",
            f"threads: {mirrored}",
            f"stale_threads_removed: {len(stale_threads)}",
            f"stale_discord_threads_deleted: {stale_cleanup['deleted']}",
            f"stale_discord_threads_missing: {stale_cleanup['missing']}",
            f"stale_discord_threads_failed: {stale_cleanup['failed']}",
            f"orphan_discord_threads_deleted: {orphan_cleanup['deleted']}",
            f"orphan_discord_threads_skipped: {orphan_cleanup['skipped']}",
            f"orphan_discord_threads_failed: {orphan_cleanup['failed']}",
            f"stale_projects_removed: {len(stale_projects)}",
            f"stale_project_channels_deleted: {stale_project_cleanup['deleted']}",
            f"stale_project_channels_missing: {stale_project_cleanup['missing']}",
            f"stale_project_channels_skipped: {stale_project_cleanup['skipped']}",
            f"stale_project_channels_failed: {stale_project_cleanup['failed']}",
            f"database: {MIRROR_DB_PATH}",
            *(
                ["", "Discord stale cleanup errors:", *[f"- {error}" for error in stale_cleanup["errors"]]]
                if stale_cleanup["errors"]
                else []
            ),
            *(
                ["", "Discord orphan cleanup errors:", *[f"- {error}" for error in orphan_cleanup["errors"]]]
                if orphan_cleanup["errors"]
                else []
            ),
            *(
                [
                    "",
                    "Discord stale project cleanup errors:",
                    *[f"- {error}" for error in stale_project_cleanup["errors"]],
                ]
                if stale_project_cleanup["errors"]
                else []
            ),
        ]
    )


def refresh_codex_bridge_session_state() -> dict[str, object]:
    session_index_count = bridge.sync_session_index_with_state()
    threads = bridge.load_recent_threads(limit=0)
    selected_before = bridge.get_selected_thread_id()
    selected_thread = next((thread for thread in threads if thread.id == selected_before), None)
    if selected_thread is not None:
        selected_action = "kept"
    elif threads:
        selected_thread = bridge.choose_thread(None, None)
        bridge.set_selected_thread_id(selected_thread.id)
        selected_action = "initialized" if not selected_before else "stale_replaced"
    else:
        if selected_before:
            bridge.set_selected_thread_id(None)
        selected_action = "cleared" if selected_before else "none"

    selected_ref = ""
    if selected_thread is not None:
        try:
            selected_ref = bridge.get_thread_workspace_ref(selected_thread, threads)
        except Exception:
            selected_ref = bridge.get_thread_workspace_name(selected_thread)
    return {
        "session_index_count": session_index_count,
        "thread_count": len(threads),
        "selected_before": selected_before or "-",
        "selected_thread_id": selected_thread.id if selected_thread else "-",
        "selected_ref": selected_ref or "-",
        "selected_action": selected_action,
    }


async def refresh_discord_bridge_session(bot: CodexDiscordBot, *, limit: int = 30) -> str:
    bounded_limit = max(1, min(100, int(limit)))
    state = await asyncio.to_thread(refresh_codex_bridge_session_state)
    mirror_output = await sync_codex_mirror(bot, limit=bounded_limit)
    return "\n".join(
        [
            "Discord bridge sync complete.",
            f"session_index_threads: {state['session_index_count']}",
            f"codex_threads: {state['thread_count']}",
            f"selected_action: {state['selected_action']}",
            f"selected_thread: {state['selected_ref']} ({state['selected_thread_id']})",
            f"selected_before: {state['selected_before']}",
            "",
            mirror_output,
        ]
    )


async def mirror_single_codex_thread(
    bot: CodexDiscordBot,
    thread_id: str,
    *,
    preferred_project_channel_id: int | None = None,
) -> discord.Thread:
    guild = await get_mirror_guild(bot)
    category = await get_or_create_mirror_category(guild)
    codex_thread = await asyncio.to_thread(bridge.choose_thread, thread_id, None)
    project_key = get_project_key(codex_thread)
    project_name = get_project_name(codex_thread)
    project_channel = None
    if preferred_project_channel_id is not None:
        candidate = guild.get_channel(int(preferred_project_channel_id))
        if not isinstance(candidate, discord.TextChannel):
            try:
                fetched = await guild.fetch_channel(int(preferred_project_channel_id))
                if isinstance(fetched, discord.TextChannel):
                    candidate = fetched
            except Exception:
                candidate = None
        if isinstance(candidate, discord.TextChannel):
            project_channel = candidate
            init_mirror_db()
            with sqlite3.connect(MIRROR_DB_PATH) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO mirror_projects
                        (project_key, project_name, discord_channel_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project_key, project_name, int(project_channel.id), time.time()),
                )
            log_line(
                f"single_thread_mirror_preferred_channel codex_thread={thread_id} "
                f"project_channel={project_channel.id}"
            )
    if project_channel is None:
        project_channel = await get_or_create_project_channel(guild, category, project_key, project_name)
    return await get_or_create_thread_channel(codex_thread, project_key, project_channel)


def build_mirror_list(limit: int = 30) -> str:
    return discord_mirror_status.build_mirror_list(
        limit,
        db_path=MIRROR_DB_PATH,
        init_mirror_db_func=init_mirror_db,
        bridge_module=bridge,
    )


def build_mirror_check() -> str:
    return discord_mirror_status.build_mirror_check(
        db_path=MIRROR_DB_PATH,
        init_mirror_db_func=init_mirror_db,
        bridge_module=bridge,
        filter_mirrorable_threads_func=filter_mirrorable_threads,
        get_project_key_func=get_project_key,
        get_project_name_func=get_project_name,
    )


def format_context_usage_line(thread: bridge.ThreadInfo) -> str:
    return discord_context.format_context_usage_line(thread, bridge_module=bridge)


def build_context_warning(target_thread_id: str | None) -> str:
    return discord_context.build_context_warning(
        target_thread_id,
        bridge_module=bridge,
        resolve_target_ref_func=resolve_target_ref,
        log_func=log_line,
    )


def build_context_message(channel_id: int | None = None, *, all_threads: bool = False, limit: int = 10) -> str:
    return discord_context.build_context_message(
        channel_id,
        all_threads=all_threads,
        limit=limit,
        bridge_module=bridge,
        get_mirrored_codex_thread_id_func=get_mirrored_codex_thread_id,
        resolve_selected_target_func=resolve_selected_target,
    )


def parse_event_timestamp(value: object) -> datetime | None:
    return discord_context.parse_event_timestamp(value)


def format_window_minutes(value: object) -> str:
    return discord_context.format_window_minutes(value, bridge_module=bridge)


def format_rate_limit_reset(value: object) -> str:
    return discord_context.format_rate_limit_reset(value, bridge_module=bridge)


def format_rate_limit_line(label: str, value: object) -> str:
    return discord_context.format_rate_limit_line(
        label,
        value,
        bridge_module=bridge,
        format_percent_func=format_percent,
    )


def build_weekly_usage_message(days: int = 7) -> str:
    return discord_context.build_weekly_usage_message(
        days,
        bridge_module=bridge,
        format_percent_func=format_percent,
    )
    return "\n".join(lines)


def build_where_message(channel_id: int | None) -> str:
    target_thread_id = get_mirrored_codex_thread_id(channel_id)
    if not target_thread_id:
        project_message = describe_mirrored_project_channel(channel_id)
        return project_message or "This Discord channel is not mapped to a Codex thread."
    try:
        thread = bridge.choose_thread(target_thread_id, None)
        busy_state = bridge.get_thread_busy_state(thread, allow_resume=True)
        return "\n".join(
            [
                "Mapped Codex thread",
                f"thread_ref: {bridge.get_thread_workspace_ref(thread)}",
                f"thread_id: {thread.id}",
                f"title: {bridge.get_thread_ui_name(thread.id, thread) or thread.title or '-'}",
                f"cwd: {thread.cwd or '-'}",
                f"state: {busy_state or 'idle'}",
                format_context_usage_line(thread),
                f"tokens_used_total: {bridge.format_token_k(thread.tokens_used)}",
            ]
        )
    except Exception as exc:
        return f"Mapped Codex thread: {target_thread_id}\nERROR: {exc}"


def get_busy_choice_counts(now: float | None = None) -> tuple[int, int]:
    return discord_store.get_busy_choice_counts(MIRROR_DB_PATH, now=now)


def get_persistent_component_claim_counts(now: float | None = None) -> tuple[int, int]:
    return discord_store.get_persistent_component_claim_counts(MIRROR_DB_PATH, now=now)


def format_discord_id_list(values: set[int], *, limit: int = 8) -> str:
    return discord_diagnostics.format_discord_id_list(values, limit=limit)


def build_discord_doctor_message(bot: CodexDiscordBot, channel_id: int | None) -> str:
    return discord_diagnostics.build_discord_doctor_message(
        bot,
        channel_id,
        empty_content_notice_count=len(EMPTY_CONTENT_NOTICE_LAST_SENT),
        get_mirrored_codex_thread_id_func=get_mirrored_codex_thread_id,
        get_mirror_project_for_channel_func=get_mirror_project_for_channel,
        get_busy_choice_counts_func=get_busy_choice_counts,
        get_persistent_component_claim_counts_func=get_persistent_component_claim_counts,
        build_mirror_check_func=build_mirror_check,
        get_discord_log_markers_func=get_discord_log_markers,
        get_recent_discord_hook_events_func=get_recent_discord_hook_events,
        discord_qa_commands_enabled_func=discord_qa_commands_enabled,
    )


def format_discord_message_type(message: object) -> str:
    return discord_diagnostics.format_discord_message_type(message)


def format_discord_message_created_at(message: object) -> str:
    return discord_diagnostics.format_discord_message_created_at(message)


async def build_discord_channel_history_lines(channel: object | None, *, limit: int = 5) -> list[str]:
    return await discord_diagnostics.build_discord_channel_history_lines(
        channel,
        limit=limit,
        format_log_text_len_func=format_log_text_len,
    )


async def resolve_discord_history_channel(bot: object, channel_id: int) -> tuple[object | None, str]:
    return await discord_diagnostics.resolve_discord_history_channel(bot, channel_id)


async def build_discord_tracked_target_user_history_lines(
    bot: object,
    *,
    per_target_limit: int = 5,
    target_limit: int = 50,
) -> list[str]:
    return await discord_diagnostics.build_discord_tracked_target_user_history_lines(
        bot,
        get_startup_probe_targets_func=get_startup_probe_targets,
        format_log_text_len_func=format_log_text_len,
        per_target_limit=per_target_limit,
        target_limit=target_limit,
    )


async def build_discord_doctor_message_with_history(
    bot: CodexDiscordBot,
    channel_id: int | None,
    channel: object | None,
) -> str:
    return await discord_diagnostics.build_discord_doctor_message_with_history(
        bot,
        channel_id,
        channel,
        build_discord_doctor_message_func=build_discord_doctor_message,
        build_discord_channel_history_lines_func=build_discord_channel_history_lines,
        build_discord_tracked_target_user_history_lines_func=build_discord_tracked_target_user_history_lines,
    )



async def build_runners_message() -> str:
    return await discord_runtime.build_runners_message(
        THREAD_RUNNERS,
        THREAD_RUNNERS_LOCK,
        resolve_target_ref_func=resolve_target_ref,
    )


def resolve_target_ref(target_thread_id: str | None) -> tuple[str | None, str]:
    return discord_thread_state.resolve_target_ref(
        target_thread_id,
        bridge_module=bridge,
        resolve_selected_target_func=resolve_selected_target,
    )


def get_interactive_state_for_thread(target_thread_id: str | None) -> tuple[str, str | None, str]:
    return discord_thread_state.get_interactive_state_for_thread(
        target_thread_id,
        bridge_module=bridge,
        resolve_target_ref_func=resolve_target_ref,
        get_selected_interactive_state_func=get_selected_interactive_state,
        state_none=INTERACTIVE_STATE_NONE,
        state_input=INTERACTIVE_STATE_INPUT,
        state_approval=INTERACTIVE_STATE_APPROVAL,
    )


def get_busy_state_for_thread(target_thread_id: str | None) -> tuple[str, str | None, str]:
    return discord_thread_state.get_busy_state_for_thread(
        target_thread_id,
        bridge_module=bridge,
        resolve_target_ref_func=resolve_target_ref,
        log_func=log_line,
    )


def normalize_runner_key(target_thread_id: str | None) -> str:
    return discord_runtime.normalize_runner_key(target_thread_id)


def mark_steering_handoff(target_thread_id: str | None) -> float:
    return discord_runtime.mark_steering_handoff(STEERING_HANDOFFS, target_thread_id)


def had_steering_handoff_since(target_thread_id: str | None, started_at: float) -> bool:
    return discord_runtime.had_steering_handoff_since(
        STEERING_HANDOFFS,
        target_thread_id,
        started_at,
    )


def register_discord_relay(target_thread_id: str | None) -> int:
    return discord_runtime.register_discord_relay(
        ACTIVE_DISCORD_RELAY_GENERATIONS,
        target_thread_id,
    )


def is_discord_relay_stale(target_thread_id: str | None, generation: int) -> bool:
    return discord_runtime.is_discord_relay_stale(
        ACTIVE_DISCORD_RELAY_GENERATIONS,
        target_thread_id,
        generation,
    )


async def get_thread_runner(target_thread_id: str | None) -> dict[str, object]:
    return await discord_runner.get_thread_runner(
        target_thread_id,
        runners=THREAD_RUNNERS,
        runners_lock=THREAD_RUNNERS_LOCK,
        normalize_runner_key_func=normalize_runner_key,
    )


async def is_thread_runner_busy(target_thread_id: str | None) -> bool:
    return await discord_runner.is_thread_runner_busy(
        target_thread_id,
        get_thread_runner_func=get_thread_runner,
    )


async def wait_for_codex_thread_idle(
    target_thread_id: str | None,
    *,
    timeout_sec: float = 3600.0,
    poll_sec: float = 5.0,
) -> tuple[str, str | None, str]:
    return await discord_runner.wait_for_codex_thread_idle(
        target_thread_id,
        get_busy_state_func=get_busy_state_for_thread,
        timeout_sec=timeout_sec,
        poll_sec=poll_sec,
    )


async def enqueue_thread_ask(
    channel: discord.abc.Messageable,
    prompt: str,
    target_thread_id: str | None,
    *,
    queued: bool = False,
    ack_sent: bool = False,
    source_message: discord.Message | None = None,
) -> int:
    return await discord_runner.enqueue_thread_ask(
        channel,
        prompt,
        target_thread_id,
        queued=queued,
        ack_sent=ack_sent,
        source_message=source_message,
        get_thread_runner_func=get_thread_runner,
        thread_runner_loop_func=thread_runner_loop,
    )


async def report_thread_runner_job_failed(job: object, target_thread_id: str | None) -> None:
    await discord_runner.report_thread_runner_job_failed(
        job,
        target_thread_id,
        log_func=log_line,
    )


async def thread_runner_loop(target_thread_id: str | None) -> None:
    await discord_runner.thread_runner_loop(
        target_thread_id,
        runners=THREAD_RUNNERS,
        runners_lock=THREAD_RUNNERS_LOCK,
        normalize_runner_key_func=normalize_runner_key,
        get_thread_runner_func=get_thread_runner,
        get_busy_state_func=get_busy_state_for_thread,
        wait_for_idle_func=wait_for_codex_thread_idle,
        run_prompt_and_send_func=run_prompt_and_send,
        report_job_failed_func=report_thread_runner_job_failed,
        log_func=log_line,
    )


async def run_prompt_and_send(
    channel: discord.abc.Messageable,
    prompt: str,
    *,
    queued: bool = False,
    ack_sent: bool = False,
    source_message: discord.Message | None = None,
    target_thread_id: str | None = None,
) -> None:
    if not ack_sent:
        await channel.send(build_ask_start_message(prompt, queued=queued))
    started_at = time.monotonic()
    target_thread_id, target_ref = resolve_target_ref(target_thread_id)
    relay = DiscordAskRelay(
        asyncio.get_running_loop(),
        channel,
        target_thread_id,
        target_ref,
        suppress_after_steering_since=started_at,
    )
    async with channel_typing(channel, context="ask_stream"):
        exit_code, output = await asyncio.to_thread(
            run_ask_stream,
            prompt,
            relay,
            target_thread_id=target_thread_id,
        )
    log_line(
        f"ask_stream_done exit={exit_code} target={target_thread_id or '-'} "
        f"sent_live={relay.sent_live} final={relay.saw_final} aborted={relay.saw_aborted} "
        f"timeout={relay.saw_timeout} output_len={format_log_text_len(output)}"
    )
    if is_selected_thread_busy_error(exit_code, output):
        log_line(
            f"ask_stream_busy_failure target={target_thread_id or '-'} "
            f"source_message={'yes' if has_busy_choice_source(source_message) else 'no'}"
        )
        if has_busy_choice_source(source_message):
            await send_busy_choice_message(
                channel,
                source_message,
                prompt,
                target_thread_id=target_thread_id,
                allow_steer=True,
                reason="late_busy_failure",
            )
            return
        await send_chunks(
            channel,
            "\n".join(
                [
                    "This Codex thread is already working.",
                    "",
                    "Send the message again when the thread is idle, or use the mapped Discord thread so I can show steering controls.",
                ]
            ),
        )
        return
    if (
        exit_code == 0
        and not relay.saw_final
        and not relay.saw_aborted
        and not relay.saw_timeout
        and had_steering_handoff_since(target_thread_id, started_at)
    ):
        log_line(
            f"ask_stream_suppressed_after_steering target={target_thread_id or '-'} "
            f"sent_live={relay.sent_live} output_len={format_log_text_len(output)}"
        )
        return
    if relay.sent_live:
        if exit_code == 0 and not relay.saw_aborted:
            if relay.saw_final:
                await channel.send("Done.")
            else:
                log_line(
                    f"ask_stream_no_final_fallback target={target_thread_id or '-'} "
                    f"output_len={format_log_text_len(output)}"
                )
                await send_chunks(channel, f"Ask finished\n\n{output or '(no final answer captured)'}")
        elif not relay.saw_aborted and not relay.saw_timeout:
            await send_chunks(channel, f"Ask failed (exit {exit_code})\n\n{output or '(no output)'}")
        return
    title = "Ask finished" if exit_code == 0 else f"Ask failed (exit {exit_code})"
    await send_chunks(channel, f"{title}\n\n{output or '(no output)'}")


def is_selected_thread_busy_error(exit_code: int, output: str) -> bool:
    return discord_busy.is_selected_thread_busy_error(exit_code, output)


def has_busy_choice_source(source_message: object) -> bool:
    return discord_busy.has_busy_choice_source(source_message)


async def run_prompt_flow(
    channel: discord.abc.Messageable,
    prompt: str,
    *,
    queued: bool = False,
    source_message: discord.Message | None = None,
    target_thread_id: str | None = None,
) -> None:
    runner = await get_thread_runner(target_thread_id)
    queue = runner["queue"]
    if bool(runner.get("active")) or (isinstance(queue, asyncio.Queue) and queue.qsize() > 0):
        position = await enqueue_thread_ask(
            channel,
            prompt,
            target_thread_id,
            queued=True,
            source_message=source_message,
        )
        warning = build_context_warning(target_thread_id)
        await send_chunks(
            channel,
            "\n\n".join(
                part
                for part in [
                    f"Queued in this Codex thread at position {position}.",
                    warning,
                ]
                if part
            )
        )
        return
    warning = build_context_warning(target_thread_id)
    if warning:
        await send_chunks(channel, warning)
    await channel.send(build_ask_start_message(prompt, queued=queued))
    await enqueue_thread_ask(
        channel,
        prompt,
        target_thread_id,
        queued=queued,
        ack_sent=True,
        source_message=source_message,
    )


def build_busy_choice_message(prompt: str, target_thread_id: str | None) -> str:
    return discord_busy.build_busy_choice_message(
        prompt,
        target_thread_id,
        discord_max_len=DISCORD_MAX_LEN,
        fit_single_message_func=fit_single_message,
    )


def make_busy_choice_payload(
    source_message: object,
    prompt: str,
    *,
    target_thread_id: str | None,
    allow_steer: bool,
) -> tuple[str, BusyChoiceView]:
    return discord_busy.make_busy_choice_payload(
        source_message,
        prompt,
        target_thread_id=target_thread_id,
        allow_steer=allow_steer,
        build_busy_choice_message_func=build_busy_choice_message,
        make_busy_choice_view_func=make_busy_choice_view,
    )


async def send_busy_choice_message(
    channel: discord.abc.Messageable,
    source_message: object,
    prompt: str,
    *,
    target_thread_id: str | None,
    allow_steer: bool,
    reason: str,
) -> bool:
    try:
        content, view = make_busy_choice_payload(
            source_message,
            prompt,
            target_thread_id=target_thread_id,
            allow_steer=allow_steer,
        )
        await channel.send(content, view=view)
        log_busy_choice_sent(reason, target_thread_id, prompt)
        return True
    except Exception:
        log_line(
            f"busy_choice_send_failed reason={reason.replace(chr(10), ' ')[:80]} "
            f"target={target_thread_id or '-'} prompt_len={format_log_text_len(prompt)}\n"
            + traceback.format_exc()
        )
    try:
        await send_chunks(
            channel,
            "\n\n".join(
                [
                    build_busy_choice_message(prompt, target_thread_id),
                    "Discord could not attach steering buttons. Send the message again when the thread is idle, or use `/ask` from the mapped Discord thread.",
                ]
            ),
        )
        log_line(
            f"busy_choice_fallback_sent reason={reason.replace(chr(10), ' ')[:80]} "
            f"target={target_thread_id or '-'}"
        )
        return False
    except Exception:
        log_line(
            f"busy_choice_fallback_failed reason={reason.replace(chr(10), ' ')[:80]} "
            f"target={target_thread_id or '-'}\n"
            + traceback.format_exc()
        )
        raise


def log_busy_choice_sent(reason: str, target_thread_id: str | None, prompt: str) -> None:
    discord_busy.log_busy_choice_sent(
        reason,
        target_thread_id,
        prompt,
        log_func=log_line,
        format_log_text_len_func=format_log_text_len,
    )


def make_busy_choice_view(
    message: discord.Message,
    prompt: str,
    *,
    target_thread_id: str | None,
    allow_steer: bool = True,
) -> BusyChoiceView:
    choice_id = create_busy_choice_record(
        message,
        prompt,
        target_thread_id,
        allow_steer=allow_steer,
    )
    return BusyChoiceView(
        message,
        prompt,
        target_thread_id=target_thread_id,
        allow_steer=allow_steer,
        choice_id=choice_id,
    )


async def handle_plain_ask(
    message: discord.Message,
    prompt: str,
    *,
    target_thread_id: str | None = None,
) -> None:
    interactive_state, resolved_thread_id, target_ref = get_interactive_state_for_thread(target_thread_id)
    if interactive_state and resolved_thread_id:
        normalized_reply = normalize_interactive_text_reply(interactive_state, prompt)
        if normalized_reply is None:
            prompt_text = "Pending approval" if interactive_state == INTERACTIVE_STATE_APPROVAL else "Pending input"
            await send_interactive_prompt(
                message.channel,
                resolved_thread_id,
                target_ref,
                interactive_state,
                prompt_text,
                [],
            )
            return
        await submit_interactive_reply(
            message.channel,
            resolved_thread_id,
            target_ref,
            interactive_state,
            normalized_reply,
        )
        return

    busy_state, busy_thread_id, busy_ref = get_busy_state_for_thread(target_thread_id)
    if busy_state != "idle":
        if busy_state == INTERACTIVE_STATE_INPUT and busy_thread_id:
            await send_interactive_prompt(
                message.channel,
                busy_thread_id,
                busy_ref,
                INTERACTIVE_STATE_INPUT,
                "Pending input",
                [],
            )
            return
        if busy_state == INTERACTIVE_STATE_APPROVAL and busy_thread_id:
            await send_interactive_prompt(
                message.channel,
                busy_thread_id,
                busy_ref,
                INTERACTIVE_STATE_APPROVAL,
                "Pending approval",
                [],
            )
            return
        await send_busy_choice_message(
            message.channel,
            message,
            prompt,
            target_thread_id=target_thread_id,
            allow_steer=True,
            reason="codex_busy_preflight",
        )
        return

    if await is_thread_runner_busy(target_thread_id):
        allow_steer = True
        await send_busy_choice_message(
            message.channel,
            message,
            prompt,
            target_thread_id=target_thread_id,
            allow_steer=allow_steer,
            reason="runner_busy_preflight",
        )
        return

    await run_prompt_flow(
        message.channel,
        prompt,
        source_message=message,
        target_thread_id=target_thread_id,
    )


async def submit_interactive_reply(
    channel: discord.abc.Messageable,
    target_thread_id: str,
    target_ref: str,
    state: str,
    answer: str,
) -> None:
    if state == INTERACTIVE_STATE_APPROVAL:
        watch_result = make_post_approval_watch_result(target_thread_id)
        exit_code, output = await asyncio.to_thread(submit_approval_reply, target_thread_id, answer)
        log_line(
            f"approval_reply_done exit={exit_code} target={target_thread_id} "
            f"answer_len={format_log_text_len(answer)} "
            f"output_len={format_log_text_len(output)}"
        )
        title = "Approval submitted" if exit_code == 0 else f"Approval failed (exit {exit_code})"
        await send_chunks(channel, f"{title}\n\n{output or '(no output)'}")
        if exit_code == 0:
            await stream_post_approval_result_to_channel(channel, watch_result, target_thread_id)
        return
    if state == INTERACTIVE_STATE_INPUT:
        exit_code, output = await asyncio.to_thread(submit_input_reply, target_thread_id, answer)
        log_line(
            f"input_reply_done exit={exit_code} target={target_thread_id} "
            f"answer_len={format_log_text_len(answer)} "
            f"output_len={format_log_text_len(output)}"
        )
        title = "Input submitted" if exit_code == 0 else f"Input failed (exit {exit_code})"
        await send_chunks(channel, f"{title}\n\n{output or '(no output)'}")
        return


class ApprovalView(discord.ui.View):
    def __init__(self, target_thread_id: str) -> None:
        super().__init__(timeout=1800)
        self.target_thread_id = target_thread_id
        self.claimed = False
        self.assign_persistent_custom_ids()

    def assign_persistent_custom_ids(self) -> None:
        labels_to_answers = {
            "Approve": "1",
            "Approve session": "2",
            "Reject": "3",
            "Cancel": "cancel",
        }
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            answer = labels_to_answers.get(str(item.label))
            if answer:
                item.custom_id = format_approval_custom_id(self.target_thread_id, answer)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_discord_user_allowed(interaction.user.id):
            return True
        log_line(f"approval_button_denied user={interaction.user.id} target={self.target_thread_id}")
        await interaction.response.send_message("This user is not allowed.", ephemeral=True)
        return False

    async def _submit(self, interaction: discord.Interaction, answer: str) -> None:
        if self.claimed:
            await interaction.response.send_message("This approval choice was already handled.", ephemeral=True)
            return
        self.claimed = True
        self.disable_all_items()
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        log_line(
            f"approval_button user={interaction.user.id} "
            f"answer_len={format_log_text_len(answer)}"
        )
        watch_result = make_post_approval_watch_result(self.target_thread_id)
        exit_code, output = await asyncio.to_thread(submit_approval_reply, self.target_thread_id, answer)
        log_line(
            f"approval_button_done exit={exit_code} target={self.target_thread_id} "
            f"answer_len={format_log_text_len(answer)}"
        )
        title = "Approval submitted" if exit_code == 0 else f"Approval failed (exit {exit_code})"
        await send_followup_chunks(
            interaction,
            f"{title}\n\n{output or '(no output)'}",
            title="Approval",
            exit_code=exit_code,
            log_prefix="button_response",
        )
        log_line(f"approval_button_sent exit={exit_code} target={self.target_thread_id}")
        if exit_code == 0:
            await stream_post_approval_result_for_interaction(
                interaction,
                watch_result,
                self.target_thread_id,
            )

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._submit(interaction, "1")

    @discord.ui.button(label="Approve session", style=discord.ButtonStyle.primary)
    async def approve_session(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._submit(interaction, "2")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._submit(interaction, "3")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._submit(interaction, "cancel")

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


class InputChoiceButton(discord.ui.Button):
    def __init__(self, target_thread_id: str, value: str, label: str) -> None:
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.primary,
            custom_id=format_input_choice_custom_id(target_thread_id, value),
        )
        self.target_thread_id = target_thread_id
        self.value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_discord_user_allowed(interaction.user.id):
            log_line(f"input_choice_button_denied user={interaction.user.id} target={self.target_thread_id}")
            await interaction.response.send_message("This user is not allowed.", ephemeral=True)
            return
        view = self.view
        if isinstance(view, InputChoiceView) and not view.claim():
            await interaction.response.send_message("This input choice was already handled.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        if isinstance(view, InputChoiceView):
            try:
                await interaction.message.edit(view=view)
            except Exception:
                pass
        log_line(
            f"input_choice_button user={interaction.user.id} "
            f"value_len={format_log_text_len(self.value)}"
        )
        exit_code, output = await asyncio.to_thread(submit_input_reply, self.target_thread_id, self.value)
        log_line(
            f"input_choice_button_done exit={exit_code} target={self.target_thread_id} "
            f"value_len={format_log_text_len(self.value)}"
        )
        title = "Input submitted" if exit_code == 0 else f"Input failed (exit {exit_code})"
        await send_followup_chunks(
            interaction,
            f"{title}\n\n{output or '(no output)'}",
            title="Input",
            exit_code=exit_code,
            log_prefix="button_response",
        )
        log_line(f"input_choice_button_sent exit={exit_code} target={self.target_thread_id}")


class InputChoiceView(discord.ui.View):
    def __init__(self, target_thread_id: str, options: list[tuple[str, str]]) -> None:
        super().__init__(timeout=1800)
        self.claimed = False
        for value, label in options[:5]:
            self.add_item(InputChoiceButton(target_thread_id, value, label))

    def claim(self) -> bool:
        if self.claimed:
            return False
        self.claimed = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        return True


class BusyChoiceView(discord.ui.View):
    def __init__(
        self,
        message: discord.Message,
        prompt: str,
        *,
        target_thread_id: str | None = None,
        allow_steer: bool = True,
        choice_id: str | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.message = message
        self.prompt = prompt
        self.target_thread_id = target_thread_id
        self.allow_steer = allow_steer
        self.choice_id = choice_id
        self.claimed = False
        self.assign_persistent_custom_ids()
        if not allow_steer:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label == "Steer now":
                    item.disabled = True

    def assign_persistent_custom_ids(self) -> None:
        if not self.choice_id:
            return
        labels_to_actions = {
            "Steer now": "steer",
            "Queue next": "queue",
            "Ignore": "ignore",
        }
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            action = labels_to_actions.get(str(item.label))
            if action:
                item.custom_id = format_busy_choice_custom_id(self.choice_id, action)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.message.author.id:
            return True
        log_line(
            f"busy_choice_denied user={interaction.user.id} "
            f"owner={self.message.author.id} target={self.target_thread_id or '-'}"
        )
        await interaction.response.send_message("Only the original sender can choose this.", ephemeral=True)
        return False

    def claim(self) -> bool:
        if self.claimed:
            return False
        if self.choice_id and not claim_busy_choice_record(self.choice_id):
            return False
        self.claimed = True
        self.disable_all_items()
        return True

    @discord.ui.button(label="Steer now", style=discord.ButtonStyle.primary)
    async def steer_now(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.allow_steer:
            await interaction.response.send_message(
                "This message targets a different Codex thread. Queue it instead.",
                ephemeral=True,
            )
            log_line(
                f"steer_now_rejected user={interaction.user.id} "
                f"target={self.target_thread_id or '-'} reason=not_allowed"
            )
            return
        if not self.claim():
            log_line(
                f"busy_choice_already_handled action=steer_now user={interaction.user.id} "
                f"target={self.target_thread_id or '-'}"
            )
            await interaction.response.send_message("This busy choice was already handled.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        log_line(
            f"steer_now user={interaction.user.id} target={self.target_thread_id or '-'} "
            f"prompt_len={format_log_text_len(self.prompt)}"
        )
        started_at = time.monotonic()
        async with channel_typing(self.message.channel, context="steer_now"):
            steering_result = await asyncio.to_thread(
                run_steering_prompt,
                self.prompt,
                self.target_thread_id,
            )
        exit_code, output = steering_result
        if exit_code == 0:
            mark_steering_handoff(self.target_thread_id)
        log_line(
            f"steer_now_done exit={exit_code} target={self.target_thread_id or '-'} "
            f"elapsed_sec={time.monotonic() - started_at:.2f} output_len={format_log_text_len(output)}"
        )
        if is_selected_thread_busy_error(exit_code, output):
            content, view = make_busy_choice_payload(
                self.message,
                self.prompt,
                target_thread_id=self.target_thread_id,
                allow_steer=True,
            )
            await send_direct_followup(
                interaction,
                content,
                view=view,
                log_prefix="button_followup",
                context="steer_busy_failure",
            )
            log_busy_choice_sent("steer_busy_failure", self.target_thread_id, self.prompt)
            log_line(
                f"steer_now_busy_choice_resent exit={exit_code} "
                f"target={self.target_thread_id or '-'}"
            )
            return
        title = "Steering sent" if exit_code == 0 else f"Steering failed (exit {exit_code})"
        await send_followup_chunks(
            interaction,
            f"{title}\n\n{output or '(no output)'}",
            title="Steering",
            exit_code=exit_code,
            log_prefix="button_response",
            ephemeral=True,
        )
        log_line(f"steer_now_sent exit={exit_code} target={self.target_thread_id or '-'}")
        if exit_code == 0:
            await stream_steering_prompt_result_to_channel(
                self.message.channel,
                steering_result,
                self.target_thread_id,
            )

    @discord.ui.button(label="Queue next", style=discord.ButtonStyle.secondary)
    async def queue_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.claim():
            log_line(
                f"busy_choice_already_handled action=queue_next user={interaction.user.id} "
                f"target={self.target_thread_id or '-'}"
            )
            await interaction.response.send_message("This busy choice was already handled.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        busy_state, _busy_thread_id, _busy_ref = await asyncio.to_thread(
            get_busy_state_for_thread,
            self.target_thread_id,
        )
        if busy_state == "idle" and not await is_thread_runner_busy(self.target_thread_id):
            log_line(
                f"queue_next_immediate user={interaction.user.id} "
                f"target={self.target_thread_id or '-'} "
                f"prompt_len={format_log_text_len(self.prompt)}"
            )
            await send_direct_followup(
                interaction,
                "No active job now. Starting this message.",
                log_prefix="button_followup",
                context="queue_next_immediate",
            )
            log_line(
                f"queue_next_immediate_sent user={interaction.user.id} "
                f"target={self.target_thread_id or '-'}"
            )
            position = await enqueue_thread_ask(
                self.message.channel,
                self.prompt,
                self.target_thread_id,
                queued=False,
                ack_sent=True,
                source_message=self.message,
            )
            log_line(
                f"queue_next_immediate_enqueued user={interaction.user.id} "
                f"position={position} target={self.target_thread_id or '-'}"
            )
            return

        position = await enqueue_thread_ask(
            self.message.channel,
            self.prompt,
            self.target_thread_id,
            queued=True,
            source_message=self.message,
        )
        log_line(
            f"queue_next user={interaction.user.id} position={position} target={self.target_thread_id or '-'} "
            f"prompt_len={format_log_text_len(self.prompt)}"
        )
        await send_direct_followup(
            interaction,
            f"Queued at position {position}.",
            log_prefix="button_followup",
            context="queue_next",
        )
        log_line(
            f"queue_next_sent user={interaction.user.id} position={position} "
            f"target={self.target_thread_id or '-'}"
        )

    @discord.ui.button(label="Ignore", style=discord.ButtonStyle.danger)
    async def ignore(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.claim():
            log_line(
                f"busy_choice_already_handled action=ignore user={interaction.user.id} "
                f"target={self.target_thread_id or '-'}"
            )
            await interaction.response.send_message("This busy choice was already handled.", ephemeral=True)
            return
        log_line(
            f"ignore_busy_prompt user={interaction.user.id} "
            f"target={self.target_thread_id or '-'}"
        )
        await interaction.response.send_message("Ignored.")
        log_line(
            f"ignore_busy_prompt_sent user={interaction.user.id} "
            f"target={self.target_thread_id or '-'}"
        )
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


async def handle_prefix_command(bot: CodexDiscordBot, message: discord.Message, command_line: str) -> None:
    parsed = discord_commands.split_prefix_command(command_line)
    if parsed is None:
        await send_chunks(message.channel, build_help())
        return
    command = parsed.command
    arg = parsed.arg

    if command in {"help", "start"}:
        await send_chunks(message.channel, build_help())
        return
    bridge_action = discord_commands.build_prefix_bridge_action(
        command,
        arg,
        message.channel.id,
        resolve_target_args_func=resolve_discord_thread_target_args,
    )
    if bridge_action is not None:
        if bridge_action.usage:
            await message.channel.send(bridge_action.usage)
            return
        await run_bridge_and_send(message.channel, bridge_action.argv or [], bridge_action.title)
        return
    if command == "doctor":
        await send_chunks(
            message.channel,
            await build_discord_doctor_message_with_history(bot, message.channel.id, message.channel),
        )
        await run_bridge_and_send(message.channel, ["doctor"], "Doctor")
        return
    if command == "steer":
        if not discord_qa_commands_enabled():
            await message.channel.send("Discord QA steering is disabled. Set DISCORD_ENABLE_QA_COMMANDS=1 to enable it.")
            return
        if not arg:
            await message.channel.send("Usage: !steer <prompt>")
            return
        target_thread_id = get_mirrored_codex_thread_id(message.channel.id)
        if target_thread_id is None:
            target_thread_id, _target_ref = resolve_selected_target()
        if not target_thread_id:
            await message.channel.send("No Codex thread target found.")
            return
        log_line(
            f"prefix_steer channel={message.channel.id} user={message.author.id} "
            f"target={target_thread_id} prompt_len={format_log_text_len(arg)}"
        )
        started_at = time.monotonic()
        async with channel_typing(message.channel, context="prefix_steer"):
            steering_result = await asyncio.to_thread(run_steering_prompt, arg, target_thread_id)
        exit_code, output = steering_result
        if exit_code == 0:
            mark_steering_handoff(target_thread_id)
        log_line(
            f"prefix_steer_done exit={exit_code} target={target_thread_id} "
            f"elapsed_sec={time.monotonic() - started_at:.2f} output_len={format_log_text_len(output)}"
        )
        title = "Steering sent" if exit_code == 0 else f"Steering failed (exit {exit_code})"
        await send_chunks(message.channel, f"{title}\n\n{output or '(no output)'}")
        if exit_code == 0:
            await stream_steering_prompt_result_to_channel(
                message.channel,
                steering_result,
                target_thread_id,
            )
        return
    if command in {"chatid", "whoami"}:
        await send_chunks(
            message.channel,
            "\n".join(
                [
                    "Discord identity",
                    f"guild_id: {message.guild.id if message.guild else '-'}",
                    f"channel_id: {message.channel.id}",
                    f"user_id: {message.author.id}",
                    f"channel_name: {getattr(message.channel, 'name', '-')}",
                    "",
                    "Copy into .env if needed:",
                    f"DISCORD_ALLOWED_CHANNEL_IDS={message.channel.id}",
                    f"DISCORD_ALLOWED_USER_IDS={message.author.id}",
                ]
            ),
        )
        return
    if command in {"where", "map"}:
        await send_chunks(message.channel, build_where_message(message.channel.id))
        return
    if command in {"context", "ctx"}:
        if arg.lower().strip() in {"all", "*"}:
            await send_chunks(message.channel, build_context_message(message.channel.id, all_threads=True, limit=20))
        else:
            await send_chunks(message.channel, build_context_message(message.channel.id))
        return
    if command in {"usage", "quota", "limit"}:
        usage_action = discord_commands.parse_usage_days(arg)
        if usage_action.usage:
            await message.channel.send(usage_action.usage)
            return
        days = int(usage_action.limit or 7)
        await send_chunks(message.channel, build_weekly_usage_message(days=days))
        return
    if command in {"runners", "queues"}:
        await send_chunks(message.channel, await build_runners_message())
        return
    if command in {"bridge_sync", "resync", "sync"} or command == "bridge":
        bridge_sync_action = discord_commands.parse_bridge_sync_limit(command, arg)
        if bridge_sync_action.usage:
            await message.channel.send(bridge_sync_action.usage)
            return
        limit = int(bridge_sync_action.limit or 30)
        await message.channel.send("Discord bridge sync started.")
        try:
            output = await refresh_discord_bridge_session(bot, limit=limit)
            await send_chunks(message.channel, output)
        except Exception as exc:
            log_line("bridge_sync_failed\n" + traceback.format_exc())
            await send_chunks(message.channel, f"Discord bridge sync failed\n\nERROR: {exc}")
        return
    if command in {"approval", "approve"}:
        target_thread_id = get_mirrored_codex_thread_id(message.channel.id)
        if not target_thread_id:
            target_thread_id, _target_ref = resolve_selected_target()
        if not target_thread_id:
            await message.channel.send("No Codex thread target found.")
            return
        state, resolved_thread_id, target_ref = get_interactive_state_for_thread(target_thread_id)
        if state != INTERACTIVE_STATE_APPROVAL or not resolved_thread_id:
            await send_chunks(
                message.channel,
                "\n".join(
                    [
                        "No pending approval for this Codex thread.",
                        build_where_message(message.channel.id),
                    ]
                ),
            )
            return
        await send_interactive_prompt(
            message.channel,
            resolved_thread_id,
            target_ref,
            INTERACTIVE_STATE_APPROVAL,
            "Pending approval",
            [],
        )
        return
    if command == "delete_archive":
        if not arg:
            await message.channel.send("Usage: !delete_archive <ref>")
            return
        exit_code, output = await asyncio.to_thread(run_bridge_command, ["delete_archive", arg])
        prefix = "Delete archive preview" if exit_code == 0 else f"Delete archive failed (exit {exit_code})"
        await send_chunks(
            message.channel,
            f"{prefix}\n\n{output or '(no output)'}\n\nTo actually delete it, run `!confirm_delete_archive <thread_id>`.",
        )
        return
    if command == "mirror":
        mirror_action = discord_commands.parse_mirror_action(arg)
        if mirror_action.usage:
            await message.channel.send(mirror_action.usage)
            return
        if mirror_action.subcommand == "sync":
            await message.channel.send("Mirror sync started.")
            try:
                output = await sync_codex_mirror(bot, limit=int(mirror_action.limit or 30))
                await send_chunks(message.channel, output)
            except Exception as exc:
                log_line("mirror_sync_failed\n" + traceback.format_exc())
                await send_chunks(message.channel, f"Mirror sync failed\n\nERROR: {exc}")
            return
        if mirror_action.subcommand == "list":
            await send_chunks(message.channel, build_mirror_list(limit=int(mirror_action.limit or 30)))
            return
        if mirror_action.subcommand == "check":
            await send_chunks(message.channel, build_mirror_check())
            return
    if command == "qa":
        if not discord_qa_commands_enabled():
            await message.channel.send("Discord QA commands are disabled. Set DISCORD_ENABLE_QA_COMMANDS=1 to enable them.")
            return
        subcommand = (arg.strip() or "buttons").lower()
        if subcommand not in {"buttons", "button"}:
            await message.channel.send("Usage: !qa buttons")
            return
        await message.channel.send("Discord button QA started.")
        try:
            output = await run_discord_button_qa(bot, message)
            await send_chunks(message.channel, output)
        except Exception as exc:
            log_line("button_qa_failed\n" + traceback.format_exc())
            await send_chunks(message.channel, f"Discord button QA failed\n\nERROR: {exc}")
        return
    if command == "new":
        if not arg:
            await message.channel.send("Usage: !new <prompt>")
            return
        _exit_code, output = await run_discord_new_thread(bot, message.channel.id, arg)
        await send_chunks(message.channel, output)
        return
    if command in {"ask", "ask_ipc"}:
        if not arg:
            await message.channel.send(f"Usage: !{command} <prompt>")
            return
        target_thread_id = get_mirrored_codex_thread_id(message.channel.id)
        if target_thread_id is None:
            project_message = describe_mirrored_project_channel(message.channel.id)
            if project_message:
                await send_chunks(message.channel, project_message)
                return
        await handle_plain_ask(message, arg, target_thread_id=target_thread_id)
        return

    await message.channel.send(f"Unknown command: !{format_discord_command_label(command)}")


def build_help() -> str:
    return discord_help.build_help(qa_commands_enabled=discord_qa_commands_enabled())


def register_commands(bot: CodexDiscordBot) -> None:
    @bot.tree.command(name="help", description="Show Discord Codex commands.")
    async def slash_help(interaction: discord.Interaction) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await send_interaction_chunks(interaction, build_help(), title="Help")

    @bot.tree.command(name="list", description="Show recent Codex threads.")
    async def slash_list(interaction: discord.Interaction, limit: int = 10) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await run_interaction_bridge_and_send(
            interaction,
            discord_commands.build_list_argv(limit, default=10, maximum=30),
            "List",
        )

    @bot.tree.command(name="archived_list", description="Show archived Codex threads.")
    async def slash_archived_list(interaction: discord.Interaction, limit: int = 10) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await run_interaction_bridge_and_send(
            interaction,
            discord_commands.build_archived_list_argv(limit, default=10, maximum=50),
            "Archived list",
        )

    @bot.tree.command(name="use", description="Select the active Codex thread.")
    async def slash_use(interaction: discord.Interaction, ref: str) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await run_interaction_bridge_and_send(interaction, ["use", ref], "Use")

    @bot.tree.command(name="status", description="Show selected Codex thread status.")
    async def slash_status(interaction: discord.Interaction, ref: str = "") -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        argv = discord_commands.build_status_argv(
            interaction.channel_id,
            ref or None,
            resolve_target_args_func=resolve_discord_thread_target_args,
        )
        await run_interaction_bridge_and_send(interaction, argv, "Status")

    @bot.tree.command(name="doctor", description="Run Codex bridge diagnostics.")
    async def slash_doctor(interaction: discord.Interaction) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await send_interaction_chunks(
            interaction,
            await build_discord_doctor_message_with_history(bot, interaction.channel_id, interaction.channel),
            title="Discord doctor",
        )
        await run_interaction_bridge_and_send(interaction, ["doctor"], "Doctor")

    @bot.tree.command(name="where", description="Show the Codex thread mapped to this Discord channel.")
    async def slash_where(interaction: discord.Interaction) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await send_interaction_chunks(
            interaction,
            build_where_message(interaction.channel_id),
            title="Where",
        )

    @bot.tree.command(name="context", description="Show context usage for this Codex thread.")
    async def slash_context(interaction: discord.Interaction, all_threads: bool = False) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        output = build_context_message(interaction.channel_id, all_threads=all_threads, limit=20)
        await send_interaction_chunks(interaction, output, title="Context")

    @bot.tree.command(name="usage", description="Show local Codex usage estimate.")
    async def slash_usage(interaction: discord.Interaction, days: int = 7) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        output = build_weekly_usage_message(days=max(1, min(30, days)))
        await send_interaction_chunks(interaction, output, title="Usage")

    @bot.tree.command(name="runners", description="Show Discord runner queues.")
    async def slash_runners(interaction: discord.Interaction) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await send_interaction_chunks(interaction, await build_runners_message(), title="Runners")

    @bot.tree.command(name="new", description="Create a new Codex thread with the first prompt.")
    async def slash_new(interaction: discord.Interaction, prompt: str) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await handle_slash_new(bot, interaction, prompt)

    @bot.tree.command(name="ask", description="Send a prompt to the mapped or selected Codex thread.")
    async def slash_ask(interaction: discord.Interaction, prompt: str) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await handle_slash_ask(interaction, prompt)

    @bot.tree.command(name="ask_ipc", description="Alias of /ask.")
    async def slash_ask_ipc(interaction: discord.Interaction, prompt: str) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await handle_slash_ask(interaction, prompt)

    @bot.tree.command(name="mirror_check", description="Check Discord mirror mappings.")
    async def slash_mirror_check(interaction: discord.Interaction) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await send_interaction_chunks(interaction, build_mirror_check(), title="Mirror check")

    @bot.tree.command(name="bridge_sync", description="Refresh Codex bridge state and Discord mirror.")
    async def slash_bridge_sync(interaction: discord.Interaction, limit: int = 30) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        bounded_limit = max(1, min(100, int(limit)))
        try:
            output = await refresh_discord_bridge_session(bot, limit=bounded_limit)
        except Exception as exc:
            log_line("slash_bridge_sync_failed\n" + traceback.format_exc())
            output = f"Discord bridge sync failed\n\nERROR: {exc}"
        await send_interaction_chunks(interaction, output, title="Bridge sync")

    if discord_qa_commands_enabled():
        @bot.tree.command(name="qa_buttons", description="Run Discord button QA smoke.")
        async def slash_qa_buttons(interaction: discord.Interaction) -> None:
            if not check_interaction_allowed(bot, interaction):
                await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
                return
            if interaction.channel is None:
                await interaction.response.send_message("Discord channel is unavailable.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            source_message = SimpleNamespace(author=interaction.user, channel=interaction.channel)
            output = await run_discord_button_qa(bot, source_message)  # type: ignore[arg-type]
            await send_interaction_chunks(interaction, output, title="Discord button QA")


def check_interaction_allowed(bot: CodexDiscordBot, interaction: discord.Interaction) -> bool:
    command_name = get_interaction_command_name(interaction)
    if not bot.is_allowed_user(interaction.user.id):
        log_line(
            f"slash_ignored command={command_name} reason=user_not_allowed "
            f"user={interaction.user.id} channel={interaction.channel_id}"
        )
        return False
    if bot.is_allowed_channel(interaction.channel_id):
        return True
    if is_mirrored_channel_id(interaction.channel_id):
        return True
    channel = interaction.channel
    if channel is not None and bot.is_allowed_message_channel(channel):
        return True
    log_line(
        f"slash_ignored command={command_name} reason=channel_not_allowed "
        f"user={interaction.user.id} channel={interaction.channel_id}"
    )
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discord adapter for codex_desktop_bridge.py")
    parser.add_argument(
        "--no-message-content",
        action="store_true",
        help="Disable prefix/plain-message handling and use slash commands only.",
    )
    return parser


def main() -> int:
    load_local_env(ENV_PATH)
    args = build_parser().parse_args()
    token = get_required_env("DISCORD_BOT_TOKEN")
    guild_id_raw = os.environ.get("DISCORD_GUILD_ID", "").strip()
    channel_ids = parse_int_set(os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", ""))
    user_ids = parse_int_set(os.environ.get("DISCORD_ALLOWED_USER_IDS", ""))
    allow_all_channels = env_flag("DISCORD_ALLOW_ALL_CHANNELS", default=False)
    if not channel_ids and not allow_all_channels:
        log_line("main_config_error reason=missing_allowed_channels")
        print("ERROR: Set DISCORD_ALLOWED_CHANNEL_IDS or DISCORD_ALLOW_ALL_CHANNELS=1.")
        return 1
    startup_channel_id = None
    startup_channel_raw = os.environ.get("DISCORD_STARTUP_CHANNEL_ID", "").strip()
    if startup_channel_raw:
        startup_channel_id = int(startup_channel_raw)
    elif len(channel_ids) == 1:
        startup_channel_id = next(iter(channel_ids))
    guild_id = int(guild_id_raw) if guild_id_raw else None
    enable_prefix_commands = (
        env_flag("DISCORD_ENABLE_MESSAGE_CONTENT", default=True)
        and not args.no_message_content
    )
    bot = CodexDiscordBot(
        allowed_channel_ids=channel_ids,
        allowed_user_ids=user_ids,
        startup_channel_id=startup_channel_id,
        guild_id=guild_id,
        enable_prefix_commands=enable_prefix_commands,
    )
    log_line(
        "main_start "
        f"guild_id={guild_id or '-'} channels={sorted(channel_ids) if channel_ids else 'ALL_EXPLICIT'} "
        f"users={sorted(user_ids) if user_ids else 'ALL'} "
        f"message_content={enable_prefix_commands}"
    )
    bot.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
