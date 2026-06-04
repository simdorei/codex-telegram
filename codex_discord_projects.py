"""Project/path helpers for Discord Codex mirrors."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path


def is_codex_projectless_chat_cwd(cwd: str, *, bridge_module: object) -> bool:
    normalized = bridge_module.normalize_workspace_path(cwd)
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


def get_project_key(thread: object, *, bridge_module: object, projectless_chat_key: str) -> str:
    cwd = bridge_module.strip_windows_extended_prefix((getattr(thread, "cwd", "") or "").strip())
    if cwd:
        if is_codex_projectless_chat_cwd(cwd, bridge_module=bridge_module):
            return projectless_chat_key
        try:
            return bridge_module.normalize_workspace_path(cwd)
        except Exception:
            return cwd.lower()
    return f"projectless:{bridge_module.get_thread_workspace_name(thread)}"


def get_project_name(thread: object, *, bridge_module: object) -> str:
    cwd = bridge_module.strip_windows_extended_prefix((getattr(thread, "cwd", "") or "").strip())
    if cwd and is_codex_projectless_chat_cwd(cwd, bridge_module=bridge_module):
        return "\ucc44\ud305"
    name = bridge_module.get_thread_workspace_name(thread)
    return name if name and name != "-" else "projectless"


def get_saved_workspace_project_keys(*, bridge_module: object) -> set[str]:
    data = bridge_module.load_json(bridge_module.GLOBAL_STATE_PATH)
    saved: set[str] = set()
    for key in ("project-order", "electron-saved-workspace-roots"):
        roots = data.get(key) or []
        if not isinstance(roots, list):
            continue
        for root in roots:
            value = str(root or "").strip()
            if value:
                saved.add(bridge_module.normalize_workspace_path(value))
    return saved


def is_thread_mirrorable(
    thread: object,
    saved_project_keys: set[str] | None = None,
    *,
    bridge_module: object,
    projectless_chat_key: str,
) -> bool:
    project_key = get_project_key(
        thread,
        bridge_module=bridge_module,
        projectless_chat_key=projectless_chat_key,
    )
    if project_key == projectless_chat_key or project_key.startswith("projectless:"):
        return True
    saved_keys = (
        saved_project_keys
        if saved_project_keys is not None
        else get_saved_workspace_project_keys(bridge_module=bridge_module)
    )
    if not saved_keys:
        return True
    return project_key in saved_keys


def filter_mirrorable_threads(
    threads: list[object],
    *,
    bridge_module: object,
    projectless_chat_key: str,
) -> list[object]:
    saved_project_keys = get_saved_workspace_project_keys(bridge_module=bridge_module)
    return [
        thread
        for thread in threads
        if is_thread_mirrorable(
            thread,
            saved_project_keys,
            bridge_module=bridge_module,
            projectless_chat_key=projectless_chat_key,
        )
    ]


def get_thread_cwd(thread_id: str | None, *, bridge_module: object) -> str | None:
    if not thread_id:
        return None
    try:
        thread = bridge_module.choose_thread(thread_id, None)
    except Exception:
        return None
    cwd = bridge_module.strip_windows_extended_prefix((getattr(thread, "cwd", "") or "").strip())
    return cwd or None


def find_projectless_new_chat_cwd(*, home_path: Path | None = None) -> str | None:
    codex_docs = (home_path or Path.home()) / "Documents" / "Codex"
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


def resolve_discord_new_thread_cwd(
    discord_channel_id: int | None,
    *,
    bridge_module: object,
    projectless_chat_key: str,
    get_mirrored_codex_thread_id_func,
    get_thread_cwd_func,
    get_mirror_project_for_channel_func,
    find_projectless_new_chat_cwd_func,
) -> str | None:
    target_thread_id = get_mirrored_codex_thread_id_func(discord_channel_id)
    thread_cwd = get_thread_cwd_func(target_thread_id)
    if thread_cwd:
        return thread_cwd

    project = get_mirror_project_for_channel_func(discord_channel_id)
    if not project:
        return None
    project_key, _project_name = project
    if project_key == projectless_chat_key:
        return find_projectless_new_chat_cwd_func()
    if project_key and not project_key.startswith("projectless:"):
        project_path = Path(bridge_module.strip_windows_extended_prefix(project_key))
        if project_path.is_dir():
            return str(project_path)
    return None


def project_keys_match(
    left: str | None,
    right: str | None,
    *,
    bridge_module: object,
    projectless_chat_key: str,
) -> bool:
    left_value = str(left or "").strip()
    right_value = str(right or "").strip()
    if not left_value or not right_value:
        return False
    if left_value == right_value:
        return True
    if left_value.startswith("projectless:") or right_value.startswith("projectless:"):
        return False
    if left_value == projectless_chat_key or right_value == projectless_chat_key:
        return False
    try:
        return bridge_module.normalize_workspace_path(left_value) == bridge_module.normalize_workspace_path(
            right_value
        )
    except Exception:
        return left_value.lower() == right_value.lower()


def resolve_discord_new_thread_project_channel_id(
    discord_channel_id: int | None,
    project_key: str | None,
    *,
    db_path: Path,
    init_mirror_db_func,
    project_keys_match_func,
) -> int | None:
    if not discord_channel_id or not project_key:
        return None
    init_mirror_db_func()
    with sqlite3.connect(db_path) as conn:
        thread_rows = conn.execute(
            """
            SELECT discord_channel_id, project_key
            FROM mirror_threads
            WHERE discord_thread_id = ?
            ORDER BY updated_at DESC
            """,
            (int(discord_channel_id),),
        ).fetchall()
        for row in thread_rows:
            if project_keys_match_func(str(row[1] or ""), project_key):
                return int(row[0])
        project_rows = conn.execute(
            """
            SELECT discord_channel_id, project_key
            FROM mirror_projects
            WHERE discord_channel_id = ?
            ORDER BY updated_at DESC
            """,
            (int(discord_channel_id),),
        ).fetchall()
    for row in project_rows:
        if project_keys_match_func(str(row[1] or ""), project_key):
            return int(row[0])
    return None
