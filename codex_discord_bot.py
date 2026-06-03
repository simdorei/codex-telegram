"""
Discord adapter for codex_desktop_bridge.py.

This keeps Discord-specific behavior separate from the Telegram adapter while
reusing the same local Codex Desktop bridge.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands

import codex_desktop_bridge as bridge


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
LOG_PATH = SCRIPT_DIR / "codex_discord_bot.log"
MIRROR_DB_PATH = SCRIPT_DIR / "discord_mirror.sqlite"
DISCORD_MAX_LEN = 1900
THREAD_RUNNERS_LOCK = asyncio.Lock()
THREAD_RUNNERS: dict[str, dict[str, object]] = {}
UI_FALLBACK_LOCK = threading.Lock()
INTERACTIVE_INPUT_TAG = "[choice_required]"
INTERACTIVE_APPROVAL_TAG = "[approval_required]"
INTERACTIVE_STATE_NONE = ""
INTERACTIVE_STATE_INPUT = "waiting-input"
INTERACTIVE_STATE_APPROVAL = "waiting-approval"
CODEX_PROJECTLESS_CHAT_KEY = "codex:chats"


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


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def parse_int_set(raw: str) -> set[int]:
    result: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


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


def get_log_path() -> Path:
    value = os.environ.get("CODEX_DISCORD_LOG_PATH", "").strip()
    if value:
        return Path(value).expanduser()
    return LOG_PATH


def parse_bounded_int_arg(raw: str, *, default: int, minimum: int, maximum: int) -> int:
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        return default


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


def split_message(text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return ["(no output)"]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def fit_single_message(text: str, limit: int = DISCORD_MAX_LEN) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    suffix = "\n\n[truncated for Discord]"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def format_log_argv(argv: list[str]) -> str:
    return " ".join(str(part).replace("\n", " ")[:120] for part in argv)


def format_log_text_len(text: str | None) -> int:
    return len(str(text or ""))


def format_discord_command_label(command: str, *, limit: int = 80) -> str:
    label = str(command or "").replace("\n", " ").replace("\r", " ").strip()
    if len(label) <= limit:
        return label
    return label[: max(0, limit - 3)].rstrip() + "..."


def run_bridge_command(argv: list[str]) -> tuple[int, str]:
    parser = bridge.build_parser()
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    exit_code = 0
    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        try:
            args = parser.parse_args(argv)
            result = args.func(args)
            exit_code = int(result or 0)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            exit_code = 1
            print(f"ERROR: {exc}")
    output = stdout_buffer.getvalue()
    stderr = stderr_buffer.getvalue()
    combined = (output + ("\n" + stderr if stderr else "")).strip()
    return exit_code, combined


def parse_bridge_output_value(output: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in (output or "").splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            return value or None
    return None


def resolve_selected_target() -> tuple[str | None, str]:
    try:
        thread = bridge.choose_thread(None, None)
    except Exception:
        return None, ""
    return thread.id, bridge.get_thread_workspace_ref(thread)


def get_selected_interactive_state() -> tuple[str, str | None, str]:
    target_thread_id, target_ref = resolve_selected_target()
    if not target_thread_id:
        return INTERACTIVE_STATE_NONE, None, target_ref
    try:
        thread = bridge.choose_thread(target_thread_id, None)
        busy_state = bridge.get_thread_busy_state(thread, allow_resume=True)
    except Exception:
        return INTERACTIVE_STATE_NONE, target_thread_id, target_ref
    if busy_state not in {INTERACTIVE_STATE_INPUT, INTERACTIVE_STATE_APPROVAL}:
        return INTERACTIVE_STATE_NONE, target_thread_id, target_ref
    return busy_state, target_thread_id, target_ref


def parse_interactive_notice(text: str) -> tuple[str, str, list[tuple[str, str]]]:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    if not lines:
        return INTERACTIVE_STATE_NONE, "", []
    first_line = lines[0].strip()
    if first_line not in {INTERACTIVE_INPUT_TAG, INTERACTIVE_APPROVAL_TAG}:
        return INTERACTIVE_STATE_NONE, "", []

    prompt_lines: list[str] = []
    options: list[tuple[str, str]] = []
    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        match = re.match(r"^(\d+)[\.\)]\s+(.+)$", stripped)
        if match:
            options.append((match.group(1), match.group(2).strip()))
            continue
        prompt_lines.append(stripped)

    state = INTERACTIVE_STATE_INPUT if first_line == INTERACTIVE_INPUT_TAG else INTERACTIVE_STATE_APPROVAL
    return state, "\n".join(prompt_lines), options


def init_mirror_db() -> None:
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mirror_projects (
                project_key TEXT PRIMARY KEY,
                project_name TEXT NOT NULL,
                discord_channel_id INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mirror_threads (
                codex_thread_id TEXT PRIMARY KEY,
                project_key TEXT NOT NULL,
                thread_title TEXT NOT NULL,
                discord_channel_id INTEGER NOT NULL,
                discord_thread_id INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )


def normalize_discord_name(value: str, *, prefix: str = "", max_len: int = 90) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^0-9a-z가-힣_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    if not text:
        text = "untitled"
    if prefix and not text.startswith(prefix):
        text = prefix + text
    return text[:max_len].strip("-_") or "untitled"


def truncate_discord_title(value: str, fallback: str, *, max_len: int = 90) -> str:
    text = " ".join((value or "").split()) or fallback
    return text[:max_len].strip() or fallback


def get_project_key(thread: bridge.ThreadInfo) -> str:
    cwd = bridge.strip_windows_extended_prefix((thread.cwd or "").strip())
    if cwd:
        if is_codex_projectless_chat_cwd(cwd):
            return CODEX_PROJECTLESS_CHAT_KEY
        try:
            return bridge.normalize_workspace_path(cwd)
        except Exception:
            return cwd.lower()
    return f"projectless:{bridge.get_thread_workspace_name(thread)}"


def get_project_name(thread: bridge.ThreadInfo) -> str:
    cwd = bridge.strip_windows_extended_prefix((thread.cwd or "").strip())
    if cwd and is_codex_projectless_chat_cwd(cwd):
        return "채팅"
    name = bridge.get_thread_workspace_name(thread)
    return name if name and name != "-" else "projectless"


def is_codex_projectless_chat_cwd(cwd: str) -> bool:
    normalized = bridge.normalize_workspace_path(cwd)
    parts = re.split(r"[\\/]+", normalized)
    if len(parts) < 4:
        return False
    return (
        len(parts) >= 5
        and parts[-1].lower().startswith("new-chat")
        and re.match(r"^\d{4}-\d{2}-\d{2}$", parts[-2] or "") is not None
        and parts[-3].lower() == "codex"
        and parts[-4].lower() == "documents"
    )


def get_mirrored_codex_thread_id(discord_channel_id: int | None) -> str | None:
    if not discord_channel_id:
        return None
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        row = conn.execute(
            "SELECT codex_thread_id FROM mirror_threads WHERE discord_thread_id = ?",
            (int(discord_channel_id),),
        ).fetchone()
        if row:
            return str(row[0])
        rows = conn.execute(
            """
            SELECT codex_thread_id
            FROM mirror_threads
            WHERE discord_channel_id = ?
            ORDER BY updated_at DESC
            LIMIT 2
            """,
            (int(discord_channel_id),),
        ).fetchall()
    if len(rows) == 1:
        return str(rows[0][0])
    return None


def describe_mirrored_project_channel(discord_channel_id: int | None) -> str:
    if not discord_channel_id:
        return ""
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        project = conn.execute(
            "SELECT project_name FROM mirror_projects WHERE discord_channel_id = ?",
            (int(discord_channel_id),),
        ).fetchone()
        if not project:
            return ""
        rows = conn.execute(
            """
            SELECT thread_title
            FROM mirror_threads
            WHERE discord_channel_id = ?
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            (int(discord_channel_id),),
        ).fetchall()
    titles = [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]
    if len(titles) <= 1:
        return ""
    return "\n".join(
        [
            f"`{project[0]}` project channel has multiple Codex threads.",
            "Send the message inside one of its Discord threads:",
            *[f"- {title}" for title in titles],
        ]
    )


def get_mirror_project_for_channel(discord_channel_id: int | None) -> tuple[str, str] | None:
    if not discord_channel_id:
        return None
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        row = conn.execute(
            "SELECT project_key, project_name FROM mirror_projects WHERE discord_channel_id = ?",
            (int(discord_channel_id),),
        ).fetchone()
    if not row:
        return None
    return str(row[0] or ""), str(row[1] or "")


def get_thread_cwd(thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    try:
        thread = bridge.choose_thread(thread_id, None)
    except Exception:
        return None
    cwd = bridge.strip_windows_extended_prefix((thread.cwd or "").strip())
    return cwd or None


def find_projectless_new_chat_cwd() -> str | None:
    codex_docs = Path.home() / "Documents" / "Codex"
    if not codex_docs.exists():
        return None
    today_new_chat = codex_docs / datetime.now().strftime("%Y-%m-%d") / "new-chat"
    if today_new_chat.is_dir():
        return str(today_new_chat)
    candidates = [path for path in codex_docs.glob("????-??-??/new-chat") if path.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return str(candidates[0])


def resolve_discord_new_thread_cwd(discord_channel_id: int | None) -> str | None:
    target_thread_id = get_mirrored_codex_thread_id(discord_channel_id)
    thread_cwd = get_thread_cwd(target_thread_id)
    if thread_cwd:
        return thread_cwd

    project = get_mirror_project_for_channel(discord_channel_id)
    if not project:
        return None
    project_key, _project_name = project
    if project_key == CODEX_PROJECTLESS_CHAT_KEY:
        return find_projectless_new_chat_cwd()
    if project_key and not project_key.startswith("projectless:"):
        project_path = Path(bridge.strip_windows_extended_prefix(project_key))
        if project_path.is_dir():
            return str(project_path)
    return None


def resolve_discord_new_thread_project_channel_id(
    discord_channel_id: int | None,
    project_key: str | None,
) -> int | None:
    if not discord_channel_id or not project_key:
        return None
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT discord_channel_id
            FROM mirror_threads
            WHERE discord_thread_id = ? AND project_key = ?
            LIMIT 1
            """,
            (int(discord_channel_id), project_key),
        ).fetchone()
        if row:
            return int(row[0])
        row = conn.execute(
            """
            SELECT discord_channel_id
            FROM mirror_projects
            WHERE discord_channel_id = ? AND project_key = ?
            LIMIT 1
            """,
            (int(discord_channel_id), project_key),
        ).fetchone()
    return int(row[0]) if row else None


def is_mirrored_channel_id(discord_channel_id: int | None) -> bool:
    if not discord_channel_id:
        return False
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM mirror_projects WHERE discord_channel_id = ?
            UNION
            SELECT 1 FROM mirror_threads WHERE discord_thread_id = ? OR discord_channel_id = ?
            LIMIT 1
            """,
            (int(discord_channel_id), int(discord_channel_id), int(discord_channel_id)),
        ).fetchone()
    return bool(row)


class LineStream(io.TextIOBase):
    def __init__(self, on_line):
        self.on_line = on_line
        self._buffer = ""
        self._all: list[str] = []

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._all.append(s)
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.on_line(line.rstrip("\r"))
        return len(s)

    def flush(self) -> None:
        if self._buffer:
            self.on_line(self._buffer.rstrip("\r"))
            self._buffer = ""

    def getvalue(self) -> str:
        return "".join(self._all)


def run_bridge_command_stream(argv: list[str], on_line) -> tuple[int, str]:
    parser = bridge.build_parser()
    stream = LineStream(on_line)
    exit_code = 0
    with redirect_stdout(stream), redirect_stderr(stream):
        try:
            args = parser.parse_args(argv)
            result = args.func(args)
            exit_code = int(result or 0)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            exit_code = 1
            print(f"ERROR: {exc}")
    stream.flush()
    return exit_code, stream.getvalue().strip()


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


def run_steering_prompt(prompt: str, target_thread_id: str | None) -> tuple[int, str]:
    target_thread_id, _target_ref = resolve_target_ref(target_thread_id)
    target_thread = bridge.choose_thread(target_thread_id, None) if target_thread_id else None
    recent_offsets = bridge.snapshot_recent_session_offsets(
        limit=10,
        include_threads=[target_thread] if target_thread else None,
    )
    exit_code, output = run_ask(
        prompt,
        force_while_busy=True,
        wait=False,
        target_thread_id=target_thread_id,
    )
    if exit_code == 0:
        return exit_code, output

    delivered_thread = bridge.wait_for_prompt_delivery(recent_offsets, prompt, timeout_sec=3.0)
    if delivered_thread is not None and (
        target_thread_id is None or delivered_thread.id == target_thread_id
    ):
        log_line(
            f"steering_nonzero_but_delivered exit={exit_code} target={target_thread_id or '-'} "
            f"delivered={delivered_thread.id}"
        )
        return 0, "\n\n".join(
            part
            for part in [
                f"[delivery_verified] {bridge.get_thread_label(delivered_thread)}",
                "Original transport returned a nonzero exit, but the steering prompt was recorded in Codex.",
                output,
            ]
            if part
        )

    log_line(
        f"steering_failed exit={exit_code} target={target_thread_id or '-'} "
        f"output_len={format_log_text_len(output)}"
    )
    return exit_code, output


class DiscordAskRelay:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        channel: discord.abc.Messageable,
        target_thread_id: str | None,
        target_ref: str,
    ) -> None:
        self.loop = loop
        self.channel = channel
        self.target_thread_id = target_thread_id
        self.target_ref = target_ref
        self.mode: str | None = None
        self.block_lines: list[str] = []
        self.sent_live = False
        self.saw_final = False
        self.saw_aborted = False
        self.saw_timeout = False
        self._send_futures = []

    def _send(self, text: str) -> None:
        future = asyncio.run_coroutine_threadsafe(send_chunks(self.channel, text), self.loop)
        self._send_futures.append(future)

    def _send_interactive_notice_if_detected(self, text: str) -> bool:
        state, prompt, options = parse_interactive_notice(text)
        if not state or not self.target_thread_id:
            return False
        future = asyncio.run_coroutine_threadsafe(
            send_interactive_prompt(
                self.channel,
                self.target_thread_id,
                self.target_ref,
                state,
                prompt,
                options,
            ),
            self.loop,
        )
        self._send_futures.append(future)
        self.sent_live = True
        return True

    def _send_block(self) -> None:
        text = "\n".join(self.block_lines).strip()
        if not text:
            self.block_lines = []
            return
        if self.mode == "commentary":
            if not self._send_interactive_notice_if_detected(text):
                self._send(f"In progress\n\n{text}")
                self.sent_live = True
        elif self.mode == "final":
            if not self._send_interactive_notice_if_detected(text):
                self._send(text)
                self.sent_live = True
                self.saw_final = True
        elif self.mode == "timeout":
            self._send(f"Timed out\n\n{text}")
            self.sent_live = True
            self.saw_timeout = True
        self.block_lines = []

    def feed_line(self, line: str) -> None:
        if line.startswith("[commentary]"):
            self._send_block()
            self.mode = "commentary"
            return
        if line.startswith("[final_answer]"):
            self._send_block()
            self.mode = "final"
            return
        if line.startswith("[timeout]"):
            self._send_block()
            self.mode = "timeout"
            return
        if line.startswith("[aborted]"):
            self._send_block()
            self.mode = None
            self.saw_aborted = True
            self._send("Aborted.")
            self.sent_live = True
            return
        if line.startswith("[ready]"):
            self._send_block()
            self.mode = None
            return
        if line.startswith("[waiting_for_final_answer]") or line.startswith("Use Ctrl+C"):
            return

        if self.mode in {"commentary", "final", "timeout"}:
            self.block_lines.append(line)
            return

        if line.startswith("target_thread:") or line.startswith("title:") or line.startswith("ui_name:") or line.startswith("cwd:"):
            return
        if line.startswith("ui_activation:") or line.startswith("sent_to_window:") or line.startswith("[delivery_verified]"):
            return
        if line.startswith("[background_watch_started]") or line.startswith("[background_watch_already_running]"):
            return
        if line.startswith("[wait_cancelled]"):
            return

    def finish(self) -> None:
        self._send_block()
        for future in self._send_futures:
            try:
                future.result(timeout=30)
            except Exception:
                log_line("discord_relay_send_failed\n" + traceback.format_exc())


def run_ask_stream(
    prompt: str,
    relay: DiscordAskRelay,
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
        "--stream",
        "--include-commentary",
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
    exit_code, output = run_bridge_command_stream(argv, relay.feed_line)
    if should_retry_ask_with_ui(exit_code, output):
        relay.feed_line("[commentary]")
        relay.feed_line("IPC attach failed for this Codex thread. Retrying through the Codex UI.")
        relay.feed_line("[ready]")
        ui_argv = build_ui_ask_argv(
            prompt,
            target_thread_id=target_thread_id,
            force_while_busy=True,
            wait=wait,
        )
        if "--stream" not in ui_argv:
            ui_argv.insert(ui_argv.index("--timeout"), "--include-commentary")
            ui_argv.insert(ui_argv.index("--include-commentary"), "--stream")
        with UI_FALLBACK_LOCK:
            exit_code, output = run_bridge_command_stream(ui_argv, relay.feed_line)
    relay.finish()
    return exit_code, output


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
        super().__init__(intents=intents)
        self.tree = LoggingCommandTree(self)
        self.allowed_channel_ids = allowed_channel_ids
        self.allowed_user_ids = allowed_user_ids
        self.startup_channel_id = startup_channel_id
        self.guild_id = guild_id
        self.enable_prefix_commands = enable_prefix_commands

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
            log_line(f"setup_hook_synced commands={','.join(command_names) or '-'}")
        except Exception as exc:
            log_line(f"setup_hook_sync_skipped error={exc}")
        log_line("setup_hook_done")

    async def on_ready(self) -> None:
        log_line(f"ready user={self.user} guilds={len(self.guilds)}")
        if env_flag("DISCORD_STARTUP_NOTIFY", default=False) and self.startup_channel_id:
            channel = self.get_channel(self.startup_channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(self.startup_channel_id)
                except Exception:
                    log_line("startup_channel_fetch_failed\n" + traceback.format_exc())
                    return
            if isinstance(channel, discord.abc.Messageable):
                await send_chunks(channel, "Codex Discord bot online. Try `!list` or `/list`.")

    async def on_message(self, message: discord.Message) -> None:
        try:
            if message.author.bot:
                return
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
                f"text={content[:160].replace(chr(10), ' ')}"
            )
            if content.startswith("!"):
                await handle_prefix_command(self, message, content[1:].strip())
                return
            if target_thread_id is None:
                project_message = describe_mirrored_project_channel(message.channel.id)
                if project_message:
                    await message.channel.send(project_message)
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
) -> None:
    chunks = split_message(text)
    command_name = get_interaction_command_name(interaction)
    exit_part = "-" if exit_code is None else str(exit_code)
    log_line(
        f"{log_prefix}_start command={command_name} title={title!r} "
        f"exit={exit_part} chunks={len(chunks)} channel={interaction.channel_id}"
    )
    for chunk in chunks:
        await interaction.followup.send(chunk)
    log_line(
        f"{log_prefix}_sent command={command_name} title={title!r} "
        f"exit={exit_part} chunks={len(chunks)}"
    )


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


async def sync_codex_mirror(bot: CodexDiscordBot, *, limit: int = 30) -> str:
    log_line(f"mirror_sync_start limit={limit}")
    guild = await get_mirror_guild(bot)
    category = await get_or_create_mirror_category(guild)
    threads = await asyncio.to_thread(bridge.load_recent_threads, limit)
    if not threads:
        return "No Codex threads found."
    all_active_threads = await asyncio.to_thread(bridge.load_recent_threads, 0)

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
                SELECT project_key FROM mirror_projects
                WHERE project_key NOT IN ({})
                """.format(",".join("?" for _ in valid_project_keys)),
                tuple(valid_project_keys),
            ).fetchall()
        else:
            stale_projects = conn.execute("SELECT project_key FROM mirror_projects").fetchall()

    stale_cleanup = await delete_stale_discord_threads(guild, stale_threads)

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
        f"orphan_failed={orphan_cleanup['failed']}"
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
    init_mirror_db()
    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT mt.thread_title, mt.codex_thread_id, mp.project_name, mt.discord_channel_id, mt.discord_thread_id
            FROM mirror_threads mt
            LEFT JOIN mirror_projects mp ON mp.project_key = mt.project_key
            ORDER BY mt.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    if not rows:
        return "No mirrored threads yet. Run `!mirror sync`."
    lines = ["Mirrored Codex threads"]
    for title, codex_thread_id, project_name, channel_id, thread_id in rows:
        context_suffix = ""
        try:
            thread = bridge.choose_thread(str(codex_thread_id), None)
            context_usage = bridge.get_thread_context_usage(thread)
            if context_usage is not None:
                status = bridge.describe_thread_context_usage(context_usage)
                archive_hint = " archive" if bridge.should_recommend_archive(thread, context_usage) else ""
                context_suffix = f" ctx={context_usage.usage_ratio * 100:.1f}%/{status}{archive_hint}"
        except Exception:
            context_suffix = ""
        lines.append(
            f"- {project_name or '-'} / {title or codex_thread_id[:8]} "
            f"=> <#{thread_id}> ({codex_thread_id[:8]}){context_suffix}"
        )
    return "\n".join(lines)


def build_mirror_check() -> str:
    init_mirror_db()
    threads = bridge.load_recent_threads(limit=0)
    expected: dict[str, tuple[str, str, str]] = {}
    for thread in threads:
        expected[thread.id] = (
            get_project_key(thread),
            get_project_name(thread),
            bridge.get_thread_ui_name(thread.id, thread) or thread.title or thread.id[:8],
        )

    with sqlite3.connect(MIRROR_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT codex_thread_id, project_key, thread_title, discord_channel_id, discord_thread_id
            FROM mirror_threads
            ORDER BY updated_at DESC
            """
        ).fetchall()

    missing = [item for item in expected if item not in {str(row[0]) for row in rows}]
    stale = [str(row[0]) for row in rows if str(row[0]) not in expected]
    wrong_project = [
        (str(row[0]), str(row[1]), expected[str(row[0])][0])
        for row in rows
        if str(row[0]) in expected and str(row[1]) != expected[str(row[0])][0]
    ]

    lines = [
        "Mirror check",
        f"codex_threads: {len(expected)}",
        f"mirrored_threads: {len(rows)}",
        f"missing: {len(missing)}",
        f"stale: {len(stale)}",
        f"wrong_project: {len(wrong_project)}",
    ]
    if missing:
        lines.append("")
        lines.append("Missing:")
        for thread_id in missing[:10]:
            project_key, project_name, title = expected[thread_id]
            lines.append(f"- {project_name} / {title} ({thread_id[:8]})")
    if wrong_project:
        lines.append("")
        lines.append("Wrong project:")
        for thread_id, current, expected_key in wrong_project[:10]:
            lines.append(f"- {thread_id[:8]} current={current} expected={expected_key}")
    if stale:
        lines.append("")
        lines.append("Stale:")
        for thread_id in stale[:10]:
            lines.append(f"- {thread_id[:8]}")
    if missing or wrong_project:
        lines.append("")
        lines.append("Run `!mirror sync` to repair.")
    return "\n".join(lines)


def format_context_usage_line(thread: bridge.ThreadInfo) -> str:
    context_usage = bridge.get_thread_context_usage(thread)
    if context_usage is None:
        return "context: -"
    status = bridge.describe_thread_context_usage(context_usage)
    archive_hint = "yes" if bridge.should_recommend_archive(thread, context_usage) else "no"
    return (
        f"context: {context_usage.usage_ratio * 100:.1f}% ({status}) "
        f"last={bridge.format_token_k(context_usage.last_input_tokens)} "
        f"peak={bridge.format_token_k(context_usage.peak_input_tokens)} "
        f"window={bridge.format_token_k(context_usage.model_context_window)} "
        f"archive_recommended={archive_hint}"
    )


def build_context_warning(target_thread_id: str | None) -> str:
    try:
        resolved_thread_id, _target_ref = resolve_target_ref(target_thread_id)
        if not resolved_thread_id:
            return ""
        thread = bridge.choose_thread(resolved_thread_id, None)
        context_usage = bridge.get_thread_context_usage(thread)
    except Exception as exc:
        log_line(f"context_warning_unavailable target={target_thread_id or '-'} error={exc}")
        return ""
    if context_usage is None:
        return ""
    status = bridge.describe_thread_context_usage(context_usage)
    archive_recommended = bridge.should_recommend_archive(thread, context_usage)
    if status not in {"high", "critical"} and not archive_recommended:
        return ""
    return (
        f"Context warning: {context_usage.usage_ratio * 100:.1f}% ({status}), "
        f"archive_recommended={'yes' if archive_recommended else 'no'}. "
        "Use `!context` to inspect, or `!new <prompt>` to continue in a fresh mirrored thread."
    )


def build_context_message(channel_id: int | None = None, *, all_threads: bool = False, limit: int = 10) -> str:
    if not all_threads:
        target_thread_id = get_mirrored_codex_thread_id(channel_id)
        if not target_thread_id:
            selected_thread_id, _target_ref = resolve_selected_target()
            target_thread_id = selected_thread_id
        if not target_thread_id:
            return "No Codex thread target found."
        try:
            thread = bridge.choose_thread(target_thread_id, None)
        except Exception as exc:
            return f"Context unavailable.\n\nERROR: {exc}"
        return "\n".join(
            [
                "Context status",
                f"thread_ref: {bridge.get_thread_workspace_ref(thread)}",
                f"title: {bridge.get_thread_ui_name(thread.id, thread) or thread.title or '-'}",
                format_context_usage_line(thread),
                f"tokens_used_total: {bridge.format_token_k(thread.tokens_used)}",
            ]
        )

    threads = bridge.load_recent_threads(limit=max(1, min(50, limit)))
    lines = ["Context status"]
    for thread in threads:
        title = bridge.get_thread_ui_name(thread.id, thread) or thread.title or thread.id[:8]
        lines.append(
            f"- {bridge.get_thread_workspace_ref(thread)} / {title}: "
            f"{format_context_usage_line(thread)}; total={bridge.format_token_k(thread.tokens_used)}"
        )
    return "\n".join(lines)


def parse_event_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_percent(value: object) -> str:
    if isinstance(value, int | float):
        return f"{float(value):.1f}%"
    try:
        return f"{float(str(value)):.1f}%"
    except (TypeError, ValueError):
        return "-"


def format_window_minutes(value: object) -> str:
    minutes = bridge.coerce_nonnegative_int(value)
    if minutes <= 0:
        return "-"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def format_rate_limit_reset(value: object) -> str:
    reset_at = bridge.coerce_nonnegative_int(value)
    if reset_at <= 0:
        return "-"
    return bridge.format_timestamp(reset_at)


def format_rate_limit_line(label: str, value: object) -> str:
    if not isinstance(value, dict):
        return f"{label}: -"
    return (
        f"{label}: used={format_percent(value.get('used_percent'))} "
        f"window={format_window_minutes(value.get('window_minutes'))} "
        f"resets={format_rate_limit_reset(value.get('resets_at'))}"
    )


def build_weekly_usage_message(days: int = 7) -> str:
    days = max(1, min(30, days))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sessions_dir = bridge.CODEX_HOME / "sessions"
    if not sessions_dir.exists():
        return f"Local usage estimate unavailable: sessions directory not found at {sessions_dir}"

    turns = 0
    token_events = 0
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    files_scanned = 0
    recent_threads: set[str] = set()
    latest_rate_limits: dict[str, object] | None = None
    latest_rate_limits_at: datetime | None = None

    for session_path in sessions_dir.rglob("*.jsonl"):
        files_scanned += 1
        try:
            for event in bridge.iter_session_events(session_path):
                moment = parse_event_timestamp(event.get("timestamp"))
                if moment is None or moment < cutoff:
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if event.get("type") == "session_meta":
                    thread_id = str(payload.get("id") or "").strip()
                    if thread_id:
                        recent_threads.add(thread_id)
                    continue
                if event.get("type") != "event_msg":
                    continue
                event_type = payload.get("type")
                if event_type == "task_started":
                    turns += 1
                    turn_id = str(payload.get("turn_id") or "").strip()
                    if turn_id:
                        recent_threads.add(turn_id)
                    continue
                if event_type != "token_count":
                    continue
                info = payload.get("info") or {}
                if not isinstance(info, dict):
                    continue
                rate_limits = payload.get("rate_limits")
                if isinstance(rate_limits, dict) and (
                    latest_rate_limits_at is None or moment > latest_rate_limits_at
                ):
                    latest_rate_limits = rate_limits
                    latest_rate_limits_at = moment
                last_usage = info.get("last_token_usage") or {}
                if not isinstance(last_usage, dict):
                    continue
                token_events += 1
                event_input = bridge.coerce_nonnegative_int(last_usage.get("input_tokens"))
                event_total = bridge.coerce_nonnegative_int(last_usage.get("total_tokens"))
                input_tokens += event_input
                total_tokens += event_total
                output_tokens += max(0, event_total - event_input)
        except Exception:
            continue

    lines = [f"Codex usage ({days}d local scan)"]
    if latest_rate_limits:
        seen_at = (
            latest_rate_limits_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if latest_rate_limits_at
            else "-"
        )
        lines.extend(
            [
                "Latest rate limits",
                f"seen_at: {seen_at}",
                f"plan: {latest_rate_limits.get('plan_type') or '-'}",
                f"limit_id: {latest_rate_limits.get('limit_id') or '-'}",
                format_rate_limit_line("primary", latest_rate_limits.get("primary")),
                format_rate_limit_line("secondary", latest_rate_limits.get("secondary")),
                f"credits: {latest_rate_limits.get('credits') or '-'}",
                f"reached: {latest_rate_limits.get('rate_limit_reached_type') or '-'}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "Latest rate limits",
                "not found in recent local token_count events",
                "",
            ]
        )

    lines.extend(
        [
            "Local token estimate",
            f"turns: {turns}",
            f"token_events: {token_events}",
            f"total_tokens: {bridge.format_token_k(total_tokens)}",
            f"input_tokens: {bridge.format_token_k(input_tokens)}",
            f"output_tokens_est: {bridge.format_token_k(output_tokens)}",
            f"recent_threads_seen: {len(recent_threads)}",
            f"session_files_scanned: {files_scanned}",
        ]
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


async def build_runners_message() -> str:
    async with THREAD_RUNNERS_LOCK:
        items = list(THREAD_RUNNERS.items())
    if not items:
        return "No active Discord runner queues."
    lines = ["Discord runner queues"]
    for key, runner in items:
        queue = runner.get("queue")
        queue_size = queue.qsize() if isinstance(queue, asyncio.Queue) else 0
        target_thread_id = str(runner.get("target_thread_id") or "").strip() or None
        _thread_id, target_ref = resolve_target_ref(target_thread_id)
        lines.append(
            f"- {target_ref}: active={bool(runner.get('active'))} queued={queue_size} key={key[:8]}"
        )
    return "\n".join(lines)


def resolve_target_ref(target_thread_id: str | None) -> tuple[str | None, str]:
    if not target_thread_id:
        return resolve_selected_target()
    try:
        thread = bridge.choose_thread(target_thread_id, None)
        return thread.id, bridge.get_thread_workspace_ref(thread)
    except Exception:
        return target_thread_id, target_thread_id[:8]


def get_interactive_state_for_thread(target_thread_id: str | None) -> tuple[str, str | None, str]:
    if not target_thread_id:
        return get_selected_interactive_state()
    target_thread_id, target_ref = resolve_target_ref(target_thread_id)
    if not target_thread_id:
        return INTERACTIVE_STATE_NONE, None, target_ref
    try:
        thread = bridge.choose_thread(target_thread_id, None)
        busy_state = bridge.get_thread_busy_state(thread, allow_resume=True)
    except Exception:
        return INTERACTIVE_STATE_NONE, target_thread_id, target_ref
    if busy_state not in {INTERACTIVE_STATE_INPUT, INTERACTIVE_STATE_APPROVAL}:
        return INTERACTIVE_STATE_NONE, target_thread_id, target_ref
    return busy_state, target_thread_id, target_ref


def get_busy_state_for_thread(target_thread_id: str | None) -> tuple[str, str | None, str]:
    target_thread_id, target_ref = resolve_target_ref(target_thread_id)
    if not target_thread_id:
        return "idle", None, target_ref
    try:
        thread = bridge.choose_thread(target_thread_id, None)
        busy_state = bridge.get_thread_busy_state(thread, allow_resume=True)
    except Exception as exc:
        log_line(f"busy_state_check_failed target={target_thread_id} error={exc}")
        return "idle", target_thread_id, target_ref
    return busy_state or "idle", target_thread_id, target_ref


def normalize_runner_key(target_thread_id: str | None) -> str:
    return target_thread_id or "__selected__"


async def get_thread_runner(target_thread_id: str | None) -> dict[str, object]:
    key = normalize_runner_key(target_thread_id)
    async with THREAD_RUNNERS_LOCK:
        runner = THREAD_RUNNERS.get(key)
        if runner is None:
            runner = {
                "queue": asyncio.Queue(),
                "task": None,
                "active": False,
                "target_thread_id": target_thread_id,
            }
            THREAD_RUNNERS[key] = runner
        return runner


async def is_thread_runner_busy(target_thread_id: str | None) -> bool:
    runner = await get_thread_runner(target_thread_id)
    queue = runner["queue"]
    return bool(runner.get("active")) or (
        isinstance(queue, asyncio.Queue) and queue.qsize() > 0
    )


async def wait_for_codex_thread_idle(
    target_thread_id: str | None,
    *,
    timeout_sec: float = 3600.0,
    poll_sec: float = 5.0,
) -> tuple[str, str | None, str]:
    deadline = time.monotonic() + timeout_sec
    last_state = "idle"
    last_thread_id: str | None = None
    last_ref = ""
    while time.monotonic() < deadline:
        state, resolved_thread_id, target_ref = await asyncio.to_thread(
            get_busy_state_for_thread,
            target_thread_id,
        )
        last_state = state
        last_thread_id = resolved_thread_id
        last_ref = target_ref
        if state == "idle":
            return state, resolved_thread_id, target_ref
        await asyncio.sleep(poll_sec)
    return last_state, last_thread_id, last_ref


async def enqueue_thread_ask(
    channel: discord.abc.Messageable,
    prompt: str,
    target_thread_id: str | None,
    *,
    queued: bool = False,
    ack_sent: bool = False,
    source_message: discord.Message | None = None,
) -> int:
    runner = await get_thread_runner(target_thread_id)
    queue = runner["queue"]
    if not isinstance(queue, asyncio.Queue):
        raise RuntimeError("Thread runner queue is invalid.")
    await queue.put(
        {
            "channel": channel,
            "prompt": prompt,
            "target_thread_id": target_thread_id,
            "queued": queued,
            "ack_sent": ack_sent,
            "source_message": source_message,
        }
    )
    task = runner.get("task")
    if not isinstance(task, asyncio.Task) or task.done():
        runner["task"] = asyncio.create_task(thread_runner_loop(target_thread_id))
    return queue.qsize()


async def report_thread_runner_job_failed(job: object, target_thread_id: str | None) -> None:
    channel = job.get("channel") if isinstance(job, dict) else None
    if channel is None or not hasattr(channel, "send"):
        return
    try:
        await channel.send("Queued ask failed. Check codex_discord_bot.log.")
        log_line(f"thread_runner_job_failure_reported target={target_thread_id or '-'}")
    except Exception:
        log_line("thread_runner_job_failure_report_failed\n" + traceback.format_exc())


async def thread_runner_loop(target_thread_id: str | None) -> None:
    key = normalize_runner_key(target_thread_id)
    while True:
        runner = await get_thread_runner(target_thread_id)
        queue = runner["queue"]
        if not isinstance(queue, asyncio.Queue):
            return
        try:
            job = await asyncio.wait_for(queue.get(), timeout=5)
        except asyncio.TimeoutError:
            async with THREAD_RUNNERS_LOCK:
                current = THREAD_RUNNERS.get(key)
                if current is runner and not bool(current.get("active")) and queue.empty():
                    THREAD_RUNNERS.pop(key, None)
                    return
            continue

        runner["active"] = True
        try:
            channel = job.get("channel")
            prompt = str(job.get("prompt") or "").strip()
            job_target_thread_id = str(job.get("target_thread_id") or "").strip() or None
            if prompt and isinstance(channel, discord.abc.Messageable):
                queued = bool(job.get("queued"))
                ack_sent = bool(job.get("ack_sent"))
                if queued:
                    busy_state, _busy_thread_id, busy_ref = await asyncio.to_thread(
                        get_busy_state_for_thread,
                        job_target_thread_id,
                    )
                    if busy_state != "idle":
                        await channel.send(
                            f"Queued ask waiting for `{busy_ref or job_target_thread_id or 'selected'}` "
                            f"to become idle. Current state: {busy_state}."
                        )
                        busy_state, _busy_thread_id, busy_ref = await wait_for_codex_thread_idle(
                            job_target_thread_id,
                        )
                        if busy_state != "idle":
                            await channel.send(
                                f"Queued ask is still blocked for `{busy_ref or job_target_thread_id or 'selected'}`. "
                                f"Current state: {busy_state}."
                            )
                            continue
                await run_prompt_and_send(
                    channel,
                    prompt,
                    queued=queued,
                    ack_sent=ack_sent,
                    source_message=job.get("source_message"),  # type: ignore[arg-type]
                    target_thread_id=job_target_thread_id,
                )
        except Exception:
            log_line("thread_runner_job_failed\n" + traceback.format_exc())
            await report_thread_runner_job_failed(job, target_thread_id)
        finally:
            runner["active"] = False
            queue.task_done()


async def run_prompt_and_send(
    channel: discord.abc.Messageable,
    prompt: str,
    *,
    queued: bool = False,
    ack_sent: bool = False,
    source_message: discord.Message | None = None,
    target_thread_id: str | None = None,
) -> None:
    label = "Queued ask started." if queued else "Ask started."
    if not ack_sent:
        await channel.send(label)
    target_thread_id, target_ref = resolve_target_ref(target_thread_id)
    relay = DiscordAskRelay(asyncio.get_running_loop(), channel, target_thread_id, target_ref)
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
            await channel.send(
                build_busy_choice_message(prompt, target_thread_id),
                view=BusyChoiceView(
                    source_message,
                    prompt,
                    target_thread_id=target_thread_id,
                    allow_steer=True,
                ),
            )
            log_busy_choice_sent("late_busy_failure", target_thread_id, prompt)
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
    if relay.sent_live:
        if exit_code == 0 and not relay.saw_aborted:
            await channel.send("Done.")
        elif not relay.saw_aborted and not relay.saw_timeout:
            await send_chunks(channel, f"Ask failed (exit {exit_code})\n\n{output or '(no output)'}")
        return
    title = "Ask finished" if exit_code == 0 else f"Ask failed (exit {exit_code})"
    await send_chunks(channel, f"{title}\n\n{output or '(no output)'}")


def is_selected_thread_busy_error(exit_code: int, output: str) -> bool:
    if exit_code == 0:
        return False
    text = (output or "").lower()
    return (
        "selected thread is still busy" in text
        or "target thread is still busy" in text
        or "--force-while-busy" in text and "still busy" in text
        or "selected thread is waiting on a follow-up choice or input" in text
        or "selected thread is waiting on an approval prompt" in text
    )


def has_busy_choice_source(source_message: object) -> bool:
    return bool(
        source_message is not None
        and getattr(source_message, "author", None) is not None
        and getattr(source_message, "channel", None) is not None
    )


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
        await channel.send(
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
    await channel.send(
        "\n\n".join(
            part
            for part in [
                "Ask received. Sending to Codex.",
                warning,
            ]
            if part
        )
    )
    await enqueue_thread_ask(
        channel,
        prompt,
        target_thread_id,
        queued=queued,
        ack_sent=True,
        source_message=source_message,
    )


def build_busy_choice_message(prompt: str, target_thread_id: str | None) -> str:
    lines = ["This Codex thread is already working.", ""]
    warning = build_context_warning(target_thread_id)
    if warning:
        lines.extend([warning, ""])
    footer = "\n\nChoose how to handle this message for this thread."
    prefix = "\n".join(lines)
    prompt_text = str(prompt or "")
    prompt_budget = max(0, DISCORD_MAX_LEN - len(prefix) - len(footer))
    if len(prompt_text) > prompt_budget:
        suffix = "\n\n[prompt truncated for Discord]"
        prompt_text = prompt_text[: max(0, prompt_budget - len(suffix))].rstrip() + suffix
    return fit_single_message(prefix + prompt_text + footer)


def log_busy_choice_sent(reason: str, target_thread_id: str | None, prompt: str) -> None:
    safe_reason = reason.replace("\n", " ")[:80]
    log_line(
        f"busy_choice_sent reason={safe_reason} target={target_thread_id or '-'} "
        f"prompt_len={format_log_text_len(prompt)}"
    )


async def handle_plain_ask(
    message: discord.Message,
    prompt: str,
    *,
    target_thread_id: str | None = None,
) -> None:
    interactive_state, resolved_thread_id, target_ref = get_interactive_state_for_thread(target_thread_id)
    if interactive_state and resolved_thread_id:
        await submit_interactive_reply(
            message.channel,
            resolved_thread_id,
            target_ref,
            interactive_state,
            prompt,
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
        view = BusyChoiceView(
            message,
            prompt,
            target_thread_id=target_thread_id,
            allow_steer=True,
        )
        await message.channel.send(
            build_busy_choice_message(prompt, target_thread_id),
            view=view,
        )
        log_busy_choice_sent("codex_busy_preflight", target_thread_id, prompt)
        return

    if await is_thread_runner_busy(target_thread_id):
        allow_steer = True
        view = BusyChoiceView(
            message,
            prompt,
            target_thread_id=target_thread_id,
            allow_steer=allow_steer,
        )
        await message.channel.send(
            build_busy_choice_message(prompt, target_thread_id),
            view=view,
        )
        log_busy_choice_sent("runner_busy_preflight", target_thread_id, prompt)
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
        exit_code, output = await asyncio.to_thread(submit_approval_reply, target_thread_id, answer)
        log_line(
            f"approval_reply_done exit={exit_code} target={target_thread_id} "
            f"answer={answer[:40].replace(chr(10), ' ')} "
            f"output_len={format_log_text_len(output)}"
        )
        title = "Approval submitted" if exit_code == 0 else f"Approval failed (exit {exit_code})"
        await send_chunks(channel, f"{title}\n\n{output or '(no output)'}")
        return
    if state == INTERACTIVE_STATE_INPUT:
        exit_code, output = await asyncio.to_thread(submit_input_reply, target_thread_id, answer)
        log_line(
            f"input_reply_done exit={exit_code} target={target_thread_id} "
            f"answer={answer[:40].replace(chr(10), ' ')} "
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
        await interaction.response.defer(thinking=True)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        log_line(f"approval_button user={interaction.user.id} answer={answer}")
        exit_code, output = await asyncio.to_thread(submit_approval_reply, self.target_thread_id, answer)
        log_line(
            f"approval_button_done exit={exit_code} target={self.target_thread_id} "
            f"answer={answer}"
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
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary)
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
        await interaction.response.defer(thinking=True)
        if isinstance(view, InputChoiceView):
            try:
                await interaction.message.edit(view=view)
            except Exception:
                pass
        log_line(f"input_choice_button user={interaction.user.id} value={self.value}")
        exit_code, output = await asyncio.to_thread(submit_input_reply, self.target_thread_id, self.value)
        log_line(
            f"input_choice_button_done exit={exit_code} target={self.target_thread_id} "
            f"value={self.value}"
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
    ) -> None:
        super().__init__(timeout=900)
        self.message = message
        self.prompt = prompt
        self.target_thread_id = target_thread_id
        self.allow_steer = allow_steer
        self.claimed = False
        if not allow_steer:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label == "Steer now":
                    item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.message.author.id:
            return True
        await interaction.response.send_message("Only the original sender can choose this.", ephemeral=True)
        return False

    def claim(self) -> bool:
        if self.claimed:
            return False
        self.claimed = True
        self.disable_all_items()
        return True

    @discord.ui.button(label="Steer now", style=discord.ButtonStyle.primary)
    async def steer_now(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.claim():
            await interaction.response.send_message("This busy choice was already handled.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        if not self.allow_steer:
            await interaction.followup.send("This message targets a different Codex thread. Queue it instead.")
            log_line(
                f"steer_now_rejected user={interaction.user.id} "
                f"target={self.target_thread_id or '-'} reason=not_allowed"
            )
            return
        log_line(
            f"steer_now user={interaction.user.id} target={self.target_thread_id or '-'} "
            f"prompt={self.prompt[:160].replace(chr(10), ' ')}"
        )
        exit_code, output = await asyncio.to_thread(
            run_steering_prompt,
            self.prompt,
            self.target_thread_id,
        )
        log_line(
            f"steer_now_done exit={exit_code} target={self.target_thread_id or '-'} "
            f"output_len={format_log_text_len(output)}"
        )
        if is_selected_thread_busy_error(exit_code, output):
            await interaction.followup.send(
                build_busy_choice_message(self.prompt, self.target_thread_id),
                view=BusyChoiceView(
                    self.message,
                    self.prompt,
                    target_thread_id=self.target_thread_id,
                    allow_steer=True,
                ),
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
        )
        log_line(f"steer_now_sent exit={exit_code} target={self.target_thread_id or '-'}")

    @discord.ui.button(label="Queue next", style=discord.ButtonStyle.secondary)
    async def queue_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.claim():
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
                f"prompt={self.prompt[:160].replace(chr(10), ' ')}"
            )
            await interaction.followup.send("No active job now. Starting this message.")
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
            f"prompt={self.prompt[:160].replace(chr(10), ' ')}"
        )
        await interaction.followup.send(f"Queued at position {position}.")
        log_line(
            f"queue_next_sent user={interaction.user.id} position={position} "
            f"target={self.target_thread_id or '-'}"
        )

    @discord.ui.button(label="Ignore", style=discord.ButtonStyle.danger)
    async def ignore(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.claim():
            await interaction.response.send_message("This busy choice was already handled.", ephemeral=True)
            return
        log_line(f"ignore_busy_prompt user={interaction.user.id}")
        await interaction.response.send_message("Ignored.")
        log_line(f"ignore_busy_prompt_sent user={interaction.user.id}")
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


async def handle_prefix_command(bot: CodexDiscordBot, message: discord.Message, command_line: str) -> None:
    if not command_line:
        await send_chunks(message.channel, build_help())
        return
    command, _, arg = command_line.partition(" ")
    command = command.lower().strip()
    arg = arg.strip()

    if command in {"help", "start"}:
        await send_chunks(message.channel, build_help())
        return
    if command == "list":
        limit = str(parse_bounded_int_arg(arg, default=10, minimum=1, maximum=30))
        await run_bridge_and_send(message.channel, ["list", "--limit", limit], "List")
        return
    if command in {"archived_list", "archive_list"}:
        limit = str(parse_bounded_int_arg(arg, default=10, minimum=1, maximum=50))
        await run_bridge_and_send(message.channel, ["archived_list", "--limit", limit], "Archived list")
        return
    if command == "use":
        if not arg:
            await message.channel.send("Usage: !use <ref>")
            return
        await run_bridge_and_send(message.channel, ["use", arg], "Use")
        return
    if command in {"open", "open_abort"}:
        if not arg:
            await message.channel.send(f"Usage: !{command} <ref>")
            return
        argv = ["open"]
        if command == "open_abort":
            argv.append("--abort")
        argv.append(arg)
        await run_bridge_and_send(message.channel, argv, "Open")
        return
    if command == "status":
        argv = ["status"]
        argv.extend(resolve_discord_thread_target_args(message.channel.id, arg or None))
        await run_bridge_and_send(message.channel, argv, "Status")
        return
    if command == "doctor":
        await run_bridge_and_send(message.channel, ["doctor"], "Doctor")
        return
    if command == "discover_codex":
        await run_bridge_and_send(message.channel, ["discover_codex"], "Codex path")
        return
    if command == "restart_codex":
        await run_bridge_and_send(message.channel, ["restart_codex"], "Codex restart")
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
        days = 7
        if arg:
            try:
                days = max(1, min(30, int(arg)))
            except ValueError:
                await message.channel.send("Usage: !usage [days]")
                return
        await send_chunks(message.channel, build_weekly_usage_message(days=days))
        return
    if command in {"runners", "queues"}:
        await send_chunks(message.channel, await build_runners_message())
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
    if command == "archive":
        argv = ["archive"]
        argv.extend(resolve_discord_thread_target_args(message.channel.id, arg or None))
        await run_bridge_and_send(message.channel, argv, "Archive")
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
    if command == "confirm_delete_archive":
        if not arg:
            await message.channel.send("Usage: !confirm_delete_archive <ref>")
            return
        await run_bridge_and_send(
            message.channel,
            ["delete_archive", "--confirm", arg],
            "Delete archive",
        )
        return
    if command == "mirror":
        subcommand, _, subarg = arg.partition(" ")
        subcommand = (subcommand or "sync").lower().strip()
        subarg = subarg.strip()
        if subcommand == "sync":
            limit = 30
            if subarg:
                try:
                    limit = max(1, min(100, int(subarg)))
                except ValueError:
                    await message.channel.send("Usage: !mirror sync [limit]")
                    return
            await message.channel.send("Mirror sync started.")
            try:
                output = await sync_codex_mirror(bot, limit=limit)
                await send_chunks(message.channel, output)
            except Exception as exc:
                log_line("mirror_sync_failed\n" + traceback.format_exc())
                await send_chunks(message.channel, f"Mirror sync failed\n\nERROR: {exc}")
            return
        if subcommand == "list":
            limit = 30
            if subarg:
                try:
                    limit = max(1, min(100, int(subarg)))
                except ValueError:
                    await message.channel.send("Usage: !mirror list [limit]")
                    return
            await send_chunks(message.channel, build_mirror_list(limit=limit))
            return
        if subcommand in {"check", "doctor"}:
            await send_chunks(message.channel, build_mirror_check())
            return
        await message.channel.send("Usage: !mirror sync [limit] | !mirror list [limit] | !mirror check")
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
                await message.channel.send(project_message)
                return
        await handle_plain_ask(message, arg, target_thread_id=target_thread_id)
        return

    await message.channel.send(f"Unknown command: !{format_discord_command_label(command)}")


def build_help() -> str:
    return "\n".join(
        [
            "Codex Discord commands",
            "!help",
            "!list [limit]",
            "!archived_list [limit]  (alias: !archive_list)",
            "!use <ref>",
            "!open <ref>",
            "!open_abort <ref>",
            "!status [ref]",
            "!doctor",
            "!discover_codex",
            "!restart_codex",
            "!chatid",
            "!where",
            "!context [all]",
            "!usage [days]",
            "!runners",
            "!mirror sync [limit]",
            "!mirror list [limit]",
            "!mirror check",
            "!approval",
            "!archive [ref]",
            "!delete_archive <ref>",
            "!confirm_delete_archive <ref>",
            "!new <prompt>  (create a new Codex thread with the first prompt)",
            "!ask <prompt>",
            "",
            "Plain messages in mirrored Discord threads are sent to that Codex thread.",
            "Slash commands: /help, /list, /archived_list, /use, /status, /doctor, /where, /context, /usage, /runners, /mirror_check, /new, /ask, /ask_ipc.",
        ]
    )


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
            ["list", "--limit", str(max(1, min(30, limit)))],
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
            ["archived_list", "--limit", str(max(1, min(50, limit)))],
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
        argv = ["status"]
        argv.extend(resolve_discord_thread_target_args(interaction.channel_id, ref or None))
        await run_interaction_bridge_and_send(interaction, argv, "Status")

    @bot.tree.command(name="doctor", description="Run Codex bridge diagnostics.")
    async def slash_doctor(interaction: discord.Interaction) -> None:
        if not check_interaction_allowed(bot, interaction):
            await interaction.response.send_message("This channel/user is not allowed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
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


def check_interaction_allowed(bot: CodexDiscordBot, interaction: discord.Interaction) -> bool:
    if not bot.is_allowed_user(interaction.user.id):
        return False
    if bot.is_allowed_channel(interaction.channel_id):
        return True
    if is_mirrored_channel_id(interaction.channel_id):
        return True
    channel = interaction.channel
    if channel is not None and bot.is_allowed_message_channel(channel):
        return True
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
        f"guild_id={guild_id or '-'} channels={sorted(channel_ids) if channel_ids else 'ALL'} "
        f"users={sorted(user_ids) if user_ids else 'ALL'} "
        f"message_content={enable_prefix_commands}"
    )
    bot.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
