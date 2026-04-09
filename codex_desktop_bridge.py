"""
Minimal bridge for talking to the current Codex Desktop thread from scripts.

This script does not use the Codex CLI. It works by:
1. Reading Codex Desktop thread/session state from CODEX_HOME or %USERPROFILE%\\.codex
2. Focusing the Codex Desktop window
3. Clicking the composer area, pasting a prompt, and pressing Enter
4. Tailing the session JSONL file until a final answer arrives

It is intentionally conservative and keeps the write path adjustable with CLI
flags because the Codex Desktop UI can change.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import platform
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only runtime
    winreg = None


def get_env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    if value:
        return Path(value).expanduser()
    return default


def resolve_state_db_path(codex_home: Path) -> Path:
    explicit = os.environ.get("CODEX_STATE_DB", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    candidates = []
    for path in codex_home.glob("state_*.sqlite"):
        if path.name.endswith((".sqlite-shm", ".sqlite-wal")):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, path.name, path))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][2]

    return codex_home / "state_5.sqlite"


CODEX_HOME = get_env_path("CODEX_HOME", Path.home() / ".codex")
GLOBAL_STATE_PATH = get_env_path("CODEX_GLOBAL_STATE", CODEX_HOME / ".codex-global-state.json")
STATE_DB_PATH = resolve_state_db_path(CODEX_HOME)
SESSION_INDEX_PATH = get_env_path("CODEX_SESSION_INDEX", CODEX_HOME / "session_index.jsonl")
BRIDGE_STATE_PATH = get_env_path("CODEX_BRIDGE_STATE", CODEX_HOME / "codex_desktop_bridge_state.json")
BACKGROUND_WATCHERS: dict[str, threading.Thread] = {}
BACKGROUND_WATCHERS_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
ULONG_PTR = wt.WPARAM

SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_BACK = 0x08
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_B = 0x42
VK_C = 0x43
VK_J = 0x4A
VK_L = 0x4C
VK_V = 0x56

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wt.DWORD),
        ("union", INPUT_UNION),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wt.LONG),
        ("top", wt.LONG),
        ("right", wt.LONG),
        ("bottom", wt.LONG),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wt.BOOL
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = wt.LPVOID
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.restype = wt.HGLOBAL


@dataclass
class ThreadInfo:
    id: str
    title: str
    cwd: str
    updated_at: int
    rollout_path: str
    model: str
    reasoning_effort: str
    tokens_used: int


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_bridge_state() -> dict:
    return load_json(BRIDGE_STATE_PATH)


def save_bridge_state(data: dict) -> None:
    save_json(BRIDGE_STATE_PATH, data)


def get_selected_thread_id() -> str | None:
    data = load_bridge_state()
    value = data.get("selected_thread_id")
    return value if isinstance(value, str) and value.strip() else None


def set_selected_thread_id(thread_id: str | None) -> None:
    data = load_bridge_state()
    if thread_id:
        data["selected_thread_id"] = thread_id
    else:
        data.pop("selected_thread_id", None)
    save_bridge_state(data)


def get_pending_new_thread_request() -> dict | None:
    data = load_bridge_state()
    value = data.get("pending_new_thread")
    return value if isinstance(value, dict) else None


def set_pending_new_thread_request(workspace_name: str, cwd: str, source_thread_id: str) -> None:
    data = load_bridge_state()
    data["pending_new_thread"] = {
        "workspace_name": workspace_name,
        "cwd": cwd,
        "source_thread_id": source_thread_id,
        "created_at": time.time(),
    }
    save_bridge_state(data)


def clear_pending_new_thread_request() -> None:
    data = load_bridge_state()
    if "pending_new_thread" in data:
        data.pop("pending_new_thread", None)
        save_bridge_state(data)


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def is_protocol_registered(protocol: str) -> bool:
    if not protocol or winreg is None:
        return False

    candidates = [
        (winreg.HKEY_CLASSES_ROOT, protocol),
        (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{protocol}"),
    ]
    for hive, subkey in candidates:
        try:
            with winreg.OpenKey(hive, subkey):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return False


def get_active_workspace_roots() -> list[str]:
    data = load_json(GLOBAL_STATE_PATH)
    roots = data.get("active-workspace-roots") or []
    return [str(Path(root)) for root in roots]


def strip_windows_extended_prefix(path: str) -> str:
    value = str(path or "").strip()
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def normalize_workspace_path(path: str) -> str:
    value = strip_windows_extended_prefix(path)
    if not value:
        return ""
    try:
        return os.path.normcase(os.path.normpath(value))
    except Exception:
        return value.lower()


def load_session_thread_names() -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not SESSION_INDEX_PATH.exists():
        return mapping

    for raw_line in SESSION_INDEX_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        thread_id = payload.get("id")
        thread_name = payload.get("thread_name")
        if isinstance(thread_id, str) and isinstance(thread_name, str) and thread_name.strip():
            mapping[thread_id] = thread_name.strip()
    return mapping


def normalize_ui_match_text(text: str) -> str:
    raw = str(text or "").replace("\r", "\n")
    for line in raw.split("\n"):
        normalized = " ".join(line.split()).strip()
        if normalized:
            return normalized
    return ""


def build_ui_name_prefixes(text: str) -> list[str]:
    text = normalize_ui_match_text(text)
    if not text:
        return []

    candidates = [text]
    for limit in (120, 96, 72, 56, 40):
        if len(text) > limit:
            candidate = text[:limit].rstrip(" .,;:!?-")
            if candidate:
                candidates.append(candidate)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(candidate)
    return deduped


def get_thread_ui_name_candidates(thread: ThreadInfo) -> list[str]:
    candidates: list[str] = []

    session_name = normalize_ui_match_text(load_session_thread_names().get(thread.id, ""))
    if session_name:
        candidates.append(session_name)

    title_name = normalize_ui_match_text(thread.title)
    if title_name:
        candidates.extend(build_ui_name_prefixes(title_name))

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(candidate)
    return deduped


def get_thread_ui_name(thread_id: str, thread: ThreadInfo | None = None) -> str | None:
    if thread is None:
        try:
            thread = get_thread_by_id(thread_id)
        except RuntimeError:
            session_name = normalize_ui_match_text(load_session_thread_names().get(thread_id, ""))
            return session_name or None

    candidates = get_thread_ui_name_candidates(thread)
    return candidates[0] if candidates else None


def load_recent_threads(limit: int = 20) -> list[ThreadInfo]:
    if not STATE_DB_PATH.exists():
        raise FileNotFoundError(
            f"Codex state database not found: {STATE_DB_PATH}. "
            "Set CODEX_HOME or CODEX_STATE_DB if your Codex data lives elsewhere."
        )

    query = """
        SELECT id, title, cwd, updated_at, rollout_path, model, reasoning_effort, tokens_used
        FROM threads
        WHERE archived = 0
        ORDER BY updated_at DESC
    """
    params: tuple[object, ...] = ()
    if limit > 0:
        query += "\n        LIMIT ?"
        params = (limit,)
    with connect_readonly(STATE_DB_PATH) as conn:
        rows = conn.execute(query, params).fetchall()

    threads = []
    for row in rows:
        threads.append(
            ThreadInfo(
                id=row[0],
                title=row[1] or "",
                cwd=row[2] or "",
                updated_at=row[3] or 0,
                rollout_path=row[4] or "",
                model=row[5] or "",
                reasoning_effort=row[6] or "",
                tokens_used=row[7] or 0,
            )
        )
    return threads


def get_thread_by_id(thread_id: str, threads: list[ThreadInfo] | None = None) -> ThreadInfo:
    pool = threads or load_recent_threads(limit=50)
    for thread in pool:
        if thread.id == thread_id:
            return thread
    raise RuntimeError(f"Thread not found: {thread_id}")


def get_thread_workspace_name(thread: ThreadInfo) -> str:
    cwd = strip_windows_extended_prefix((thread.cwd or "").strip())
    if not cwd:
        return "-"
    try:
        return Path(cwd).name or cwd
    except Exception:
        return cwd


def get_thread_label(thread: ThreadInfo) -> str:
    return f"{get_thread_workspace_name(thread)}:{thread.id[:8]}"


def build_workspace_ref_map(threads: list[ThreadInfo]) -> dict[str, str]:
    totals: dict[str, int] = {}
    for thread in threads:
        workspace = get_thread_workspace_name(thread)
        totals[workspace] = totals.get(workspace, 0) + 1

    seen: dict[str, int] = {}
    mapping: dict[str, str] = {}
    for thread in threads:
        workspace = get_thread_workspace_name(thread)
        seen[workspace] = seen.get(workspace, 0) + 1
        if totals.get(workspace, 0) > 1:
            mapping[thread.id] = f"{workspace}:{seen[workspace]}"
        else:
            mapping[thread.id] = workspace
    return mapping


def get_thread_workspace_ref(thread: ThreadInfo, threads: list[ThreadInfo] | None = None) -> str:
    pool = threads or load_recent_threads(limit=50)
    return build_workspace_ref_map(pool).get(thread.id, get_thread_workspace_name(thread))


def resolve_thread_ref(thread_ref: str, limit: int = 50) -> ThreadInfo:
    threads = load_recent_threads(limit=limit)
    if not threads:
        raise RuntimeError("No Codex threads found in the local state DB.")

    normalized = thread_ref.strip().lower()
    if normalized in {"other", "next"}:
        selected_thread_id = get_selected_thread_id()
        for thread in threads:
            if thread.id != selected_thread_id:
                return thread
        raise RuntimeError("No alternate thread found.")

    if thread_ref.isdigit():
        index = int(thread_ref)
        if 1 <= index <= len(threads):
            return threads[index - 1]
        raise RuntimeError(f"Thread index out of range: {thread_ref}")

    workspace_map = build_workspace_ref_map(threads)
    for thread in threads:
        if workspace_map.get(thread.id, "").lower() == normalized:
            return thread

    for thread in threads:
        if normalize_workspace_path(thread.cwd) == normalize_workspace_path(thread_ref):
            return thread

    workspace_matches = [thread for thread in threads if get_thread_workspace_name(thread).lower() == normalized]
    if len(workspace_matches) > 1:
        refs = ", ".join(workspace_map.get(thread.id, thread.id) for thread in workspace_matches)
        raise RuntimeError(
            f"Multiple threads match workspace `{thread_ref}`. Use one of: {refs}"
        )
    for thread in threads:
        if get_thread_workspace_name(thread).lower() == normalized:
            return thread

    return get_thread_by_id(thread_ref, threads=load_recent_threads(limit=0))


def resolve_new_thread_source(thread_ref: str | None, thread_id: str | None, cwd: str | None) -> ThreadInfo:
    if not thread_ref:
        return choose_thread(thread_id, cwd)

    threads = load_recent_threads(limit=50)
    normalized = thread_ref.strip().lower()
    for thread in threads:
        if get_thread_workspace_name(thread).lower() == normalized:
            return thread

    return resolve_thread_ref(thread_ref, limit=50)


def get_thread_slot(thread: ThreadInfo, limit: int = 9) -> int | None:
    threads = load_recent_threads(limit=max(limit, 9))
    for index, item in enumerate(threads, start=1):
        if item.id == thread.id:
            return index
    return None


def choose_thread(thread_id: str | None, cwd: str | None) -> ThreadInfo:
    threads = load_recent_threads(limit=50)
    if not threads:
        raise RuntimeError("No Codex threads found in the local state DB.")

    if thread_id:
        for thread in threads:
            if thread.id == thread_id:
                return thread
        raise RuntimeError(f"Thread not found: {thread_id}")

    if cwd:
        target = normalize_workspace_path(cwd)
        for thread in threads:
            if normalize_workspace_path(thread.cwd) == target:
                return thread

    selected_thread_id = get_selected_thread_id()
    if selected_thread_id:
        for thread in threads:
            if thread.id == selected_thread_id:
                return thread

    active_roots = get_active_workspace_roots()
    if active_roots:
        active_set = {normalize_workspace_path(root) for root in active_roots}
        for thread in threads:
            if normalize_workspace_path(thread.cwd) in active_set:
                return thread

    return threads[0]


def format_timestamp(unix_seconds: int) -> str:
    if not unix_seconds:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_seconds))


def extract_message_text(payload: dict) -> str:
    parts = payload.get("content") or []
    texts = []
    for part in parts:
        if part.get("type") in ("input_text", "output_text"):
            text = part.get("text", "")
            if text:
                texts.append(text)
    return "\n".join(texts).strip()


def iter_session_events(session_path: Path) -> Iterator[dict]:
    with session_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def get_last_user_and_assistant_messages(session_path: Path) -> tuple[str, str]:
    last_user = ""
    last_assistant = ""

    for event in iter_session_events(session_path):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if event.get("type") == "response_item" and payload.get("type") == "message":
            text = extract_message_text(payload)
            if payload.get("role") == "user" and text:
                last_user = text
            if payload.get("role") == "assistant" and text:
                last_assistant = text

    return last_user, last_assistant


def is_thread_busy(session_path: Path) -> bool:
    last_started = -1
    last_complete = -1
    last_final = -1
    last_aborted = -1

    for index, event in enumerate(iter_session_events(session_path)):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if event.get("type") == "event_msg":
            event_type = payload.get("type")
            if event_type == "task_started":
                last_started = index
            elif event_type == "task_complete":
                last_complete = index
            elif event_type in {"turn_aborted", "task_aborted", "task_cancelled"}:
                last_aborted = index
            continue

        if event.get("type") != "response_item":
            continue
        if payload.get("type") != "message":
            continue
        if payload.get("role") != "assistant":
            continue
        if payload.get("phase") == "final_answer":
            last_final = index

    return last_started > max(last_complete, last_final, last_aborted)


def get_busy_threads(limit: int = 50) -> list[ThreadInfo]:
    busy_threads: list[ThreadInfo] = []
    for thread in load_recent_threads(limit=limit):
        session_path = Path(thread.rollout_path)
        if not session_path.exists():
            continue
        if is_thread_busy(session_path):
            busy_threads.append(thread)
    return busy_threads


def read_new_session_events(session_path: Path, start_offset: int) -> tuple[list[dict], int]:
    events = []
    if not session_path.exists():
        return events, start_offset

    with session_path.open("r", encoding="utf-8") as handle:
        handle.seek(start_offset)
        while True:
            pos = handle.tell()
            raw = handle.readline()
            if not raw:
                return events, pos

            line = raw.strip()
            if not line:
                continue

            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                handle.seek(pos)
                return events, pos


def extract_user_text_from_event(event: dict) -> str:
    payload = event.get("payload") or {}
    if event.get("type") != "response_item":
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return ""
    return extract_message_text(payload)


def normalize_prompt_text(text: str) -> str:
    return " ".join(str(text).replace("\r", " ").replace("\n", " ").split()).strip()


def snapshot_recent_session_offsets(
    limit: int = 10,
    include_threads: list[ThreadInfo] | None = None,
) -> dict[str, tuple[ThreadInfo, Path, int]]:
    snapshot: dict[str, tuple[ThreadInfo, Path, int]] = {}
    threads = load_recent_threads(limit=limit)
    if include_threads:
        seen_ids = {thread.id for thread in threads}
        for thread in include_threads:
            if thread.id not in seen_ids:
                threads.append(thread)
                seen_ids.add(thread.id)
    for thread in threads:
        session_path = Path(thread.rollout_path)
        if not session_path.exists():
            continue
        snapshot[thread.id] = (thread, session_path, session_path.stat().st_size)
    return snapshot


def wait_for_prompt_delivery(
    session_offsets: dict[str, tuple[ThreadInfo, Path, int]],
    prompt: str,
    timeout_sec: float = 4.0,
    allow_new_cwd: str | None = None,
    require_new_thread: bool = False,
) -> ThreadInfo | None:
    normalized_prompt = normalize_prompt_text(prompt)
    deadline = time.time() + timeout_sec
    cursors = {thread_id: offset for thread_id, (_, _, offset) in session_offsets.items()}
    initial_thread_ids = set(session_offsets)
    target_cwd = normalize_workspace_path(allow_new_cwd) if allow_new_cwd else ""

    while time.time() < deadline:
        if target_cwd:
            for candidate in load_recent_threads(limit=0):
                if candidate.id in session_offsets:
                    continue
                if normalize_workspace_path(candidate.cwd) != target_cwd:
                    continue
                session_path = Path(candidate.rollout_path)
                if not session_path.exists():
                    continue
                session_offsets[candidate.id] = (candidate, session_path, 0)
                cursors[candidate.id] = 0

        for thread_id, (thread, session_path, _initial_offset) in list(session_offsets.items()):
            cursor = cursors.get(thread_id, 0)
            events, cursor = read_new_session_events(session_path, cursor)
            cursors[thread_id] = cursor
            for event in events:
                user_text = extract_user_text_from_event(event)
                if not user_text:
                    continue
                if normalize_prompt_text(user_text) == normalized_prompt:
                    if require_new_thread and thread_id in initial_thread_ids:
                        continue
                    return thread
        time.sleep(0.2)

    return None


def watch_for_final_answer(
    session_path: Path,
    start_offset: int,
    timeout_sec: float,
    include_commentary: bool,
    stream_live: bool = False,
    stream_label: str = "",
) -> dict:
    deadline = time.time() + timeout_sec if timeout_sec > 0 else None
    cursor = start_offset
    commentary: list[str] = []
    final_answer = ""
    seen_agent_messages: set[str] = set()
    did_stream_live = False

    while deadline is None or time.time() < deadline:
        events, cursor = read_new_session_events(session_path, cursor)
        for event in events:
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue

            if event.get("type") == "event_msg" and payload.get("type") == "agent_message":
                if str(payload.get("phase", "") or "") == "final_answer":
                    continue
                message = str(payload.get("message", "")).strip()
                if include_commentary and message:
                    if message not in seen_agent_messages:
                        seen_agent_messages.add(message)
                        commentary.append(message)
                        if stream_live:
                            did_stream_live = True
                            with PRINT_LOCK:
                                prefix = f"{stream_label} " if stream_label else ""
                                print(f"{prefix}[commentary]")
                                print(message)
                                print("")
                continue

            if event.get("type") == "event_msg" and payload.get("type") in {"turn_aborted", "task_aborted", "task_cancelled"}:
                return {
                    "status": "aborted",
                    "commentary": commentary,
                    "final_answer": final_answer,
                    "streamed_live": did_stream_live,
                }

            if event.get("type") != "response_item":
                continue

            if payload.get("type") != "message":
                continue

            if payload.get("role") != "assistant":
                continue

            text = extract_message_text(payload)
            if not text:
                continue

            phase = payload.get("phase", "")
            if phase == "final_answer":
                final_answer = text
                if stream_live:
                    did_stream_live = True
                    with PRINT_LOCK:
                        prefix = f"{stream_label} " if stream_label else ""
                        print(f"{prefix}[final_answer]")
                        print(final_answer)
                        print("")
                return {
                    "status": "final",
                    "commentary": commentary,
                    "final_answer": final_answer,
                    "streamed_live": did_stream_live,
                }

            if include_commentary and phase == "commentary":
                if not commentary or commentary[-1] != text:
                    commentary.append(text)
                    if stream_live:
                        did_stream_live = True
                        with PRINT_LOCK:
                            prefix = f"{stream_label} " if stream_label else ""
                            print(f"{prefix}[commentary]")
                            print(text)
                            print("")

        time.sleep(0.35)

    return {
        "status": "timeout",
        "commentary": commentary,
        "final_answer": final_answer,
        "streamed_live": did_stream_live,
    }


def _background_watch_worker(
    thread: ThreadInfo,
    start_offset: int,
    timeout_sec: float,
    include_commentary: bool,
    stream_output: bool,
) -> None:
    label = get_thread_label(thread)
    try:
        result = watch_for_final_answer(
            session_path=Path(thread.rollout_path),
            start_offset=start_offset,
            timeout_sec=timeout_sec,
            include_commentary=include_commentary,
            stream_live=stream_output,
            stream_label=label,
        )
        with PRINT_LOCK:
            if result["final_answer"]:
                print(f"{label} [ready]")
                print("")
            elif result["status"] == "aborted":
                print(f"{label} [aborted]")
                print("")
            elif result["status"] == "timeout":
                print(f"{label} [watch_timeout]")
                print("")
    finally:
        with BACKGROUND_WATCHERS_LOCK:
            current = BACKGROUND_WATCHERS.get(thread.id)
            if current is threading.current_thread():
                BACKGROUND_WATCHERS.pop(thread.id, None)


def start_background_watch(
    thread: ThreadInfo,
    start_offset: int,
    timeout_sec: float,
    include_commentary: bool,
    stream_output: bool,
) -> bool:
    with BACKGROUND_WATCHERS_LOCK:
        existing = BACKGROUND_WATCHERS.get(thread.id)
        if existing and existing.is_alive():
            return False
        worker = threading.Thread(
            target=_background_watch_worker,
            args=(thread, start_offset, timeout_sec, include_commentary, stream_output),
            daemon=True,
            name=f"codex-bridge-watch-{thread.id[:8]}",
        )
        BACKGROUND_WATCHERS[thread.id] = worker
        worker.start()
        return True


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def find_codex_window() -> WindowInfo:
    found: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def enum_windows_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True

        title = get_window_text(hwnd).strip()
        if "Codex" not in title:
            return True

        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True

        found.append(
            WindowInfo(
                hwnd=hwnd,
                title=title,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
            )
        )
        return True

    user32.EnumWindows(enum_windows_proc, 0)

    if not found:
        raise RuntimeError("Visible Codex Desktop window not found.")

    foreground = user32.GetForegroundWindow()
    for window in found:
        if window.hwnd == foreground:
            return window

    return found[0]


def focus_window(window: WindowInfo) -> None:
    user32.ShowWindow(window.hwnd, SW_RESTORE)
    user32.SetForegroundWindow(window.hwnd)
    user32.BringWindowToTop(window.hwnd)
    time.sleep(0.2)


def focus_codex_composer() -> bool:
    script = r"""
$code = @'
using System;
using System.Runtime.InteropServices;
public static class Native {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int maxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
'@
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type $code
$script:result = [IntPtr]::Zero
$cb = [Native+EnumWindowsProc]{
  param($hWnd, $lParam)
  if (-not [Native]::IsWindowVisible($hWnd)) { return $true }
  $sb = New-Object System.Text.StringBuilder 512
  [void][Native]::GetWindowText($hWnd, $sb, $sb.Capacity)
  if ($sb.ToString() -like '*Codex*') { $script:result = $hWnd; return $false }
  return $true
}
[void][Native]::EnumWindows($cb, [IntPtr]::Zero)
if ($script:result -eq [IntPtr]::Zero) { Write-Output 'NO_CODEX_WINDOW'; exit 2 }
$cond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
  [int]$script:result
)
$win = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
  [System.Windows.Automation.TreeScope]::Descendants,
  $cond
)
if (-not $win) { Write-Output 'NO_AUTOMATION_WINDOW'; exit 3 }
$all = $win.FindAll(
  [System.Windows.Automation.TreeScope]::Descendants,
  [System.Windows.Automation.Condition]::TrueCondition
)
foreach ($el in $all) {
  if ($el.Current.ClassName -like 'ProseMirror*' -and $el.Current.IsKeyboardFocusable) {
    try {
      $el.SetFocus()
      Start-Sleep -Milliseconds 120
      $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
      if ($focused -and $focused.Current.ClassName -like 'ProseMirror*') {
        Write-Output 'OK'
        exit 0
      }
    } catch {}
  }
}
Write-Output 'NO_PROSEMIRROR'
exit 4
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False

    output = (result.stdout or "").strip()
    return result.returncode == 0 and output.endswith("OK")


def ensure_codex_composer_focus(attempts: int = 4) -> bool:
    if focus_codex_composer():
        return True

    for _ in range(attempts):
        send_key_event(VK_TAB, keyup=False)
        send_key_event(VK_TAB, keyup=True)
        time.sleep(0.08)
        if focus_codex_composer():
            return True

    return False


def activate_thread_by_sidebar(thread_name: str, project_name: str | None = None) -> str:
    if not thread_name.strip():
        raise RuntimeError("Missing thread_name for sidebar activation.")

    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$targetThread = $env:CODEX_THREAD_NAME
$targetThread = if ($targetThread) { $targetThread.Trim() } else { '' }
$projectName = $env:CODEX_PROJECT_NAME
$projectName = if ($projectName) { $projectName.Trim() } else { '' }
if (-not $targetThread) { Write-Output 'NO_THREAD_NAME'; exit 2 }

$code = @'
using System;
using System.Runtime.InteropServices;
public static class Native {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int maxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
  [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
'@
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type $code

function Get-CodexWindowHandle {
  $script:result = [IntPtr]::Zero
  $cb = [Native+EnumWindowsProc]{
    param($hWnd, $lParam)
    if (-not [Native]::IsWindowVisible($hWnd)) { return $true }
    $sb = New-Object System.Text.StringBuilder 512
    [void][Native]::GetWindowText($hWnd, $sb, $sb.Capacity)
    if ($sb.ToString() -like '*Codex*') { $script:result = $hWnd; return $false }
    return $true
  }
  [void][Native]::EnumWindows($cb, [IntPtr]::Zero)
  return $script:result
}

function Find-CodexAutomationWindow {
  param([IntPtr]$Handle)
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
    [int]$Handle
  )
  return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    $cond
  )
}

function Get-AllElements {
  param($Root)
  return $Root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
  )
}

function Invoke-Or-Click {
  param($Element)

  $pattern = $null
  if ($Element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
    try { $pattern.Select(); return $true } catch {}
  }
  $pattern = $null
  if ($Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
    try { $pattern.Invoke(); return $true } catch {}
  }
  try {
    $point = $Element.GetClickablePoint()
    [void][Native]::SetCursorPos([int]$point.X, [int]$point.Y)
    Start-Sleep -Milliseconds 80
    [Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    return $true
  } catch {}
  try {
    $rect = $Element.Current.BoundingRectangle
    if ($rect.Width -gt 1 -and $rect.Height -gt 1) {
      $x = [int]($rect.Left + ($rect.Width / 2))
      $y = [int]($rect.Top + ($rect.Height / 2))
      [void][Native]::SetCursorPos($x, $y)
      Start-Sleep -Milliseconds 80
      [Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
      [Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
      return $true
    }
  } catch {}
  return $false
}

function Find-ElementByNameContains {
  param($Elements, [string]$ControlTypeName, [string]$Needle)
  foreach ($el in $Elements) {
    $name = ($el.Current.Name -replace "`r|`n", ' ').Trim()
    $type = $el.Current.ControlType.ProgrammaticName
    if ($name -and $type -eq $ControlTypeName -and $name.Contains($Needle)) {
      return $el
    }
  }
  return $null
}

function Get-ListItemNames {
  param($Elements)
  $names = New-Object System.Collections.Generic.List[string]
  foreach ($el in $Elements) {
    $type = $el.Current.ControlType.ProgrammaticName
    if ($type -ne 'ControlType.ListItem') { continue }
    $name = ($el.Current.Name -replace "`r|`n", ' ').Trim()
    if (-not $name) { continue }
    if (-not $names.Contains($name)) { [void]$names.Add($name) }
  }
  return ($names -join ' | ')
}

function Refresh-AllElements {
  param($Window)
  return Get-AllElements $Window
}

function Send-CtrlB {
  [Native]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  [Native]::keybd_event(0x42, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 50
  [Native]::keybd_event(0x42, 0, 0x0002, [UIntPtr]::Zero)
  [Native]::keybd_event(0x11, 0, 0x0002, [UIntPtr]::Zero)
}

function Expand-ProjectSection {
  param($Window, $Elements, [string]$ProjectName)
  if (-not $ProjectName) { return $Elements }

  $projectItem = Find-ElementByNameContains $Elements 'ControlType.ListItem' $ProjectName
  $projectButton = Find-ElementByNameContains $Elements 'ControlType.Button' $ProjectName
  $expandButton = $null

  foreach ($candidate in @($projectItem, $projectButton)) {
    if (-not $candidate) { continue }
    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
      [System.Windows.Automation.ControlType]::Button
    )
    $buttons = $candidate.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonCondition)
    for ($i=0; $i -lt $buttons.Count; $i++) {
      $btn = $buttons.Item($i)
      $btnName = ($btn.Current.Name -replace "`r|`n", ' ').Trim()
      if ($btnName -like '*폴더 확장*') { $expandButton = $btn; break }
    }
    if ($expandButton) { break }
  }

  if ($expandButton) {
    if (Invoke-Or-Click $expandButton) {
      Start-Sleep -Milliseconds 300
      return Refresh-AllElements $Window
    }
  }

  if ($projectButton) {
    if (Invoke-Or-Click $projectButton) {
      Start-Sleep -Milliseconds 300
      return Refresh-AllElements $Window
    }
  }
  return $Elements
}

$handle = Get-CodexWindowHandle
if ($handle -eq [IntPtr]::Zero) { Write-Output 'NO_CODEX_WINDOW'; exit 3 }
[void][Native]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 180
$win = Find-CodexAutomationWindow $handle
if (-not $win) { Write-Output 'NO_AUTOMATION_WINDOW'; exit 4 }
$all = Get-AllElements $win

$hideSidebar = Find-ElementByNameContains $all 'ControlType.Button' '사이드바 숨기기'
if (-not $hideSidebar) {
  $showSidebar = Find-ElementByNameContains $all 'ControlType.Button' '사이드바 표시'
  if ($showSidebar) {
    if (-not (Invoke-Or-Click $showSidebar)) { Write-Output 'SIDEBAR_TOGGLE_FAILED'; exit 5 }
    Start-Sleep -Milliseconds 250
    $all = Get-AllElements $win
  }
}

$target = Find-ElementByNameContains $all 'ControlType.ListItem' $targetThread
if (-not $target) {
  $projectHit = $null
  if ($projectName) {
    $projectHit = Find-ElementByNameContains $all 'ControlType.ListItem' $projectName
    if (-not $projectHit) {
      $projectHit = Find-ElementByNameContains $all 'ControlType.Button' $projectName
    }
  }
  if (-not $projectHit) {
    Send-CtrlB
    Start-Sleep -Milliseconds 350
    $all = Refresh-AllElements $win
  }
  $all = Expand-ProjectSection $win $all $projectName
  $target = Find-ElementByNameContains $all 'ControlType.ListItem' $targetThread
}
if (-not $target) {
  $visible = Get-ListItemNames $all
  Write-Output "NOT_FOUND:$targetThread || VISIBLE:$visible"
  exit 6
}

$buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Button
)
$clickTarget = $target.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $buttonCondition)
if (-not $clickTarget) { $clickTarget = $target }

if (-not (Invoke-Or-Click $clickTarget)) {
  Write-Output "ACTIVATE_FAILED:$targetThread"
  exit 7
}

Start-Sleep -Milliseconds 800
Write-Output ("OK:" + (($target.Current.Name -replace "`r|`n", ' ').Trim()))
"""
    env = os.environ.copy()
    env["CODEX_THREAD_NAME"] = thread_name
    env["CODEX_PROJECT_NAME"] = project_name or ""

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            env=env,
        )
    except Exception as exc:
        raise RuntimeError(f"Sidebar activation subprocess failed: {exc}") from exc

    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0 or not output.startswith("OK:"):
        detail = output or error or f"exit={result.returncode}"
        raise RuntimeError(f"Sidebar activation failed: {detail}")
    return output[3:].strip()


def activate_thread_by_sidebar_v2(thread_name: str, project_name: str | None = None) -> str:
    if not thread_name.strip():
        raise RuntimeError("Missing thread_name for sidebar activation.")

    # The UIAutomation subprocess is more reliable if Python already pulled Codex
    # to the foreground. Without this, mouse fallback can land in VS Code/Terminal.
    focus_window(find_codex_window())

    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$targetThread = $env:CODEX_THREAD_NAME
$targetThread = if ($targetThread) { $targetThread.Trim() } else { '' }
$projectName = $env:CODEX_PROJECT_NAME
$projectName = if ($projectName) { $projectName.Trim() } else { '' }
if (-not $targetThread) { Write-Output 'NO_THREAD_NAME'; exit 2 }

$code = @'
using System;
using System.Runtime.InteropServices;
public static class Native {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int maxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
  [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
'@
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type $code

function Get-CodexWindowHandle {
  $script:result = [IntPtr]::Zero
  $cb = [Native+EnumWindowsProc]{
    param($hWnd, $lParam)
    if (-not [Native]::IsWindowVisible($hWnd)) { return $true }
    $sb = New-Object System.Text.StringBuilder 512
    [void][Native]::GetWindowText($hWnd, $sb, $sb.Capacity)
    if ($sb.ToString() -like '*Codex*') { $script:result = $hWnd; return $false }
    return $true
  }
  [void][Native]::EnumWindows($cb, [IntPtr]::Zero)
  return $script:result
}

function Find-CodexAutomationWindow {
  param([IntPtr]$Handle)
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
    [int]$Handle
  )
  return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    $cond
  )
}

function Get-AllElements {
  param($Root)
  return $Root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
  )
}

function Normalize-Name {
  param([string]$Name)
  return (($Name -replace "`r|`n", ' ') -replace '\s+', ' ').Trim()
}

function Refresh-AllElements {
  param($Window)
  return Get-AllElements $Window
}

function Invoke-Or-Click {
  param($Element)
  $pattern = $null
  if ($Element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
    try { $pattern.Select(); return $true } catch {}
  }
  $pattern = $null
  if ($Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
    try { $pattern.Invoke(); return $true } catch {}
  }
  try {
    $point = $Element.GetClickablePoint()
    [void][Native]::SetCursorPos([int]$point.X, [int]$point.Y)
    Start-Sleep -Milliseconds 80
    [Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    return $true
  } catch {}
  try {
    $rect = $Element.Current.BoundingRectangle
    if ($rect.Width -gt 1 -and $rect.Height -gt 1) {
      $x = [int]($rect.Left + ($rect.Width / 2))
      $y = [int]($rect.Top + ($rect.Height / 2))
      [void][Native]::SetCursorPos($x, $y)
      Start-Sleep -Milliseconds 80
      [Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
      [Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
      return $true
    }
  } catch {}
  return $false
}

function Send-Key {
  param([byte]$VirtualKey)
  [Native]::keybd_event($VirtualKey, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 45
  [Native]::keybd_event($VirtualKey, 0, 0x0002, [UIntPtr]::Zero)
}

function Send-Hotkey {
  param([byte[]]$Keys)
  foreach ($key in $Keys) {
    [Native]::keybd_event($key, 0, 0, [UIntPtr]::Zero)
  }
  Start-Sleep -Milliseconds 50
  for ($i = $Keys.Length - 1; $i -ge 0; $i--) {
    [Native]::keybd_event($Keys[$i], 0, 0x0002, [UIntPtr]::Zero)
  }
}

function Get-SidebarRightBoundary {
  param($WindowRect)
  return [double]($WindowRect.Left + [Math]::Max(320, [Math]::Floor($WindowRect.Width * 0.42)))
}

function Is-SidebarElement {
  param($Element, $WindowRect)
  try {
    $rect = $Element.Current.BoundingRectangle
    if ($rect.Width -le 1 -or $rect.Height -le 1) { return $false }
    if ($rect.Top -lt ($WindowRect.Top + 40)) { return $false }
    if ($rect.Left -gt (Get-SidebarRightBoundary $WindowRect)) { return $false }
    if ($rect.Right -lt ($WindowRect.Left + 12)) { return $false }
    if ($rect.Bottom -gt ($WindowRect.Bottom - 36) -and $rect.Top -gt ($WindowRect.Top + ($WindowRect.Height * 0.65))) {
      return $false
    }
    return $true
  } catch {
    return $false
  }
}

function Find-SidebarElementByNameContains {
  param($Elements, $WindowRect, [string]$Needle)
  if (-not $Needle) { return $null }
  $types = @('ControlType.ListItem', 'ControlType.Button', 'ControlType.Text')
  foreach ($controlTypeName in $types) {
    foreach ($el in $Elements) {
      if ($el.Current.ControlType.ProgrammaticName -ne $controlTypeName) { continue }
      if (-not (Is-SidebarElement $el $WindowRect)) { continue }
      $name = Normalize-Name $el.Current.Name
      if ($name -and $name.Contains($Needle)) {
        return $el
      }
    }
  }
  return $null
}

function Get-VisibleSidebarNames {
  param($Elements, $WindowRect)
  $names = New-Object System.Collections.Generic.List[string]
  foreach ($el in $Elements) {
    $type = $el.Current.ControlType.ProgrammaticName
    if ($type -notin @('ControlType.ListItem', 'ControlType.Button', 'ControlType.Text')) { continue }
    if (-not (Is-SidebarElement $el $WindowRect)) { continue }
    $name = Normalize-Name $el.Current.Name
    if (-not $name) { continue }
    if (-not $names.Contains($name)) { [void]$names.Add($name) }
  }
  return ($names -join ' | ')
}

function Has-TerminalTabs {
  param($Elements)
  foreach ($el in $Elements) {
    if ($el.Current.ControlType.ProgrammaticName -ne 'ControlType.ListItem') { continue }
    $name = Normalize-Name $el.Current.Name
    if ($name -match '^Terminal\s+\d+') {
      return $true
    }
  }
  return $false
}

function Try-ExpandElement {
  param($Element)
  if (-not $Element) { return $false }
  $pattern = $null
  if ($Element.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern)) {
    try {
      if ($pattern.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Collapsed) {
        $pattern.Expand()
        return $true
      }
      if ($pattern.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Expanded) {
        return $true
      }
    } catch {}
  }
  return $false
}

function Expand-ProjectSection {
  param($Window, $Elements, $WindowRect, [string]$ProjectName)
  if (-not $ProjectName) { return $Elements }
  $projectElement = Find-SidebarElementByNameContains $Elements $WindowRect $ProjectName
  if (-not $projectElement) { return $Elements }

  if (Try-ExpandElement $projectElement) {
    Start-Sleep -Milliseconds 220
    return Refresh-AllElements $Window
  }

  $buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button
  )
  $buttons = $projectElement.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonCondition)
  for ($i = 0; $i -lt $buttons.Count; $i++) {
    $btn = $buttons.Item($i)
    if (Try-ExpandElement $btn) {
      Start-Sleep -Milliseconds 220
      return Refresh-AllElements $Window
    }
  }

  if (Invoke-Or-Click $projectElement) {
    Start-Sleep -Milliseconds 260
    return Refresh-AllElements $Window
  }

  return $Elements
}

function Stabilize-Ui {
  param($Window, $Elements, $WindowRect)
  for ($i = 0; $i -lt 2; $i++) {
    Send-Key 0x1B
    Start-Sleep -Milliseconds 120
  }
  $Elements = Refresh-AllElements $Window
  $sidebarNames = Get-VisibleSidebarNames $Elements $WindowRect

  if ((-not $sidebarNames) -and (Has-TerminalTabs $Elements)) {
    Send-Hotkey @(0x11, 0x4A)
    Start-Sleep -Milliseconds 350
    $Elements = Refresh-AllElements $Window
    $sidebarNames = Get-VisibleSidebarNames $Elements $WindowRect
  }

  if (-not $sidebarNames) {
    Send-Hotkey @(0x11, 0x42)
    Start-Sleep -Milliseconds 350
    $Elements = Refresh-AllElements $Window
  }

  return $Elements
}

function Get-SidebarAnchorPoint {
  param($WindowRect)
  $x = [int]($WindowRect.Left + [Math]::Min(260, [Math]::Max(150, [Math]::Floor($WindowRect.Width * 0.16))))
  $y = [int]($WindowRect.Top + [Math]::Min($WindowRect.Height - 200, [Math]::Max(220, [Math]::Floor($WindowRect.Height * 0.38))))
  return @{ X = $x; Y = $y }
}

function Scroll-Sidebar {
  param($WindowRect, [int]$Delta)
  $anchor = Get-SidebarAnchorPoint $WindowRect
  [void][Native]::SetCursorPos($anchor.X, $anchor.Y)
  Start-Sleep -Milliseconds 50
  [Native]::mouse_event(0x0800, 0, 0, $Delta, [UIntPtr]::Zero)
}

function Find-ThreadWithScroll {
  param($Window, $Elements, $WindowRect, [string]$ThreadName, [string]$ProjectName)
  $candidate = Find-SidebarElementByNameContains $Elements $WindowRect $ThreadName
  if ($candidate) { return @{ Elements = $Elements; Target = $candidate } }

  for ($i = 0; $i -lt 6; $i++) {
    Scroll-Sidebar $WindowRect 120
    Start-Sleep -Milliseconds 120
  }
  $Elements = Refresh-AllElements $Window
  $Elements = Expand-ProjectSection $Window $Elements $WindowRect $ProjectName
  $candidate = Find-SidebarElementByNameContains $Elements $WindowRect $ThreadName
  if ($candidate) { return @{ Elements = $Elements; Target = $candidate } }

  for ($step = 0; $step -lt 18; $step++) {
    Scroll-Sidebar $WindowRect -240
    Start-Sleep -Milliseconds 150
    $Elements = Refresh-AllElements $Window
    $Elements = Expand-ProjectSection $Window $Elements $WindowRect $ProjectName
    $candidate = Find-SidebarElementByNameContains $Elements $WindowRect $ThreadName
    if ($candidate) {
      return @{ Elements = $Elements; Target = $candidate }
    }
  }

  return @{ Elements = $Elements; Target = $null }
}

$handle = Get-CodexWindowHandle
if ($handle -eq [IntPtr]::Zero) { Write-Output 'NO_CODEX_WINDOW'; exit 3 }
[void][Native]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 180
$win = Find-CodexAutomationWindow $handle
if (-not $win) { Write-Output 'NO_AUTOMATION_WINDOW'; exit 4 }
$windowRect = $win.Current.BoundingRectangle
$all = Get-AllElements $win
$all = Stabilize-Ui $win $all $windowRect
$all = Expand-ProjectSection $win $all $windowRect $projectName

$target = Find-SidebarElementByNameContains $all $windowRect $targetThread
if (-not $target) {
  $searchResult = Find-ThreadWithScroll $win $all $windowRect $targetThread $projectName
  $all = $searchResult.Elements
  $target = $searchResult.Target
}
if (-not $target) {
  $visible = Get-VisibleSidebarNames $all $windowRect
  if (-not $visible) { $visible = 'NONE' }
  Write-Output "NOT_FOUND:$targetThread || VISIBLE:$visible"
  exit 6
}

$buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Button
)
$clickTarget = $target.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $buttonCondition)
if (-not $clickTarget) { $clickTarget = $target }

if (-not (Invoke-Or-Click $clickTarget)) {
  Write-Output "ACTIVATE_FAILED:$targetThread"
  exit 7
}

Start-Sleep -Milliseconds 800
Write-Output ("OK:" + (Normalize-Name $target.Current.Name))
"""
    env = os.environ.copy()
    env["CODEX_THREAD_NAME"] = thread_name
    env["CODEX_PROJECT_NAME"] = project_name or ""

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            env=env,
        )
    except Exception as exc:
        raise RuntimeError(f"Sidebar activation subprocess failed: {exc}") from exc

    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0 or not output.startswith("OK:"):
        detail = output or error or f"exit={result.returncode}"
        raise RuntimeError(f"Sidebar activation failed: {detail}")
    return output[3:].strip()


def get_clipboard_text() -> str | None:
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def verify_active_thread(thread_id: str) -> str | None:
    original = get_clipboard_text()
    try:
        focus_window(find_codex_window())
        for attempt in range(2):
            sentinel = f"__CODEX_BRIDGE__{time.time_ns()}__L__"
            set_clipboard_text(sentinel)
            send_hotkey(VK_CONTROL, VK_MENU, VK_L)
            time.sleep(0.25)
            deeplink = get_clipboard_text() or ""
            if deeplink != sentinel and thread_id in deeplink:
                return "copy-deeplink"

            sentinel = f"__CODEX_BRIDGE__{time.time_ns()}__C__"
            set_clipboard_text(sentinel)
            send_hotkey(VK_CONTROL, VK_MENU, VK_C)
            time.sleep(0.25)
            session_id = get_clipboard_text() or ""
            if session_id != sentinel and thread_id.strip() == session_id.strip():
                return "copy-session-id"

            if attempt == 0:
                send_key_event(VK_ESCAPE, keyup=False)
                send_key_event(VK_ESCAPE, keyup=True)
                time.sleep(0.15)
        return None
    finally:
        if original is not None:
            try:
                set_clipboard_text(original)
            except Exception:
                pass


def verify_active_thread_by_header(thread_name: str) -> str | None:
    if not thread_name.strip():
        return None

    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$targetThread = $env:CODEX_THREAD_NAME
$targetThread = if ($targetThread) { $targetThread.Trim() } else { '' }
if (-not $targetThread) { Write-Output 'NO_THREAD_NAME'; exit 2 }

$code = @'
using System;
using System.Runtime.InteropServices;
public static class Native {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int maxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type $code

$script:result = [IntPtr]::Zero
$cb = [Native+EnumWindowsProc]{
  param($hWnd, $lParam)
  if (-not [Native]::IsWindowVisible($hWnd)) { return $true }
  $sb = New-Object System.Text.StringBuilder 512
  [void][Native]::GetWindowText($hWnd, $sb, $sb.Capacity)
  if ($sb.ToString() -like '*Codex*') { $script:result = $hWnd; return $false }
  return $true
}
[void][Native]::EnumWindows($cb, [IntPtr]::Zero)
if ($script:result -eq [IntPtr]::Zero) { Write-Output 'NO_CODEX_WINDOW'; exit 3 }
[void][Native]::SetForegroundWindow($script:result)
Start-Sleep -Milliseconds 120
$cond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
  [int]$script:result
)
$win = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
  [System.Windows.Automation.TreeScope]::Descendants,
  $cond
)
if (-not $win) { Write-Output 'NO_AUTOMATION_WINDOW'; exit 4 }
$windowRect = $win.Current.BoundingRectangle
$all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
for ($i=0; $i -lt $all.Count; $i++) {
  $el = $all.Item($i)
  $type = $el.Current.ControlType.ProgrammaticName
  if ($type -notin @('ControlType.Text','ControlType.Button')) { continue }
  $name = ($el.Current.Name -replace "`r|`n", ' ').Trim()
  if (-not $name) { continue }
  $rect = $el.Current.BoundingRectangle
  if ($rect.Top -gt 240) { continue }
  if ($rect.Width -le 1 -or $rect.Height -le 1) { continue }
  if ($rect.Left -lt ($windowRect.Left + ($windowRect.Width * 0.28))) { continue }
  if ($rect.Right -gt ($windowRect.Right - 32)) { continue }
  if ($rect.Top -lt ($windowRect.Top + 16)) { continue }
  if ($name.Contains($targetThread)) {
    Write-Output ('OK:' + $name)
    exit 0
  }
}
Write-Output 'NO_HEADER_MATCH'
exit 5
"""
    env = os.environ.copy()
    env["CODEX_THREAD_NAME"] = thread_name
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            check=False,
            env=env,
        )
    except Exception:
        return None

    output = (result.stdout or "").strip()
    if result.returncode == 0 and output.startswith("OK:"):
        return "header"
    return None


def activate_thread_in_ui(thread: ThreadInfo) -> str:
    ui_name_candidates = get_thread_ui_name_candidates(thread)
    for thread_name in ui_name_candidates:
        header_verified = verify_active_thread_by_header(thread_name)
        if header_verified:
            return f"already-open [{header_verified}]"

    verified_by = verify_active_thread(thread.id)
    if verified_by:
        return f"already-open [{verified_by}]"

    last_error = ""
    for thread_name in ui_name_candidates:
        try:
            matched_label = activate_thread_by_sidebar_v2(
                thread_name,
                Path(thread.cwd).name if thread.cwd else None,
            )
        except Exception as exc:
            last_error = str(exc)
            continue

        time.sleep(0.35)
        header_verified = verify_active_thread_by_header(thread_name)
        if header_verified:
            return f"sidebar:{matched_label} [{header_verified}]"

        verified_by = verify_active_thread(thread.id)
        if verified_by:
            return f"sidebar:{matched_label} [{verified_by}]"

        last_error = (
            "Clicked the sidebar thread item, but the main Codex conversation header did not switch."
        )

    if ui_name_candidates:
        raise RuntimeError(last_error or "Unable to activate the target thread in the Codex UI sidebar.")

    raise RuntimeError(
        "Unable to activate the target thread in the Codex UI sidebar because no usable UI label was found."
    )


def set_clipboard_text(text: str) -> None:
    if not user32.OpenClipboard(None):
        raise RuntimeError("Failed to open the clipboard.")
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("Failed to empty the clipboard.")

        data = text.encode("utf-16-le") + b"\x00\x00"
        h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_global:
            raise RuntimeError("GlobalAlloc failed.")

        pointer = kernel32.GlobalLock(h_global)
        if not pointer:
            kernel32.GlobalFree(h_global)
            raise RuntimeError("GlobalLock failed.")

        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(h_global)

        if not user32.SetClipboardData(CF_UNICODETEXT, h_global):
            kernel32.GlobalFree(h_global)
            raise RuntimeError("SetClipboardData failed.")
    finally:
        user32.CloseClipboard()


def send_key_event(vk: int, keyup: bool = False) -> None:
    flags = KEYEVENTF_KEYUP if keyup else 0
    input_struct = INPUT(
        type=INPUT_KEYBOARD,
        union=INPUT_UNION(
            ki=KEYBDINPUT(
                wVk=vk,
                wScan=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    user32.SendInput(1, ctypes.byref(input_struct), ctypes.sizeof(INPUT))


def send_hotkey(*keys: int) -> None:
    for vk in keys:
        send_key_event(vk, keyup=False)
    for vk in reversed(keys):
        send_key_event(vk, keyup=True)
    time.sleep(0.05)


def cancel_codex_reply_if_busy(timeout_sec: float = 3.0) -> tuple[list[str], list[str]]:
    busy_before = get_busy_threads(limit=50)
    if not busy_before:
        return [], []

    labels_before = [get_thread_label(thread) for thread in busy_before]
    try:
        focus_window(find_codex_window())
    except Exception:
        return labels_before, labels_before

    for _ in range(2):
        send_key_event(VK_ESCAPE, keyup=False)
        send_key_event(VK_ESCAPE, keyup=True)
        time.sleep(0.12)

    deadline = time.time() + timeout_sec
    remaining_threads = busy_before
    while time.time() < deadline:
        remaining_threads = get_busy_threads(limit=50)
        if not remaining_threads:
            return labels_before, []
        time.sleep(0.2)

    return labels_before, [get_thread_label(thread) for thread in remaining_threads]


def click_window(window: WindowInfo, x_ratio: float, y_offset: int) -> tuple[int, int]:
    x = window.left + int(window.width * x_ratio)
    y = max(window.top + 40, window.bottom - y_offset)
    user32.SetCursorPos(x, y)
    time.sleep(0.1)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.1)
    return x, y


def send_prompt_to_codex(
    prompt: str,
    click_x_ratio: float,
    click_y_offset: int,
    skip_click: bool,
) -> WindowInfo:
    window = find_codex_window()
    focus_window(window)
    composer_focused = ensure_codex_composer_focus()
    if not skip_click:
        click_window(window, x_ratio=click_x_ratio, y_offset=click_y_offset)
        composer_focused = ensure_codex_composer_focus() or composer_focused
    set_clipboard_text(prompt)
    send_hotkey(VK_CONTROL, VK_V)
    send_key_event(VK_RETURN, keyup=False)
    send_key_event(VK_RETURN, keyup=True)
    if not composer_focused:
        print("[warning] Composer focus was not confirmed before paste.")
    return window


def build_workspace_new_thread_button_name(workspace_name: str) -> str:
    normalized = " ".join(str(workspace_name or "").split()).strip()
    if not normalized:
        raise RuntimeError("Missing workspace name for new-thread button lookup.")
    return f"{normalized}에서 새 스레드 시작"


def start_new_thread_in_workspace(workspace_name: str) -> str:
    target_button = build_workspace_new_thread_button_name(workspace_name)
    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$targetButton = $env:CODEX_NEW_THREAD_BUTTON
$targetButton = if ($targetButton) { $targetButton.Trim() } else { '' }
if (-not $targetButton) { Write-Output 'NO_TARGET_BUTTON'; exit 2 }

$code = @'
using System;
using System.Runtime.InteropServices;
public static class Native {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int maxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
}
'@
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type $code

function Normalize-Name {
  param([string]$Name)
  return (($Name -replace "`r|`n", ' ') -replace '\s+', ' ').Trim()
}

function Get-CodexWindowHandle {
  $script:result = [IntPtr]::Zero
  $cb = [Native+EnumWindowsProc]{
    param($hWnd, $lParam)
    if (-not [Native]::IsWindowVisible($hWnd)) { return $true }
    $sb = New-Object System.Text.StringBuilder 512
    [void][Native]::GetWindowText($hWnd, $sb, $sb.Capacity)
    if ($sb.ToString() -like '*Codex*') { $script:result = $hWnd; return $false }
    return $true
  }
  [void][Native]::EnumWindows($cb, [IntPtr]::Zero)
  return $script:result
}

function Find-CodexAutomationWindow {
  param([IntPtr]$Handle)
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
    [int]$Handle
  )
  return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    $cond
  )
}

function Get-AllElements {
  param($Root)
  return $Root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
  )
}

function Invoke-Or-Click {
  param($Element)
  $pattern = $null
  if ($Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
    try { $pattern.Invoke(); return $true } catch {}
  }
  try {
    $point = $Element.GetClickablePoint()
    [void][Native]::SetCursorPos([int]$point.X, [int]$point.Y)
    Start-Sleep -Milliseconds 80
    [Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    return $true
  } catch {}
  try {
    $rect = $Element.Current.BoundingRectangle
    if ($rect.Width -gt 1 -and $rect.Height -gt 1) {
      $x = [int]($rect.Left + ($rect.Width / 2))
      $y = [int]($rect.Top + ($rect.Height / 2))
      [void][Native]::SetCursorPos($x, $y)
      Start-Sleep -Milliseconds 80
      [Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
      [Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
      return $true
    }
  } catch {}
  return $false
}

function Get-SidebarRightBoundary {
  param($WindowRect)
  return [double]($WindowRect.Left + [Math]::Max(420, [Math]::Floor($WindowRect.Width * 0.28)))
}

function Find-VisibleSidebarButtonByExactName {
  param($Elements, $WindowRect, [string]$Needle)
  $sidebarRight = Get-SidebarRightBoundary $WindowRect
  foreach ($el in $Elements) {
    $type = $el.Current.ControlType.ProgrammaticName
    if ($type -ne 'ControlType.Button') { continue }
    $name = Normalize-Name $el.Current.Name
    if ($name -ne $Needle) { continue }
    $rect = $el.Current.BoundingRectangle
    if ($rect.Width -le 1 -or $rect.Height -le 1) { continue }
    if ($rect.Left -lt ($WindowRect.Left + 8)) { continue }
    if ($rect.Right -gt $sidebarRight) { continue }
    if ($rect.Top -lt ($WindowRect.Top + 20)) { continue }
    if ($rect.Bottom -gt ($WindowRect.Bottom - 20)) { continue }
    return $el
  }
  return $null
}

function Get-VisibleSidebarButtonNames {
  param($Elements, $WindowRect)
  $sidebarRight = Get-SidebarRightBoundary $WindowRect
  $names = New-Object System.Collections.Generic.List[string]
  foreach ($el in $Elements) {
    $type = $el.Current.ControlType.ProgrammaticName
    if ($type -ne 'ControlType.Button') { continue }
    $name = Normalize-Name $el.Current.Name
    if (-not $name) { continue }
    $rect = $el.Current.BoundingRectangle
    if ($rect.Width -le 1 -or $rect.Height -le 1) { continue }
    if ($rect.Left -lt ($WindowRect.Left + 8)) { continue }
    if ($rect.Right -gt $sidebarRight) { continue }
    if ($rect.Top -lt ($WindowRect.Top + 20)) { continue }
    if ($rect.Bottom -gt ($WindowRect.Bottom - 20)) { continue }
    if ($name -like '*새 스레드*' -or $name -like '*사이드바*') {
      if (-not $names.Contains($name)) { [void]$names.Add($name) }
    }
  }
  return ($names -join ' | ')
}

$handle = Get-CodexWindowHandle
if ($handle -eq [IntPtr]::Zero) { Write-Output 'NO_CODEX_WINDOW'; exit 3 }
[void][Native]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 180
$win = Find-CodexAutomationWindow $handle
if (-not $win) { Write-Output 'NO_AUTOMATION_WINDOW'; exit 4 }
$windowRect = $win.Current.BoundingRectangle
$all = Get-AllElements $win

$hideSidebar = Find-VisibleSidebarButtonByExactName $all $windowRect '사이드바 숨기기'
if (-not $hideSidebar) {
  $showSidebar = Find-VisibleSidebarButtonByExactName $all $windowRect '사이드바 표시'
  if ($showSidebar) {
    if (-not (Invoke-Or-Click $showSidebar)) { Write-Output 'SIDEBAR_TOGGLE_FAILED'; exit 5 }
    Start-Sleep -Milliseconds 250
    $all = Get-AllElements $win
  }
}

$target = Find-VisibleSidebarButtonByExactName $all $windowRect $targetButton
if (-not $target) {
  $visible = Get-VisibleSidebarButtonNames $all $windowRect
  if (-not $visible) { $visible = 'NONE' }
  Write-Output "NOT_FOUND:$targetButton || VISIBLE:$visible"
  exit 6
}

if (-not (Invoke-Or-Click $target)) {
  Write-Output "ACTIVATE_FAILED:$targetButton"
  exit 7
}

Start-Sleep -Milliseconds 700
Write-Output ("OK:" + (Normalize-Name $target.Current.Name))
"""
    env = os.environ.copy()
    env["CODEX_NEW_THREAD_BUTTON"] = target_button
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            env=env,
        )
    except Exception as exc:
        raise RuntimeError(f"New-thread button subprocess failed: {exc}") from exc

    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0 or not output.startswith("OK:"):
        detail = output or error or f"exit={result.returncode}"
        raise RuntimeError(f"Workspace new-thread activation failed: {detail}")
    return output[3:].strip()


def wait_for_new_thread_in_workspace(
    existing_thread_ids: set[str],
    workspace_cwd: str,
    timeout_sec: float = 6.0,
) -> ThreadInfo | None:
    target_cwd = normalize_workspace_path(workspace_cwd)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for thread in load_recent_threads(limit=0):
            if thread.id in existing_thread_ids:
                continue
            if normalize_workspace_path(thread.cwd) != target_cwd:
                continue
            return thread
        time.sleep(0.2)
    return None


def print_thread_list(threads: list[ThreadInfo]) -> None:
    selected_thread_id = get_selected_thread_id()
    workspace_refs = build_workspace_ref_map(threads)
    for index, thread in enumerate(threads, start=1):
        marker = "*" if thread.id == selected_thread_id else " "
        ui_name = get_thread_ui_name(thread.id, thread)
        summary = ui_name or thread.title[:70]
        workspace = workspace_refs.get(thread.id, get_thread_workspace_name(thread))
        busy = is_thread_busy(Path(thread.rollout_path))
        state = "busy" if busy else "idle"
        print(
            f"{marker}{index:>2} | {workspace:<12} | {state:<4} | {thread.id} | {format_timestamp(thread.updated_at)} | {summary}"
        )


def command_list(args: argparse.Namespace) -> int:
    threads = load_recent_threads(limit=args.limit)
    print_thread_list(threads)
    return 0


def command_status(args: argparse.Namespace) -> int:
    thread = choose_thread(args.thread_id, args.cwd)
    session_path = Path(thread.rollout_path)
    last_user, last_assistant = get_last_user_and_assistant_messages(session_path)
    busy = is_thread_busy(session_path)
    slot = get_thread_slot(thread)
    ui_name = get_thread_ui_name(thread.id, thread)
    print(f"thread_id: {thread.id}")
    print(f"thread_ref: {get_thread_workspace_ref(thread)}")
    print(f"title: {thread.title}")
    print(f"ui_name: {ui_name or '-'}")
    print(f"cwd: {thread.cwd}")
    print(f"updated_at: {format_timestamp(thread.updated_at)}")
    print(f"model: {thread.model} / {thread.reasoning_effort}")
    print(f"tokens_used: {thread.tokens_used}")
    print(f"ui_slot: {slot if slot is not None else '-'}")
    print(f"busy: {busy}")
    print(f"session_path: {session_path}")
    print("")
    if last_user:
        print("[last_user]")
        print(last_user)
        print("")
    if last_assistant:
        print("[last_assistant]")
        print(last_assistant)
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    print(f"platform: {platform.platform()}")
    print(f"python_version: {platform.python_version()}")
    print(f"python_executable: {sys.executable}")
    print(f"codex_home: {CODEX_HOME}")
    print(f"codex_home_exists: {CODEX_HOME.exists()}")
    print(f"state_db_path: {STATE_DB_PATH}")
    print(f"state_db_exists: {STATE_DB_PATH.exists()}")
    print(f"session_index_path: {SESSION_INDEX_PATH}")
    print(f"session_index_exists: {SESSION_INDEX_PATH.exists()}")
    print(f"global_state_path: {GLOBAL_STATE_PATH}")
    print(f"global_state_exists: {GLOBAL_STATE_PATH.exists()}")
    print(f"bridge_state_path: {BRIDGE_STATE_PATH}")
    print(f"bridge_state_parent_exists: {BRIDGE_STATE_PATH.parent.exists()}")
    print(f"codex_protocol_registered: {is_protocol_registered('codex')}")
    print(f"selected_thread_id: {get_selected_thread_id() or '-'}")

    thread_count = 0
    db_error = ""
    if STATE_DB_PATH.exists():
        try:
            with connect_readonly(STATE_DB_PATH) as conn:
                row = conn.execute("SELECT COUNT(*) FROM threads WHERE archived = 0").fetchone()
            thread_count = int(row[0] or 0) if row else 0
        except Exception as exc:
            db_error = str(exc)
    print(f"thread_count: {thread_count}")
    if db_error:
        print(f"db_error: {db_error}")

    try:
        window = find_codex_window()
        print(f"codex_window_found: True")
        print(f"codex_window_title: {window.title}")
        print(
            "codex_window_rect: "
            f"({window.left},{window.top})-({window.right},{window.bottom})"
        )
    except Exception as exc:
        print("codex_window_found: False")
        print(f"codex_window_error: {exc}")

    busy_threads = get_busy_threads(limit=max(10, args.limit))
    if busy_threads:
        labels = ", ".join(get_thread_workspace_ref(thread) for thread in busy_threads[: args.limit])
        print(f"busy_threads: {labels}")
    else:
        print("busy_threads: -")

    return 0


def command_focus(args: argparse.Namespace) -> int:
    window = find_codex_window()
    focus_window(window)
    composer_focused = ensure_codex_composer_focus()
    if args.click:
        x, y = click_window(window, args.click_x_ratio, args.click_y_offset)
        print(f"clicked: {x},{y}")
        composer_focused = ensure_codex_composer_focus() or composer_focused
    print(
        f"focused_window: hwnd={window.hwnd} title={window.title} "
        f"rect=({window.left},{window.top})-({window.right},{window.bottom})"
    )
    print(f"composer_focused: {composer_focused}")
    return 0


def command_use(args: argparse.Namespace) -> int:
    if args.clear:
        clear_pending_new_thread_request()
        set_selected_thread_id(None)
        print("selected_thread: cleared")
        return 0

    if args.thread_ref:
        thread = resolve_thread_ref(args.thread_ref)
    else:
        thread = choose_thread(args.thread_id, args.cwd)

    clear_pending_new_thread_request()
    set_selected_thread_id(thread.id)
    print(f"selected_thread: {thread.id}")
    print(f"title: {thread.title}")
    print(f"ui_name: {get_thread_ui_name(thread.id, thread) or '-'}")
    print(f"cwd: {thread.cwd}")
    return 0


def command_tail(args: argparse.Namespace) -> int:
    thread = choose_thread(args.thread_id, args.cwd)
    session_path = Path(thread.rollout_path)
    if not session_path.exists():
        raise RuntimeError(f"Session file not found: {session_path}")

    start_offset = session_path.stat().st_size if args.only_new else 0
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    cursor = start_offset
    seen_agent_messages: set[str] = set()

    while True:
        events, cursor = read_new_session_events(session_path, cursor)
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
                print(f"[commentary] {message}")
                continue

            if event.get("type") == "response_item" and payload.get("type") == "message":
                text = extract_message_text(payload)
                role = payload.get("role", "?")
                phase = payload.get("phase", "")
                if text:
                    if role == "assistant" and phase == "commentary" and text in seen_agent_messages:
                        continue
                    print(f"[{role}:{phase}]")
                    print(text)
                    print("")

        if deadline is not None and time.time() >= deadline:
            return 0
        time.sleep(0.35)


def command_new(args: argparse.Namespace) -> int:
    source_thread = resolve_new_thread_source(getattr(args, "thread_ref", None), args.thread_id, args.cwd)
    workspace_name = get_thread_workspace_name(source_thread)
    if workspace_name == "-" or not normalize_workspace_path(source_thread.cwd):
        raise RuntimeError("Unable to determine the workspace for the requested new thread.")

    cancelled_labels: list[str] = []
    cancel_remaining: list[str] = []
    busy_now = get_busy_threads(limit=50)
    if busy_now:
        labels = ", ".join(get_thread_label(item) for item in busy_now[:3])
        if not args.abort:
            raise RuntimeError(
                "A Codex reply is still in progress. Creating a new thread can stop the current reply. "
                f"Busy thread(s): {labels}. Wait for `[ready]` or rerun with `new --abort ...`."
            )
        cancelled_labels, cancel_remaining = cancel_codex_reply_if_busy(timeout_sec=3.0)

    before_threads = load_recent_threads(limit=0)
    before_ids = {thread.id for thread in before_threads}
    source_activation = activate_thread_in_ui(source_thread)
    clicked_button = start_new_thread_in_workspace(workspace_name)
    new_thread = wait_for_new_thread_in_workspace(before_ids, source_thread.cwd, timeout_sec=6.0)
    if new_thread is None:
        set_selected_thread_id(None)
        set_pending_new_thread_request(workspace_name, source_thread.cwd, source_thread.id)
        print("selected_thread: pending-new-thread")
        print("target_thread: pending-new-thread")
        print(f"source_thread: {source_thread.id}")
        print(f"source_workspace: {workspace_name}")
        print(f"source_activation: {source_activation}")
        print(f"title: -")
        print(f"ui_name: -")
        print(f"cwd: {source_thread.cwd}")
        if cancelled_labels:
            print(f"reply_abort_requested: {', '.join(cancelled_labels)}")
            if cancel_remaining:
                print(f"reply_abort_pending: {', '.join(cancel_remaining)}")
        print(f"ui_activation: workspace-new:{clicked_button} [pending-first-prompt]")
        return 0

    clear_pending_new_thread_request()
    set_selected_thread_id(new_thread.id)
    verified_by = verify_active_thread(new_thread.id)
    ui_activation = (
        f"workspace-new:{clicked_button} [{verified_by}]"
        if verified_by
        else f"workspace-new:{clicked_button} [unverified]"
    )

    print(f"selected_thread: {new_thread.id}")
    print(f"target_thread: {new_thread.id}")
    print(f"source_thread: {source_thread.id}")
    print(f"source_workspace: {workspace_name}")
    print(f"source_activation: {source_activation}")
    print(f"title: {new_thread.title or '-'}")
    print(f"ui_name: {get_thread_ui_name(new_thread.id, new_thread) or '-'}")
    print(f"cwd: {new_thread.cwd}")
    if cancelled_labels:
        print(f"reply_abort_requested: {', '.join(cancelled_labels)}")
        if cancel_remaining:
            print(f"reply_abort_pending: {', '.join(cancel_remaining)}")
    print(f"ui_activation: {ui_activation}")
    return 0


def command_open(args: argparse.Namespace) -> int:
    if getattr(args, "thread_ref", None):
        thread = resolve_thread_ref(args.thread_ref)
    else:
        thread = choose_thread(args.thread_id, args.cwd)
    cancelled_labels: list[str] = []
    cancel_remaining: list[str] = []
    busy_now = get_busy_threads(limit=50)
    busy_ids = {item.id for item in busy_now}
    if busy_now and thread.id not in busy_ids:
        labels = ", ".join(get_thread_label(item) for item in busy_now[:3])
        if not args.abort:
            raise RuntimeError(
                "A Codex reply is still in progress. Changing threads will stop the current reply. "
                f"Busy thread(s): {labels}. Wait for `[ready]` or rerun with `open --abort ...`."
            )
        cancelled_labels, cancel_remaining = cancel_codex_reply_if_busy(timeout_sec=3.0)
    clear_pending_new_thread_request()
    set_selected_thread_id(thread.id)
    activation_warning = ""
    try:
        activation_method = activate_thread_in_ui(thread)
    except Exception as exc:
        activation_method = "best-effort (unverified)"
        activation_warning = str(exc)
    session_path = Path(thread.rollout_path)
    last_user, last_assistant = get_last_user_and_assistant_messages(session_path)
    print(f"selected_thread: {thread.id}")
    print(f"target_thread: {thread.id}")
    print(f"title: {thread.title}")
    print(f"ui_name: {get_thread_ui_name(thread.id, thread) or '-'}")
    print(f"cwd: {thread.cwd}")
    if cancelled_labels:
        print(f"reply_abort_requested: {', '.join(cancelled_labels)}")
        if cancel_remaining:
            print(f"reply_abort_pending: {', '.join(cancel_remaining)}")
    print(f"ui_activation: {activation_method}")
    if activation_warning:
        print(f"ui_warning: {activation_warning}")
    if last_user:
        print("")
        print("[last_user]")
        print(last_user)
    if last_assistant:
        print("")
        print("[last_assistant]")
        print(last_assistant)
    return 0


def command_ask(args: argparse.Namespace) -> int:
    pending_new = None
    if not args.thread_id and not args.cwd:
        pending_new = get_pending_new_thread_request()
        if pending_new:
            try:
                thread = get_thread_by_id(
                    str(pending_new.get("source_thread_id", "")),
                    threads=load_recent_threads(limit=0),
                )
            except RuntimeError:
                clear_pending_new_thread_request()
                pending_new = None
            else:
                session_path = Path(thread.rollout_path)
                if not session_path.exists():
                    clear_pending_new_thread_request()
                    pending_new = None
    if not pending_new:
        thread = choose_thread(args.thread_id, args.cwd)
        session_path = Path(thread.rollout_path)
        if not session_path.exists():
            raise RuntimeError(f"Session file not found: {session_path}")

    prompt = args.prompt
    if pending_new:
        print("target_thread: pending-new-thread")
        print(f"source_thread: {thread.id}")
        print(f"source_workspace: {pending_new.get('workspace_name') or get_thread_workspace_name(thread)}")
    else:
        print(f"target_thread: {thread.id}")
    print(f"title: {thread.title}")
    print(f"ui_name: {get_thread_ui_name(thread.id, thread) or '-'}")
    print(f"cwd: {pending_new.get('cwd') if pending_new else thread.cwd}")
    print("")

    if args.dry_run:
        print("[dry_run]")
        print(prompt)
        return 0

    busy_threads = get_busy_threads(limit=50)
    if busy_threads and not args.force_while_busy:
        labels = ", ".join(get_thread_label(item) for item in busy_threads[:3])
        raise RuntimeError(
            "A Codex reply is still in progress. You can `open` other threads, but `ask` is blocked until it finishes. "
            f"Busy thread(s): {labels}. Pass --force-while-busy to override."
        )

    if not pending_new and is_thread_busy(session_path) and not args.force_while_busy:
        raise RuntimeError(
            "The selected thread is still busy. This often means the same Codex thread is currently active "
            "or another task is still running. Wait, switch to another thread, or pass --force-while-busy."
        )

    start_offset = session_path.stat().st_size if not pending_new else 0
    recent_offsets = snapshot_recent_session_offsets(limit=10, include_threads=[thread])
    activation_warning = ""
    if pending_new:
        activation_method = "pending-new-thread [current-ui]"
    elif args.switch_thread:
        try:
            activation_method = activate_thread_in_ui(thread)
        except Exception as exc:
            activation_method = "best-effort-switch (unverified)"
            activation_warning = str(exc)
    else:
        verified_by = verify_active_thread_by_header(get_thread_ui_name(thread.id, thread) or "")
        if not verified_by:
            activation_method = "best-effort-current-ui (unverified)"
            activation_warning = (
                "The selected thread could not be verified as the currently open Codex thread. "
                "Proceeding anyway and checking where the prompt is actually recorded."
            )
        else:
            activation_method = f"already-open [{verified_by}]"
    print(f"ui_activation: {activation_method}")
    if activation_warning:
        print(f"ui_warning: {activation_warning}")
    window = send_prompt_to_codex(
        prompt=prompt,
        click_x_ratio=args.click_x_ratio,
        click_y_offset=args.click_y_offset,
        skip_click=not args.click,
    )
    print(
        f"sent_to_window: hwnd={window.hwnd} title={window.title} "
        f"rect=({window.left},{window.top})-({window.right},{window.bottom})"
    )

    delivery_timeout_sec = 10.0 if pending_new else 4.0
    delivered_thread = wait_for_prompt_delivery(
        recent_offsets,
        prompt,
        timeout_sec=delivery_timeout_sec,
        allow_new_cwd=str(pending_new.get("cwd", "")) if pending_new else None,
        require_new_thread=bool(pending_new),
    )
    if delivered_thread is None:
        if pending_new:
            raise RuntimeError(
                "Prompt delivery could not be confirmed in the pending new thread. "
                "The blank thread UI likely moved, or the first prompt was not recorded."
            )
        raise RuntimeError(
            "Prompt delivery could not be confirmed in any recent Codex thread. "
            "The UI likely moved, but the message was not recorded."
        )
    if pending_new:
        thread = delivered_thread
        session_path = Path(thread.rollout_path)
        start_offset = 0
        clear_pending_new_thread_request()
        set_selected_thread_id(thread.id)
    elif delivered_thread.id != thread.id:
        raise RuntimeError(
            "Prompt landed in a different thread. "
            f"Expected {get_thread_label(thread)}, but it was recorded in {get_thread_label(delivered_thread)}."
        )
    print(f"[delivery_verified] {get_thread_label(thread)}")

    if args.background:
        started = start_background_watch(
            thread=thread,
            start_offset=start_offset,
            timeout_sec=args.timeout,
            include_commentary=args.include_commentary,
            stream_output=args.stream,
        )
        if started:
            print(f"[background_watch_started] {get_thread_label(thread)}")
        else:
            print(f"[background_watch_already_running] {get_thread_label(thread)}")
        return 0

    if not args.wait:
        return 0

    print("[waiting_for_final_answer]")
    print("Use Ctrl+C to stop waiting after the prompt is sent.")

    try:
        result = watch_for_final_answer(
            session_path=session_path,
            start_offset=start_offset,
            timeout_sec=args.timeout,
            include_commentary=args.include_commentary,
            stream_live=args.stream,
        )
    except KeyboardInterrupt:
        print("[wait_cancelled]")
        print("Prompt was already sent. Waiting stopped by user.")
        print("Use `status` or `tail --only-new` to monitor the same thread.")
        return 0

    if args.include_commentary and result["commentary"] and not result.get("streamed_live"):
        for item in result["commentary"]:
            print("[commentary]")
            print(item)
            print("")

    if result["final_answer"]:
        if result.get("streamed_live"):
            print("[ready]")
        else:
            print("[final_answer]")
            print(result["final_answer"])
            print("")
            print("[ready]")
        return 0

    if result["status"] == "aborted":
        print("[aborted]")
        return 0

    print("[timeout]")
    if result["commentary"]:
        print(result["commentary"][-1])
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge to the current Codex Desktop thread without using Codex CLI.",
    )
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--thread-id", default=None, help="Target a specific thread id.")
    common_parser.add_argument("--cwd", default=None, help="Prefer the newest thread for this workspace path.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list",
        help="List recent local Codex Desktop threads.",
        parents=[common_parser],
    )
    list_parser.add_argument("--limit", type=int, default=10)
    list_parser.set_defaults(func=command_list)

    status_parser = subparsers.add_parser(
        "status",
        help="Show the selected thread and last messages.",
        parents=[common_parser],
    )
    status_parser.set_defaults(func=command_status)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose Codex Desktop bridge environment and detection state.",
        parents=[common_parser],
    )
    doctor_parser.add_argument("--limit", type=int, default=5, help="Max busy threads to print.")
    doctor_parser.set_defaults(func=command_doctor)

    focus_parser = subparsers.add_parser(
        "focus",
        help="Focus the Codex Desktop window.",
        parents=[common_parser],
    )
    focus_parser.add_argument("--click-x-ratio", type=float, default=0.5)
    focus_parser.add_argument("--click-y-offset", type=int, default=90)
    focus_parser.add_argument("--click", action="store_true", help="Also click inside the window after focusing.")
    focus_parser.set_defaults(func=command_focus)

    tail_parser = subparsers.add_parser(
        "tail",
        help="Tail session events for the selected thread.",
        parents=[common_parser],
    )
    tail_parser.add_argument("--timeout", type=float, default=0.0, help="0 means run forever.")
    tail_parser.add_argument("--only-new", action="store_true")
    tail_parser.set_defaults(func=command_tail)

    new_parser = subparsers.add_parser(
        "new",
        help="Create a new thread inside an existing workspace.",
        parents=[common_parser],
    )
    new_parser.add_argument(
        "thread_ref",
        nargs="?",
        help="Optional workspace or thread reference. Bare workspace names use the newest thread in that workspace.",
    )
    new_parser.add_argument(
        "--abort",
        action="store_true",
        help="Abort the currently running Codex reply before creating a new thread.",
    )
    new_parser.set_defaults(func=command_new)

    open_parser = subparsers.add_parser(
        "open",
        help="Select and open a thread in Codex Desktop without sending a prompt.",
        parents=[common_parser],
    )
    open_parser.add_argument(
        "thread_ref",
        nargs="?",
        help="Optional workspace name, list index, `other`, or exact thread id.",
    )
    open_parser.add_argument(
        "--abort",
        action="store_true",
        help="Abort the currently running Codex reply before switching threads.",
    )
    open_parser.set_defaults(func=command_open)

    use_parser = subparsers.add_parser(
        "use",
        help="Select a default thread without opening it. Advanced.",
        parents=[common_parser],
    )
    use_parser.add_argument(
        "thread_ref",
        nargs="?",
        help="Workspace name, list index, `other`, or exact thread id.",
    )
    use_parser.add_argument("--clear", action="store_true", help="Clear the persisted selection.")
    use_parser.set_defaults(func=command_use)

    ask_parser = subparsers.add_parser(
        "ask",
        help="Send a prompt to the currently open Codex thread and stream the reply.",
        parents=[common_parser],
    )
    ask_parser.add_argument("prompt", help="Prompt text to send.")
    ask_parser.add_argument("--timeout", type=float, default=600.0)
    ask_parser.add_argument("--click-x-ratio", type=float, default=0.5)
    ask_parser.add_argument("--click-y-offset", type=int, default=90)
    ask_parser.add_argument("--click", action="store_true", help="Click inside the window before pasting.")
    ask_parser.add_argument("--dry-run", action="store_true")
    ask_parser.add_argument("--no-wait", dest="wait", action="store_false")
    ask_parser.add_argument("--background", dest="background", action="store_true", help="Return immediately and stream the reply in the background.")
    ask_parser.add_argument("--foreground", dest="background", action="store_false", help="Keep the current terminal occupied until the reply finishes.")
    ask_parser.add_argument("--include-commentary", dest="include_commentary", action="store_true")
    ask_parser.add_argument("--no-commentary", dest="include_commentary", action="store_false")
    ask_parser.add_argument("--stream", dest="stream", action="store_true", help="Stream commentary while a reply is in progress.")
    ask_parser.add_argument("--no-stream", dest="stream", action="store_false", help="Do not stream reply text; only print ready.")
    ask_parser.add_argument("--force-while-busy", action="store_true")
    ask_parser.add_argument(
        "--switch-thread",
        dest="switch_thread",
        action="store_true",
        help="Switch the Codex UI to the target thread before sending.",
    )
    ask_parser.add_argument(
        "--no-switch-thread",
        dest="switch_thread",
        action="store_false",
        help="Do not switch threads. Send to the currently open Codex thread only. Default behavior.",
    )
    ask_parser.set_defaults(
        func=command_ask,
        wait=True,
        background=False,
        switch_thread=False,
        stream=False,
        include_commentary=False,
    )

    return parser


def split_repl_command(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)

    cleaned = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            cleaned.append(token[1:-1])
        else:
            cleaned.append(token)
    return cleaned


def run_repl() -> int:
    known_commands = {"list", "use", "status", "doctor", "focus", "tail", "new", "open", "ask", "help", "exit", "quit"}
    print("Codex bridge shell")
    print("Commands: list, new, open, ask, status, doctor, tail, focus, use, help, exit")
    print("Primary flow: list -> open ai -> ask \"...\"")
    print("Example: open ai")
    print("Example: new ai")
    print("Example: open --abort ai")
    print("Example: open other")
    print("Example: doctor")
    print('Example: ask "이 파일 수정해줘"')
    print("`open` selects + opens a thread. `use` only selects without opening.")
    print('Tip: plain text is treated as `ask --no-switch-thread --stream --include-commentary "..."`')
    print("Busy safety: `open` is blocked while another reply is running unless you pass `--abort`.")
    print("Default ask is foreground: it streams the reply and keeps the prompt occupied until done.")
    print("")

    while True:
        try:
            selected = get_selected_thread_id()
            suffix = f"[{selected[:8]}]" if selected else ""
            line = input(f"codex-bridge{suffix}> ").strip()
        except EOFError:
            print("")
            return 0
        except KeyboardInterrupt:
            print("")
            return 130

        if not line:
            continue

        lowered = line.lower()
        if lowered in {"exit", "quit"}:
            return 0

        if lowered == "help":
            build_parser().print_help()
            print("")
            continue

        argv = split_repl_command(line)
        if not argv:
            continue

        if argv[0].lower() not in known_commands and not argv[0].startswith("-"):
            argv = ["ask", "--no-switch-thread", "--stream", "--include-commentary", line]
        elif argv[0].lower() == "ask":
            has_wait_mode = any(
                token in {"--background", "--foreground", "--no-wait"} for token in argv[1:]
            )
            has_stream_mode = any(
                token in {"--stream", "--no-stream"} for token in argv[1:]
            )
            has_commentary_mode = any(
                token in {"--include-commentary", "--no-commentary"} for token in argv[1:]
            )
            if not has_wait_mode:
                argv.insert(1, "--foreground")
            if not has_stream_mode:
                argv.insert(1, "--stream")
            if not has_commentary_mode:
                argv.insert(1, "--include-commentary")

        parser = build_parser()

        try:
            args = parser.parse_args(argv)
            exit_code = int(args.func(args))
            if exit_code not in (0, None):
                print(f"(exit {exit_code})")
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if code != 0:
                print("(invalid command)")
        except KeyboardInterrupt:
            print("Interrupted.")
        except Exception as exc:
            print(f"ERROR: {exc}")
        print("")


def main() -> int:
    if len(sys.argv) == 1:
        return run_repl()

    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
