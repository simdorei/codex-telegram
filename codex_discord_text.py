"""Text and parsing helpers for the Discord Codex adapter."""

from __future__ import annotations

import os
import re


DISCORD_MAX_LEN = 1900


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


def parse_bounded_int_arg(raw: str, *, default: int, minimum: int, maximum: int) -> int:
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        return default


def parse_bounded_float_env(name: str, *, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


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


def extract_prompt_first_sentence(prompt: str, *, limit: int = 240) -> str:
    first_line = ""
    for raw_line in str(prompt or "").replace("\r", "\n").split("\n"):
        normalized = " ".join(raw_line.split()).strip()
        if normalized:
            first_line = normalized
            break
    if not first_line:
        return "(empty prompt)"
    sentence_match = re.match(r"(.+?[.!?。！？])(?:\s|$)", first_line)
    preview = sentence_match.group(1).strip() if sentence_match else first_line
    if len(preview) <= limit:
        return preview
    return preview[: max(0, limit - 3)].rstrip() + "..."


def build_ask_start_message(prompt: str, *, queued: bool = False) -> str:
    label = "Discord ask queued." if queued else "Discord ask submitted."
    return fit_single_message(
        "\n".join(
            [
                label,
                f"message: {extract_prompt_first_sentence(prompt)}",
            ]
        )
    )


def normalize_discord_name(value: str, *, prefix: str = "", max_len: int = 90) -> str:
    name = str(value or "").strip().lower()
    name = re.sub(r"[^a-z0-9가-힣._-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-._")
    if prefix and not name.startswith(prefix):
        name = prefix + name
    if not name:
        name = prefix.rstrip("-") or "codex"
    return name[:max_len].strip("-._") or "codex"


def truncate_discord_title(value: str, fallback: str, *, max_len: int = 90) -> str:
    text = str(value or "").replace("\n", " ").strip() or fallback
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)].rstrip() + "..."


def format_percent(value: object) -> str:
    if isinstance(value, int | float):
        return f"{float(value):.1f}%"
    try:
        return f"{float(str(value)):.1f}%"
    except (TypeError, ValueError):
        return "-"
