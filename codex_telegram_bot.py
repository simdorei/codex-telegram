"""
Telegram adapter for codex_desktop_bridge.py.

This uses the Telegram Bot HTTP API directly with the Python standard library.
It keeps the Codex bridge in-process so foreground ask/watch behavior stays alive.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import json
import os
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
TELEGRAM_MAX_LEN = 3900
ACTIVE_JOB_LOCK = threading.Lock()
ACTIVE_JOB: dict[str, object] = {"thread": None, "chat_id": None, "summary": ""}
PENDING_ASKS_LOCK = threading.Lock()
PENDING_ASKS: list[dict[str, object]] = []
OPEN_WAITERS_LOCK = threading.Lock()
OPEN_WAITERS: dict[int, dict[str, object]] = {}
SINGLE_INSTANCE_MUTEX = None

ERROR_ALREADY_EXISTS = 183


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
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
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
    def __init__(self, token: str, chat_id: int, reply_to_message_id: int | None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.mode: str | None = None
        self.block_lines: list[str] = []
        self.sent_live = False
        self.saw_final = False
        self.saw_ready = False
        self.saw_aborted = False
        self.saw_timeout = False

    def _send_block(self) -> None:
        text = "\n".join(self.block_lines).strip()
        if not text:
            self.block_lines = []
            return
        if self.mode == "commentary":
            send_text(self.token, self.chat_id, f"진행중\n\n{text}", reply_to_message_id=self.reply_to_message_id)
            self.sent_live = True
        elif self.mode == "final":
            send_text(self.token, self.chat_id, text, reply_to_message_id=self.reply_to_message_id)
            self.sent_live = True
            self.saw_final = True
        elif self.mode == "timeout":
            send_text(self.token, self.chat_id, f"시간 초과\n\n{text}", reply_to_message_id=self.reply_to_message_id)
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
            send_text(self.token, self.chat_id, "중단됨.", reply_to_message_id=self.reply_to_message_id)
            self.sent_live = True
            return
        if line.startswith("[ready]"):
            self._send_block()
            self.mode = None
            self.saw_ready = True
            return
        if line.startswith("[waiting_for_final_answer]") or line.startswith("Use Ctrl+C"):
            return
        if line.startswith("target_thread:") or line.startswith("title:") or line.startswith("ui_name:") or line.startswith("cwd:"):
            return
        if line.startswith("ui_activation:") or line.startswith("sent_to_window:") or line.startswith("[delivery_verified]"):
            return
        if line.startswith("[background_watch_started]") or line.startswith("[background_watch_already_running]"):
            return
        if line.startswith("[wait_cancelled]"):
            return

        if self.mode in {"commentary", "final", "timeout"}:
            self.block_lines.append(line)

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


def abort_current_reply() -> tuple[int, str]:
    cancelled, remaining = bridge.cancel_codex_reply_if_busy(timeout_sec=3.0)
    if not cancelled:
        return 0, "No active Codex reply to abort."
    lines = [f"reply_abort_requested: {', '.join(cancelled)}"]
    if remaining:
        lines.append(f"reply_abort_pending: {', '.join(remaining)}")
        return 1, "\n".join(lines)
    lines.append("reply_abort_done")
    return 0, "\n".join(lines)


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


def enqueue_pending_ask(chat_id: int, prompt: str, reply_to_message_id: int | None) -> int:
    with PENDING_ASKS_LOCK:
        PENDING_ASKS.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return len(PENDING_ASKS)


def pop_pending_ask() -> dict[str, object] | None:
    with PENDING_ASKS_LOCK:
        if not PENDING_ASKS:
            return None
        return PENDING_ASKS.pop(0)


def start_ask_worker(
    token: str,
    chat_id: int,
    prompt: str,
    reply_to_message_id: int | None,
    *,
    queued: bool = False,
) -> bool:
    with ACTIVE_JOB_LOCK:
        current = ACTIVE_JOB.get("thread")
        if current and getattr(current, "is_alive", lambda: False)():
            return False
        worker = threading.Thread(
            target=run_ask_job,
            args=(token, chat_id, prompt, reply_to_message_id),
            daemon=True,
            name="codex-telegram-ask",
        )
        ACTIVE_JOB["thread"] = worker
        ACTIVE_JOB["chat_id"] = chat_id
        ACTIVE_JOB["summary"] = f"Running ask: {prompt[:120]}"
    worker.start()
    start_label = "대기 요청 시작됨." if queued else "Ask started."
    send_text(token, chat_id, f"{start_label}\n\n{prompt}", reply_to_message_id=reply_to_message_id)
    return True


def start_next_pending_ask(token: str) -> bool:
    pending = pop_pending_ask()
    if not pending:
        return False
    chat_id = int(pending["chat_id"])
    prompt = str(pending["prompt"])
    reply_to_message_id = pending.get("reply_to_message_id")
    started = start_ask_worker(
        token=token,
        chat_id=chat_id,
        prompt=prompt,
        reply_to_message_id=reply_to_message_id if isinstance(reply_to_message_id, int) else None,
        queued=True,
    )
    if not started:
        enqueue_pending_ask(chat_id, prompt, reply_to_message_id if isinstance(reply_to_message_id, int) else None)
        return False
    log_line(f"pending_ask_started chat_id={chat_id} prompt={prompt[:120].replace(chr(10), ' ')}")
    return True


def get_busy_labels(limit: int = 50) -> list[str]:
    try:
        return [bridge.get_thread_label(item) for item in bridge.get_busy_threads(limit=limit)]
    except Exception:
        log_line("get_busy_labels_error\n" + traceback.format_exc())
        return []


def _clear_open_waiter(chat_id: int, worker: threading.Thread) -> None:
    with OPEN_WAITERS_LOCK:
        current = OPEN_WAITERS.get(chat_id)
        if current and current.get("thread") is worker:
            OPEN_WAITERS.pop(chat_id, None)


def wait_for_busy_to_clear(
    token: str,
    chat_id: int,
    target_ref: str,
    watched_labels: list[str],
    reply_to_message_id: int | None,
    timeout_sec: float = 3600.0,
) -> None:
    worker = threading.current_thread()
    watched_set = {label for label in watched_labels if label}
    deadline = time.time() + timeout_sec
    log_line(
        f"open_waiter_start chat_id={chat_id} target_ref={target_ref} watched={sorted(watched_set)} timeout={timeout_sec}"
    )
    try:
        last_busy = list(watched_labels)
        while time.time() < deadline:
            busy_now = get_busy_labels(limit=50)
            remaining = [label for label in busy_now if label in watched_set]
            if not remaining:
                send_text(
                    token,
                    chat_id,
                    "\n".join(
                        [
                            "Busy finished.",
                            "",
                            f"You can retry now:",
                            f"/open {target_ref}",
                        ]
                    ),
                    reply_to_message_id=reply_to_message_id,
                )
                log_line(f"open_waiter_ready chat_id={chat_id} target_ref={target_ref}")
                return
            last_busy = busy_now
            time.sleep(2.0)

        send_text(
            token,
            chat_id,
            "\n".join(
                [
                    "Busy wait timed out.",
                    "",
                    f"Still busy: {', '.join(last_busy[:3]) or '-'}",
                    f"Retry later: /open {target_ref}",
                ]
            ),
            reply_to_message_id=reply_to_message_id,
        )
        log_line(f"open_waiter_timeout chat_id={chat_id} target_ref={target_ref} last_busy={last_busy[:3]}")
    except Exception:
        log_line(f"open_waiter_crash chat_id={chat_id} target_ref={target_ref}\n{traceback.format_exc()}")
    finally:
        _clear_open_waiter(chat_id, worker)


def ensure_open_waiter(
    token: str,
    chat_id: int,
    target_ref: str,
    watched_labels: list[str],
    reply_to_message_id: int | None,
) -> tuple[bool, str | None]:
    with OPEN_WAITERS_LOCK:
        current = OPEN_WAITERS.get(chat_id)
        if current:
            current_thread = current.get("thread")
            if current_thread and getattr(current_thread, "is_alive", lambda: False)():
                return False, str(current.get("target_ref") or "")
            OPEN_WAITERS.pop(chat_id, None)

        worker = threading.Thread(
            target=wait_for_busy_to_clear,
            args=(token, chat_id, target_ref, watched_labels, reply_to_message_id),
            daemon=True,
            name=f"codex-open-wait-{chat_id}",
        )
        OPEN_WAITERS[chat_id] = {
            "thread": worker,
            "target_ref": target_ref,
            "watched_labels": list(watched_labels),
        }
        worker.start()
        return True, None


def run_ask_job(token: str, chat_id: int, prompt: str, reply_to_message_id: int | None = None) -> None:
    relay = TelegramAskRelay(token, chat_id, reply_to_message_id)
    try:
        log_line(f"ask_job_start chat_id={chat_id} prompt={prompt[:160].replace(chr(10), ' ')}")
        exit_code, output = run_bridge_command_stream(
            [
                "ask",
                "--no-switch-thread",
                "--click",
                "--foreground",
                "--stream",
                "--include-commentary",
                "--timeout",
                "0",
                prompt,
            ],
            relay.feed_line,
        )
        log_line(f"ask_job_finish chat_id={chat_id} exit_code={exit_code}")
        relay.finish()
        if relay.sent_live:
            if exit_code == 0:
                send_text(token, chat_id, "완료.", reply_to_message_id=reply_to_message_id)
            elif not relay.saw_aborted and not relay.saw_timeout:
                send_text(
                    token,
                    chat_id,
                    f"Ask failed (exit {exit_code})\n\n{output or '(no output)'}",
                    reply_to_message_id=reply_to_message_id,
                )
        else:
            title = "Ask finished" if exit_code == 0 else f"Ask failed (exit {exit_code})"
            message = f"{title}\n\n{output or '(no output)'}"
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


def handle_open_command(token: str, chat_id: int, ref: str, abort: bool, reply_to_message_id: int | None) -> None:
    argv = ["open"]
    if abort:
        argv.append("--abort")
    argv.append(ref)
    exit_code, output = run_bridge_command(argv)
    prefix = "Open ok" if exit_code == 0 else f"Open failed (exit {exit_code})"
    note = ""
    if (
        exit_code != 0
        and not abort
        and "A Codex reply is still in progress." in output
    ):
        busy_labels = get_busy_labels(limit=50)
        if busy_labels:
            started, existing_ref = ensure_open_waiter(
                token=token,
                chat_id=chat_id,
                target_ref=ref,
                watched_labels=busy_labels,
                reply_to_message_id=reply_to_message_id,
            )
            if started:
                note = (
                    "\n\nBusy-end notifier armed."
                    f"\nI'll send a message here when the current reply finishes so you can retry `/open {ref}`."
                )
            else:
                note = (
                    "\n\nBusy-end notifier already active."
                    + (f"\nCurrent waiting target: /open {existing_ref}" if existing_ref else "")
                )
    send_text(
        token,
        chat_id,
        f"{prefix}\n\n{output or '(no output)'}{note}",
        reply_to_message_id=reply_to_message_id,
    )


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

    if not text.startswith("/"):
        if active_summary:
            position = enqueue_pending_ask(chat_id, text, reply_to_message_id)
            send_text(
                token,
                chat_id,
                f"Busy.\n\n{active_summary}\n\n대기열에 추가했습니다. ({position})\n현재 답변이 끝나면 자동으로 이어서 보냅니다.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        start_ask_worker(token, chat_id, text, reply_to_message_id)
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
                    "/open <ref>",
                    "/open_abort <ref>",
                    "/status [ref]",
                    "/doctor",
                    "/ask <prompt>",
                    "/abort",
                    "/chatid",
                    "",
                    "Thread refs use the same format as the bridge, for example:",
                    "ai:1, ai:2, taxlab, other, 1, 2",
                ]
            ),
            reply_to_message_id=reply_to_message_id,
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

    if command == "/abort":
        exit_code, output = abort_current_reply()
        prefix = "Abort" if exit_code == 0 else f"Abort pending (exit {exit_code})"
        send_text(token, chat_id, f"{prefix}\n\n{output}", reply_to_message_id=reply_to_message_id)
        return

    if command == "/open":
        if not arg:
            send_text(token, chat_id, "Usage: /open <ref>", reply_to_message_id=reply_to_message_id)
            return
        handle_open_command(token, chat_id, arg, abort=False, reply_to_message_id=reply_to_message_id)
        return

    if command == "/open_abort":
        if not arg:
            send_text(token, chat_id, "Usage: /open_abort <ref>", reply_to_message_id=reply_to_message_id)
            return
        handle_open_command(token, chat_id, arg, abort=True, reply_to_message_id=reply_to_message_id)
        return

    if command == "/ask":
        if not arg:
            send_text(token, chat_id, "Usage: /ask <prompt>", reply_to_message_id=reply_to_message_id)
            return
        if active_summary:
            position = enqueue_pending_ask(chat_id, arg, reply_to_message_id)
            send_text(
                token,
                chat_id,
                f"Busy.\n\n{active_summary}\n\n대기열에 추가했습니다. ({position})\n현재 답변이 끝나면 자동으로 이어서 보냅니다.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        start_ask_worker(token, chat_id, arg, reply_to_message_id)
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
