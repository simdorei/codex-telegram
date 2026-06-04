"""Interactive prompt parsing helpers for the Discord bridge."""

from __future__ import annotations

import re


def parse_interactive_notice(
    text: str,
    *,
    state_none: str,
    state_input: str,
    state_approval: str,
    input_tag: str,
    approval_tag: str,
) -> tuple[str, str, list[tuple[str, str]]]:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    if not lines:
        return state_none, "", []
    first_line = lines[0].strip()
    if first_line not in {input_tag, approval_tag}:
        return state_none, "", []

    prompt_lines: list[str] = []
    options: list[tuple[str, str]] = []
    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        match = re.match(r"^(\d+)[\.\)]\s+(.+)$", stripped)
        if match:
            options.append((match.group(1), match.group(2).strip()))
            continue
        prompt_lines.append(stripped)

    state = state_input if first_line == input_tag else state_approval
    return state, "\n".join(prompt_lines), options


def normalize_interactive_text_reply(
    state: str,
    answer: str,
    *,
    state_input: str,
    state_approval: str,
) -> str | None:
    stripped = str(answer or "").strip()
    if not stripped:
        return None
    if state == state_approval:
        normalized = re.sub(r"\s+", " ", stripped.casefold())
        approval_answers = {
            "1": "1",
            "approve": "1",
            "yes": "1",
            "y": "1",
            "2": "2",
            "approve session": "2",
            "3": "3",
            "reject": "3",
            "no": "3",
            "n": "3",
            "cancel": "cancel",
        }
        return approval_answers.get(normalized)
    if state == state_input:
        return stripped
    return None
