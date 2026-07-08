"""Windows-local frontend harness for Codex app/web bridge decisions.

This module keeps Codex state/preflight decisions out of Discord UI code.
It is intentionally small: Discord owns routing and buttons, while this
harness reports structured target/global busy state and local runtime facts.
Codex CLI probing is diagnostic only; Discord remains the frontend surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import codex_desktop_bridge as bridge


HARNESS_VERSION = "2026.06.05-1"


@dataclass(frozen=True)
class HarnessThread:
    id: str
    ref: str
    title: str
    cwd: str
    state: str
    updated_at: int


@dataclass(frozen=True)
class HarnessRuntime:
    version: str
    platform: str
    codex_cli_path: str
    codex_cli_status: str
    codex_desktop_status: str


@dataclass(frozen=True)
class AskPreflight:
    target_thread_id: str | None
    target_ref: str
    target_state: str
    route: str
    accepted: bool
    can_steer: bool
    not_sent_reason: str
    global_busy_threads: list[HarnessThread] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def thread_ref(thread: bridge.ThreadInfo, *, bridge_module: object = bridge) -> str:
    try:
        return bridge_module.get_thread_workspace_ref(thread)
    except Exception:
        return thread.id[:8]


def thread_label(thread: bridge.ThreadInfo, *, bridge_module: object = bridge) -> str:
    try:
        return bridge_module.get_thread_label(thread)
    except Exception:
        return thread.id[:8]


def thread_snapshot(
    thread: bridge.ThreadInfo,
    *,
    state: str,
    bridge_module: object = bridge,
) -> HarnessThread:
    return HarnessThread(
        id=thread.id,
        ref=thread_label(thread, bridge_module=bridge_module),
        title=thread.title,
        cwd=thread.cwd,
        state=state,
        updated_at=int(thread.updated_at),
    )


def choose_target_thread(
    target_thread_id: str | None,
    *,
    bridge_module: object = bridge,
) -> tuple[bridge.ThreadInfo | None, str]:
    try:
        thread = bridge_module.choose_thread(target_thread_id, None)
    except Exception:
        if target_thread_id:
            return None, target_thread_id[:8]
        return None, ""
    return thread, thread_ref(thread, bridge_module=bridge_module)


def get_target_state(
    thread: bridge.ThreadInfo | None,
    *,
    bridge_module: object = bridge,
) -> str:
    if thread is None:
        return "idle"
    try:
        return bridge_module.get_thread_busy_state(thread, allow_resume=True) or "idle"
    except Exception:
        return "idle"


def get_global_busy_thread_snapshots(
    *,
    exclude_thread_id: str | None,
    bridge_module: object = bridge,
    limit: int = 50,
) -> list[HarnessThread]:
    try:
        busy_threads = bridge_module.get_busy_threads(limit=limit)
    except Exception:
        return []
    snapshots: list[HarnessThread] = []
    for thread in busy_threads:
        if exclude_thread_id and thread.id == exclude_thread_id:
            continue
        snapshots.append(
            thread_snapshot(
                thread,
                state=get_target_state(thread, bridge_module=bridge_module),
                bridge_module=bridge_module,
            )
        )
    return snapshots


def preflight_ask(
    target_thread_id: str | None,
    *,
    bridge_module: object = bridge,
    now: float | None = None,
) -> AskPreflight:
    checked_at = time.time() if now is None else now
    target_thread, target_ref = choose_target_thread(target_thread_id, bridge_module=bridge_module)
    resolved_target_id = target_thread.id if target_thread is not None else target_thread_id
    target_state = get_target_state(target_thread, bridge_module=bridge_module)
    events = [
        {
            "type": "preflight_checked",
            "at": checked_at,
            "target_thread_id": resolved_target_id,
            "target_ref": target_ref,
            "target_state": target_state,
        }
    ]

    if target_state != "idle":
        events.append({"type": "not_sent", "reason": "target_busy"})
        return AskPreflight(
            target_thread_id=resolved_target_id,
            target_ref=target_ref,
            target_state=target_state,
            route="target_busy",
            accepted=False,
            can_steer=True,
            not_sent_reason="target_busy",
            events=events,
        )

    global_busy_threads = get_global_busy_thread_snapshots(
        exclude_thread_id=resolved_target_id,
        bridge_module=bridge_module,
    )
    if global_busy_threads:
        events.append(
            {
                "type": "accepted",
                "reason": "target_idle_other_threads_busy",
                "busy_thread_ids": [thread.id for thread in global_busy_threads],
            }
        )
        return AskPreflight(
            target_thread_id=resolved_target_id,
            target_ref=target_ref,
            target_state=target_state,
            route="ask",
            accepted=True,
            can_steer=False,
            not_sent_reason="",
            global_busy_threads=global_busy_threads,
            events=events,
        )

    events.append({"type": "accepted", "reason": "target_idle"})
    return AskPreflight(
        target_thread_id=resolved_target_id,
        target_ref=target_ref,
        target_state=target_state,
        route="ask",
        accepted=True,
        can_steer=False,
        not_sent_reason="",
        events=events,
    )


def probe_codex_cli(path: str | None = None) -> tuple[str, str]:
    cli_path = path or shutil.which("codex") or ""
    if not cli_path:
        return "", "not_found"
    try:
        completed = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except PermissionError:
        return cli_path, "permission_denied"
    except Exception:
        return cli_path, "unavailable"
    return cli_path, "ok" if completed.returncode == 0 else "unavailable"


def get_runtime_status(*, bridge_module: object = bridge) -> HarnessRuntime:
    cli_path, cli_status = probe_codex_cli()
    desktop_status = "unknown"
    try:
        desktop_path, _desktop_source = bridge_module.discover_codex_desktop_executable()
        desktop_status = "available" if desktop_path is not None else "not_found"
    except Exception:
        desktop_status = "unavailable"
    return HarnessRuntime(
        version=HARNESS_VERSION,
        platform="windows-local",
        codex_cli_path=cli_path,
        codex_cli_status=cli_status,
        codex_desktop_status=desktop_status,
    )


def print_json(data: object) -> None:
    if hasattr(data, "to_dict"):
        data = data.to_dict()
    elif hasattr(data, "__dataclass_fields__"):
        data = asdict(data)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Windows-local Codex frontend harness status/preflight.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("runtime")
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()

    if args.command == "runtime":
        print_json(get_runtime_status())
        return 0
    if args.command == "preflight":
        print_json(preflight_ask(args.thread_id))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
