"""Help text for the Discord bridge commands."""

from __future__ import annotations


def build_help(*, qa_commands_enabled: bool) -> str:
    slash_commands = [
        "/help",
        "/list",
        "/archived_list",
        "/use",
        "/status",
        "/doctor",
        "/where",
        "/context",
        "/usage",
        "/runners",
        "/mirror_check",
        "/bridge_sync",
        "/new",
        "/ask",
        "/ask_ipc",
    ]
    lines = [
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
        "!bridge sync [limit]",
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
    ]
    if qa_commands_enabled:
        lines.insert(lines.index("!approval"), "!qa buttons")
        lines.insert(lines.index("!qa buttons") + 1, "!steer <prompt>  (QA-only text path for Steer now)")
        slash_commands.insert(slash_commands.index("/new"), "/qa_buttons")
    lines.append(f"Slash commands: {', '.join(slash_commands)}.")
    return "\n".join(lines)
