"""SQLite-backed Discord adapter persistence helpers."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

from codex_discord_components import get_persistent_component_claim_key


def init_mirror_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS busy_choices (
                choice_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                target_thread_id TEXT,
                prompt TEXT NOT NULL,
                allow_steer INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                claimed_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persistent_component_claims (
                claim_key TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_processed_messages (
                message_id INTEGER PRIMARY KEY,
                seen_at REAL NOT NULL
            )
            """
        )


def cleanup_expired_busy_choices(db_path: Path, now: float | None = None) -> int:
    current = time.time() if now is None else now
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "DELETE FROM busy_choices WHERE expires_at <= ? OR claimed_at IS NOT NULL",
            (current,),
        )
        return result.rowcount


def cleanup_expired_persistent_component_claims(db_path: Path, now: float | None = None) -> int:
    current = time.time() if now is None else now
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "DELETE FROM persistent_component_claims WHERE expires_at <= ?",
            (current,),
        )
        return result.rowcount


def create_busy_choice_record(
    db_path: Path,
    message: object,
    prompt: str,
    target_thread_id: str | None,
    *,
    allow_steer: bool,
    ttl_seconds: float,
) -> str:
    cleanup_expired_busy_choices(db_path)
    now = time.time()
    choice_id = hashlib.sha256(
        f"{now}:{os.urandom(16).hex()}:{getattr(message.author, 'id', '-')}".encode("utf-8")
    ).hexdigest()[:24]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO busy_choices (
                choice_id, owner_user_id, channel_id, target_thread_id, prompt,
                allow_steer, created_at, expires_at, claimed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                choice_id,
                int(getattr(message.author, "id")),
                int(getattr(message.channel, "id")),
                target_thread_id,
                str(prompt or ""),
                1 if allow_steer else 0,
                now,
                now + ttl_seconds,
            ),
        )
    return choice_id


def get_busy_choice_record(db_path: Path, choice_id: str) -> dict[str, object] | None:
    init_mirror_db(db_path)
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT owner_user_id, channel_id, target_thread_id, prompt, allow_steer, expires_at, claimed_at
            FROM busy_choices
            WHERE choice_id = ?
            """,
            (choice_id,),
        ).fetchone()
        if not row:
            return None
        if float(row[5]) <= now or row[6] is not None:
            conn.execute("DELETE FROM busy_choices WHERE choice_id = ?", (choice_id,))
            return None
    return {
        "choice_id": choice_id,
        "owner_user_id": int(row[0]),
        "channel_id": int(row[1]),
        "target_thread_id": str(row[2] or "") or None,
        "prompt": str(row[3] or ""),
        "allow_steer": bool(row[4]),
        "expires_at": float(row[5]),
    }


def claim_busy_choice_record(db_path: Path, choice_id: str) -> bool:
    init_mirror_db(db_path)
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            """
            UPDATE busy_choices
            SET claimed_at = ?
            WHERE choice_id = ?
              AND claimed_at IS NULL
              AND expires_at > ?
            """,
            (now, choice_id, now),
        )
        return result.rowcount == 1


def claim_persistent_component_interaction(
    db_path: Path,
    interaction: object,
    custom_id: str,
    *,
    ttl_seconds: float = 86400.0,
) -> bool:
    claim_key = get_persistent_component_claim_key(interaction, custom_id)
    if claim_key is None:
        return True
    init_mirror_db(db_path)
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM persistent_component_claims WHERE expires_at <= ?", (now,))
        result = conn.execute(
            """
            INSERT OR IGNORE INTO persistent_component_claims (claim_key, created_at, expires_at)
            VALUES (?, ?, ?)
            """,
            (claim_key, now, now + ttl_seconds),
        )
        return result.rowcount == 1


def cleanup_processed_discord_messages(db_path: Path, *, retention_seconds: float, now: float | None = None) -> int:
    current = time.time() if now is None else now
    cutoff = current - retention_seconds
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "DELETE FROM discord_processed_messages WHERE seen_at < ?",
            (cutoff,),
        )
        return result.rowcount


def claim_persistent_discord_message_id(db_path: Path, message_id: int, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            """
            INSERT OR IGNORE INTO discord_processed_messages (message_id, seen_at)
            VALUES (?, ?)
            """,
            (int(message_id), current),
        )
        return result.rowcount == 1


def is_processed_discord_message_id(db_path: Path, message_id: int) -> bool:
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM discord_processed_messages WHERE message_id = ?",
            (int(message_id),),
        ).fetchone()
        return row is not None


def mark_processed_discord_message_id(db_path: Path, message_id: int, now: float | None = None) -> None:
    current = time.time() if now is None else now
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO discord_processed_messages (message_id, seen_at)
            VALUES (?, ?)
            """,
            (int(message_id), current),
        )


def get_startup_probe_targets(
    db_path: Path,
    allowed_channel_ids: set[int],
    startup_channel_id: int | None,
    *,
    limit: int = 30,
) -> list[tuple[str, int]]:
    seen: set[int] = set()
    targets: list[tuple[str, int]] = []

    def add(label: str, channel_id: int | None) -> None:
        if not channel_id:
            return
        normalized = int(channel_id)
        if normalized in seen or len(targets) >= limit:
            return
        seen.add(normalized)
        targets.append((label, normalized))

    add("startup", startup_channel_id)
    for channel_id in sorted(allowed_channel_ids):
        add("allowed", channel_id)

    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        for row in conn.execute(
            """
            SELECT discord_channel_id FROM mirror_projects
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall():
            add("mirror_project", int(row[0]))
        for row in conn.execute(
            """
            SELECT discord_thread_id FROM mirror_threads
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall():
            add("mirror_thread", int(row[0]))
    return targets


def get_mirrored_codex_thread_id(db_path: Path, discord_channel_id: int | None) -> str | None:
    if not discord_channel_id:
        return None
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
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


def describe_mirrored_project_channel(db_path: Path, discord_channel_id: int | None) -> str:
    if not discord_channel_id:
        return ""
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
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
    lines = [
        f"`{project[0]}` project channel has multiple Codex threads.",
        "Send the message inside one of its Discord threads:",
    ]
    lines.extend(f"- {title}" for title in titles)
    return "\n".join(lines)


def get_mirror_project_for_channel(db_path: Path, discord_channel_id: int | None) -> tuple[str, str] | None:
    if not discord_channel_id:
        return None
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT project_key, project_name FROM mirror_projects WHERE discord_channel_id = ?",
            (int(discord_channel_id),),
        ).fetchone()
    if not row:
        return None
    return str(row[0] or ""), str(row[1] or "")


def is_mirrored_channel_id(db_path: Path, discord_channel_id: int | None) -> bool:
    if discord_channel_id is None:
        return False
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM mirror_threads WHERE discord_thread_id = ? OR discord_channel_id = ?
            UNION ALL
            SELECT 1 FROM mirror_projects WHERE discord_channel_id = ?
            LIMIT 1
            """,
            (int(discord_channel_id), int(discord_channel_id), int(discord_channel_id)),
        ).fetchone()
    return row is not None


def get_busy_choice_counts(db_path: Path, now: float | None = None) -> tuple[int, int]:
    current = time.time() if now is None else now
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM busy_choices WHERE expires_at > ? AND claimed_at IS NULL",
            (current,),
        ).fetchone()
        stale = conn.execute(
            "SELECT COUNT(*) FROM busy_choices WHERE expires_at <= ? OR claimed_at IS NOT NULL",
            (current,),
        ).fetchone()
    return int(active[0] if active else 0), int(stale[0] if stale else 0)


def get_persistent_component_claim_counts(db_path: Path, now: float | None = None) -> tuple[int, int]:
    current = time.time() if now is None else now
    init_mirror_db(db_path)
    with sqlite3.connect(db_path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM persistent_component_claims WHERE expires_at > ?",
            (current,),
        ).fetchone()
        stale = conn.execute(
            "SELECT COUNT(*) FROM persistent_component_claims WHERE expires_at <= ?",
            (current,),
        ).fetchone()
    return int(active[0] if active else 0), int(stale[0] if stale else 0)
