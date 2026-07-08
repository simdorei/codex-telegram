"""Steering prompt result and delivery verification helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class SteeringPromptResult:
    exit_code: int
    output: str
    target_thread_id: str | None = None
    target_ref: str = ""
    session_path: str | None = None
    start_offset: int | None = None
    delivery_pending: bool = False

    def __iter__(self):
        yield self.exit_code
        yield self.output


def make_steering_prompt_result(
    exit_code: int,
    output: str,
    *,
    target_thread: object | None,
    target_ref: str,
    recent_offsets: dict[str, tuple[object, Path, int]],
    delivery_pending: bool = False,
) -> SteeringPromptResult:
    if target_thread is None:
        return SteeringPromptResult(
            exit_code,
            output,
            target_ref=target_ref,
            delivery_pending=delivery_pending,
        )
    _thread, session_path, start_offset = recent_offsets.get(
        target_thread.id,
        (target_thread, Path(target_thread.rollout_path), 0),
    )
    return SteeringPromptResult(
        exit_code,
        output,
        target_thread_id=target_thread.id,
        target_ref=target_ref,
        session_path=str(session_path),
        start_offset=start_offset,
        delivery_pending=delivery_pending,
    )


def is_ipc_delivery_confirmation_timeout(output: str) -> bool:
    text = (output or "").lower()
    return (
        "prompt delivery could not be confirmed in any recent codex thread after ipc delivery"
        in text
        and "transport reported success" in text
    )


def format_pending_ipc_delivery_output(output: str) -> str:
    metadata_lines = [
        line
        for line in (output or "").splitlines()
        if line.strip()
        and not line.lstrip().upper().startswith("ERROR:")
        and "Prompt delivery could not be confirmed" not in line
        and "transport reported success" not in line
    ]
    return "\n".join(
        part
        for part in [
            "[delivery_pending] Codex IPC accepted the steering request, but local session recording is delayed.",
            "Discord will keep watching this thread for the next Codex reply.",
            "\n".join(metadata_lines),
        ]
        if part
    )


def run_steering_prompt(
    prompt: str,
    target_thread_id: str | None,
    *,
    bridge_module: object,
    resolve_target_ref_func,
    run_ask_func,
    get_steering_delivery_confirm_timeout_func,
    log_func,
    format_log_text_len_func,
) -> SteeringPromptResult:
    target_thread_id, _target_ref = resolve_target_ref_func(target_thread_id)
    target_thread = bridge_module.choose_thread(target_thread_id, None) if target_thread_id else None
    target_ref = bridge_module.get_thread_workspace_ref(target_thread) if target_thread else (_target_ref or "-")
    recent_offsets = bridge_module.snapshot_recent_session_offsets(
        limit=10,
        include_threads=[target_thread] if target_thread else None,
    )
    exit_code, output = run_ask_func(
        prompt,
        force_while_busy=True,
        wait=False,
        target_thread_id=target_thread_id,
        timeout_sec=get_steering_delivery_confirm_timeout_func(),
    )
    if exit_code == 0:
        return make_steering_prompt_result(
            exit_code,
            output,
            target_thread=target_thread,
            target_ref=target_ref,
            recent_offsets=recent_offsets,
        )

    if is_ipc_delivery_confirmation_timeout(output) and target_thread is not None:
        log_func(
            f"steering_ipc_delivery_pending exit={exit_code} target={target_thread_id or '-'} "
            "confirm_timeout=0.0 "
            f"output_len={format_log_text_len_func(output)}"
        )
        return make_steering_prompt_result(
            0,
            format_pending_ipc_delivery_output(output),
            target_thread=target_thread,
            target_ref=target_ref,
            recent_offsets=recent_offsets,
            delivery_pending=True,
        )

    delivered_thread = bridge_module.wait_for_prompt_delivery(
        recent_offsets,
        prompt,
        timeout_sec=get_steering_delivery_confirm_timeout_func(),
    )
    if delivered_thread is not None and (
        target_thread_id is None or delivered_thread.id == target_thread_id
    ):
        log_func(
            f"steering_nonzero_but_delivered exit={exit_code} target={target_thread_id or '-'} "
            f"delivered={delivered_thread.id}"
        )
        return make_steering_prompt_result(
            0,
            "\n\n".join(
                part
                for part in [
                    f"[delivery_verified] {bridge_module.get_thread_label(delivered_thread)}",
                    "Original transport returned a nonzero exit, but the steering prompt was recorded in Codex.",
                    output,
                ]
                if part
            ),
            target_thread=delivered_thread,
            target_ref=bridge_module.get_thread_workspace_ref(delivered_thread),
            recent_offsets=recent_offsets,
        )

    log_func(
        f"steering_failed exit={exit_code} target={target_thread_id or '-'} "
        f"output_len={format_log_text_len_func(output)}"
    )
    return SteeringPromptResult(exit_code, output, target_thread_id=target_thread_id, target_ref=target_ref)
