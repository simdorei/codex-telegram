"""Pure command parsing and argv builders for the Discord adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PrefixCommand:
    command: str
    arg: str


@dataclass(frozen=True)
class PrefixBridgeAction:
    argv: list[str] | None
    title: str
    usage: str | None = None


@dataclass(frozen=True)
class PrefixLimitAction:
    limit: int | None
    usage: str | None = None


@dataclass(frozen=True)
class MirrorAction:
    subcommand: str | None
    limit: int | None = None
    usage: str | None = None


def split_prefix_command(command_line: str) -> PrefixCommand | None:
    if not command_line:
        return None
    command, _, arg = str(command_line or "").partition(" ")
    command = command.lower().strip()
    if not command:
        return None
    return PrefixCommand(command=command, arg=arg.strip())


def parse_bounded_int(raw: object, *, default: int, minimum: int, maximum: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def parse_required_bounded_int(raw: object, *, default: int, minimum: int, maximum: int) -> int | None:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return max(minimum, min(maximum, value))


def build_list_argv(raw_limit: object = "", *, default: int = 10, maximum: int = 30) -> list[str]:
    limit = parse_bounded_int(raw_limit, default=default, minimum=1, maximum=maximum)
    return ["list", "--limit", str(limit)]


def build_archived_list_argv(raw_limit: object = "", *, default: int = 10, maximum: int = 50) -> list[str]:
    limit = parse_bounded_int(raw_limit, default=default, minimum=1, maximum=maximum)
    return ["archived_list", "--limit", str(limit)]


def build_open_argv(command: str, ref: str) -> list[str]:
    argv = ["open"]
    if command == "open_abort":
        argv.append("--abort")
    argv.append(ref)
    return argv


def build_status_argv(
    channel_id: int | None,
    ref: str | None,
    *,
    resolve_target_args_func: Callable[[int | None, str | None], list[str]],
) -> list[str]:
    argv = ["status"]
    argv.extend(resolve_target_args_func(channel_id, ref or None))
    return argv


def build_archive_argv(
    channel_id: int | None,
    ref: str | None,
    *,
    resolve_target_args_func: Callable[[int | None, str | None], list[str]],
) -> list[str]:
    argv = ["archive"]
    argv.extend(resolve_target_args_func(channel_id, ref or None))
    return argv


def build_prefix_bridge_action(
    command: str,
    arg: str,
    channel_id: int | None,
    *,
    resolve_target_args_func: Callable[[int | None, str | None], list[str]],
) -> PrefixBridgeAction | None:
    if command == "list":
        return PrefixBridgeAction(build_list_argv(arg), "List")
    if command in {"archived_list", "archive_list"}:
        return PrefixBridgeAction(build_archived_list_argv(arg), "Archived list")
    if command == "use":
        if not arg:
            return PrefixBridgeAction(None, "Use", "Usage: !use <ref>")
        return PrefixBridgeAction(["use", arg], "Use")
    if command in {"open", "open_abort"}:
        if not arg:
            return PrefixBridgeAction(None, "Open", f"Usage: !{command} <ref>")
        return PrefixBridgeAction(build_open_argv(command, arg), "Open")
    if command == "status":
        return PrefixBridgeAction(
            build_status_argv(
                channel_id,
                arg or None,
                resolve_target_args_func=resolve_target_args_func,
            ),
            "Status",
        )
    if command == "discover_codex":
        return PrefixBridgeAction(["discover_codex"], "Codex path")
    if command == "restart_codex":
        return PrefixBridgeAction(["restart_codex"], "Codex restart")
    if command == "archive":
        return PrefixBridgeAction(
            build_archive_argv(
                channel_id,
                arg or None,
                resolve_target_args_func=resolve_target_args_func,
            ),
            "Archive",
        )
    if command == "confirm_delete_archive":
        if not arg:
            return PrefixBridgeAction(None, "Delete archive", "Usage: !confirm_delete_archive <ref>")
        return PrefixBridgeAction(["delete_archive", "--confirm", arg], "Delete archive")
    return None


def parse_usage_days(arg: str) -> PrefixLimitAction:
    days = parse_required_bounded_int(arg, default=7, minimum=1, maximum=30)
    if days is None:
        return PrefixLimitAction(None, "Usage: !usage [days]")
    return PrefixLimitAction(days)


def parse_bridge_sync_limit(command: str, arg: str) -> PrefixLimitAction:
    if command == "bridge":
        subcommand, _, subarg = arg.partition(" ")
        if (subcommand or "sync").lower().strip() != "sync":
            return PrefixLimitAction(None, "Usage: !bridge sync [limit]")
        limit_arg = subarg.strip()
    else:
        limit_arg = arg
    limit = parse_bounded_int(limit_arg, default=30, minimum=1, maximum=100)
    return PrefixLimitAction(limit)


def parse_mirror_action(arg: str) -> MirrorAction:
    subcommand, _, subarg = arg.partition(" ")
    subcommand = (subcommand or "sync").lower().strip()
    subarg = subarg.strip()
    if subcommand == "sync":
        limit = parse_required_bounded_int(subarg, default=30, minimum=1, maximum=100)
        if limit is None:
            return MirrorAction(None, usage="Usage: !mirror sync [limit]")
        return MirrorAction("sync", limit=limit)
    if subcommand == "list":
        limit = parse_required_bounded_int(subarg, default=30, minimum=1, maximum=100)
        if limit is None:
            return MirrorAction(None, usage="Usage: !mirror list [limit]")
        return MirrorAction("list", limit=limit)
    if subcommand in {"check", "doctor"}:
        return MirrorAction("check")
    return MirrorAction(None, usage="Usage: !mirror sync [limit] | !mirror list [limit] | !mirror check")
