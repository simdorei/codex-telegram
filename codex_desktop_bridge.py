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
import queue
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
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


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = get_env_path("CODEX_HOME", Path.home() / ".codex")
GLOBAL_STATE_PATH = get_env_path("CODEX_GLOBAL_STATE", CODEX_HOME / ".codex-global-state.json")
STATE_DB_PATH = resolve_state_db_path(CODEX_HOME)
SESSION_INDEX_PATH = get_env_path("CODEX_SESSION_INDEX", CODEX_HOME / "session_index.jsonl")
BRIDGE_STATE_PATH = get_env_path("CODEX_BRIDGE_STATE", CODEX_HOME / "codex_desktop_bridge_state.json")
LOG_DB_PATH = get_env_path("CODEX_LOG_DB", CODEX_HOME / "logs_2.sqlite")
ARCHIVED_SESSIONS_DIR = get_env_path("CODEX_ARCHIVED_SESSIONS_DIR", CODEX_HOME / "archived_sessions")
MAINTENANCE_BACKUP_ROOT = get_env_path("CODEX_MAINTENANCE_BACKUP_ROOT", CODEX_HOME / "maintenance_backups")
CODEX_IPC_PIPE = r"\\.\pipe\codex-ipc"
CODEX_APP_SERVER_EXE = os.environ.get("CODEX_EXE", "").strip()
SINGLE_BACKUP_LOG_LIMIT_BYTES = 500 * 1024
IPC_PROBE_LOG_PATH = SCRIPT_DIR / "_ipc_probe_log.jsonl"
HIGH_CONTEXT_INPUT_RATIO_THRESHOLD = 0.60
CRITICAL_CONTEXT_INPUT_RATIO_THRESHOLD = 0.80
ARCHIVE_RECOMMEND_TOKENS_USED_THRESHOLD = 50_000_000
ARCHIVE_RECOMMEND_CONTEXT_TOKENS_THRESHOLD = 200_000
BACKGROUND_WATCHERS: dict[str, threading.Thread] = {}
BACKGROUND_WATCHERS_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
ULONG_PTR = wt.WPARAM
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_PEEK_RETRY_SEC = 0.05
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_BACK = 0x08
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_A = 0x41
VK_B = 0x42
VK_C = 0x43
VK_J = 0x4A
VK_L = 0x4C
VK_V = 0x56

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def rotate_single_backup_file(
    path: Path,
    *,
    max_bytes: int = SINGLE_BACKUP_LOG_LIMIT_BYTES,
    incoming_bytes: int = 0,
) -> None:
    try:
        if max_bytes <= 0:
            return
        current_path = Path(path)
        if not current_path.exists():
            return
        projected_size = current_path.stat().st_size + max(0, incoming_bytes)
        if projected_size <= max_bytes:
            return
        backup_path = current_path.with_name(current_path.name + ".bak")
        if backup_path.exists():
            backup_path.unlink()
        current_path.replace(backup_path)
        current_path.touch()
    except OSError:
        pass


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
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
kernel32.ReadFile.restype = wt.BOOL
kernel32.WriteFile.argtypes = [wt.HANDLE, wt.LPCVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
kernel32.WriteFile.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.PeekNamedPipe.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]
kernel32.PeekNamedPipe.restype = wt.BOOL


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
    archived_at: int = 0


@dataclass
class ThreadContextUsage:
    model_context_window: int
    last_input_tokens: int
    last_total_tokens: int
    peak_input_tokens: int
    peak_total_tokens: int
    usage_ratio: float


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


def collapse_list_text(value: str, limit: int = 70) -> str:
    collapsed = " ".join((value or "").replace("\r", " ").replace("\n", " ").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)].rstrip() + "..."


def make_console_safe_text(value: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding)


def format_title_preview(value: str, limit: int = 120) -> str:
    return make_console_safe_text(collapse_list_text(value, limit=limit))


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


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def connect_writable(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


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


def format_session_index_timestamp(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}0Z"


def write_session_index_entries(entries: list[dict]) -> None:
    SESSION_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(json.dumps(item, ensure_ascii=False) for item in entries)
    if rendered:
        rendered += "\n"
    SESSION_INDEX_PATH.write_text(rendered, encoding="utf-8")


def sync_session_index_with_state() -> int:
    threads = load_recent_threads(limit=0)
    entries = [
        {
            "id": thread.id,
            "thread_name": thread.title or get_thread_ui_name(thread.id, thread) or thread.id,
            "updated_at": format_session_index_timestamp(float(thread.updated_at or time.time())),
        }
        for thread in threads
    ]
    write_session_index_entries(entries)
    return len(entries)


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


def load_archived_threads(limit: int = 20) -> list[ThreadInfo]:
    if not STATE_DB_PATH.exists():
        raise FileNotFoundError(
            f"Codex state database not found: {STATE_DB_PATH}. "
            "Set CODEX_HOME or CODEX_STATE_DB if your Codex data lives elsewhere."
        )

    query = """
        SELECT id, title, cwd, updated_at, rollout_path, model, reasoning_effort, tokens_used, archived_at
        FROM threads
        WHERE archived = 1
        ORDER BY archived_at DESC, updated_at DESC
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
                archived_at=row[8] or 0,
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


def resolve_archived_thread_ref(thread_ref: str, limit: int = 100) -> ThreadInfo:
    threads = load_archived_threads(limit=limit)
    if not threads:
        raise RuntimeError("No archived Codex threads found in the local state DB.")

    normalized = thread_ref.strip().lower()

    if thread_ref.isdigit():
        index = int(thread_ref)
        if 1 <= index <= len(threads):
            return threads[index - 1]
        raise RuntimeError(f"Archived thread index out of range: {thread_ref}")

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
            f"Multiple archived threads match workspace `{thread_ref}`. Use one of: {refs}"
        )
    for thread in threads:
        if get_thread_workspace_name(thread).lower() == normalized:
            return thread

    return get_thread_by_id(thread_ref, threads=load_archived_threads(limit=0))


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


def format_token_k(value: int) -> str:
    if value <= 0:
        return "-"
    if value < 1000:
        return str(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{value / 1000:.1f}k"


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


def coerce_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return 0


def get_thread_context_usage(thread: ThreadInfo) -> ThreadContextUsage | None:
    session_path = Path(thread.rollout_path)
    if not session_path.exists():
        return None

    model_context_window = 0
    last_input_tokens = 0
    last_total_tokens = 0
    peak_input_tokens = 0
    peak_total_tokens = 0
    saw_token_count = False

    for event in iter_session_events(session_path):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event.get("type") != "event_msg":
            continue

        event_type = payload.get("type")
        if event_type == "task_started":
            model_context_window = coerce_nonnegative_int(payload.get("model_context_window")) or model_context_window
            continue
        if event_type != "token_count":
            continue

        info = payload.get("info") or {}
        if not isinstance(info, dict):
            continue

        model_context_window = coerce_nonnegative_int(info.get("model_context_window")) or model_context_window

        last_usage = info.get("last_token_usage") or {}
        if isinstance(last_usage, dict):
            saw_token_count = True
            last_input_tokens = coerce_nonnegative_int(last_usage.get("input_tokens"))
            last_total_tokens = coerce_nonnegative_int(last_usage.get("total_tokens"))
            peak_input_tokens = max(peak_input_tokens, last_input_tokens)
            peak_total_tokens = max(peak_total_tokens, last_total_tokens)

    if not saw_token_count or model_context_window <= 0:
        return None

    usage_ratio = min(1.0, last_input_tokens / model_context_window) if last_input_tokens else 0.0
    return ThreadContextUsage(
        model_context_window=model_context_window,
        last_input_tokens=last_input_tokens,
        last_total_tokens=last_total_tokens,
        peak_input_tokens=peak_input_tokens,
        peak_total_tokens=peak_total_tokens,
        usage_ratio=usage_ratio,
    )


def describe_thread_context_usage(context_usage: ThreadContextUsage) -> str:
    if context_usage.usage_ratio >= CRITICAL_CONTEXT_INPUT_RATIO_THRESHOLD:
        return "critical"
    if context_usage.usage_ratio >= HIGH_CONTEXT_INPUT_RATIO_THRESHOLD:
        return "high"
    return "normal"


def get_high_context_threads(limit: int = 20) -> list[tuple[ThreadInfo, ThreadContextUsage]]:
    flagged: list[tuple[ThreadInfo, ThreadContextUsage]] = []
    for thread in load_recent_threads(limit=limit):
        context_usage = get_thread_context_usage(thread)
        if context_usage is None:
            continue
        if context_usage.usage_ratio >= HIGH_CONTEXT_INPUT_RATIO_THRESHOLD:
            flagged.append((thread, context_usage))

    flagged.sort(key=lambda item: (item[1].usage_ratio, item[0].updated_at), reverse=True)
    return flagged


def should_recommend_archive(thread: ThreadInfo, context_usage: ThreadContextUsage | None) -> bool:
    if thread.tokens_used >= ARCHIVE_RECOMMEND_TOKENS_USED_THRESHOLD:
        return True
    if context_usage is None:
        return False
    return (
        context_usage.last_input_tokens >= ARCHIVE_RECOMMEND_CONTEXT_TOKENS_THRESHOLD
        or context_usage.peak_input_tokens >= ARCHIVE_RECOMMEND_CONTEXT_TOKENS_THRESHOLD
    )


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


def _get_last_win_error_message() -> str:
    code = int(kernel32.GetLastError())
    if not code:
        return "unknown Windows error"
    return f"{ctypes.WinError(code)}"


def _open_codex_ipc_pipe() -> int:
    handle = kernel32.CreateFileW(
        CODEX_IPC_PIPE,
        GENERIC_READ | GENERIC_WRITE,
        0,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise RuntimeError(f"Could not open {CODEX_IPC_PIPE}: {_get_last_win_error_message()}")
    return int(handle)


def _peek_pipe_bytes_available(handle: int) -> int:
    total_available = wt.DWORD(0)
    ok = kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(total_available), None)
    if not ok:
        raise RuntimeError(f"Could not peek {CODEX_IPC_PIPE}: {_get_last_win_error_message()}")
    return int(total_available.value)


def _wait_for_pipe_bytes(handle: int, min_bytes: int, timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _peek_pipe_bytes_available(handle) >= min_bytes:
            return
        time.sleep(PIPE_PEEK_RETRY_SEC)
    raise TimeoutError(f"Timed out waiting for IPC data from {CODEX_IPC_PIPE}.")


def _read_pipe_exact(handle: int, size: int, timeout_sec: float) -> bytes:
    _wait_for_pipe_bytes(handle, size, timeout_sec)
    buffer = ctypes.create_string_buffer(size)
    bytes_read = wt.DWORD(0)
    ok = kernel32.ReadFile(handle, buffer, size, ctypes.byref(bytes_read), None)
    if not ok:
        raise RuntimeError(f"Could not read from {CODEX_IPC_PIPE}: {_get_last_win_error_message()}")
    if int(bytes_read.value) != size:
        raise RuntimeError(f"Short IPC read from {CODEX_IPC_PIPE}: expected {size}, got {bytes_read.value}.")
    return buffer.raw[:size]


def _read_ipc_message(handle: int, timeout_sec: float) -> dict:
    header = _read_pipe_exact(handle, 4, timeout_sec)
    payload_size = int.from_bytes(header, "little")
    if payload_size < 0:
        raise RuntimeError("IPC payload length was negative.")
    payload = _read_pipe_exact(handle, payload_size, timeout_sec)
    return json.loads(payload.decode("utf-8", errors="ignore"))


def _write_ipc_message(handle: int, payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    frame = len(data).to_bytes(4, "little") + data
    buffer = ctypes.create_string_buffer(frame)
    bytes_written = wt.DWORD(0)
    ok = kernel32.WriteFile(handle, buffer, len(frame), ctypes.byref(bytes_written), None)
    if not ok:
        raise RuntimeError(f"Could not write to {CODEX_IPC_PIPE}: {_get_last_win_error_message()}")
    if int(bytes_written.value) != len(frame):
        raise RuntimeError(
            f"Short IPC write to {CODEX_IPC_PIPE}: expected {len(frame)}, got {bytes_written.value}."
        )


def _record_owner_client_from_ipc_message(message: dict, owner_clients: dict[str, str]) -> None:
    if message.get("type") != "broadcast" or message.get("method") != "thread-stream-state-changed":
        return
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return
    conversation_id = str(params.get("conversationId") or "").strip()
    source_client_id = str(message.get("sourceClientId") or "").strip()
    if conversation_id and source_client_id:
        owner_clients[conversation_id] = source_client_id


def _read_ipc_response(
    handle: int,
    request_id: str,
    timeout_sec: float,
    owner_clients: dict[str, str],
) -> dict:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        message = _read_ipc_message(handle, max(PIPE_PEEK_RETRY_SEC, deadline - time.time()))
        _record_owner_client_from_ipc_message(message, owner_clients)
        if message.get("type") == "response" and message.get("requestId") == request_id:
            return message
    raise TimeoutError(f"Timed out waiting for IPC response to request {request_id}.")


def _initialize_ipc_client(handle: int, owner_clients: dict[str, str], timeout_sec: float = 3.0) -> str:
    request_id = str(uuid.uuid4())
    _write_ipc_message(
        handle,
        {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": "initializing-client",
            "version": 0,
            "method": "initialize",
            "params": {"clientType": "codex-desktop-bridge"},
        },
    )
    response = _read_ipc_response(handle, request_id, timeout_sec=timeout_sec, owner_clients=owner_clients)
    if response.get("resultType") != "success":
        raise RuntimeError(f"IPC initialize failed: {response.get('error') or 'unknown error'}")
    result = response.get("result") or {}
    if not isinstance(result, dict):
        raise RuntimeError("IPC initialize returned an invalid payload.")
    client_id = str(result.get("clientId") or "").strip()
    if not client_id:
        raise RuntimeError("IPC initialize did not return a clientId.")
    return client_id


def _discover_owner_client_for_thread(handle: int, thread_id: str, timeout_sec: float) -> str | None:
    owner_clients: dict[str, str] = {}
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if thread_id in owner_clients:
            return owner_clients[thread_id]
        try:
            message = _read_ipc_message(handle, max(PIPE_PEEK_RETRY_SEC, deadline - time.time()))
        except TimeoutError:
            return owner_clients.get(thread_id)
        _record_owner_client_from_ipc_message(message, owner_clients)
        if thread_id in owner_clients:
            return owner_clients[thread_id]
    return None


class IPCNoClientFoundError(RuntimeError):
    pass


class CodexSidecarError(RuntimeError):
    pass


def _request_start_turn_via_ipc(
    handle: int,
    source_client_id: str,
    thread: ThreadInfo,
    prompt: str,
    timeout_sec: float,
    owner_clients: dict[str, str],
) -> dict[str, str]:
    owner_client_id = owner_clients.get(thread.id)

    request_id = str(uuid.uuid4())
    request = {
        "type": "request",
        "requestId": request_id,
        "sourceClientId": source_client_id,
        "version": 1,
        "method": "thread-follower-start-turn",
        "params": {
            "conversationId": thread.id,
            "turnStartParams": {
                "inheritThreadSettings": True,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "cwd": None,
                "approvalPolicy": None,
                "sandboxPolicy": None,
                "approvalsReviewer": "user",
                "model": None,
                "serviceTier": "default",
                "effort": None,
                "summary": "none",
                "personality": None,
                "outputSchema": None,
                "collaborationMode": None,
                "attachments": [],
            },
        },
    }
    if owner_client_id:
        request["targetClientId"] = owner_client_id
    _write_ipc_message(handle, request)
    response = _read_ipc_response(handle, request_id, timeout_sec=timeout_sec, owner_clients=owner_clients)
    if response.get("resultType") != "success":
        error = str(response.get("error") or "unknown error")
        if "no-client-found" in error:
            raise IPCNoClientFoundError(error)
        raise RuntimeError(f"IPC start-turn failed: {error}")
    payload = response.get("result") or {}
    if not isinstance(payload, dict):
        raise RuntimeError("IPC start-turn returned an invalid payload.")
    nested_result = payload.get("result") or {}
    turn = nested_result.get("turn") if isinstance(nested_result, dict) else {}
    turn_id = str((turn or {}).get("id") or "").strip()
    handled_by_client_id = str(
        response.get("handledByClientId")
        or owner_clients.get(thread.id)
        or owner_client_id
        or ""
    ).strip()
    return {
        "owner_client_id": handled_by_client_id,
        "turn_id": turn_id,
    }


def start_turn_via_ipc(
    thread: ThreadInfo,
    prompt: str,
    timeout_sec: float = 4.0,
    *,
    allow_ui_recovery: bool = False,
) -> dict[str, str]:
    handle = _open_codex_ipc_pipe()
    owner_clients: dict[str, str] = {}
    recovery_method = ""
    last_activation_error = ""
    max_attempts = 3 if allow_ui_recovery else 2
    retry_sleep_base = 0.75 if allow_ui_recovery else 0.35
    discover_timeout_sec = (
        max(2.0, min(timeout_sec, 6.0))
        if allow_ui_recovery
        else max(0.75, min(timeout_sec, 1.5))
    )
    try:
        source_client_id = _initialize_ipc_client(handle, owner_clients, timeout_sec=min(timeout_sec, 3.0))

        for attempt in range(max_attempts):
            try:
                result = _request_start_turn_via_ipc(
                    handle,
                    source_client_id,
                    thread,
                    prompt,
                    timeout_sec,
                    owner_clients,
                )
                if recovery_method:
                    result["recovery_method"] = recovery_method
                return result
            except IPCNoClientFoundError:
                if attempt >= (max_attempts - 1):
                    if allow_ui_recovery:
                        detail = (
                            "IPC owner client for the selected thread was not discovered even after "
                            "re-activating the thread in the Codex UI. The app is likely still loading "
                            "or lagging. Wait a few seconds, open the thread once, and retry."
                        )
                        if last_activation_error:
                            detail += f" Last activation error: {last_activation_error}"
                    else:
                        detail = (
                            "IPC owner client for the selected thread was not discovered in background mode. "
                            "The target thread may still be loading. Open that thread once, wait a few seconds, "
                            "or rerun with --ipc-recover-ui if you want an automatic UI recovery attempt."
                        )
                    raise RuntimeError(detail)

                if allow_ui_recovery:
                    last_activation_error = ""
                    try:
                        recovery_method = activate_thread_in_ui(thread)
                    except Exception as exc:
                        last_activation_error = str(exc)

                time.sleep(retry_sleep_base * (attempt + 1))
                discovered_owner = _discover_owner_client_for_thread(
                    handle,
                    thread.id,
                    timeout_sec=discover_timeout_sec,
                )
                if discovered_owner:
                    owner_clients[thread.id] = discovered_owner
    finally:
        kernel32.CloseHandle(handle)


def resolve_codex_app_server_executable() -> str:
    if CODEX_APP_SERVER_EXE:
        return CODEX_APP_SERVER_EXE

    bundled_name = "codex.exe" if os.name == "nt" else "codex"
    bundled_path = CODEX_HOME / ".sandbox-bin" / bundled_name
    if bundled_path.exists():
        return str(bundled_path)

    for candidate in ("codex", "codex.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    return bundled_name


class CodexAppServerSidecar:
    def __init__(self) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [resolve_codex_app_server_executable(), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
        )
        if self.process.stdin is None or self.process.stdout is None:
            self.close()
            raise RuntimeError("Failed to start the Codex app-server sidecar.")

        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stdout_thread.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-desktop-bridge",
                    "version": "1.0",
                }
            },
            timeout_sec=5.0,
        )

    def __enter__(self) -> "CodexAppServerSidecar":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _drain_stdout(self) -> None:
        try:
            assert self.process.stdout is not None
            for raw_line in self.process.stdout:
                self._stdout_queue.put(raw_line.rstrip("\r\n"))
        finally:
            self._stdout_queue.put(None)

    def close(self) -> None:
        stdin = self.process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass

        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

    def request(self, method: str, params: dict, *, timeout_sec: float = 10.0) -> dict:
        if self.process.poll() is not None:
            raise RuntimeError(f"Codex app-server sidecar exited with code {self.process.returncode}.")

        assert self.process.stdin is not None
        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

        deadline = time.time() + max(timeout_sec, 0.0)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for app-server response to {method}.")
            try:
                raw_line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for app-server response to {method}.") from exc

            if raw_line is None:
                raise RuntimeError(f"Codex app-server sidecar exited while waiting for {method}.")

            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                if isinstance(error, dict):
                    detail = str(error.get("message") or error)
                else:
                    detail = str(error)
                raise CodexSidecarError(f"{method} failed: {detail}")
            result = message.get("result") or {}
            if not isinstance(result, dict):
                raise RuntimeError(f"{method} returned an invalid payload.")
            return result

    def start_thread(self, cwd: str | None) -> dict:
        params: dict[str, object] = {}
        if cwd:
            params["cwd"] = cwd
        return self.request("thread/start", params, timeout_sec=10.0)

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict:
        return self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
            timeout_sec=8.0,
        )

    def resume_thread(self, thread_id: str) -> dict:
        return self.request("thread/resume", {"threadId": thread_id}, timeout_sec=10.0)

    def start_turn(self, thread_id: str, prompt: str) -> dict:
        return self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
            timeout_sec=12.0,
        )

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict:
        return self.request(
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": turn_id,
            },
            timeout_sec=10.0,
        )

    def clean_background_terminals(self, thread_id: str) -> dict:
        return self.request(
            "thread/backgroundTerminals/clean",
            {"threadId": thread_id},
            timeout_sec=10.0,
        )

    def archive_thread(self, thread_id: str) -> dict:
        return self.request("thread/archive", {"threadId": thread_id}, timeout_sec=10.0)


def load_thread_record_by_id(thread_id: str) -> tuple[ThreadInfo, bool] | None:
    if not STATE_DB_PATH.exists():
        return None
    query = """
        SELECT id, title, cwd, updated_at, rollout_path, model, reasoning_effort, tokens_used, archived
        FROM threads
        WHERE id = ?
        LIMIT 1
    """
    with connect_readonly(STATE_DB_PATH) as conn:
        row = conn.execute(query, (thread_id,)).fetchone()
    if row is None:
        return None

    thread = ThreadInfo(
        id=row[0],
        title=row[1] or "",
        cwd=row[2] or "",
        updated_at=row[3] or 0,
        rollout_path=row[4] or "",
        model=row[5] or "",
        reasoning_effort=row[6] or "",
        tokens_used=row[7] or 0,
    )
    return thread, bool(row[8])


def wait_for_thread_record(
    thread_id: str,
    *,
    archived: bool | None = None,
    timeout_sec: float = 8.0,
) -> tuple[ThreadInfo, bool] | None:
    deadline = time.time() + max(timeout_sec, 0.0)
    while time.time() < deadline:
        record = load_thread_record_by_id(thread_id)
        if record is not None:
            thread, is_archived = record
            if archived is None or is_archived == archived:
                return thread, is_archived
        time.sleep(0.2)
    return None


def is_path_within_directory(path: Path, directory: Path) -> bool:
    candidate = Path(strip_windows_extended_prefix(str(path))).expanduser().resolve(strict=False)
    root = Path(strip_windows_extended_prefix(str(directory))).expanduser().resolve(strict=False)
    try:
        return os.path.commonpath([str(candidate), str(root)]) == str(root)
    except ValueError:
        return False


def sqlite_backup_to_path(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with connect_writable(source) as src, connect_writable(destination) as dst:
        src.backup(dst)
    return True


def copy_file_to_backup(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def create_archive_delete_backup_dir(thread_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    backup_dir = MAINTENANCE_BACKUP_ROOT / f"delete-archive-{stamp}-{thread_id[:8]}"
    if backup_dir.exists():
        backup_dir = backup_dir.with_name(f"{backup_dir.name}-{uuid.uuid4().hex[:6]}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    return backup_dir


def backup_archive_delete_inputs(backup_dir: Path) -> list[Path]:
    copied: list[Path] = []
    if sqlite_backup_to_path(STATE_DB_PATH, backup_dir / STATE_DB_PATH.name):
        copied.append(backup_dir / STATE_DB_PATH.name)
    if sqlite_backup_to_path(LOG_DB_PATH, backup_dir / LOG_DB_PATH.name):
        copied.append(backup_dir / LOG_DB_PATH.name)
    for source in (GLOBAL_STATE_PATH, BRIDGE_STATE_PATH, SESSION_INDEX_PATH):
        destination = backup_dir / source.name
        if copy_file_to_backup(source, destination):
            copied.append(destination)
    return copied


def scrub_bridge_state_deleted_thread(thread_id: str) -> list[str]:
    data = load_bridge_state()
    changed: list[str] = []
    if data.get("selected_thread_id") == thread_id:
        data.pop("selected_thread_id", None)
        changed.append("selected_thread_id")
    recent_ui_thread = data.get("recent_ui_thread")
    if isinstance(recent_ui_thread, dict) and str(recent_ui_thread.get("thread_id") or "") == thread_id:
        data.pop("recent_ui_thread", None)
        changed.append("recent_ui_thread")
    if changed:
        save_bridge_state(data)
    return changed


def scrub_global_state_deleted_thread(thread_id: str) -> list[str]:
    if not GLOBAL_STATE_PATH.exists():
        return []
    data = load_json(GLOBAL_STATE_PATH)
    changed: list[str] = []
    queued_follow_ups = data.get("queued-follow-ups")
    if isinstance(queued_follow_ups, dict) and thread_id in queued_follow_ups:
        queued_follow_ups.pop(thread_id, None)
        changed.append("queued-follow-ups")
    pinned_thread_ids = data.get("pinned-thread-ids")
    if isinstance(pinned_thread_ids, list):
        filtered = [item for item in pinned_thread_ids if str(item) != thread_id]
        if len(filtered) != len(pinned_thread_ids):
            data["pinned-thread-ids"] = filtered
            changed.append("pinned-thread-ids")
    if changed:
        save_json(GLOBAL_STATE_PATH, data)
    return changed


def scrub_session_index_deleted_thread(thread_id: str) -> int:
    if not SESSION_INDEX_PATH.exists():
        return 0
    original = SESSION_INDEX_PATH.read_text(encoding="utf-8")
    kept_lines: list[str] = []
    removed = 0
    for raw_line in original.splitlines():
        line = raw_line.strip()
        if not line:
            kept_lines.append(raw_line)
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            kept_lines.append(raw_line)
            continue
        if str(payload.get("id") or "") == thread_id:
            removed += 1
            continue
        kept_lines.append(raw_line)
    if removed:
        rewritten = "\n".join(kept_lines)
        if original.endswith(("\n", "\r")) and rewritten:
            rewritten += "\n"
        SESSION_INDEX_PATH.write_text(rewritten, encoding="utf-8")
    return removed


def delete_archived_thread_locally(thread: ThreadInfo) -> dict[str, object]:
    rollout_path = Path(strip_windows_extended_prefix(thread.rollout_path)).expanduser()
    if not is_path_within_directory(rollout_path, ARCHIVED_SESSIONS_DIR):
        raise RuntimeError(
            "Refusing to delete an archived thread whose rollout path is outside the archived_sessions directory."
        )

    record = load_thread_record_by_id(thread.id)
    if record is None:
        raise RuntimeError("The archived thread no longer exists in the local state DB.")
    _thread_record, is_archived = record
    if not is_archived:
        raise RuntimeError("Refusing to delete an active thread. Only archived threads can be deleted.")

    backup_dir = create_archive_delete_backup_dir(thread.id)
    backup_paths = backup_archive_delete_inputs(backup_dir)

    with connect_writable(STATE_DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            DELETE FROM thread_spawn_edges
            WHERE child_thread_id = ?
               OR parent_thread_id = ?
            """,
            (thread.id, thread.id),
        )
        deleted_rows = conn.execute("DELETE FROM threads WHERE id = ?", (thread.id,)).rowcount
        if deleted_rows != 1:
            conn.rollback()
            raise RuntimeError("Archived thread deletion aborted because the target row changed during deletion.")
        conn.commit()

    deleted_log_rows = 0
    if LOG_DB_PATH.exists():
        with connect_writable(LOG_DB_PATH) as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted_log_rows = conn.execute("DELETE FROM logs WHERE thread_id = ?", (thread.id,)).rowcount
            conn.commit()

    bridge_state_scrubbed = scrub_bridge_state_deleted_thread(thread.id)
    global_state_scrubbed = scrub_global_state_deleted_thread(thread.id)
    session_index_removed = scrub_session_index_deleted_thread(thread.id)

    deleted_rollout_path = ""
    if rollout_path.exists():
        rollout_path.unlink()
        deleted_rollout_path = str(rollout_path)

    if load_thread_record_by_id(thread.id) is not None:
        raise RuntimeError("Archived thread row is still present after deletion.")

    remaining_log_rows = 0
    if LOG_DB_PATH.exists():
        with connect_readonly(LOG_DB_PATH) as conn:
            remaining_log_rows = int(conn.execute("SELECT COUNT(*) FROM logs WHERE thread_id = ?", (thread.id,)).fetchone()[0] or 0)
    if remaining_log_rows:
        raise RuntimeError("Archived thread logs are still present after deletion.")

    if rollout_path.exists():
        raise RuntimeError("Archived rollout file is still present after deletion.")

    return {
        "backup_dir": backup_dir,
        "backup_paths": backup_paths,
        "deleted_log_rows": deleted_log_rows,
        "deleted_rollout_path": deleted_rollout_path or str(rollout_path),
        "bridge_state_scrubbed": bridge_state_scrubbed,
        "global_state_scrubbed": global_state_scrubbed,
        "session_index_removed": session_index_removed,
    }


def resolve_new_thread_cwd(cwd: str | None) -> str:
    target_source = str(cwd or "").strip()
    if not target_source:
        try:
            target_source = strip_windows_extended_prefix(choose_thread(None, None).cwd)
        except Exception:
            target_source = ""
    if not target_source:
        target_source = os.getcwd()

    target = Path(target_source).expanduser()
    if not target.is_absolute():
        target = target.resolve()
    if not target.exists():
        raise RuntimeError(f"New-thread cwd does not exist: {target}")
    if not target.is_dir():
        raise RuntimeError(f"New-thread cwd is not a directory: {target}")
    return str(target)


def get_sidecar_thread_status_type(thread_payload: dict) -> str:
    status = thread_payload.get("status") or {}
    if isinstance(status, dict):
        return str(status.get("type") or "").strip()
    return ""


def ensure_thread_loaded_via_sidecar(client: CodexAppServerSidecar, thread_id: str) -> dict:
    thread_payload = (client.read_thread(thread_id, include_turns=False).get("thread") or {})
    if get_sidecar_thread_status_type(thread_payload) != "notLoaded":
        return thread_payload
    resumed = client.resume_thread(thread_id)
    resumed_thread = resumed.get("thread") or {}
    if not isinstance(resumed_thread, dict):
        raise RuntimeError("thread/resume did not return a thread payload.")
    return resumed_thread


def get_in_progress_turn_id(thread_payload: dict) -> str | None:
    turns = thread_payload.get("turns") or []
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("id") or "").strip()
        status = str(turn.get("status") or "").strip()
        if turn_id and status == "inProgress":
            return turn_id
    return None


def interrupt_thread_via_sidecar(thread: ThreadInfo) -> bool:
    with CodexAppServerSidecar() as client:
        ensure_thread_loaded_via_sidecar(client, thread.id)
        thread_payload = (client.read_thread(thread.id, include_turns=True).get("thread") or {})
        turn_id = get_in_progress_turn_id(thread_payload)
        if turn_id:
            client.interrupt_turn(thread.id, turn_id)
            return True
        client.clean_background_terminals(thread.id)
        return True


def is_transient_sidecar_attach_error(exc: Exception) -> bool:
    detail = str(exc).lower()
    return "thread not found" in detail or "no rollout found" in detail


def start_turn_via_sidecar(
    thread: ThreadInfo,
    prompt: str,
    *,
    timeout_sec: float = 10.0,
    keep_client_open: bool = False,
) -> dict[str, object]:
    deadline = time.time() + max(timeout_sec, 0.0)
    attempt = 0
    last_error = ""
    while True:
        attempt += 1
        client: CodexAppServerSidecar | None = None
        keep_open = False
        try:
            client = CodexAppServerSidecar()
            ensure_thread_loaded_via_sidecar(client, thread.id)
            result = client.start_turn(thread.id, prompt)
            turn = result.get("turn") or {}
            payload: dict[str, object] = {
                "owner_client_id": "",
                "turn_id": str(turn.get("id") or "").strip(),
                "attempts": str(attempt),
            }
            if keep_client_open:
                payload["_client"] = client
                keep_open = True
            return payload
        except Exception as exc:
            last_error = str(exc)
            if time.time() >= deadline or not is_transient_sidecar_attach_error(exc):
                raise RuntimeError(
                    "Local sidecar could not attach to the selected thread in time. "
                    f"Last error: {last_error}"
                ) from exc
            time.sleep(min(0.5 * attempt, 1.5))
        finally:
            if client is not None and not keep_open:
                client.close()


def spawn_background_new_thread_runner(prompt: str, cwd: str) -> subprocess.Popen:
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )
    return subprocess.Popen(
        [
            resolve_codex_app_server_executable(),
            "debug",
            "app-server",
            "send-message-v2",
            prompt,
        ],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


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
) -> ThreadInfo | None:
    normalized_prompt = normalize_prompt_text(prompt)
    deadline = time.time() + timeout_sec
    cursors = {thread_id: offset for thread_id, (_, _, offset) in session_offsets.items()}

    while time.time() < deadline:
        for thread_id, (thread, session_path, _initial_offset) in session_offsets.items():
            cursor = cursors.get(thread_id, 0)
            events, cursor = read_new_session_events(session_path, cursor)
            cursors[thread_id] = cursor
            for event in events:
                user_text = extract_user_text_from_event(event)
                if not user_text:
                    continue
                if normalize_prompt_text(user_text) == normalized_prompt:
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


def _legacy_activate_thread_by_sidebar(thread_name: str, project_name: str | None = None) -> str:
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


def verify_thread_in_ui(thread: ThreadInfo) -> str | None:
    for thread_name in get_thread_ui_name_candidates(thread):
        header_verified = verify_active_thread_by_header(thread_name)
        if header_verified:
            return header_verified
    return verify_active_thread(thread.id)


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


def wait_for_new_thread(previous_ids: set[str], timeout_sec: float = 8.0) -> ThreadInfo | None:
    deadline = time.time() + max(timeout_sec, 0.0)
    scan_limit = max(20, len(previous_ids) + 5)
    while time.time() < deadline:
        for thread in load_recent_threads(limit=scan_limit):
            if thread.id not in previous_ids:
                return thread
        time.sleep(0.25)
    return None


def cancel_codex_reply_if_busy(timeout_sec: float = 3.0) -> tuple[list[str], list[str]]:
    busy_before = get_busy_threads(limit=50)
    if not busy_before:
        return [], []

    labels_before = [get_thread_label(thread) for thread in busy_before]
    selected_thread_id = get_selected_thread_id()
    target_thread = None
    if selected_thread_id:
        for thread in busy_before:
            if thread.id == selected_thread_id:
                target_thread = thread
                break
    if target_thread is None and len(busy_before) == 1:
        target_thread = busy_before[0]

    if target_thread is None:
        return labels_before, labels_before

    try:
        interrupt_thread_via_sidecar(target_thread)
    except Exception:
        return labels_before, labels_before

    deadline = time.time() + timeout_sec
    remaining_threads = busy_before
    while time.time() < deadline:
        remaining_threads = get_busy_threads(limit=50)
        if not any(thread.id == target_thread.id for thread in remaining_threads):
            return labels_before, [get_thread_label(thread) for thread in remaining_threads]
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
    if composer_focused:
        send_hotkey(VK_CONTROL, VK_A)
        send_key_event(VK_BACK, keyup=False)
        send_key_event(VK_BACK, keyup=True)
        time.sleep(0.05)
    set_clipboard_text(prompt)
    if get_clipboard_text() != prompt:
        time.sleep(0.05)
        if get_clipboard_text() != prompt:
            raise RuntimeError("Clipboard did not contain the prompt after setting it.")
    send_hotkey(VK_CONTROL, VK_V)
    send_key_event(VK_RETURN, keyup=False)
    send_key_event(VK_RETURN, keyup=True)
    if not composer_focused:
        print("[warning] Composer focus was not confirmed before paste.")
    return window


def print_thread_list(threads: list[ThreadInfo]) -> None:
    selected_thread_id = get_selected_thread_id()
    workspace_refs = build_workspace_ref_map(threads)
    for index, thread in enumerate(threads, start=1):
        marker = "*" if thread.id == selected_thread_id else " "
        ui_name = get_thread_ui_name(thread.id, thread)
        summary = collapse_list_text(ui_name or thread.title, limit=70)
        workspace = workspace_refs.get(thread.id, get_thread_workspace_name(thread))
        busy = is_thread_busy(Path(thread.rollout_path))
        state = "busy" if busy else "idle"
        context_usage = get_thread_context_usage(thread)
        if context_usage is None:
            ctx_display = "-/-"
        else:
            ctx_display = (
                f"{format_token_k(context_usage.last_input_tokens)}/"
                f"{format_token_k(context_usage.peak_input_tokens)}"
            )
        used_display = format_token_k(thread.tokens_used)
        rec_display = "archive" if should_recommend_archive(thread, context_usage) else "-"
        line = (
            f"{marker}{index:>2} | {workspace:<12} | {state:<4} | "
            f"ctx {ctx_display:>15} | used {used_display:>7} | rec {rec_display:<7} | "
            f"{thread.id} | {format_timestamp(thread.updated_at)} | {summary}"
        )
        print(make_console_safe_text(line))


def print_archived_thread_list(threads: list[ThreadInfo]) -> None:
    selected_thread_id = get_selected_thread_id()
    workspace_refs = build_workspace_ref_map(threads)
    for index, thread in enumerate(threads, start=1):
        marker = "*" if thread.id == selected_thread_id else " "
        summary = collapse_list_text(thread.title, limit=70)
        workspace = workspace_refs.get(thread.id, get_thread_workspace_name(thread))
        archived_at = format_timestamp(thread.archived_at or thread.updated_at)
        line = f"{marker}{index:>2} | {workspace:<12} | {thread.id} | {archived_at} | {summary}"
        print(make_console_safe_text(line))


def command_list(args: argparse.Namespace) -> int:
    threads = load_recent_threads(limit=args.limit)
    print_thread_list(threads)
    return 0


def command_archived_list(args: argparse.Namespace) -> int:
    threads = load_archived_threads(limit=args.limit)
    print_archived_thread_list(threads)
    return 0


def command_status(args: argparse.Namespace) -> int:
    thread = choose_thread(args.thread_id, args.cwd)
    session_path = Path(thread.rollout_path)
    last_user, last_assistant = get_last_user_and_assistant_messages(session_path)
    busy = is_thread_busy(session_path)
    slot = get_thread_slot(thread)
    ui_name = get_thread_ui_name(thread.id, thread)
    context_usage = get_thread_context_usage(thread)
    print(f"thread_id: {thread.id}")
    print(f"thread_ref: {get_thread_workspace_ref(thread)}")
    print(f"title: {thread.title}")
    print(f"ui_name: {ui_name or '-'}")
    print(f"cwd: {thread.cwd}")
    print(f"updated_at: {format_timestamp(thread.updated_at)}")
    print(f"model: {thread.model} / {thread.reasoning_effort}")
    print(f"tokens_used: {thread.tokens_used}")
    if context_usage is not None:
        print(f"context_window: {context_usage.model_context_window}")
        print(f"last_input_tokens: {context_usage.last_input_tokens}")
        print(f"last_total_tokens: {context_usage.last_total_tokens}")
        print(
            "context_usage: "
            f"{context_usage.usage_ratio * 100:.1f}% ({describe_thread_context_usage(context_usage)})"
        )
    else:
        print("context_usage: -")
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

    print(f"high_context_threshold: {HIGH_CONTEXT_INPUT_RATIO_THRESHOLD * 100:.1f}%")
    scan_limit = max(20, args.limit * 4)
    high_context_threads = get_high_context_threads(limit=scan_limit)
    if high_context_threads:
        labels = ", ".join(
            f"{get_thread_workspace_ref(thread)}={usage.usage_ratio * 100:.1f}%"
            for thread, usage in high_context_threads[: args.limit]
        )
        print(f"high_context_threads: {labels}")
    else:
        print("high_context_threads: -")

    try:
        window = find_codex_window()
        safe_title = make_console_safe_text(window.title)
        print(f"codex_window_found: True")
        print(f"codex_window_title: {safe_title}")
        print(
            make_console_safe_text(
                "codex_window_rect: "
                f"({window.left},{window.top})-({window.right},{window.bottom})"
            )
        )
    except Exception as exc:
        print("codex_window_found: False")
        print(f"codex_window_error: {make_console_safe_text(str(exc))}")

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
        make_console_safe_text(
            f"focused_window: hwnd={window.hwnd} title={window.title} "
            f"rect=({window.left},{window.top})-({window.right},{window.bottom})"
        )
    )
    print(f"composer_focused: {composer_focused}")
    return 0


def command_new(args: argparse.Namespace) -> int:
    cancelled_labels: list[str] = []
    cancel_remaining: list[str] = []
    if args.abort:
        cancelled_labels, cancel_remaining = cancel_codex_reply_if_busy(timeout_sec=3.0)

    target_cwd = resolve_new_thread_cwd(args.cwd)
    prompt = args.prompt or ""
    if not prompt:
        raise RuntimeError(
            "Background `new` requires an initial prompt. The local runner only persists a new thread once the first "
            "message is sent."
        )

    previous_ids = {thread.id for thread in load_recent_threads(limit=0)}
    runner = spawn_background_new_thread_runner(prompt, target_cwd)
    thread = wait_for_new_thread(previous_ids, timeout_sec=args.create_timeout)
    if thread is None:
        exit_code = runner.poll()
        if exit_code is None:
            raise RuntimeError(
                "Background new-thread runner started, but a new persisted thread did not appear in local Codex state in time."
            )
        raise RuntimeError(
            f"Background new-thread runner exited before a new thread appeared in local Codex state (exit={exit_code})."
        )

    set_selected_thread_id(thread.id)

    print(f"selected_thread: {thread.id}")
    print(f"target_thread: {thread.id}")
    print(f"title: {format_title_preview(thread.title)}")
    print(f"ui_name: {get_thread_ui_name(thread.id, thread) or '-'}")
    print(f"cwd: {thread.cwd or target_cwd}")
    print("transport: local-sidecar runner (debug app-server send-message-v2)")
    if cancelled_labels:
        print(f"reply_abort_requested: {', '.join(cancelled_labels)}")
        if cancel_remaining:
            print(f"reply_abort_pending: {', '.join(cancel_remaining)}")

    session_path = Path(thread.rollout_path)
    if session_path.exists():
        delivered_thread = wait_for_prompt_delivery(
            {thread.id: (thread, session_path, 0)},
            prompt,
            timeout_sec=6.0,
        )
        if delivered_thread is None:
            raise RuntimeError(
                "Prompt delivery could not be confirmed in the newly created thread."
            )
        print(f"[delivery_verified] {get_thread_label(thread)}")

    sync_session_index_with_state()
    print(f"[background_runner_pid] {runner.pid}")
    return 0


def command_archive(args: argparse.Namespace) -> int:
    if getattr(args, "thread_ref", None):
        thread = resolve_thread_ref(args.thread_ref)
    else:
        thread = choose_thread(args.thread_id, args.cwd)

    session_path = Path(thread.rollout_path)
    if session_path.exists() and is_thread_busy(session_path):
        raise RuntimeError(
            "The selected thread is still busy. Wait for it to finish before archiving it."
        )

    with CodexAppServerSidecar() as client:
        client.archive_thread(thread.id)

    archived_record = wait_for_thread_record(thread.id, archived=True, timeout_sec=args.timeout)
    if archived_record is None:
        raise RuntimeError(
            "thread/archive returned, but the thread did not appear as archived in local Codex state in time."
        )
    archived_thread, _archived = archived_record

    if get_selected_thread_id() == thread.id:
        set_selected_thread_id(None)
        print("selected_thread: cleared")

    sync_session_index_with_state()
    print(f"archived_thread: {thread.id}")
    print(f"title: {format_title_preview(thread.title)}")
    print(f"cwd: {thread.cwd}")
    print(f"archived_rollout_path: {archived_thread.rollout_path}")
    print("transport: local-sidecar thread/archive")
    return 0


def command_delete_archive(args: argparse.Namespace) -> int:
    thread = resolve_archived_thread_ref(args.thread_ref, limit=0)
    print(f"thread_id: {thread.id}")
    print(f"title: {format_title_preview(thread.title)}")
    print(f"cwd: {thread.cwd}")
    print(f"archived_at: {format_timestamp(thread.archived_at or thread.updated_at)}")
    print(f"rollout_path: {thread.rollout_path}")
    if not args.confirm:
        print("delete_mode: preview")
        print(f"rerun: delete_archive --confirm {thread.id}")
        return 0

    result = delete_archived_thread_locally(thread)
    sync_session_index_with_state()
    print("delete_mode: confirmed")
    print(f"deleted_log_rows: {result['deleted_log_rows']}")
    print(f"deleted_rollout_path: {result['deleted_rollout_path']}")
    print(f"backup_dir: {result['backup_dir']}")
    backup_paths = result.get("backup_paths") or []
    if backup_paths:
        print("backup_files:")
        for path in backup_paths:
            print(f"- {path}")
    bridge_state_scrubbed = result.get("bridge_state_scrubbed") or []
    if bridge_state_scrubbed:
        print(f"bridge_state_scrubbed: {', '.join(bridge_state_scrubbed)}")
    global_state_scrubbed = result.get("global_state_scrubbed") or []
    if global_state_scrubbed:
        print(f"global_state_scrubbed: {', '.join(global_state_scrubbed)}")
    session_index_removed = int(result.get("session_index_removed") or 0)
    print(f"session_index_removed: {session_index_removed}")
    return 0


def command_use(args: argparse.Namespace) -> int:
    if args.clear:
        set_selected_thread_id(None)
        print("selected_thread: cleared")
        return 0

    if args.thread_ref:
        thread = resolve_thread_ref(args.thread_ref)
    else:
        thread = choose_thread(args.thread_id, args.cwd)

    set_selected_thread_id(thread.id)
    session_path = Path(thread.rollout_path)
    last_user, last_assistant = get_last_user_and_assistant_messages(session_path)
    print(f"selected_thread: {thread.id}")
    if last_user:
        print("")
        print("[last_user]")
        print(last_user)
    if last_assistant:
        print("")
        print("[last_assistant]")
        print(last_assistant)
    print("")
    print(f"title: {format_title_preview(thread.title)}")
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
    print(f"title: {format_title_preview(thread.title)}")
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
    thread = choose_thread(args.thread_id, args.cwd)
    session_path = Path(thread.rollout_path)
    if not session_path.exists():
        raise RuntimeError(f"Session file not found: {session_path}")

    prompt = args.prompt
    print(f"target_thread: {thread.id}")
    print(f"title: {format_title_preview(thread.title)}")
    print(f"ui_name: {get_thread_ui_name(thread.id, thread) or '-'}")
    print(f"cwd: {thread.cwd}")
    print("")

    if args.dry_run:
        print("[dry_run]")
        print(prompt)
        return 0

    busy_threads = get_busy_threads(limit=50)
    if busy_threads and not args.force_while_busy and not args.ipc:
        labels = ", ".join(get_thread_label(item) for item in busy_threads[:3])
        raise RuntimeError(
            "A Codex reply is still in progress. You can `open` other threads, but `ask` is blocked until it finishes. "
            f"Busy thread(s): {labels}. Pass --force-while-busy to override."
        )

    if is_thread_busy(session_path) and not args.force_while_busy:
        raise RuntimeError(
            "The selected thread is still busy. This often means the same Codex thread is currently active "
            "or another task is still running. Wait, switch to another thread, or pass --force-while-busy."
        )

    start_offset = session_path.stat().st_size
    recent_offsets = snapshot_recent_session_offsets(limit=10, include_threads=[thread])
    sidecar_client: CodexAppServerSidecar | None = None
    try:
        if args.ipc:
            print("ui_activation: ipc-thread-follower-start-turn")
            ipc_result: dict[str, object]
            try:
                ipc_result = start_turn_via_ipc(
                    thread,
                    prompt,
                    timeout_sec=10.0,
                    allow_ui_recovery=args.ipc_recover_ui,
                )
            except RuntimeError as exc:
                if "IPC owner client for the selected thread was not discovered" not in str(exc):
                    raise
                ipc_result = start_turn_via_sidecar(
                    thread,
                    prompt,
                    timeout_sec=10.0,
                    keep_client_open=not args.background,
                )
                maybe_client = ipc_result.pop("_client", None)
                if isinstance(maybe_client, CodexAppServerSidecar):
                    sidecar_client = maybe_client
                ipc_result["fallback_transport"] = "local-sidecar"
            delivered_thread = wait_for_prompt_delivery(recent_offsets, prompt, timeout_sec=6.0)
            if delivered_thread is None:
                raise RuntimeError(
                    "Prompt delivery could not be confirmed in any recent Codex thread after IPC delivery. "
                    "The transport reported success, but no matching user message was recorded."
                )
            if delivered_thread.id != thread.id:
                raise RuntimeError(
                    "Prompt landed in a different thread after IPC delivery. "
                    f"Expected {get_thread_label(thread)}, but it was recorded in {get_thread_label(delivered_thread)}."
                )
            print(f"[delivery_verified] {get_thread_label(thread)}")
            if ipc_result.get("fallback_transport"):
                print(
                    f"[ipc_fallback] transport={ipc_result['fallback_transport']} "
                    f"attempts={ipc_result.get('attempts', '-')}"
                )
            if ipc_result.get("recovery_method"):
                print(f"[ipc_recovery] {ipc_result['recovery_method']}")
            print(
                f"[ipc_delivery] owner_client={ipc_result['owner_client_id']} "
                f"turn_id={ipc_result['turn_id'] or '-'}"
            )
        else:
            if args.switch_thread:
                activation_method = activate_thread_in_ui(thread)
            else:
                verified_by = verify_thread_in_ui(thread)
                if not verified_by:
                    raise RuntimeError(
                        "The selected thread is not confirmed as the currently open Codex thread. "
                        "Refusing to paste because it could create a new chat instead. "
                        "Open the thread first or rerun with --switch-thread."
                    )
                activation_method = f"already-open [{verified_by}]"
            print(f"ui_activation: {activation_method}")
            window = send_prompt_to_codex(
                prompt=prompt,
                click_x_ratio=args.click_x_ratio,
                click_y_offset=args.click_y_offset,
                skip_click=not args.click,
            )
            print(
                make_console_safe_text(
                    f"sent_to_window: hwnd={window.hwnd} title={window.title} "
                    f"rect=({window.left},{window.top})-({window.right},{window.bottom})"
                )
            )

            delivered_thread = wait_for_prompt_delivery(recent_offsets, prompt, timeout_sec=4.0)
            if delivered_thread is None:
                raise RuntimeError(
                    "Prompt delivery could not be confirmed in any recent Codex thread. "
                    "The UI likely moved, but the message was not recorded."
                )
            if delivered_thread.id != thread.id:
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
    finally:
        if sidecar_client is not None:
            sidecar_client.close()


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

    archived_list_parser = subparsers.add_parser(
        "archived_list",
        help="List archived local Codex Desktop threads.",
    )
    archived_list_parser.add_argument("--limit", type=int, default=10)
    archived_list_parser.set_defaults(func=command_archived_list)

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

    new_parser = subparsers.add_parser(
        "new",
        help="Create a new Codex thread through the local app-server sidecar.",
    )
    new_parser.add_argument("prompt", nargs="?", help="Optional first prompt for the new chat.")
    new_parser.add_argument("--abort", action="store_true", help="Abort the currently running Codex reply first.")
    new_parser.add_argument("--cwd", default=None, help="Working directory for the new thread. Defaults to the current shell cwd.")
    new_parser.add_argument("--click-x-ratio", type=float, default=0.5)
    new_parser.add_argument("--click-y-offset", type=int, default=90)
    new_parser.add_argument("--click", action="store_true", help="Click inside the window before pasting.")
    new_parser.add_argument(
        "--create-timeout",
        type=float,
        default=8.0,
        help="How long to wait for the newly created thread to appear in local state after sending a prompt.",
    )
    new_parser.set_defaults(func=command_new)

    archive_parser = subparsers.add_parser(
        "archive",
        help="Archive a thread through the local app-server sidecar.",
        parents=[common_parser],
    )
    archive_parser.add_argument(
        "thread_ref",
        nargs="?",
        help="Optional workspace name, list index, `other`, or exact thread id.",
    )
    archive_parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="How long to wait for the archived state to appear in local Codex state.",
    )
    archive_parser.set_defaults(func=command_archive)

    delete_archive_parser = subparsers.add_parser(
        "delete_archive",
        help="Delete a locally archived thread and its local traces.",
    )
    delete_archive_parser.add_argument(
        "thread_ref",
        help="Archived thread index, workspace ref, workspace name, or exact thread id.",
    )
    delete_archive_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete the archived thread after previewing it.",
    )
    delete_archive_parser.set_defaults(func=command_delete_archive)

    tail_parser = subparsers.add_parser(
        "tail",
        help="Tail session events for the selected thread.",
        parents=[common_parser],
    )
    tail_parser.add_argument("--timeout", type=float, default=0.0, help="0 means run forever.")
    tail_parser.add_argument("--only-new", action="store_true")
    tail_parser.set_defaults(func=command_tail)

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
        "--ipc",
        dest="ipc",
        action="store_true",
        help="Send the prompt through Codex IPC without UI paste. Default behavior.",
    )
    ask_parser.add_argument(
        "--ui",
        dest="ipc",
        action="store_false",
        help="Use the legacy UI paste path. This can move the Codex window to the foreground.",
    )
    ask_parser.add_argument(
        "--ipc-recover-ui",
        action="store_true",
        help="If background IPC cannot find the target thread owner, reactivate the thread in the Codex UI and retry.",
    )
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
        ipc=True,
        ipc_recover_ui=False,
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
    known_commands = {
        "list",
        "archived_list",
        "use",
        "status",
        "doctor",
        "focus",
        "new",
        "archive",
        "delete_archive",
        "tail",
        "open",
        "ask",
        "help",
        "exit",
        "quit",
    }
    print("Codex bridge shell")
    print("Commands: list, archived_list, open, use, new, archive, delete_archive, ask, status, doctor, tail, focus, help, exit")
    print("Primary flow: list -> open ai -> ask \"...\"")
    print("Example: open ai")
    print('Example: new "테스트"')
    print("Example: archive other")
    print("Example: archived_list")
    print("Example: delete_archive 1")
    print("Example: open --abort ai")
    print("Example: open other")
    print("Example: doctor")
    print('Example: ask "이 파일 수정해줘"')
    print("`open` selects + opens a thread. `use` only selects without opening.")
    print('Tip: plain text is treated as `ask --stream --include-commentary "..."`')
    print("Busy safety: `open` is blocked while another reply is running unless you pass `--abort`.")
    print("Default ask uses background IPC. Pass `--ui` to use the legacy foreground paste path.")
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
            argv = ["ask", "--stream", "--include-commentary", line]
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
    rotate_single_backup_file(IPC_PROBE_LOG_PATH)
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
