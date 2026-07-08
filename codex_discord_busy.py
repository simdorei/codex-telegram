"""Busy-thread helper logic for Discord ask flows."""

from __future__ import annotations


def is_selected_thread_busy_error(exit_code: int, output: str) -> bool:
    if exit_code == 0:
        return False
    text = (output or "").lower()
    return (
        "selected thread is still busy" in text
        or "target thread is still busy" in text
        or "a codex reply is still in progress" in text
        or "--force-while-busy" in text and "still busy" in text
        or "selected thread is waiting on a follow-up choice or input" in text
        or "selected thread is waiting on an approval prompt" in text
        or "timed out waiting for ipc data" in text and "codex-ipc" in text
    )


def is_global_codex_busy_error(exit_code: int, output: str) -> bool:
    if exit_code == 0:
        return False
    return "a codex reply is still in progress" in (output or "").lower()


def has_busy_choice_source(source_message: object) -> bool:
    return bool(
        source_message is not None
        and getattr(source_message, "author", None) is not None
        and getattr(source_message, "channel", None) is not None
    )


def build_busy_choice_message(
    prompt: str,
    target_thread_id: str | None,
    *,
    discord_max_len: int,
    fit_single_message_func,
) -> str:
    lines = ["Codex app is still processing this mapped thread.", ""]
    footer = "\n\nChoose the Discord action for this message."
    prefix = "\n".join(lines)
    prompt_text = str(prompt or "")
    prompt_budget = max(0, discord_max_len - len(prefix) - len(footer))
    if len(prompt_text) > prompt_budget:
        suffix = "\n\n[prompt truncated for Discord]"
        prompt_text = prompt_text[: max(0, prompt_budget - len(suffix))].rstrip() + suffix
    return fit_single_message_func(prefix + prompt_text + footer)


def make_busy_choice_payload(
    source_message: object,
    prompt: str,
    *,
    target_thread_id: str | None,
    allow_steer: bool,
    build_busy_choice_message_func,
    make_busy_choice_view_func,
) -> tuple[str, object]:
    return (
        build_busy_choice_message_func(prompt, target_thread_id),
        make_busy_choice_view_func(
            source_message,
            prompt,
            target_thread_id=target_thread_id,
            allow_steer=allow_steer,
        ),
    )


def log_busy_choice_sent(
    reason: str,
    target_thread_id: str | None,
    prompt: str,
    *,
    log_func,
    format_log_text_len_func,
) -> None:
    safe_reason = reason.replace("\n", " ")[:80]
    log_func(
        f"busy_choice_sent reason={safe_reason} target={target_thread_id or '-'} "
        f"prompt_len={format_log_text_len_func(prompt)}"
    )
