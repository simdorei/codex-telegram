"""
Telegram adapter for codex_desktop_bridge.py.

This uses the Telegram Bot HTTP API directly with the Python standard library.
It keeps the Codex bridge in-process so ask/watch behavior stays alive.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import codex_desktop_bridge as bridge


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
LOG_PATH = SCRIPT_DIR / "codex_telegram_bot.log"
PROBE_LOG_PATH = SCRIPT_DIR / "_ipc_probe_log.jsonl"
TELEGRAM_MAX_LEN = 3900
ACTIVE_JOB_LOCK = threading.Lock()
ACTIVE_JOB: dict[str, object] = {"thread": None, "chat_id": None, "summary": ""}
PENDING_ASKS_LOCK = threading.Lock()
PENDING_ASKS: list[dict[str, object]] = []
ASK_WAITERS_LOCK = threading.Lock()
ASK_WAITERS: dict[int, dict[str, object]] = {}
FOLLOW_WATCHERS_LOCK = threading.Lock()
FOLLOW_WATCHERS: dict[int, dict[str, object]] = {}
SINGLE_INSTANCE_MUTEX = None
RESTART_LOCK = threading.Lock()
RESTART_SCHEDULED = False
LOG_FILE_LOCK = threading.Lock()

ERROR_ALREADY_EXISTS = 183
TELEGRAM_HELP_LINES = [
    "Commands / 명령",
    "/list [limit]",
    "/archived_list [limit]",
    "/new <prompt>",
    "/archive [ref]",
    "/delete_archive <ref>",
    "/confirm_delete_archive <ref>",
    "/open <ref>",
    "/open_abort <ref>",
    "/use <ref>",
    "/status [ref]",
    "/doctor",
    "/discover_codex",
    "/ask <prompt>",
    "/ask_ipc <prompt> (alias)",
    "/restart_bot",
    "/restart_codex",
    "/chatid",
    "",
    "Plain text works like /ask <message>.",
    "If the selected thread is waiting-input, plain text replies to that prompt.",
    "If the selected thread is waiting-approval, follow the shown options.",
    "일반 텍스트 메시지는 /ask <message>처럼 동작합니다.",
    "",
    "Thread refs follow the bridge format:",
    "ai:1, ai:2, taxlab, other, 1, 2",
]
LIST_THREAD_LINE_RE = re.compile(
    r"^(\s*\*?\s*\d+\s*\|\s*[^|]+\|\s*)(waiting-input|waiting-approval|busy|idle)(\s*\|.*)$"
)
INTERACTIVE_INPUT_TAG = "[choice_required]"
INTERACTIVE_APPROVAL_TAG = "[approval_required]"
INTERACTIVE_STATE_NONE = ""
INTERACTIVE_STATE_INPUT = "waiting-input"
INTERACTIVE_STATE_APPROVAL = "waiting-approval"


def acquire_single_instance_mutex(token: str | None = None) -> bool:
    global SINGLE_INSTANCE_MUTEX

    mutex_source = (token or "").strip() or str(SCRIPT_DIR)
    mutex_key = hashlib.sha1(mutex_source.encode("utf-8")).hexdigest()
    mutex_name = f"Local\\CodexTelegramBot_{mutex_key}"
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wt.DWORD

    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return True

    SINGLE_INSTANCE_MUTEX = handle
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def release_single_instance_mutex() -> None:
    global SINGLE_INSTANCE_MUTEX
    handle = SINGLE_INSTANCE_MUTEX
    if not handle:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    SINGLE_INSTANCE_MUTEX = None


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


def log_line(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}\n"
    try:
        with LOG_FILE_LOCK:
            bridge.rotate_single_backup_file(
                LOG_PATH,
                incoming_bytes=len(line.encode("utf-8")),
            )
            bridge.rotate_single_backup_file(PROBE_LOG_PATH)
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        pass


def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_allowed_chat_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return set()
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def telegram_api(method: str, token: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    if params is not None:
        data = urllib.parse.urlencode(params).encode("utf-8")
    with urllib.request.urlopen(url, data=data, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {payload}")
    return payload


def split_message(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    text = text.strip()
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


def send_text(token: str, chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
    chunks = split_message(text)
    log_line(
        f"send_text chat_id={chat_id} reply_to={reply_to_message_id} chunks={len(chunks)} "
        f"preview={text[:120].replace(chr(10), ' ')}"
    )
    for chunk in chunks:
        params = {
            "chat_id": str(chat_id),
            "text": chunk,
        }
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = str(reply_to_message_id)
        telegram_api("sendMessage", token, params=params)


def build_help_message() -> str:
    return "\n".join(TELEGRAM_HELP_LINES)


def build_busy_queue_message(active_summary: str, position: int) -> str:
    return "\n".join(
        [
            "Busy.",
            "",
            active_summary,
            "",
            f"Queued at position {position}.",
            "현재 작업이 끝나면 자동으로 이어서 처리합니다.",
        ]
    )


def send_usage(token: str, chat_id: int, reply_to_message_id: int | None, usage: str) -> None:
    send_text(token, chat_id, f"Usage: {usage}", reply_to_message_id=reply_to_message_id)


def send_bridge_command_result(
    token: str,
    chat_id: int,
    reply_to_message_id: int | None,
    argv: list[str],
    success_prefix: str,
    failure_prefix: str | None = None,
) -> tuple[int, str]:
    exit_code, output = run_bridge_command(argv)
    failed_prefix = failure_prefix or success_prefix
    prefix = success_prefix if exit_code == 0 else f"{failed_prefix} (exit {exit_code})"
    send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
    return exit_code, output


def parse_bounded_int_arg(raw: str, *, default: int, minimum: int, maximum: int) -> int:
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        return default


def parse_interactive_notice(text: str) -> tuple[str, str]:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    if not lines:
        return INTERACTIVE_STATE_NONE, ""
    first_line = lines[0].strip()
    if first_line not in {INTERACTIVE_INPUT_TAG, INTERACTIVE_APPROVAL_TAG}:
        return INTERACTIVE_STATE_NONE, ""

    prompt_lines: list[str] = []
    if first_line == INTERACTIVE_INPUT_TAG:
        for raw_line in lines[1:]:
            stripped = raw_line.strip()
            if not stripped:
                continue
            if re.match(r"^\d+[\.\)]\s+", stripped):
                continue
            prompt_lines.append(stripped)
        return INTERACTIVE_STATE_INPUT, "\n".join(prompt_lines)

    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if stripped:
            prompt_lines.append(stripped)
    return INTERACTIVE_STATE_APPROVAL, "\n".join(prompt_lines)


def build_interactive_waiting_text(thread_ref: str, state: str, prompt: str) -> str:
    if state == INTERACTIVE_STATE_INPUT:
        lines = ["Waiting input", f"thread: {thread_ref or '-'}", ""]
        if prompt:
            lines.extend([prompt, ""])
        lines.append("Reply here with plain text to answer this prompt.")
        return "\n".join(lines)
    if state == INTERACTIVE_STATE_APPROVAL:
        lines = ["Waiting approval", f"thread: {thread_ref or '-'}", ""]
        prompt_lines = [line.strip() for line in (prompt or "").splitlines() if line.strip()]
        prompt_has_options = any(re.match(r"^\d+[\.\)]\s+", line) for line in prompt_lines)
        if prompt:
            lines.extend(prompt_lines)
            lines.append("")
        if prompt_has_options:
            lines.append("Reply here with 1, 2, or 3.")
            return "\n".join(lines)
        lines.extend(
            [
                "1. yes",
                "2. yes + do not ask again in this session",
                "3. reject + submit reason",
                "",
                "Reply here with 1, 2, or 3.",
            ]
        )
        return "\n".join(lines)
    return ""


def get_current_interactive_prompt(thread: bridge.ThreadInfo) -> tuple[str, str]:
    session_path = Path(thread.rollout_path)
    try:
        busy_state = bridge.get_thread_busy_state(thread, allow_resume=True)
    except Exception:
        return INTERACTIVE_STATE_NONE, ""

    if busy_state == INTERACTIVE_STATE_APPROVAL:
        live_state, live_lines = bridge.get_live_pending_approval_display_lines(
            thread,
            timeout_sec=0.75,
        )
        if live_state and live_lines:
            return live_state, "\n".join(line for line in live_lines if line)

    if not session_path.exists():
        return INTERACTIVE_STATE_NONE, ""

    session_state, session_lines = bridge.get_pending_interactive_display_lines(session_path)
    if not session_state or not session_lines:
        return INTERACTIVE_STATE_NONE, ""
    return session_state, "\n".join(line for line in session_lines if line)


def get_current_interactive_prompt_for_ref(thread_ref: str) -> tuple[str, str]:
    normalized_ref = str(thread_ref or "").strip()
    if not normalized_ref:
        return INTERACTIVE_STATE_NONE, ""
    try:
        thread = bridge.resolve_thread_ref(normalized_ref)
    except Exception:
        return INTERACTIVE_STATE_NONE, ""
    return get_current_interactive_prompt(thread)


def rewrite_list_output_for_telegram(output: str) -> str:
    text = output or "(no output)"
    lines = text.splitlines()
    rewritten: list[str] = []
    for line in lines:
        match = LIST_THREAD_LINE_RE.match(line)
        if not match:
            rewritten.append(line)
            continue
        rewritten.append(line)
    return "\n".join(rewritten)


def resolve_restart_python_exe() -> Path:
    current = Path(sys.executable).resolve()
    if current.name.lower() == "pythonw.exe":
        python_exe = current.with_name("python.exe")
        if python_exe.exists():
            return python_exe
    return current


def _exit_after_restart_delay(delay_sec: float) -> None:
    time.sleep(delay_sec)
    os._exit(0)


def schedule_bot_restart() -> tuple[bool, str]:
    global RESTART_SCHEDULED
    with RESTART_LOCK:
        if RESTART_SCHEDULED:
            return False, "Restart already scheduled."
        restart_exe = resolve_restart_python_exe()
        script_path = SCRIPT_DIR / "codex_telegram_bot.py"
        creationflags = 0
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        release_single_instance_mutex()
        subprocess.Popen(
            [str(restart_exe), str(script_path), "--skip-old-updates"],
            cwd=str(SCRIPT_DIR),
            creationflags=creationflags,
            close_fds=True,
        )
        RESTART_SCHEDULED = True
        log_line(
            f"restart_scheduled exe={restart_exe} script={script_path} args=--skip-old-updates"
        )
        threading.Thread(
            target=_exit_after_restart_delay,
            args=(0.5,),
            daemon=True,
            name="codex-telegram-restart",
        ).start()
        return True, "Restart scheduled."


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


class TelegramAskRelay:
    def __init__(self, token: str, chat_id: int, reply_to_message_id: int | None, thread_ref: str = "") -> None:
        self.token = token
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.thread_ref = thread_ref
        self.mode: str | None = None
        self.block_lines: list[str] = []
        self.sent_live = False
        self.saw_final = False
        self.saw_ready = False
        self.saw_aborted = False
        self.saw_timeout = False
        self.last_interactive_signature = ""

    def _send_interactive_notice_if_detected(self, text: str) -> bool:
        state, prompt = parse_interactive_notice(text)
        if not state:
            return False
        if state == INTERACTIVE_STATE_APPROVAL:
            current_state, current_prompt = get_current_interactive_prompt_for_ref(self.thread_ref)
            if current_state == INTERACTIVE_STATE_APPROVAL and current_prompt:
                state = current_state
                prompt = current_prompt
        signature = "\n".join([state, prompt])
        if signature == self.last_interactive_signature:
            return True
        self.last_interactive_signature = signature
        send_text(
            self.token,
            self.chat_id,
            build_interactive_waiting_text(self.thread_ref, state, prompt),
            reply_to_message_id=self.reply_to_message_id,
        )
        return True

    def _send_block(self) -> None:
        text = "\n".join(self.block_lines).strip()
        if not text:
            self.block_lines = []
            return
        if self.mode == "commentary":
            if not self._send_interactive_notice_if_detected(text):
                send_text(self.token, self.chat_id, f"In progress\n\n{text}", reply_to_message_id=self.reply_to_message_id)
                self.sent_live = True
            else:
                self.sent_live = True
        elif self.mode == "final":
            if not self._send_interactive_notice_if_detected(text):
                send_text(self.token, self.chat_id, text, reply_to_message_id=self.reply_to_message_id)
                self.sent_live = True
                self.saw_final = True
            else:
                self.sent_live = True
        elif self.mode == "timeout":
            send_text(self.token, self.chat_id, f"Timed out\n\n{text}", reply_to_message_id=self.reply_to_message_id)
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
            send_text(self.token, self.chat_id, "Aborted.", reply_to_message_id=self.reply_to_message_id)
            self.sent_live = True
            return
        if line.startswith("[ready]"):
            self._send_block()
            self.mode = None
            self.saw_ready = True
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
        except Exception as exc:  # pragma: no cover - operational path
            exit_code = 1
            print(f"ERROR: {exc}")
    output = stdout_buffer.getvalue()
    stderr = stderr_buffer.getvalue()
    combined = (output + ("\n" + stderr if stderr else "")).strip()
    return exit_code, combined


def run_bridge_script_subprocess(argv: list[str], timeout_sec: float) -> tuple[int, str]:
    bridge_script = SCRIPT_DIR / "codex_desktop_bridge.py"
    python_exe = resolve_restart_python_exe()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [str(python_exe), str(bridge_script), *argv],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "").strip()
        stderr = str(exc.stderr or "").strip()
        combined = stdout
        if stderr:
            combined = f"{combined}\n{stderr}".strip()
        message = combined or f"Bridge subprocess timed out after {timeout_sec:.1f}s."
        return 124, message

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    combined = stdout
    if stderr:
        combined = f"{combined}\n{stderr}".strip()
    return completed.returncode, combined


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
        except Exception as exc:  # pragma: no cover - operational path
            exit_code = 1
            print(f"ERROR: {exc}")
    stream.flush()
    return exit_code, stream.getvalue().strip()


def resolve_status_args(ref: str | None) -> list[str]:
    if not ref:
        return ["status"]
    thread = bridge.resolve_thread_ref(ref)
    return ["status", "--thread-id", thread.id]


def parse_bridge_key_value_output(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip().lower().replace(" ", "_")
        if normalized_key:
            fields[normalized_key] = value.strip()
    return fields


def get_active_job_summary() -> str:
    with ACTIVE_JOB_LOCK:
        thread = ACTIVE_JOB.get("thread")
        summary = str(ACTIVE_JOB.get("summary") or "")
    if thread and getattr(thread, "is_alive", lambda: False)():
        return summary or "A Telegram-triggered Codex job is still running."
    return ""


def set_active_job(thread: threading.Thread | None, chat_id: int | None = None, summary: str = "") -> None:
    with ACTIVE_JOB_LOCK:
        ACTIVE_JOB["thread"] = thread
        ACTIVE_JOB["chat_id"] = chat_id
        ACTIVE_JOB["summary"] = summary


def resolve_selected_target() -> tuple[str | None, str, str]:
    try:
        target_thread = bridge.choose_thread(None, None)
    except Exception:
        return None, "", ""
    return (
        target_thread.id,
        bridge.get_thread_workspace_ref(target_thread),
        bridge.get_thread_label(target_thread),
    )


def get_follow_watcher_target(chat_id: int) -> tuple[str | None, str, str]:
    with FOLLOW_WATCHERS_LOCK:
        current = FOLLOW_WATCHERS.get(chat_id)
        if not current:
            return None, "", ""
        watcher_thread = current.get("thread")
        if watcher_thread and not getattr(watcher_thread, "is_alive", lambda: False)():
            return None, "", ""
        target_thread_id = str(current.get("target_thread_id") or "").strip() or None
        target_ref = str(current.get("target_ref") or "").strip()
    if not target_thread_id:
        return None, "", ""
    try:
        target_thread = bridge.choose_thread(target_thread_id, None)
    except Exception:
        return None, "", ""
    return (
        target_thread.id,
        target_ref or bridge.get_thread_workspace_ref(target_thread),
        bridge.get_thread_label(target_thread),
    )


def resolve_interactive_reply_target(
    chat_id: int,
    prompt: str,
    selected_thread_id: str | None,
    selected_ref: str,
    selected_label: str,
) -> tuple[str | None, str, str]:
    checked_thread_ids: set[str] = set()

    def evaluate_candidate(
        thread_id: str | None,
        thread_ref: str,
        thread_label: str,
    ) -> tuple[str | None, str, str]:
        if not thread_id or thread_id in checked_thread_ids:
            return None, "", ""
        checked_thread_ids.add(thread_id)
        try:
            target_thread = bridge.choose_thread(thread_id, None)
            busy_state = bridge.get_thread_busy_state(target_thread, allow_resume=True)
        except Exception:
            return None, "", ""
        if busy_state not in {INTERACTIVE_STATE_INPUT, INTERACTIVE_STATE_APPROVAL}:
            return None, "", ""
        return (
            target_thread.id,
            thread_ref or bridge.get_thread_workspace_ref(target_thread),
            thread_label or bridge.get_thread_label(target_thread),
        )

    candidate = evaluate_candidate(selected_thread_id, selected_ref, selected_label)
    if candidate[0]:
        return candidate

    follow_thread_id, follow_ref, follow_label = get_follow_watcher_target(chat_id)
    candidate = evaluate_candidate(follow_thread_id, follow_ref, follow_label)
    if candidate[0]:
        return candidate

    normalized_prompt = str(prompt).strip()
    lowered_prompt = normalized_prompt.lower()
    approval_like = normalized_prompt in {"1", "2", "3"} or lowered_prompt in {
        "approve",
        "approved",
        "accept",
        "yes",
        "y",
        "ok",
        "cancel",
        "skip",
        "dismiss",
    }
    if not approval_like:
        return None, "", ""

    try:
        threads = bridge.load_recent_threads(limit=10)
    except Exception:
        return None, "", ""

    approval_candidates: list[tuple[str, str, str]] = []
    for thread in threads:
        if thread.id in checked_thread_ids:
            continue
        try:
            busy_state = bridge.get_thread_busy_state(thread, allow_resume=True)
        except Exception:
            continue
        if busy_state != INTERACTIVE_STATE_APPROVAL:
            continue
        approval_candidates.append(
            (
                thread.id,
                bridge.get_thread_workspace_ref(thread),
                bridge.get_thread_label(thread),
            )
        )
        if len(approval_candidates) > 1:
            break

    if len(approval_candidates) == 1:
        target_thread_id, target_ref, target_label = approval_candidates[0]
        log_line(
            f"interactive_reply_target_override chat_id={chat_id} "
            f"selected_ref={selected_ref or '-'} routed_ref={target_ref or '-'} prompt={prompt[:80]}"
        )
        return target_thread_id, target_ref, target_label

    return None, "", ""


def maybe_submit_waiting_input_reply(
    token: str,
    chat_id: int,
    prompt: str,
    reply_to_message_id: int | None,
    target_thread_id: str | None,
    target_ref: str,
) -> bool:
    if not target_thread_id:
        return False
    try:
        target_thread = bridge.choose_thread(target_thread_id, None)
    except Exception:
        return False

    try:
        busy_state = bridge.get_thread_busy_state(target_thread, allow_resume=True)
    except Exception:
        log_line("waiting_input_state_error\n" + traceback.format_exc())
        return False

    if busy_state == INTERACTIVE_STATE_APPROVAL:
        log_line(
            f"approval_reply_start chat_id={chat_id} thread={target_thread.id} "
            f"ref={target_ref or bridge.get_thread_workspace_ref(target_thread)} prompt={prompt[:80]}"
        )
        exit_code, output = run_bridge_script_subprocess(
            [
                "approval_reply",
                "--thread-id",
                target_thread.id,
                "--timeout",
                "8.0",
                prompt,
            ],
            timeout_sec=12.0,
        )
        log_line(
            f"approval_reply_finish chat_id={chat_id} thread={target_thread.id} "
            f"exit_code={exit_code} preview={(output or '(no output)')[:160]}"
        )
        if exit_code != 0:
            send_text(
                token,
                chat_id,
                "\n".join(
                    [
                        "Waiting approval reply failed.",
                        f"thread: {target_ref or bridge.get_thread_workspace_ref(target_thread)}",
                        "",
                        output or "(no output)",
                    ]
                ),
                reply_to_message_id=reply_to_message_id,
            )
            return True

        result = parse_bridge_key_value_output(output)
        lines = [
            "Approval submitted.",
            f"thread: {target_ref or result.get('thread_ref') or bridge.get_thread_workspace_ref(target_thread)}",
            f"decision: {result.get('decision_action') or '-'}",
        ]
        request_kind = str(result.get("request_kind") or "").strip()
        if request_kind and request_kind != "-":
            lines.append(f"kind: {request_kind}")
        send_text(
            token,
            chat_id,
            "\n".join(lines),
            reply_to_message_id=reply_to_message_id,
        )
        ensure_follow_watcher(
            token=token,
            chat_id=chat_id,
            target_thread_id=target_thread.id,
            target_ref=target_ref or result.get("thread_ref") or bridge.get_thread_workspace_ref(target_thread),
            reply_to_message_id=reply_to_message_id,
        )
        return True

    if busy_state != INTERACTIVE_STATE_INPUT:
        return False

    try:
        result = bridge.reply_to_pending_user_input(
            target_thread,
            prompt,
            timeout_sec=8.0,
        )
    except Exception as exc:
        send_text(
            token,
            chat_id,
            "\n".join(
                [
                    "Waiting input reply failed.",
                    f"thread: {target_ref or bridge.get_thread_workspace_ref(target_thread)}",
                    "",
                    str(exc),
                ]
            ),
            reply_to_message_id=reply_to_message_id,
        )
        return True

    answers_by_question = result.get("answers_by_question") or {}
    answer_lines: list[str] = []
    if isinstance(answers_by_question, dict):
        for question_id, values in answers_by_question.items():
            if not isinstance(values, list):
                continue
            answer_lines.append(f"- {question_id}: {' | '.join(str(value) for value in values)}")

    lines = [
        "Waiting input submitted.",
        f"thread: {target_ref or bridge.get_thread_workspace_ref(target_thread)}",
    ]
    if answer_lines:
        lines.extend(["", *answer_lines])
    send_text(
        token,
        chat_id,
        "\n".join(lines),
        reply_to_message_id=reply_to_message_id,
    )
    maybe_follow_selected_thread(token, chat_id, reply_to_message_id)
    return True


def start_or_queue_ask(
    token: str,
    chat_id: int,
    prompt: str,
    reply_to_message_id: int | None,
    active_summary: str,
    target_thread_id: str | None,
    target_ref: str,
    target_label: str,
) -> None:
    if active_summary:
        position = enqueue_pending_ask(
            chat_id,
            prompt,
            reply_to_message_id,
            target_thread_id=target_thread_id,
            target_ref=target_ref,
            target_label=target_label,
        )
        send_text(
            token,
            chat_id,
            build_busy_queue_message(active_summary, position),
            reply_to_message_id=reply_to_message_id,
        )
        return

    start_ask_worker(
        token,
        chat_id,
        prompt,
        reply_to_message_id,
        target_thread_id=target_thread_id,
        target_ref=target_ref,
        target_label=target_label,
    )


def enqueue_pending_ask(
    chat_id: int,
    prompt: str,
    reply_to_message_id: int | None,
    target_thread_id: str | None = None,
    target_ref: str = "",
    target_label: str = "",
) -> int:
    with PENDING_ASKS_LOCK:
        PENDING_ASKS.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "reply_to_message_id": reply_to_message_id,
                "target_thread_id": target_thread_id,
                "target_ref": target_ref,
                "target_label": target_label,
            }
        )
        return len(PENDING_ASKS)


def pop_pending_ask() -> dict[str, object] | None:
    with PENDING_ASKS_LOCK:
        if not PENDING_ASKS:
            return None
        return PENDING_ASKS.pop(0)


def get_pending_queue_size() -> int:
    with PENDING_ASKS_LOCK:
        return len(PENDING_ASKS)


def start_ask_worker(
    token: str,
    chat_id: int,
    prompt: str,
    reply_to_message_id: int | None,
    *,
    queued: bool = False,
    target_thread_id: str | None = None,
    target_ref: str = "",
    target_label: str = "",
) -> bool:
    if not target_thread_id:
        target_thread_id, target_ref, target_label = resolve_selected_target()
    with ACTIVE_JOB_LOCK:
        current = ACTIVE_JOB.get("thread")
        if current and getattr(current, "is_alive", lambda: False)():
            return False
        worker = threading.Thread(
            target=run_ask_job,
            args=(token, chat_id, prompt, reply_to_message_id, target_thread_id, target_ref, target_label),
            daemon=True,
            name="codex-telegram-ask",
        )
        ACTIVE_JOB["thread"] = worker
        ACTIVE_JOB["chat_id"] = chat_id
        if target_ref:
            ACTIVE_JOB["summary"] = f"Running ask on {target_ref}: {prompt[:120]}"
        else:
            ACTIVE_JOB["summary"] = f"Running ask: {prompt[:120]}"
    worker.start()
    start_label = "Queued ask started." if queued else "Ask started."
    send_text(token, chat_id, f"{start_label}\n\n{prompt}", reply_to_message_id=reply_to_message_id)
    return True


def start_next_pending_ask(token: str) -> bool:
    pending = pop_pending_ask()
    if not pending:
        return False
    chat_id = int(pending["chat_id"])
    prompt = str(pending["prompt"])
    reply_to_message_id = pending.get("reply_to_message_id")
    target_thread_id = str(pending.get("target_thread_id") or "").strip() or None
    target_ref = str(pending.get("target_ref") or "")
    target_label = str(pending.get("target_label") or "")
    started = start_ask_worker(
        token=token,
        chat_id=chat_id,
        prompt=prompt,
        reply_to_message_id=reply_to_message_id if isinstance(reply_to_message_id, int) else None,
        queued=True,
        target_thread_id=target_thread_id,
        target_ref=target_ref,
        target_label=target_label,
    )
    if not started:
        enqueue_pending_ask(
            chat_id,
            prompt,
            reply_to_message_id if isinstance(reply_to_message_id, int) else None,
            target_thread_id=target_thread_id,
            target_ref=target_ref,
            target_label=target_label,
        )
        return False
    log_line(
        f"pending_ask_started chat_id={chat_id} "
        f"target_ref={target_ref or '-'} "
        f"prompt={prompt[:120].replace(chr(10), ' ')}"
    )
    return True


def build_waiting_list_suffix(active_summary: str) -> str:
    pending_count = get_pending_queue_size()
    lines: list[str] = []
    if active_summary:
        lines.append(f"active: {active_summary}")
    if pending_count > 0:
        lines.append(f"queued: {pending_count}")
    if not lines:
        return ""
    return "\n\nActive\n" + "\n".join(lines)


def get_busy_labels(limit: int = 50) -> list[str]:
    try:
        return [bridge.get_thread_label(item) for item in bridge.get_busy_threads(limit=limit)]
    except Exception:
        log_line("get_busy_labels_error\n" + traceback.format_exc())
        return []


def _clear_ask_waiter(chat_id: int, worker: threading.Thread) -> None:
    with ASK_WAITERS_LOCK:
        current = ASK_WAITERS.get(chat_id)
        if current and current.get("thread") is worker:
            ASK_WAITERS.pop(chat_id, None)


def _clear_follow_watcher(chat_id: int, worker: threading.Thread) -> None:
    with FOLLOW_WATCHERS_LOCK:
        current = FOLLOW_WATCHERS.get(chat_id)
        if current and current.get("thread") is worker:
            FOLLOW_WATCHERS.pop(chat_id, None)


def stop_follow_watcher(chat_id: int) -> None:
    with FOLLOW_WATCHERS_LOCK:
        current = FOLLOW_WATCHERS.pop(chat_id, None)
    if not current:
        return
    stop_event = current.get("stop_event")
    if isinstance(stop_event, threading.Event):
        stop_event.set()


def send_latest_assistant_reply_if_changed(
    token: str,
    chat_id: int,
    session_path: Path,
    reply_to_message_id: int | None,
    baseline_last_assistant: str,
    seen_agent_messages: set[str],
) -> bool:
    try:
        _last_user, last_assistant = bridge.get_last_user_and_assistant_messages(session_path)
    except Exception:
        log_line("follow_latest_assistant_error\n" + traceback.format_exc())
        return False

    latest_text = str(last_assistant or "").strip()
    baseline_text = str(baseline_last_assistant or "").strip()
    if not latest_text or latest_text == baseline_text or latest_text in seen_agent_messages:
        return False

    send_text(token, chat_id, latest_text, reply_to_message_id=reply_to_message_id)
    return True


def follow_thread_output(
    token: str,
    chat_id: int,
    target_thread_id: str,
    target_ref: str,
    reply_to_message_id: int | None,
    stop_event: threading.Event,
    timeout_sec: float = 3600.0,
) -> None:
    worker = threading.current_thread()
    seen_agent_messages: set[str] = set()
    seen_interactive_signatures: set[str] = set()
    deadline = time.time() + timeout_sec
    try:
        target_thread = bridge.choose_thread(target_thread_id, None)
        session_path = Path(target_thread.rollout_path)
        if not session_path.exists():
            return
        _last_user, baseline_last_assistant = bridge.get_last_user_and_assistant_messages(session_path)
        current_state, current_prompt = get_current_interactive_prompt(target_thread)
        if current_state:
            signature = "\n".join([current_state, current_prompt])
            if signature and signature not in seen_interactive_signatures:
                seen_interactive_signatures.add(signature)
                send_text(
                    token,
                    chat_id,
                    build_interactive_waiting_text(target_ref, current_state, current_prompt),
                    reply_to_message_id=reply_to_message_id,
                )
        cursor = session_path.stat().st_size
        while time.time() < deadline and not stop_event.is_set():
            events, cursor = bridge.read_new_session_events(session_path, cursor)
            for event in events:
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue

                if event.get("type") == "event_msg" and payload.get("type") == "agent_message":
                    if str(payload.get("phase", "") or "") == "final_answer":
                        continue
                    message = str(payload.get("message", "")).strip()
                    if not message:
                        continue
                    seen_agent_messages.add(message)
                    send_text(
                        token,
                        chat_id,
                        f"In progress ({target_ref})\n\n{message}",
                        reply_to_message_id=reply_to_message_id,
                    )
                    continue

                if event.get("type") == "response_item" and payload.get("type") == "message":
                    text = bridge.extract_message_text(payload)
                    role = str(payload.get("role") or "").strip().lower()
                    phase = str(payload.get("phase") or "").strip().lower()
                    if role != "assistant" or not text:
                        continue
                    if phase == "commentary":
                        if text in seen_agent_messages:
                            continue
                        send_text(
                            token,
                            chat_id,
                            f"In progress ({target_ref})\n\n{text}",
                            reply_to_message_id=reply_to_message_id,
                        )
                        continue
                    if phase == "final_answer":
                        send_text(token, chat_id, text, reply_to_message_id=reply_to_message_id)
                        return

                if event.get("type") == "response_item" and payload.get("type") == "function_call":
                    notice = bridge.build_interactive_notice_from_function_call(payload)
                    state, prompt = parse_interactive_notice(notice)
                    if state:
                        if state == INTERACTIVE_STATE_APPROVAL:
                            current_state, current_prompt = get_current_interactive_prompt(target_thread)
                            if current_state == INTERACTIVE_STATE_APPROVAL and current_prompt:
                                state = current_state
                                prompt = current_prompt
                        signature = "\n".join([state, prompt])
                        if signature and signature not in seen_interactive_signatures:
                            seen_interactive_signatures.add(signature)
                            send_text(
                                token,
                                chat_id,
                                build_interactive_waiting_text(target_ref, state, prompt),
                                reply_to_message_id=reply_to_message_id,
                            )

            current_state, current_prompt = get_current_interactive_prompt(target_thread)
            if current_state:
                signature = "\n".join([current_state, current_prompt])
                if signature and signature not in seen_interactive_signatures:
                    seen_interactive_signatures.add(signature)
                    send_text(
                        token,
                        chat_id,
                        build_interactive_waiting_text(target_ref, current_state, current_prompt),
                        reply_to_message_id=reply_to_message_id,
                    )

            if not bridge.is_thread_busy(session_path):
                idle_grace_deadline = time.time() + 4.5
                while time.time() < idle_grace_deadline and not stop_event.is_set():
                    if send_latest_assistant_reply_if_changed(
                        token,
                        chat_id,
                        session_path,
                        reply_to_message_id,
                        baseline_last_assistant,
                        seen_agent_messages,
                    ):
                        return
                    time.sleep(0.35)
                return
            time.sleep(0.35)
    except Exception:
        log_line(
            f"follow_thread_output_crash chat_id={chat_id} target_ref={target_ref}\n"
            + traceback.format_exc()
        )
    finally:
        _clear_follow_watcher(chat_id, worker)


def ensure_follow_watcher(
    token: str,
    chat_id: int,
    target_thread_id: str,
    target_ref: str,
    reply_to_message_id: int | None,
) -> tuple[bool, str | None]:
    with FOLLOW_WATCHERS_LOCK:
        current = FOLLOW_WATCHERS.get(chat_id)
        if current:
            current_thread = current.get("thread")
            current_target_thread_id = str(current.get("target_thread_id") or "")
            if (
                current_thread
                and getattr(current_thread, "is_alive", lambda: False)()
                and current_target_thread_id == target_thread_id
            ):
                return False, str(current.get("target_ref") or "")
            current_stop_event = current.get("stop_event")
            if isinstance(current_stop_event, threading.Event):
                current_stop_event.set()
            FOLLOW_WATCHERS.pop(chat_id, None)

        stop_event = threading.Event()
        worker = threading.Thread(
            target=follow_thread_output,
            args=(token, chat_id, target_thread_id, target_ref, reply_to_message_id, stop_event),
            daemon=True,
            name=f"codex-follow-{chat_id}",
        )
        FOLLOW_WATCHERS[chat_id] = {
            "thread": worker,
            "stop_event": stop_event,
            "target_thread_id": target_thread_id,
            "target_ref": target_ref,
        }
        worker.start()
        return True, None


def maybe_follow_selected_thread(
    token: str,
    chat_id: int,
    reply_to_message_id: int | None,
) -> None:
    target_thread_id, target_ref, _target_label = resolve_selected_target()
    if not target_thread_id:
        stop_follow_watcher(chat_id)
        return
    try:
        target_thread = bridge.choose_thread(target_thread_id, None)
    except Exception:
        stop_follow_watcher(chat_id)
        return

    session_path = Path(target_thread.rollout_path)
    if not session_path.exists() or not bridge.is_thread_busy(session_path):
        stop_follow_watcher(chat_id)
        return

    started, existing_ref = ensure_follow_watcher(
        token=token,
        chat_id=chat_id,
        target_thread_id=target_thread_id,
        target_ref=target_ref,
        reply_to_message_id=reply_to_message_id,
    )
    if not started:
        return

    send_text(
        token,
        chat_id,
        "\n".join(
            [
                f"Following busy thread: {target_ref}",
                "I'll forward in-progress updates here.",
            ]
        ),
        reply_to_message_id=reply_to_message_id,
    )
    log_line(f"follow_watcher_start chat_id={chat_id} target_ref={target_ref} replaced={existing_ref or '-'}")


def wait_for_selected_thread_to_clear(
    token: str,
    chat_id: int,
    target_ref: str,
    target_label: str,
    reply_to_message_id: int | None,
    timeout_sec: float = 3600.0,
) -> None:
    worker = threading.current_thread()
    deadline = time.time() + timeout_sec
    log_line(
        f"ask_waiter_start chat_id={chat_id} target_ref={target_ref} "
        f"target_label={target_label} timeout={timeout_sec}"
    )
    try:
        last_busy = target_label
        while time.time() < deadline:
            busy_now = get_busy_labels(limit=50)
            if target_label not in busy_now:
                send_text(
                    token,
                    chat_id,
                    "\n".join(
                        [
                            "Busy finished.",
                            "",
                            f"{target_ref} is ready now.",
                            "Send your next message when ready.",
                        ]
                    ),
                    reply_to_message_id=reply_to_message_id,
                )
                log_line(f"ask_waiter_ready chat_id={chat_id} target_ref={target_ref}")
                return
            if busy_now:
                last_busy = ", ".join(busy_now[:3])
            time.sleep(2.0)

        send_text(
            token,
            chat_id,
            "\n".join(
                [
                    "Busy wait timed out.",
                    "",
                    f"Still busy: {last_busy or '-'}",
                    f"Target: {target_ref}",
                ]
            ),
            reply_to_message_id=reply_to_message_id,
        )
        log_line(f"ask_waiter_timeout chat_id={chat_id} target_ref={target_ref} last_busy={last_busy}")
    except Exception:
        log_line(f"ask_waiter_crash chat_id={chat_id} target_ref={target_ref}\n{traceback.format_exc()}")
    finally:
        _clear_ask_waiter(chat_id, worker)


def ensure_ask_waiter(
    token: str,
    chat_id: int,
    target_ref: str,
    target_label: str,
    reply_to_message_id: int | None,
) -> tuple[bool, str | None]:
    with ASK_WAITERS_LOCK:
        current = ASK_WAITERS.get(chat_id)
        if current:
            current_thread = current.get("thread")
            if current_thread and getattr(current_thread, "is_alive", lambda: False)():
                return False, str(current.get("target_ref") or "")
            ASK_WAITERS.pop(chat_id, None)

        worker = threading.Thread(
            target=wait_for_selected_thread_to_clear,
            args=(token, chat_id, target_ref, target_label, reply_to_message_id),
            daemon=True,
            name=f"codex-ask-wait-{chat_id}",
        )
        ASK_WAITERS[chat_id] = {
            "thread": worker,
            "target_ref": target_ref,
            "target_label": target_label,
        }
        worker.start()
        return True, None


def run_ask_job(
    token: str,
    chat_id: int,
    prompt: str,
    reply_to_message_id: int | None = None,
    target_thread_id: str | None = None,
    target_ref: str = "",
    target_label: str = "",
) -> None:
    if target_thread_id and (not target_ref or not target_label):
        try:
            target_thread = bridge.choose_thread(target_thread_id, None)
            target_ref = bridge.get_thread_workspace_ref(target_thread)
            target_label = bridge.get_thread_label(target_thread)
        except Exception:
            pass
    elif not target_thread_id:
        target_thread_id, target_ref, target_label = resolve_selected_target()
    relay = TelegramAskRelay(token, chat_id, reply_to_message_id, thread_ref=target_ref)
    try:
        log_line(
            f"ask_job_start chat_id={chat_id} "
            f"target_ref={target_ref or '-'} "
            f"prompt={prompt[:160].replace(chr(10), ' ')}"
        )
        argv = [
            "ask",
            "--ipc",
            "--foreground",
            "--stream",
            "--include-commentary",
            "--timeout",
            "0",
        ]
        if target_thread_id:
            argv.extend(["--thread-id", target_thread_id])
        argv.append(prompt)
        exit_code, output = run_bridge_command_stream(
            argv,
            relay.feed_line,
        )
        log_line(f"ask_job_finish chat_id={chat_id} exit_code={exit_code}")
        if exit_code != 0:
            log_line(
                "ask_job_failure_output "
                f"chat_id={chat_id} target_ref={target_ref or '-'}\n"
                f"{output or '(no output)'}"
            )
        note = ""
        if (
            exit_code != 0
            and "The selected thread is still busy." in output
            and target_ref
            and target_label
        ):
            started, existing_ref = ensure_ask_waiter(
                token=token,
                chat_id=chat_id,
                target_ref=target_ref,
                target_label=target_label,
                reply_to_message_id=reply_to_message_id,
            )
            if started:
                note = (
                    "\n\nBusy-end notifier armed."
                    f"\nI'll send a message here when {target_ref} is ready."
                )
            else:
                note = (
                    "\n\nBusy-end notifier already active."
                    + (f"\nCurrent waiting target: {existing_ref}" if existing_ref else "")
                )
        elif exit_code != 0 and "IPC owner client for the selected thread was not discovered" in output:
            note = (
                "\n\nIPC recovery tip / IPC 복구 안내"
                "\n- Restart the Telegram bot and try again."
                "\n- 텔레그램 봇을 재시작한 뒤 다시 시도해보세요."
                "\n- Restart command / 재시작 명령: /restart_bot"
            )
        relay.finish()
        if relay.sent_live:
            if exit_code == 0 and not relay.saw_aborted:
                send_text(token, chat_id, "Done.", reply_to_message_id=reply_to_message_id)
            elif not relay.saw_aborted and not relay.saw_timeout:
                send_text(
                    token,
                    chat_id,
                    f"Ask failed (exit {exit_code})\n\n{output or '(no output)'}{note}",
                    reply_to_message_id=reply_to_message_id,
                )
        else:
            title = "Ask finished" if exit_code == 0 else f"Ask failed (exit {exit_code})"
            message = f"{title}\n\n{output or '(no output)'}{note}"
            send_text(token, chat_id, message, reply_to_message_id=reply_to_message_id)
    except Exception:  # pragma: no cover - operational path
        log_line(f"ask_job_crash chat_id={chat_id}\n{traceback.format_exc()}")
        send_text(
            token,
            chat_id,
            "Ask worker crashed.\n\n" + traceback.format_exc(),
            reply_to_message_id=reply_to_message_id,
        )
    finally:
        set_active_job(None)
        start_next_pending_ask(token)


def _legacy_handle_message(token: str, message: dict, allowed_chat_ids: set[int]) -> None:
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id"))
    text = (message.get("text") or "").strip()
    reply_to_message_id = message.get("message_id")

    log_line(
        f"handle_message chat_id={chat_id} allowed={sorted(allowed_chat_ids) if allowed_chat_ids else 'ALL'} "
        f"text={text[:160].replace(chr(10), ' ')}"
    )

    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        log_line(f"ignored_message chat_id={chat_id} reason=not_allowed")
        return
    if not text:
        log_line(f"ignored_message chat_id={chat_id} reason=no_text")
        return

    active_summary = get_active_job_summary()
    target_thread_id, target_ref, target_label = resolve_selected_target()

    if not text.startswith("/"):
        if active_summary:
            position = enqueue_pending_ask(
                chat_id,
                text,
                reply_to_message_id,
                target_thread_id=target_thread_id,
                target_ref=target_ref,
                target_label=target_label,
            )
            send_text(
                token,
                chat_id,
                f"Busy.\n\n{active_summary}\n\n대기열에 추가했습니다. ({position})\n현재 답변이 끝나면 자동으로 이어서 보냅니다.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        start_ask_worker(
            token,
            chat_id,
            text,
            reply_to_message_id,
            target_thread_id=target_thread_id,
            target_ref=target_ref,
            target_label=target_label,
        )
        return

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command in {"/start", "/help"}:
        send_text(
            token,
            chat_id,
            "\n".join(
                [
                    "Commands:",
                    "/list [limit]",
                    "/archived_list [limit]",
                    "/new <prompt>",
                    "/archive [ref]",
                    "/delete_archive <ref>",
                    "/confirm_delete_archive <ref>",
                    "/open <ref>",
                    "/open_abort <ref>",
                    "/use <ref>",
                    "/status [ref]",
                    "/doctor",
                    "/ask <prompt>",
                    "/ask_ipc <prompt> (alias)",
                    "/restart_bot",
                    "/chatid",
                    "",
                    "Thread refs use the same format as the bridge, for example:",
                    "ai:1, ai:2, taxlab, other, 1, 2",
                ]
            ),
            reply_to_message_id=reply_to_message_id,
        )
        return

    if command == "/restart_bot":
        try:
            started, detail = schedule_bot_restart()
        except Exception:
            log_line("restart_command_error\n" + traceback.format_exc())
            send_text(
                token,
                chat_id,
                "Bot restart failed.\n봇 재시작에 실패했습니다.\n\n" + traceback.format_exc(),
                reply_to_message_id=reply_to_message_id,
            )
            return
        message = (
            "Restarting Telegram bot.\n"
            "텔레그램 봇을 재시작합니다.\n\n"
            "Retry the last IPC ask after the bot comes back.\n"
            "봇이 다시 올라온 뒤 마지막 IPC 요청을 다시 보내세요."
        )
        if not started and detail:
            message += f"\n\n{detail}"
        send_text(token, chat_id, message, reply_to_message_id=reply_to_message_id)
        return

    if command in {"/chatid", "/whoami"}:
        send_text(
            token,
            chat_id,
            "\n".join(
                [
                    f"chat_id: {chat_id}",
                    f"chat_type: {chat.get('type', '-')}",
                    f"chat_title: {chat.get('title') or '-'}",
                    "",
                    f"Copy into .env:",
                    f"TELEGRAM_ALLOWED_CHAT_IDS={chat_id}",
                ]
            ),
            reply_to_message_id=reply_to_message_id,
        )
        return

    if command == "/list":
        limit = 10
        if arg:
            try:
                limit = max(1, min(30, int(arg)))
            except ValueError:
                pass
        exit_code, output = run_bridge_command(["list", "--limit", str(limit)])
        prefix = "List" if exit_code == 0 else f"List failed (exit {exit_code})"
        display_output = rewrite_list_output_for_telegram(output or "(no output)")
        waiting_suffix = build_waiting_list_suffix(active_summary)
        send_text(
            token,
            chat_id,
            f"{prefix}\n\n{display_output}{waiting_suffix}",
            reply_to_message_id=reply_to_message_id,
        )
        return

    if command == "/archived_list":
        limit = 10
        if arg:
            try:
                limit = max(1, min(50, int(arg)))
            except ValueError:
                pass
        exit_code, output = run_bridge_command(["archived_list", "--limit", str(limit)])
        prefix = "Archived list" if exit_code == 0 else f"Archived list failed (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
        return

    if command == "/new":
        if not arg:
            send_text(token, chat_id, "Usage: /new <prompt>", reply_to_message_id=reply_to_message_id)
            return
        exit_code, output = run_bridge_command(["new", arg])
        prefix = "New ok" if exit_code == 0 else f"New failed (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
        return

    if command == "/archive":
        argv = ["archive"]
        if arg:
            argv.append(arg)
        exit_code, output = run_bridge_command(argv)
        prefix = "Archive ok" if exit_code == 0 else f"Archive failed (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
        return

    if command == "/delete_archive":
        if not arg:
            send_text(token, chat_id, "Usage: /delete_archive <ref>", reply_to_message_id=reply_to_message_id)
            return
        exit_code, output = run_bridge_command(["delete_archive", arg])
        prefix = "Delete archive preview" if exit_code == 0 else f"Delete archive failed (exit {exit_code})"
        message = (
            f"{prefix}\n\n{output or '(no output)'}"
            "\n\nTo actually delete it, run /confirm_delete_archive <thread_id>."
        )
        send_text(token, chat_id, message, reply_to_message_id=reply_to_message_id)
        return

    if command == "/confirm_delete_archive":
        if not arg:
            send_text(token, chat_id, "Usage: /confirm_delete_archive <ref>", reply_to_message_id=reply_to_message_id)
            return
        exit_code, output = run_bridge_command(["delete_archive", "--confirm", arg])
        prefix = "Delete archive ok" if exit_code == 0 else f"Delete archive failed (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
        return

    if command == "/doctor":
        exit_code, output = run_bridge_command(["doctor"])
        prefix = "Doctor" if exit_code == 0 else f"Doctor failed (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
        return

    if command == "/status":
        argv = resolve_status_args(arg or None)
        exit_code, output = run_bridge_command(argv)
        prefix = "Status" if exit_code == 0 else f"Status failed (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
        return

    if command == "/use":
        if not arg:
            send_text(token, chat_id, "Usage: /use <ref>", reply_to_message_id=reply_to_message_id)
            return
        exit_code, output = run_bridge_command(["use", arg])
        prefix = "Use ok" if exit_code == 0 else f"Use failed (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output or '(no output)'}", reply_to_message_id=reply_to_message_id)
        if exit_code == 0:
            maybe_follow_selected_thread(token, chat_id, reply_to_message_id)
        return

    if command in {"/open", "/open_abort"}:
        if not arg:
            send_text(token, chat_id, f"Usage: {command} <ref>", reply_to_message_id=reply_to_message_id)
            return
        argv = ["open"]
        if command == "/open_abort":
            argv.append("--abort")
        argv.append(arg)
        exit_code, output = run_bridge_command(argv)
        prefix = "Open ok" if exit_code == 0 else f"Open failed (exit {exit_code})"
        send_text(
            token,
            chat_id,
            f"{prefix}\n\n{output or '(no output)'}",
            reply_to_message_id=reply_to_message_id,
        )
        if exit_code == 0:
            maybe_follow_selected_thread(token, chat_id, reply_to_message_id)
        return

    if command == "/ask":
        if not arg:
            send_text(token, chat_id, "Usage: /ask <prompt>", reply_to_message_id=reply_to_message_id)
            return
        if active_summary:
            position = enqueue_pending_ask(
                chat_id,
                arg,
                reply_to_message_id,
                target_thread_id=target_thread_id,
                target_ref=target_ref,
                target_label=target_label,
            )
            send_text(
                token,
                chat_id,
                f"Busy.\n\n{active_summary}\n\n대기열에 추가했습니다. ({position})\n현재 답변이 끝나면 자동으로 이어서 보냅니다.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        start_ask_worker(
            token,
            chat_id,
            arg,
            reply_to_message_id,
            target_thread_id=target_thread_id,
            target_ref=target_ref,
            target_label=target_label,
        )
        return

    if command == "/ask_ipc":
        if not arg:
            send_text(token, chat_id, "Usage: /ask_ipc <prompt>", reply_to_message_id=reply_to_message_id)
            return
        if active_summary:
            position = enqueue_pending_ask(
                chat_id,
                arg,
                reply_to_message_id,
                target_thread_id=target_thread_id,
                target_ref=target_ref,
                target_label=target_label,
            )
            send_text(
                token,
                chat_id,
                f"Busy.\n\n{active_summary}\n\n대기열에 추가했습니다. ({position})\n현재 답변이 끝나면 자동으로 이어서 보냅니다.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        start_ask_worker(
            token,
            chat_id,
            arg,
            reply_to_message_id,
            target_thread_id=target_thread_id,
            target_ref=target_ref,
            target_label=target_label,
        )
        return

    send_text(token, chat_id, f"Unknown command: {command}", reply_to_message_id=reply_to_message_id)


def handle_message(token: str, message: dict, allowed_chat_ids: set[int]) -> None:
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id"))
    text = (message.get("text") or "").strip()
    reply_to_message_id = message.get("message_id")

    log_line(
        f"handle_message chat_id={chat_id} allowed={sorted(allowed_chat_ids) if allowed_chat_ids else 'ALL'} "
        f"text={text[:160].replace(chr(10), ' ')}"
    )

    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        log_line(f"ignored_message chat_id={chat_id} reason=not_allowed")
        return
    if not text:
        log_line(f"ignored_message chat_id={chat_id} reason=no_text")
        return

    active_summary = get_active_job_summary()
    target_thread_id, target_ref, target_label = resolve_selected_target()

    if not text.startswith("/"):
        interactive_thread_id, interactive_ref, _interactive_label = resolve_interactive_reply_target(
            chat_id,
            text,
            target_thread_id,
            target_ref,
            target_label,
        )
        if maybe_submit_waiting_input_reply(
            token,
            chat_id,
            text,
            reply_to_message_id,
            interactive_thread_id or target_thread_id,
            interactive_ref or target_ref,
        ):
            return
        start_or_queue_ask(
            token,
            chat_id,
            text,
            reply_to_message_id,
            active_summary,
            target_thread_id,
            target_ref,
            target_label,
        )
        return

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command in {"/start", "/help"}:
        send_text(token, chat_id, build_help_message(), reply_to_message_id=reply_to_message_id)
        return

    if command == "/restart_bot":
        try:
            started, detail = schedule_bot_restart()
        except Exception:
            log_line("restart_command_error\n" + traceback.format_exc())
            send_text(
                token,
                chat_id,
                "Bot restart failed.\n\n" + traceback.format_exc(),
                reply_to_message_id=reply_to_message_id,
            )
            return
        message_text = (
            "Restarting Telegram bot.\n"
            "텔레그램 봇을 재시작합니다.\n\n"
            "Retry the last IPC ask after the bot comes back.\n"
            "봇이 다시 올라오면 마지막 IPC 요청을 다시 보내세요."
        )
        if not started and detail:
            message_text += f"\n\n{detail}"
        send_text(token, chat_id, message_text, reply_to_message_id=reply_to_message_id)
        return

    if command == "/restart_codex":
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            ["restart_codex"],
            "Codex restart ok",
            "Codex restart failed",
        )
        return

    if command in {"/chatid", "/whoami"}:
        send_text(
            token,
            chat_id,
            "\n".join(
                [
                    f"chat_id: {chat_id}",
                    f"chat_type: {chat.get('type', '-')}",
                    f"chat_title: {chat.get('title') or '-'}",
                    "",
                    "Copy into .env:",
                    f"TELEGRAM_ALLOWED_CHAT_IDS={chat_id}",
                ]
            ),
            reply_to_message_id=reply_to_message_id,
        )
        return

    if command == "/list":
        limit = parse_bounded_int_arg(arg, default=10, minimum=1, maximum=30)
        exit_code, output = run_bridge_command(["list", "--limit", str(limit)])
        prefix = "List" if exit_code == 0 else f"List failed (exit {exit_code})"
        display_output = rewrite_list_output_for_telegram(output or "(no output)")
        waiting_suffix = build_waiting_list_suffix(active_summary)
        send_text(
            token,
            chat_id,
            f"{prefix}\n\n{display_output}{waiting_suffix}",
            reply_to_message_id=reply_to_message_id,
        )
        return

    if command == "/archived_list":
        limit = parse_bounded_int_arg(arg, default=10, minimum=1, maximum=50)
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            ["archived_list", "--limit", str(limit)],
            "Archived list",
            "Archived list failed",
        )
        return

    if command == "/new":
        if not arg:
            send_usage(token, chat_id, reply_to_message_id, "/new <prompt>")
            return
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            ["new", arg],
            "New ok",
            "New failed",
        )
        return

    if command == "/archive":
        argv = ["archive"]
        if arg:
            argv.append(arg)
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            argv,
            "Archive ok",
            "Archive failed",
        )
        return

    if command == "/delete_archive":
        if not arg:
            send_usage(token, chat_id, reply_to_message_id, "/delete_archive <ref>")
            return
        exit_code, output = run_bridge_command(["delete_archive", arg])
        prefix = "Delete archive preview" if exit_code == 0 else f"Delete archive failed (exit {exit_code})"
        message_text = (
            f"{prefix}\n\n{output or '(no output)'}"
            "\n\nTo actually delete it, run /confirm_delete_archive <thread_id>."
        )
        send_text(token, chat_id, message_text, reply_to_message_id=reply_to_message_id)
        return

    if command == "/confirm_delete_archive":
        if not arg:
            send_usage(token, chat_id, reply_to_message_id, "/confirm_delete_archive <ref>")
            return
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            ["delete_archive", "--confirm", arg],
            "Delete archive ok",
            "Delete archive failed",
        )
        return

    if command == "/doctor":
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            ["doctor"],
            "Doctor",
            "Doctor failed",
        )
        return

    if command == "/discover_codex":
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            ["discover_codex"],
            "Codex path ok",
            "Codex path failed",
        )
        return

    if command == "/status":
        send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            resolve_status_args(arg or None),
            "Status",
            "Status failed",
        )
        return

    if command == "/use":
        if not arg:
            send_usage(token, chat_id, reply_to_message_id, "/use <ref>")
            return
        exit_code, _output = send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            ["use", arg],
            "Use ok",
            "Use failed",
        )
        if exit_code == 0:
            maybe_follow_selected_thread(token, chat_id, reply_to_message_id)
        return

    if command in {"/open", "/open_abort"}:
        if not arg:
            send_usage(token, chat_id, reply_to_message_id, f"{command} <ref>")
            return
        argv = ["open"]
        if command == "/open_abort":
            argv.append("--abort")
        argv.append(arg)
        exit_code, _output = send_bridge_command_result(
            token,
            chat_id,
            reply_to_message_id,
            argv,
            "Open ok",
            "Open failed",
        )
        if exit_code == 0:
            maybe_follow_selected_thread(token, chat_id, reply_to_message_id)
        return

    if command in {"/ask", "/ask_ipc"}:
        if not arg:
            send_usage(token, chat_id, reply_to_message_id, f"{command} <prompt>")
            return
        interactive_thread_id, interactive_ref, _interactive_label = resolve_interactive_reply_target(
            chat_id,
            arg,
            target_thread_id,
            target_ref,
            target_label,
        )
        if maybe_submit_waiting_input_reply(
            token,
            chat_id,
            arg,
            reply_to_message_id,
            interactive_thread_id or target_thread_id,
            interactive_ref or target_ref,
        ):
            return
        start_or_queue_ask(
            token,
            chat_id,
            arg,
            reply_to_message_id,
            active_summary,
            target_thread_id,
            target_ref,
            target_label,
        )
        return

    send_text(token, chat_id, f"Unknown command: {command}", reply_to_message_id=reply_to_message_id)


def bootstrap_offset(token: str, skip_old_updates: bool) -> int | None:
    if not skip_old_updates:
        log_line("bootstrap_offset skip_old_updates=False")
        return None
    payload = telegram_api("getUpdates", token, params={"timeout": "1"})
    results = payload.get("result") or []
    if not results:
        log_line("bootstrap_offset no_existing_updates")
        return None
    offset = int(results[-1]["update_id"]) + 1
    log_line(f"bootstrap_offset skipped_to={offset}")
    return offset


def run_polling(token: str, allowed_chat_ids: set[int], poll_timeout: int, skip_old_updates: bool) -> None:
    offset = bootstrap_offset(token, skip_old_updates)
    log_line(
        f"run_polling_start poll_timeout={poll_timeout} skip_old_updates={skip_old_updates} "
        f"offset={offset} allowed={sorted(allowed_chat_ids) if allowed_chat_ids else 'ALL'}"
    )
    while True:
        try:
            params = {"timeout": str(poll_timeout)}
            if offset is not None:
                params["offset"] = str(offset)
            payload = telegram_api("getUpdates", token, params=params)
            results = payload.get("result") or []
            if results:
                log_line(f"poll_batch updates={len(results)} first={results[0].get('update_id')} last={results[-1].get('update_id')}")
            for update in results:
                offset = int(update["update_id"]) + 1
                message = update.get("message")
                if not message:
                    log_line(f"skip_update update_id={update.get('update_id')} reason=no_message_keys={','.join(sorted(update.keys()))}")
                    continue
                try:
                    handle_message(token, message, allowed_chat_ids)
                except Exception:
                    log_line(f"handle_message_crash update_id={update.get('update_id')}\n{traceback.format_exc()}")
        except KeyboardInterrupt:
            log_line("run_polling_keyboard_interrupt")
            raise
        except Exception:
            log_line("run_polling_exception\n" + traceback.format_exc())
            print(traceback.format_exc())
            time.sleep(3)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram adapter for codex_desktop_bridge.py")
    parser.add_argument("--poll-timeout", type=int, default=int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30")))
    parser.add_argument(
        "--skip-old-updates",
        dest="skip_old_updates",
        action="store_true",
        default=os.environ.get("TELEGRAM_SKIP_OLD_UPDATES", "1").strip() not in {"0", "false", "False"},
    )
    parser.add_argument(
        "--no-skip-old-updates",
        dest="skip_old_updates",
        action="store_false",
        help="Process pending Telegram messages that existed before startup.",
    )
    return parser


def main() -> int:
    load_local_env(ENV_PATH)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not acquire_single_instance_mutex(token=token):
        log_line("main existing_instance")
        print("Telegram bot is already running.")
        return 0
    args = build_parser().parse_args()
    token = get_required_env("TELEGRAM_BOT_TOKEN")
    allowed_chat_ids = get_allowed_chat_ids()
    log_line(
        f"main_start script_dir={SCRIPT_DIR} env_path={ENV_PATH} "
        f"allowed={sorted(allowed_chat_ids) if allowed_chat_ids else 'ALL'} "
        f"skip_old_updates={args.skip_old_updates} poll_timeout={args.poll_timeout}"
    )
    print(f"Telegram bot starting. Allowed chats: {sorted(allowed_chat_ids) if allowed_chat_ids else 'ALL'}")
    run_polling(
        token=token,
        allowed_chat_ids=allowed_chat_ids,
        poll_timeout=args.poll_timeout,
        skip_old_updates=args.skip_old_updates,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
