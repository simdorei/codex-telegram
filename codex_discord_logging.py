"""File logging and hook-log summarization for the Discord Codex adapter."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import codex_desktop_bridge as bridge


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_PATH = SCRIPT_DIR / "codex_discord_bot.log"


def get_log_path() -> Path:
    value = os.environ.get("CODEX_DISCORD_LOG_PATH", "").strip()
    if value:
        return Path(value).expanduser()
    return LOG_PATH


def log_line(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}\n"
    log_path = get_log_path()
    try:
        bridge.rotate_single_backup_file(
            log_path,
            incoming_bytes=len(line.encode("utf-8")),
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        pass


def parse_log_line(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\[([^\]]+)\]\s+(.*)$", str(line or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def get_log_field(body: str, key: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(key)}=([^\s]+)", body)
    return match.group(1) if match else "-"


def summarize_discord_hook_log_line(line: str) -> str | None:
    parsed = parse_log_line(line)
    if not parsed:
        return None
    timestamp, body = parsed
    if body.startswith("socket_message_create_untracked "):
        return (
            f"{timestamp} raw_message_untracked "
            f"channel={get_log_field(body, 'channel')} source={get_log_field(body, 'source')}"
        )
    if body.startswith("socket_message_create "):
        return (
            f"{timestamp} raw_message "
            f"channel={get_log_field(body, 'channel')} source={get_log_field(body, 'source')} "
            f"bot={get_log_field(body, 'bot')} content_len={get_log_field(body, 'content_len')}"
        )
    if body.startswith("message_received "):
        return (
            f"{timestamp} message_received "
            f"channel={get_log_field(body, 'chat')} content_len={get_log_field(body, 'content_len')}"
        )
    if body.startswith("ignored_message "):
        return (
            f"{timestamp} ignored_message "
            f"reason={get_log_field(body, 'reason')} channel={get_log_field(body, 'chat')}"
        )
    if body.startswith("history_poll_message "):
        return (
            f"{timestamp} history_poll_message "
            f"channel={get_log_field(body, 'channel')} content_len={get_log_field(body, 'content_len')}"
        )
    if body.startswith("history_poll_primed "):
        return (
            f"{timestamp} history_poll_primed "
            f"channel={get_log_field(body, 'channel')} messages={get_log_field(body, 'messages')}"
        )
    if body.startswith("message "):
        return (
            f"{timestamp} message_routed "
            f"channel={get_log_field(body, 'chat')} target={get_log_field(body, 'target')} "
            f"prefix={get_log_field(body, 'prefix')} text_len={get_log_field(body, 'text_len')}"
        )
    if body.startswith("socket_interaction_create "):
        return (
            f"{timestamp} raw_interaction "
            f"channel={get_log_field(body, 'channel')} type={get_log_field(body, 'type')} "
            f"command={get_log_field(body, 'command')}"
        )
    if body.startswith("interaction_received "):
        return (
            f"{timestamp} interaction_received "
            f"channel={get_log_field(body, 'channel')} type={get_log_field(body, 'type')} "
            f"command={get_log_field(body, 'command')}"
        )
    if body.startswith("slash_"):
        slash_event = body.split(" ", 1)[0]
        return (
            f"{timestamp} {slash_event} "
            f"channel={get_log_field(body, 'channel')} command={get_log_field(body, 'command')} "
            f"exit={get_log_field(body, 'exit')} response={get_log_field(body, 'response')} "
            f"reason={get_log_field(body, 'reason')}"
        )
    if body.startswith("component_interaction_"):
        return (
            f"{timestamp} component_event "
            f"channel={get_log_field(body, 'channel')} custom_id={get_log_field(body, 'custom_id')}"
        )
    if body.startswith("busy_choice_"):
        return (
            f"{timestamp} busy_choice_event "
            f"reason={get_log_field(body, 'reason')} target={get_log_field(body, 'target')}"
        )
    if body.startswith("approval_persistent"):
        return (
            f"{timestamp} approval_persistent "
            f"target={get_log_field(body, 'target')} exit={get_log_field(body, 'exit')}"
        )
    if body.startswith("input_choice_persistent"):
        return (
            f"{timestamp} input_choice_persistent "
            f"target={get_log_field(body, 'target')} exit={get_log_field(body, 'exit')}"
        )
    return None


def is_user_or_control_hook_summary(summary: str) -> bool:
    if " raw_message " in summary:
        return " bot=False " in summary or " bot=- " in summary
    if " raw_message_untracked " in summary:
        return True
    return any(
        marker in summary
        for marker in [
            " message_received ",
            " ignored_message ",
            " history_poll_message ",
            " message_routed ",
            " raw_interaction ",
            " interaction_received ",
            " slash_",
            " component_event ",
            " busy_choice_event ",
            " approval_persistent ",
            " input_choice_persistent ",
        ]
    )


def get_recent_discord_hook_events(
    *,
    limit: int = 8,
    max_lines: int = 1000,
    user_or_control_only: bool = False,
) -> list[str]:
    log_path = get_log_path()
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[str] = []
    for line in lines[-max_lines:]:
        summary = summarize_discord_hook_log_line(line)
        if summary:
            if user_or_control_only and not is_user_or_control_hook_summary(summary):
                continue
            events.append(summary)
    return events[-limit:]


def get_discord_log_markers(*, max_lines: int = 2000) -> dict[str, str]:
    log_path = get_log_path()
    markers = {
        "last_ready_at": "-",
        "last_gateway_event_at": "-",
        "last_raw_interaction_at": "-",
        "last_interaction_at": "-",
        "last_component_at": "-",
        "last_user_or_control_hook_at": "-",
        "last_button_qa_at": "-",
        "last_button_qa_result": "-",
        "last_steering_button_at": "-",
        "last_steering_button_exit": "-",
        "last_steering_button_elapsed_sec": "-",
    }
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return markers
    for line in lines[-max_lines:]:
        parsed = parse_log_line(line)
        if not parsed:
            continue
        timestamp, body = parsed
        if body.startswith("ready "):
            markers["last_ready_at"] = timestamp
        if body.startswith(("socket_message_create ", "socket_message_create_untracked ", "socket_interaction_create ")):
            markers["last_gateway_event_at"] = timestamp
        if body.startswith("socket_interaction_create "):
            markers["last_raw_interaction_at"] = timestamp
        if body.startswith("interaction_received "):
            markers["last_interaction_at"] = timestamp
            if " type=component " in f" {body} ":
                markers["last_component_at"] = timestamp
        if body.startswith((
            "component_interaction_",
            "busy_choice_persistent_",
            "approval_persistent",
            "input_choice_persistent",
        )):
            markers["last_component_at"] = timestamp
        if body.startswith("button_qa_done "):
            markers["last_button_qa_at"] = timestamp
            markers["last_button_qa_result"] = get_log_field(body, "result")
        if body.startswith(("steer_now_done ", "busy_choice_persistent_steer_done ")):
            markers["last_steering_button_at"] = timestamp
            markers["last_steering_button_exit"] = get_log_field(body, "exit")
            markers["last_steering_button_elapsed_sec"] = get_log_field(body, "elapsed_sec")
        summary = summarize_discord_hook_log_line(line)
        if summary and is_user_or_control_hook_summary(summary):
            markers["last_user_or_control_hook_at"] = timestamp
    return markers
