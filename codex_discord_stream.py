"""Stream relay helpers for Discord ask/watch output."""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path


class DiscordAskRelay:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        channel: object,
        target_thread_id: str | None,
        target_ref: str,
        *,
        quiet_notice_delay_sec: float,
        suppress_after_steering_since: float | None = None,
        send_timeout_blocks: bool = True,
        send_chunks_func,
        parse_interactive_notice_func,
        send_interactive_prompt_func,
        register_discord_relay_func,
        is_discord_relay_stale_func,
        had_steering_handoff_since_func,
        log_func,
        format_log_text_len_func,
    ) -> None:
        self.loop = loop
        self.channel = channel
        self.target_thread_id = target_thread_id
        self.target_ref = target_ref
        self.quiet_notice_delay_sec = quiet_notice_delay_sec
        self.suppress_after_steering_since = suppress_after_steering_since
        self.send_timeout_blocks = send_timeout_blocks
        self._send_chunks = send_chunks_func
        self._parse_interactive_notice = parse_interactive_notice_func
        self._send_interactive_prompt = send_interactive_prompt_func
        self._is_discord_relay_stale = is_discord_relay_stale_func
        self._had_steering_handoff_since = had_steering_handoff_since_func
        self._log = log_func
        self._format_log_text_len = format_log_text_len_func
        self.relay_generation = register_discord_relay_func(target_thread_id)
        self.mode: str | None = None
        self.block_lines: list[str] = []
        self.sent_live = False
        self.quiet_notice_sent = False
        self.suppressed_after_steering = False
        self.saw_final = False
        self.saw_aborted = False
        self.saw_timeout = False
        self._send_futures = []
        self._quiet_notice_future = None

    def _schedule_send(self, send_coro) -> None:
        self._cancel_quiet_notice()
        previous = self._send_futures[-1] if self._send_futures else None

        async def send_in_order() -> None:
            if previous is not None:
                try:
                    await asyncio.wrap_future(previous)
                except Exception:
                    pass
            await send_coro

        future = asyncio.run_coroutine_threadsafe(send_in_order(), self.loop)
        self._send_futures.append(future)

    def _send(self, text: str) -> None:
        self._schedule_send(self._send_chunks(self.channel, text))

    async def _send_quiet_notice_after_delay(self) -> None:
        await asyncio.sleep(max(0.0, self.quiet_notice_delay_sec))
        if (
            self.sent_live
            or self.quiet_notice_sent
            or self.saw_final
            or self.saw_aborted
            or self.saw_timeout
        ):
            return
        await self._send_chunks(
            self.channel,
            "\n".join(
                [
                    "Codex is still working.",
                    "",
                    "High-context threads can stay quiet while Codex compacts context before the next visible reply.",
                ]
            ),
        )
        self.quiet_notice_sent = True

    def _schedule_quiet_notice(self) -> None:
        if self.quiet_notice_delay_sec < 0:
            return
        current = self._quiet_notice_future
        if current is not None and not current.done():
            return
        self._quiet_notice_future = asyncio.run_coroutine_threadsafe(
            self._send_quiet_notice_after_delay(),
            self.loop,
        )

    def _cancel_quiet_notice(self) -> None:
        future = self._quiet_notice_future
        if future is not None and not future.done():
            future.cancel()

    def _should_suppress_for_steering(self) -> bool:
        if self._is_discord_relay_stale(self.target_thread_id, self.relay_generation):
            return True
        if self.suppress_after_steering_since is None:
            return False
        if self.mode != "final":
            return False
        return self._had_steering_handoff_since(
            self.target_thread_id,
            self.suppress_after_steering_since,
        )

    def _send_interactive_notice_if_detected(self, text: str) -> bool:
        state, prompt, options = self._parse_interactive_notice(text)
        if not state or not self.target_thread_id:
            return False
        self._schedule_send(
            self._send_interactive_prompt(
                self.channel,
                self.target_thread_id,
                self.target_ref,
                state,
                prompt,
                options,
            )
        )
        self.sent_live = True
        return True

    def _send_block(self) -> None:
        text = "\n".join(self.block_lines).strip()
        if not text:
            self.block_lines = []
            return
        if self._should_suppress_for_steering():
            self.suppressed_after_steering = True
            self._log(
                f"discord_relay_suppressed_after_steering target={self.target_thread_id or '-'} "
                f"mode={self.mode or '-'} text_len={self._format_log_text_len(text)}"
            )
            self.block_lines = []
            return
        if self.mode == "commentary":
            if not self._send_interactive_notice_if_detected(text):
                self._send(f"In progress\n\n{text}")
                self.sent_live = True
        elif self.mode == "final":
            if not self._send_interactive_notice_if_detected(text):
                self._send(text)
                self.sent_live = True
                self.saw_final = True
        elif self.mode == "timeout":
            self.saw_timeout = True
            if self.send_timeout_blocks:
                self._send(f"Timed out\n\n{text}")
                self.sent_live = True
        self.block_lines = []

    def feed_line(self, line: str) -> None:
        if line.startswith("[commentary]"):
            self._send_block()
            self.mode = "commentary"
            return
        if line.startswith("[final_answer]"):
            self._send_block()
            self.mode = "final"
            return
        if line.startswith("[timeout]"):
            self._send_block()
            self.mode = "timeout"
            self.saw_timeout = True
            return
        if line.startswith("[aborted]"):
            self._send_block()
            self.mode = None
            self.saw_aborted = True
            self._send("Aborted.")
            self.sent_live = True
            return
        if line.startswith("[ready]"):
            self._send_block()
            self.mode = None
            return
        if line.startswith("[waiting_for_final_answer]") or line.startswith("Use Ctrl+C"):
            if line.startswith("[waiting_for_final_answer]"):
                self._schedule_quiet_notice()
            return

        if self.mode in {"commentary", "final", "timeout"}:
            self.block_lines.append(line)
            return

        if line.startswith("target_thread:") or line.startswith("title:") or line.startswith("ui_name:") or line.startswith("cwd:"):
            return
        if line.startswith("ui_activation:") or line.startswith("sent_to_window:") or line.startswith("[delivery_verified]"):
            return
        if line.startswith("[background_watch_started]") or line.startswith("[background_watch_already_running]"):
            return
        if line.startswith("[wait_cancelled]"):
            return

    def finish(self) -> None:
        self._cancel_quiet_notice()
        self._send_block()
        quiet_future = self._quiet_notice_future
        if quiet_future is not None and quiet_future.done():
            if not quiet_future.cancelled():
                try:
                    quiet_future.result(timeout=0)
                except Exception:
                    self._log("discord_relay_quiet_notice_failed\n" + traceback.format_exc())
        for future in self._send_futures:
            try:
                future.result(timeout=30)
            except Exception:
                self._log("discord_relay_send_failed\n" + traceback.format_exc())


def build_stream_ask_argv(
    prompt: str,
    *,
    force_while_busy: bool = False,
    wait: bool = True,
    target_thread_id: str | None = None,
) -> list[str]:
    argv = [
        "ask",
        "--ipc",
        "--ipc-recover-ui",
        "--foreground",
        "--stream",
        "--include-commentary",
        "--timeout",
        "0",
    ]
    if target_thread_id:
        argv.extend(["--thread-id", target_thread_id])
    if force_while_busy:
        argv.append("--force-while-busy")
    if not wait:
        argv.append("--no-wait")
    argv.append(prompt)
    return argv


def ensure_ui_stream_flags(ui_argv: list[str]) -> list[str]:
    if "--stream" in ui_argv:
        return ui_argv
    result = list(ui_argv)
    result.insert(result.index("--timeout"), "--include-commentary")
    result.insert(result.index("--include-commentary"), "--stream")
    return result


def run_ask_stream(
    prompt: str,
    relay: DiscordAskRelay,
    *,
    force_while_busy: bool = False,
    wait: bool = True,
    target_thread_id: str | None = None,
    run_bridge_command_stream_func,
    should_retry_ask_with_ui_func,
    build_ui_ask_argv_func,
    ui_fallback_lock,
) -> tuple[int, str]:
    argv = build_stream_ask_argv(
        prompt,
        force_while_busy=force_while_busy,
        wait=wait,
        target_thread_id=target_thread_id,
    )
    exit_code, output = run_bridge_command_stream_func(argv, relay.feed_line)
    if should_retry_ask_with_ui_func(exit_code, output):
        relay.feed_line("[commentary]")
        relay.feed_line("IPC attach failed for this Codex thread. Retrying through the Codex UI.")
        relay.feed_line("[ready]")
        ui_argv = build_ui_ask_argv_func(
            prompt,
            target_thread_id=target_thread_id,
            force_while_busy=True,
            wait=wait,
        )
        ui_argv = ensure_ui_stream_flags(ui_argv)
        with ui_fallback_lock:
            exit_code, output = run_bridge_command_stream_func(ui_argv, relay.feed_line)
    relay.finish()
    return exit_code, output


def run_steering_watch_stream(
    steering_result: object,
    relay: DiscordAskRelay,
    *,
    timeout_sec: float = 0,
    watch_for_final_answer_func,
) -> tuple[int, str]:
    session_path = getattr(steering_result, "session_path", None)
    start_offset = getattr(steering_result, "start_offset", None)
    if not session_path or start_offset is None:
        relay.finish()
        return 0, ""

    relay.feed_line("[waiting_for_final_answer]")
    relay.feed_line("Use Ctrl+C to stop waiting after the prompt is sent.")
    output_lines = [
        "[waiting_for_final_answer]",
        "Use Ctrl+C to stop waiting after the prompt is sent.",
    ]

    def relay_stream_line(line: str) -> None:
        relay.feed_line(line)
        output_lines.append(line)

    try:
        result = watch_for_final_answer_func(
            session_path=Path(session_path),
            start_offset=start_offset,
            timeout_sec=timeout_sec,
            include_commentary=True,
            stream_live=True,
            stream_callback=relay_stream_line,
        )

        if result["final_answer"]:
            if result.get("final_streamed_live"):
                relay.feed_line("[ready]")
                output_lines.append("[ready]")
            else:
                final_lines = str(result["final_answer"]).splitlines()
                for line in ["[final_answer]", *final_lines, "", "[ready]"]:
                    relay.feed_line(line)
                    output_lines.append(line)
            relay.finish()
            return 0, "\n".join(output_lines).strip()

        if result["status"] == "aborted":
            relay.feed_line("[aborted]")
            output_lines.append("[aborted]")
            relay.finish()
            return 0, "\n".join(output_lines).strip()

        relay.feed_line("[timeout]")
        output_lines.append("[timeout]")
        commentary = result.get("commentary") or []
        if commentary and not result.get("streamed_live"):
            for line in str(commentary[-1]).splitlines():
                relay.feed_line(line)
                output_lines.append(line)
        relay.finish()
        return 2, "\n".join(output_lines).strip()
    except Exception:
        relay.finish()
        raise
