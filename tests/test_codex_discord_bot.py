import asyncio
import datetime
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import codex_discord_bot as bot
import codex_discord_commands as commands
import codex_desktop_bridge as bridge
import codex_windows_harness as harness


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.kwargs: list[dict[str, object]] = []

    async def send(self, content: str, view=None, **kwargs) -> None:
        self.messages.append(content if view is None else (content, view))
        self.kwargs.append(kwargs)


class FailingFollowup:
    def __init__(self, fail_after: int = 0) -> None:
        self.messages: list[object] = []
        self.fail_after = fail_after

    async def send(self, content: str, view=None, **kwargs) -> None:
        if len(self.messages) >= self.fail_after:
            raise RuntimeError("followup unavailable")
        self.messages.append(content if view is None else (content, view))


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deferred = False
        self.done = False
        self.defer_kwargs: list[dict[str, object]] = []

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append(content)
        self.done = True

    async def defer(self, thinking: bool = False, **kwargs) -> None:
        self.deferred = True
        self.done = True
        self.defer_kwargs.append({"thinking": thinking, **kwargs})

    def is_done(self) -> bool:
        return self.done


class AlreadyAcknowledgedFakeResponse(FakeResponse):
    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        raise RuntimeError("Interaction has already been acknowledged.")


class AlreadyRespondedFakeResponse(FakeResponse):
    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        raise RuntimeError("This interaction has already been responded to before.")


class FakeInteractionMessage:
    _next_id = 1000

    def __init__(self) -> None:
        self.id = FakeInteractionMessage._next_id
        FakeInteractionMessage._next_id += 1
        self.edits: list[object | None] = []
        self.components: list[object] = []

    async def edit(self, view=None) -> None:
        self.edits.append(view)
        if view is None:
            self.components = []


class FakeTyping:
    def __init__(self, target: "FakeTarget") -> None:
        self.target = target

    async def __aenter__(self) -> None:
        self.target.typing_events.append("enter")

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.target.typing_events.append("exit")


class FakeTarget:
    def __init__(self, channel_id: int = 222, parent_id: int | None = None) -> None:
        self.messages: list[tuple[str, object | None]] = []
        self.typing_events: list[str] = []
        self.id = channel_id
        self.parent_id = parent_id

    async def send(self, content: str, view=None) -> None:
        self.messages.append((content, view))

    def typing(self) -> FakeTyping:
        return FakeTyping(self)


class ReturningTarget(FakeTarget):
    def __init__(self, channel_id: int = 222, parent_id: int | None = None) -> None:
        super().__init__(channel_id=channel_id, parent_id=parent_id)
        self.sent_messages: list[FakeInteractionMessage] = []

    async def send(self, content: str, view=None) -> FakeInteractionMessage:
        self.messages.append((content, view))
        sent_message = FakeInteractionMessage()
        if view is not None:
            sent_message.components = [
                SimpleNamespace(
                    children=[
                        SimpleNamespace(custom_id=getattr(item, "custom_id", None))
                        for item in getattr(view, "children", [])
                    ]
                )
            ]
        self.sent_messages.append(sent_message)
        return sent_message


class ViewFailingTarget(FakeTarget):
    async def send(self, content: str, view=None) -> None:
        if view is not None:
            raise RuntimeError("view rejected")
        await super().send(content, view=view)


class FakeInteraction:
    def __init__(self, command_name: str = "help", channel_id: int = 12345) -> None:
        self.command = SimpleNamespace(name=command_name)
        self.channel_id = channel_id
        self.followup = FakeFollowup()
        self.response = FakeResponse()
        self.user = SimpleNamespace(id=242286902982606848)
        self.channel = None
        self.message = FakeInteractionMessage()
        self.type = bot.discord.InteractionType.application_command
        self.data: dict[str, object] = {}


class FakeMessage:
    def __init__(self, content: str = "", channel_id: int = 222, message_id: int | None = None) -> None:
        self.id = message_id
        self.channel = FakeTarget(channel_id=channel_id)
        self.author = SimpleNamespace(id=242286902982606848, bot=False)
        self.content = content
        self.raw_mentions: list[int] = []
        self.mentions: list[object] = []
        self.attachments: list[object] = []
        self.embeds: list[object] = []
        self.stickers: list[object] = []


class FakeBot:
    def __init__(self, *, allowed_user: bool = True, allowed_channel: bool = False) -> None:
        self.allowed_user = allowed_user
        self.allowed_channel = allowed_channel

    def is_allowed_user(self, user_id: int | None) -> bool:
        return self.allowed_user

    def is_allowed_channel(self, channel_id: int | None) -> bool:
        return self.allowed_channel

    def is_allowed_message_channel(self, channel) -> bool:
        return False


class EnvPatch:
    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value
        self.original: str | None = None

    def __enter__(self) -> None:
        self.original = os.environ.get(self.key)
        os.environ[self.key] = self.value

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.original is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.original


class DiscordBotHelperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._old_mirror_db_path = bot.MIRROR_DB_PATH
        self._old_discord_log_path = os.environ.get("CODEX_DISCORD_LOG_PATH")
        self._mirror_db_temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        bot.MIRROR_DB_PATH = Path(self._mirror_db_temp_dir.name) / "mirror.sqlite"
        os.environ["CODEX_DISCORD_LOG_PATH"] = str(
            Path(self._mirror_db_temp_dir.name) / "test_discord_bot.log"
        )
        bot.init_mirror_db()

    def tearDown(self) -> None:
        bot.MIRROR_DB_PATH = self._old_mirror_db_path
        if self._old_discord_log_path is None:
            os.environ.pop("CODEX_DISCORD_LOG_PATH", None)
        else:
            os.environ["CODEX_DISCORD_LOG_PATH"] = self._old_discord_log_path
        self._mirror_db_temp_dir.cleanup()

    def test_log_path_override_writes_to_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                bot.log_line("isolated_smoke_log")
                self.assertEqual(bot.get_log_path(), log_path)

            self.assertTrue(log_path.exists())
            self.assertIn("isolated_smoke_log", log_path.read_text(encoding="utf-8"))

    def test_discord_message_mark_persists_across_restart_after_processing(self) -> None:
        first_owner = SimpleNamespace(_processed_message_ids={})
        restarted_owner = SimpleNamespace(_processed_message_ids={})
        message = FakeMessage(content="please hook", message_id=123)
        other_message = FakeMessage(content="next hook", message_id=124)

        self.assertTrue(bot.claim_discord_message(first_owner, message))
        bot.mark_discord_message_processed(first_owner, message)
        self.assertFalse(bot.claim_discord_message(restarted_owner, message))
        self.assertTrue(bot.claim_discord_message(restarted_owner, other_message))

    def test_discord_message_claim_without_mark_still_blocks_restart_duplicate(self) -> None:
        first_owner = SimpleNamespace(_processed_message_ids={})
        restarted_owner = SimpleNamespace(_processed_message_ids={})
        message = FakeMessage(content="recover me", message_id=123)

        self.assertTrue(bot.claim_discord_message(first_owner, message))
        self.assertFalse(bot.claim_discord_message(restarted_owner, message))

    def test_mirrored_channel_id_authorizes_interaction_without_channel_object(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_threads (
                            codex_thread_id, project_key, thread_title,
                            discord_channel_id, discord_thread_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("thread-1", "project", "title", 111, 222, 1.0),
                    )
                interaction = FakeInteraction(channel_id=222)
                self.assertTrue(bot.check_interaction_allowed(FakeBot(), interaction))
            finally:
                bot.MIRROR_DB_PATH = old_db_path

    def test_interaction_user_denial_is_logged(self) -> None:
        interaction = FakeInteraction(command_name="ask", channel_id=222)
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                allowed = bot.check_interaction_allowed(
                    FakeBot(allowed_user=False),
                    interaction,
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertFalse(allowed)
        self.assertIn("slash_ignored command=ask reason=user_not_allowed", log_text)
        self.assertIn("channel=222", log_text)

    def test_interaction_channel_denial_is_logged(self) -> None:
        interaction = FakeInteraction(command_name="ask", channel_id=333)
        interaction.channel = None
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                allowed = bot.check_interaction_allowed(
                    FakeBot(allowed_user=True, allowed_channel=False),
                    interaction,
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertFalse(allowed)
        self.assertIn("slash_ignored command=ask reason=channel_not_allowed", log_text)
        self.assertIn("channel=333", log_text)

    def test_discord_thread_target_args_prefer_mapped_thread(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_threads (
                            codex_thread_id, project_key, thread_title,
                            discord_channel_id, discord_thread_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("thread-1", "project", "title", 111, 222, 1.0),
                    )

                self.assertEqual(
                    bot.resolve_discord_thread_target_args(222, None),
                    ["--thread-id", "thread-1"],
                )
            finally:
                bot.MIRROR_DB_PATH = old_db_path

    def test_bridge_stream_runs_in_subprocess_without_cross_thread_output(self) -> None:
        original_get_bridge_script_path = bot.get_bridge_script_path
        outputs: dict[str, tuple[int, str, list[str]]] = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "bridge_stream_fixture.py"
            script_path.write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "import sys",
                        "import time",
                        "name = sys.argv[1]",
                        "print(f'{name}:start', flush=True)",
                        "time.sleep(0.05)",
                        "print(f'{name}:end', flush=True)",
                    ]
                ),
                encoding="utf-8",
            )

            def worker(name: str) -> None:
                lines: list[str] = []
                exit_code, output = bot.run_bridge_command_stream([name], lines.append)
                outputs[name] = (exit_code, output, lines)

            try:
                bot.get_bridge_script_path = lambda: script_path
                threads = [
                    threading.Thread(target=worker, args=("a",)),
                    threading.Thread(target=worker, args=("b",)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            finally:
                bot.get_bridge_script_path = original_get_bridge_script_path

        self.assertEqual(outputs["a"], (0, "a:start\na:end", ["a:start", "a:end"]))
        self.assertEqual(outputs["b"], (0, "b:start\nb:end", ["b:start", "b:end"]))

    def test_bridge_stream_forces_utf8_child_output(self) -> None:
        original_get_bridge_script_path = bot.get_bridge_script_path
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "bridge_utf8_child.py"
            script_path.write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "import os",
                        "print(os.environ.get('PYTHONIOENCODING', '-'))",
                        "print(os.environ.get('PYTHONUTF8', '-'))",
                        "print(os.environ.get('PYTHONUNBUFFERED', '-'))",
                        "print('\\ud55c\\uae00 approval\\ud14c\\uc2a4\\ud2b8')",
                    ]
                ),
                encoding="utf-8",
            )
            lines: list[str] = []
            try:
                bot.get_bridge_script_path = lambda: script_path
                exit_code, output = bot.run_bridge_command_stream([], lines.append)
            finally:
                bot.get_bridge_script_path = original_get_bridge_script_path

        self.assertEqual(exit_code, 0)
        self.assertEqual(lines, ["utf-8", "1", "1", "\ud55c\uae00 approval\ud14c\uc2a4\ud2b8"])
        self.assertEqual(output, "\n".join(lines))

    def test_bridge_stream_delivers_child_lines_before_process_exit(self) -> None:
        original_get_bridge_script_path = bot.get_bridge_script_path
        start_seen = threading.Event()
        finished = threading.Event()
        lines: list[str] = []
        result: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "bridge_unbuffered_child.py"
            script_path.write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "import time",
                        "print('child:start')",
                        "time.sleep(2.0)",
                        "print('child:end')",
                    ]
                ),
                encoding="utf-8",
            )

            def on_line(line: str) -> None:
                lines.append(line)
                if line == "child:start":
                    start_seen.set()

            def worker() -> None:
                try:
                    exit_code, output = bot.run_bridge_command_stream([], on_line)
                    result["exit_code"] = exit_code
                    result["output"] = output
                finally:
                    finished.set()

            try:
                bot.get_bridge_script_path = lambda: script_path
                thread = threading.Thread(target=worker)
                thread.start()
                try:
                    self.assertTrue(start_seen.wait(timeout=1.0))
                    self.assertFalse(finished.is_set())
                finally:
                    thread.join(timeout=4.0)
            finally:
                bot.get_bridge_script_path = original_get_bridge_script_path

        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get("exit_code"), 0)
        self.assertEqual(lines, ["child:start", "child:end"])
        self.assertEqual(result.get("output"), "child:start\nchild:end")

    def test_discord_busy_detection_accepts_global_bridge_busy_error(self) -> None:
        output = (
            "ERROR: A Codex reply is still in progress. You can `open` other threads, "
            "but `ask` is blocked until it finishes. Busy thread(s): repo:1. "
            "Pass --force-while-busy to override."
        )

        self.assertTrue(bot.is_selected_thread_busy_error(1, output))
        self.assertTrue(bot.is_global_codex_busy_error(1, output))
        self.assertFalse(bot.is_selected_thread_busy_error(0, output))
        self.assertFalse(bot.is_global_codex_busy_error(0, output))

    def test_discord_busy_detection_treats_ipc_timeout_as_thread_retryable(self) -> None:
        output = (
            "target_thread: 019e90d5-bd7a-7781-9979-e886f63781a7\n"
            "ui_activation: ipc-thread-follower-start-turn\n"
            "ERROR: Timed out waiting for IPC data from \\\\.\\pipe\\codex-ipc."
        )

        self.assertTrue(bot.is_selected_thread_busy_error(1, output))
        self.assertFalse(bot.is_global_codex_busy_error(1, output))

    def test_bridge_ipc_ask_respects_global_busy_guard(self) -> None:
        original_choose_thread = bridge.choose_thread
        original_get_busy_threads = bridge.get_busy_threads
        original_get_thread_ui_name = bridge.get_thread_ui_name
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                target_thread = bridge.ThreadInfo(
                    id="target-thread",
                    title="Target",
                    cwd=str(temp_dir),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=0,
                )
                busy_thread = bridge.ThreadInfo(
                    id="busy-thread",
                    title="Busy",
                    cwd=str(temp_dir),
                    updated_at=2,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=1,
                )

                bridge.choose_thread = lambda thread_id=None, cwd=None: target_thread
                bridge.get_busy_threads = lambda limit=50: [busy_thread]
                bridge.get_thread_ui_name = lambda thread_id, thread=None: None
                args = SimpleNamespace(
                    thread_id="target-thread",
                    cwd=None,
                    prompt="qa prompt",
                    dry_run=False,
                    force_while_busy=False,
                    ipc=True,
                )

                with self.assertRaisesRegex(RuntimeError, "A Codex reply is still in progress"):
                    with redirect_stdout(io.StringIO()):
                        bridge.command_ask(args)
        finally:
            bridge.choose_thread = original_choose_thread
            bridge.get_busy_threads = original_get_busy_threads
            bridge.get_thread_ui_name = original_get_thread_ui_name

    def test_bridge_ipc_ask_keeps_selected_thread_busy_steerable(self) -> None:
        original_choose_thread = bridge.choose_thread
        original_get_busy_threads = bridge.get_busy_threads
        original_get_thread_ui_name = bridge.get_thread_ui_name
        original_is_thread_busy = bridge.is_thread_busy
        original_get_thread_busy_state = bridge.get_thread_busy_state
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                target_thread = bridge.ThreadInfo(
                    id="target-thread",
                    title="Target",
                    cwd=str(temp_dir),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=0,
                )

                bridge.choose_thread = lambda thread_id=None, cwd=None: target_thread
                bridge.get_busy_threads = lambda limit=50: [target_thread]
                bridge.get_thread_ui_name = lambda thread_id, thread=None: None
                bridge.is_thread_busy = lambda path: True
                bridge.get_thread_busy_state = lambda thread, allow_resume=True: "busy"
                args = SimpleNamespace(
                    thread_id="target-thread",
                    cwd=None,
                    prompt="qa prompt",
                    dry_run=False,
                    force_while_busy=False,
                    ipc=True,
                )

                with self.assertRaisesRegex(RuntimeError, "selected thread is still busy"):
                    with redirect_stdout(io.StringIO()):
                        bridge.command_ask(args)
        finally:
            bridge.choose_thread = original_choose_thread
            bridge.get_busy_threads = original_get_busy_threads
            bridge.get_thread_ui_name = original_get_thread_ui_name
            bridge.is_thread_busy = original_is_thread_busy
            bridge.get_thread_busy_state = original_get_thread_busy_state

    def test_bridge_ipc_ask_prefers_target_busy_over_other_global_busy(self) -> None:
        original_choose_thread = bridge.choose_thread
        original_get_busy_threads = bridge.get_busy_threads
        original_get_thread_ui_name = bridge.get_thread_ui_name
        original_is_thread_busy = bridge.is_thread_busy
        original_get_thread_busy_state = bridge.get_thread_busy_state
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                target_thread = bridge.ThreadInfo(
                    id="target-thread",
                    title="Target",
                    cwd=str(temp_dir),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=0,
                )
                other_thread = bridge.ThreadInfo(
                    id="other-thread",
                    title="Other",
                    cwd=str(temp_dir),
                    updated_at=2,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=1,
                )

                bridge.choose_thread = lambda thread_id=None, cwd=None: target_thread
                bridge.get_busy_threads = lambda limit=50: [other_thread]
                bridge.get_thread_ui_name = lambda thread_id, thread=None: None
                bridge.is_thread_busy = lambda path: True
                bridge.get_thread_busy_state = lambda thread, allow_resume=True: "busy"
                args = SimpleNamespace(
                    thread_id="target-thread",
                    cwd=None,
                    prompt="qa prompt",
                    dry_run=False,
                    force_while_busy=False,
                    ipc=True,
                )

                with self.assertRaisesRegex(RuntimeError, "selected thread is still busy"):
                    with redirect_stdout(io.StringIO()):
                        bridge.command_ask(args)
        finally:
            bridge.choose_thread = original_choose_thread
            bridge.get_busy_threads = original_get_busy_threads
            bridge.get_thread_ui_name = original_get_thread_ui_name
            bridge.is_thread_busy = original_is_thread_busy
            bridge.get_thread_busy_state = original_get_thread_busy_state

    def test_bridge_ipc_ask_allows_sidecar_idle_orphan_without_force(self) -> None:
        original_choose_thread = bridge.choose_thread
        original_get_busy_threads = bridge.get_busy_threads
        original_get_thread_ui_name = bridge.get_thread_ui_name
        original_is_thread_busy = bridge.is_thread_busy
        original_get_thread_busy_state = bridge.get_thread_busy_state
        original_start_turn_via_ipc = bridge.start_turn_via_ipc
        original_wait_for_prompt_delivery = bridge.wait_for_prompt_delivery
        original_watch_for_final_answer = bridge.watch_for_final_answer
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("old", encoding="utf-8")
                target_thread = bridge.ThreadInfo(
                    id="target-thread",
                    title="Target",
                    cwd=str(temp_dir),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=0,
                )

                bridge.choose_thread = lambda thread_id=None, cwd=None: target_thread
                bridge.get_busy_threads = lambda limit=50: []
                bridge.get_thread_ui_name = lambda thread_id, thread=None: None
                bridge.is_thread_busy = lambda path: True
                bridge.get_thread_busy_state = lambda thread, allow_resume=True: "idle"
                bridge.start_turn_via_ipc = lambda thread, prompt, timeout_sec=10.0, allow_ui_recovery=False: {
                    "owner_client_id": "client-1",
                    "turn_id": "turn-1",
                }
                bridge.wait_for_prompt_delivery = lambda session_offsets, prompt, timeout_sec=4.0: target_thread
                bridge.watch_for_final_answer = lambda **kwargs: {
                    "commentary": [],
                    "final_answer": "done",
                    "status": "ready",
                    "streamed_live": False,
                    "final_streamed_live": False,
                }
                args = SimpleNamespace(
                    thread_id="target-thread",
                    cwd=None,
                    prompt="qa prompt",
                    dry_run=False,
                    force_while_busy=False,
                    ipc=True,
                    ipc_recover_ui=False,
                    background=False,
                    wait=True,
                    timeout=30.0,
                    include_commentary=False,
                    stream=False,
                )
                stdout = io.StringIO()

                with redirect_stdout(stdout):
                    exit_code = bridge.command_ask(args)

            self.assertEqual(exit_code, 0)
            self.assertIn("[final_answer]\ndone", stdout.getvalue())
        finally:
            bridge.choose_thread = original_choose_thread
            bridge.get_busy_threads = original_get_busy_threads
            bridge.get_thread_ui_name = original_get_thread_ui_name
            bridge.is_thread_busy = original_is_thread_busy
            bridge.get_thread_busy_state = original_get_thread_busy_state
            bridge.start_turn_via_ipc = original_start_turn_via_ipc
            bridge.wait_for_prompt_delivery = original_wait_for_prompt_delivery
            bridge.watch_for_final_answer = original_watch_for_final_answer

    def test_bridge_ipc_ask_pending_delivery_keeps_waiting_for_final(self) -> None:
        original_choose_thread = bridge.choose_thread
        original_get_busy_threads = bridge.get_busy_threads
        original_get_thread_ui_name = bridge.get_thread_ui_name
        original_is_thread_busy = bridge.is_thread_busy
        original_start_turn_via_ipc = bridge.start_turn_via_ipc
        original_wait_for_prompt_delivery = bridge.wait_for_prompt_delivery
        original_watch_for_final_answer = bridge.watch_for_final_answer
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                target_thread = bridge.ThreadInfo(
                    id="target-thread",
                    title="Target",
                    cwd=str(temp_dir),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=0,
                )
                watched: list[tuple[Path, int]] = []

                bridge.choose_thread = lambda thread_id=None, cwd=None: target_thread
                bridge.get_busy_threads = lambda limit=50: []
                bridge.get_thread_ui_name = lambda thread_id, thread=None: None
                bridge.is_thread_busy = lambda path: False
                bridge.start_turn_via_ipc = lambda thread, prompt, timeout_sec=10.0, allow_ui_recovery=False: {
                    "owner_client_id": "client-1",
                    "turn_id": "turn-1",
                }
                bridge.wait_for_prompt_delivery = lambda session_offsets, prompt, timeout_sec=4.0: None

                def fake_watch_for_final_answer(
                    *,
                    session_path: Path,
                    start_offset: int,
                    timeout_sec: float,
                    include_commentary: bool,
                    stream_live: bool = False,
                    stream_label: str = "",
                    stream_callback=None,
                ) -> dict:
                    watched.append((session_path, start_offset))
                    return {
                        "commentary": [],
                        "final_answer": "done",
                        "status": "ready",
                        "streamed_live": False,
                        "final_streamed_live": False,
                    }

                bridge.watch_for_final_answer = fake_watch_for_final_answer
                args = SimpleNamespace(
                    thread_id="target-thread",
                    cwd=None,
                    prompt="qa prompt",
                    dry_run=False,
                    force_while_busy=False,
                    ipc=True,
                    ipc_recover_ui=False,
                    background=False,
                    wait=True,
                    timeout=30.0,
                    include_commentary=False,
                    stream=False,
                )
                stdout = io.StringIO()

                with redirect_stdout(stdout):
                    exit_code = bridge.command_ask(args)

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertEqual(watched, [(session_path, 0)])
            self.assertIn("[delivery_pending]", output)
            self.assertIn("Continuing to watch for the next Codex reply.", output)
            self.assertIn("[final_answer]\ndone", output)
            self.assertNotIn("Prompt delivery could not be confirmed", output)
        finally:
            bridge.choose_thread = original_choose_thread
            bridge.get_busy_threads = original_get_busy_threads
            bridge.get_thread_ui_name = original_get_thread_ui_name
            bridge.is_thread_busy = original_is_thread_busy
            bridge.start_turn_via_ipc = original_start_turn_via_ipc
            bridge.wait_for_prompt_delivery = original_wait_for_prompt_delivery
            bridge.watch_for_final_answer = original_watch_for_final_answer

    def test_bridge_busy_ignores_stale_orphan_task_started_after_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_complete"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            old_time = time.time() - 120.0
            os.utime(session_path, (old_time, old_time))

            with EnvPatch("CODEX_BRIDGE_ORPHAN_TASK_STARTED_GRACE_SECONDS", "60"):
                self.assertFalse(bridge.is_thread_busy(session_path))

    def test_bridge_busy_keeps_fresh_orphan_task_started_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "task_complete"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )

            with EnvPatch("CODEX_BRIDGE_ORPHAN_TASK_STARTED_GRACE_SECONDS", "60"):
                self.assertTrue(bridge.is_thread_busy(session_path))

    def test_bridge_busy_ignores_old_unfinished_noninteractive_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "run"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "working",
                    },
                },
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            old_time = time.time() - 3600.0
            os.utime(session_path, (old_time, old_time))

            with EnvPatch("CODEX_BRIDGE_STALE_BUSY_SESSION_SECONDS", "1800"):
                self.assertFalse(bridge.is_thread_busy(session_path))

    def test_bridge_busy_keeps_old_pending_interactive_session_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-1",
                        "arguments": "{}",
                    },
                },
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            old_time = time.time() - 3600.0
            os.utime(session_path, (old_time, old_time))

            with EnvPatch("CODEX_BRIDGE_STALE_BUSY_SESSION_SECONDS", "1800"):
                self.assertTrue(bridge.is_thread_busy(session_path))

    def test_bridge_busy_state_trusts_sidecar_idle_for_noninteractive_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "stuck"}},
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            old_time = time.time() - 120.0
            os.utime(session_path, (old_time, old_time))
            thread = bridge.ThreadInfo(
                id="thread-1",
                title="title",
                cwd=str(temp_dir),
                updated_at=1,
                rollout_path=str(session_path),
                model="gpt",
                reasoning_effort="high",
                tokens_used=1,
            )
            client = SimpleNamespace(
                read_thread=lambda thread_id, include_turns=False: {
                    "thread": {"status": {"type": "idle"}}
                }
            )

            with EnvPatch("CODEX_BRIDGE_ORPHAN_TASK_STARTED_GRACE_SECONDS", "60"):
                self.assertTrue(bridge.is_thread_busy(session_path))
                self.assertEqual(bridge.get_thread_busy_state(thread, client=client), "idle")

    def test_bridge_busy_state_keeps_interactive_even_if_sidecar_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-1",
                        "arguments": "{}",
                    },
                },
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            thread = bridge.ThreadInfo(
                id="thread-1",
                title="title",
                cwd=str(temp_dir),
                updated_at=1,
                rollout_path=str(session_path),
                model="gpt",
                reasoning_effort="high",
                tokens_used=1,
            )
            client = SimpleNamespace(
                read_thread=lambda thread_id, include_turns=False: {
                    "thread": {"status": {"type": "idle"}}
                }
            )

            self.assertEqual(bridge.get_thread_busy_state(thread, client=client), "waiting-input")

    def test_get_busy_threads_excludes_sidecar_idle_orphan(self) -> None:
        original_load_recent_threads = bridge.load_recent_threads
        original_sidecar = bridge.CodexAppServerSidecar
        try:
            class FakeSidecar:
                def read_thread(self, thread_id, include_turns=False):
                    return {"thread": {"status": {"type": "idle"}}}

                def close(self):
                    pass

            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                events = [
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "stuck"}},
                ]
                session_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n",
                    encoding="utf-8",
                )
                old_time = time.time() - 120.0
                os.utime(session_path, (old_time, old_time))
                thread = bridge.ThreadInfo(
                    id="thread-1",
                    title="title",
                    cwd=str(temp_dir),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=1,
                )
                bridge.load_recent_threads = lambda limit=50: [thread]
                bridge.CodexAppServerSidecar = FakeSidecar

                with EnvPatch("CODEX_BRIDGE_ORPHAN_TASK_STARTED_GRACE_SECONDS", "60"):
                    self.assertEqual(bridge.get_busy_threads(), [])
        finally:
            bridge.load_recent_threads = original_load_recent_threads
            bridge.CodexAppServerSidecar = original_sidecar

    def test_windows_harness_preflight_separates_target_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = bridge.ThreadInfo(
                id="target-thread",
                title="Target",
                cwd=str(temp_dir),
                updated_at=10,
                rollout_path=str(Path(temp_dir) / "session.jsonl"),
                model="gpt",
                reasoning_effort="high",
                tokens_used=0,
            )
            fake_bridge = SimpleNamespace(
                choose_thread=lambda thread_id, cwd: thread,
                get_thread_workspace_ref=lambda item: "repo:1",
                get_thread_label=lambda item: "repo:1",
                get_thread_busy_state=lambda item, allow_resume=True: "busy",
                get_busy_threads=lambda limit=50: [],
            )

            preflight = harness.preflight_ask("target-thread", bridge_module=fake_bridge, now=1.0)

        self.assertFalse(preflight.accepted)
        self.assertEqual(preflight.route, "target_busy")
        self.assertTrue(preflight.can_steer)
        self.assertEqual(preflight.not_sent_reason, "target_busy")
        self.assertEqual(preflight.global_busy_threads, [])

    def test_windows_harness_preflight_accepts_target_idle_with_other_busy_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = bridge.ThreadInfo(
                id="target-thread",
                title="Target",
                cwd=str(temp_dir),
                updated_at=10,
                rollout_path=str(Path(temp_dir) / "target.jsonl"),
                model="gpt",
                reasoning_effort="high",
                tokens_used=0,
            )
            other = bridge.ThreadInfo(
                id="other-thread",
                title="Other",
                cwd=str(temp_dir),
                updated_at=11,
                rollout_path=str(Path(temp_dir) / "other.jsonl"),
                model="gpt",
                reasoning_effort="high",
                tokens_used=0,
            )

            def fake_state(thread, allow_resume=True):
                return "busy" if thread.id == "other-thread" else "idle"

            fake_bridge = SimpleNamespace(
                choose_thread=lambda thread_id, cwd: target,
                get_thread_workspace_ref=lambda item: "repo:1" if item.id == "target-thread" else "repo:2",
                get_thread_label=lambda item: "repo:1" if item.id == "target-thread" else "repo:2",
                get_thread_busy_state=fake_state,
                get_busy_threads=lambda limit=50: [other],
            )

            preflight = harness.preflight_ask("target-thread", bridge_module=fake_bridge, now=1.0)

        self.assertTrue(preflight.accepted)
        self.assertEqual(preflight.route, "ask")
        self.assertFalse(preflight.can_steer)
        self.assertEqual(preflight.not_sent_reason, "")
        self.assertEqual([thread.ref for thread in preflight.global_busy_threads], ["repo:2"])

    def test_discord_global_busy_message_accepts_harness_thread_snapshot(self) -> None:
        busy = harness.HarnessThread(
            id="other-thread",
            ref="repo:2",
            title="Other",
            cwd="C:/repo",
            state="busy",
            updated_at=1,
        )

        message = bot.build_global_busy_message([busy])

        self.assertIn("Codex app is already processing a turn.", message)
        self.assertIn("Codex app transport state", message)
        self.assertIn("Busy thread(s): repo:2", message)

    def test_windows_harness_runtime_reports_desktop_available(self) -> None:
        fake_bridge = SimpleNamespace(
            discover_codex_desktop_executable=lambda: (Path("C:/Codex/Codex.exe"), "fake")
        )

        with mock.patch.object(harness, "probe_codex_cli", return_value=("codex.exe", "ok")):
            status = harness.get_runtime_status(bridge_module=fake_bridge)

        self.assertEqual(status.platform, "windows-local")
        self.assertEqual(status.codex_cli_status, "ok")
        self.assertEqual(status.codex_desktop_status, "available")

    def test_windows_harness_runtime_reports_desktop_not_found(self) -> None:
        fake_bridge = SimpleNamespace(discover_codex_desktop_executable=lambda: (None, ""))

        with mock.patch.object(harness, "probe_codex_cli", return_value=("", "not_found")):
            status = harness.get_runtime_status(bridge_module=fake_bridge)

        self.assertEqual(status.codex_cli_status, "not_found")
        self.assertEqual(status.codex_desktop_status, "not_found")

    def test_windows_harness_runtime_reports_desktop_unavailable_on_error(self) -> None:
        def fail_discovery():
            raise RuntimeError("probe failed")

        fake_bridge = SimpleNamespace(discover_codex_desktop_executable=fail_discovery)

        with mock.patch.object(harness, "probe_codex_cli", return_value=("", "permission_denied")):
            status = harness.get_runtime_status(bridge_module=fake_bridge)

        self.assertEqual(status.codex_cli_status, "permission_denied")
        self.assertEqual(status.codex_desktop_status, "unavailable")

    def test_choice_views_claim_once_and_disable_buttons(self) -> None:
        approval_view = bot.ApprovalView("thread-1")
        approval_custom_ids = {
            getattr(item, "label", ""): getattr(item, "custom_id", "")
            for item in approval_view.children
        }
        self.assertEqual(approval_custom_ids["Approve"], "codex_approval:thread-1:1")
        self.assertEqual(approval_custom_ids["Approve session"], "codex_approval:thread-1:2")
        self.assertEqual(approval_custom_ids["Reject"], "codex_approval:thread-1:3")
        self.assertEqual(approval_custom_ids["Cancel"], "codex_approval:thread-1:cancel")

        input_view = bot.InputChoiceView("thread-1", [("1", "First"), ("2", "Second")])
        input_custom_ids = {
            getattr(item, "label", ""): getattr(item, "custom_id", "")
            for item in input_view.children
        }
        self.assertEqual(bot.parse_input_choice_custom_id(input_custom_ids["First"]), ("thread-1", "1"))
        self.assertEqual(bot.parse_input_choice_custom_id(input_custom_ids["Second"]), ("thread-1", "2"))
        self.assertTrue(input_view.claim())
        self.assertFalse(input_view.claim())
        self.assertTrue(all(getattr(item, "disabled", False) for item in input_view.children))

        message = SimpleNamespace(author=SimpleNamespace(id=1), channel=None)
        busy_view = bot.BusyChoiceView(message, "prompt", target_thread_id="thread-1")
        self.assertTrue(busy_view.claim())
        self.assertFalse(busy_view.claim())
        self.assertTrue(all(getattr(item, "disabled", False) for item in busy_view.children))

    async def test_busy_choice_denied_user_is_logged(self) -> None:
        message = FakeMessage()
        view = bot.BusyChoiceView(message, "please steer", target_thread_id="thread-1")
        interaction = FakeInteraction(command_name="ask", channel_id=222)
        interaction.user = SimpleNamespace(id=999)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                allowed = await view.interaction_check(interaction)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertFalse(allowed)
        self.assertEqual(interaction.response.messages, ["Only the original sender can choose this."])
        self.assertIn("busy_choice_denied user=999 owner=242286902982606848 target=thread-1", log_text)

    async def test_busy_choice_duplicate_click_is_logged(self) -> None:
        message = FakeMessage()
        view = bot.BusyChoiceView(message, "please steer", target_thread_id="thread-1")
        self.assertTrue(view.claim())
        button = next(item for item in view.children if getattr(item, "label", "") == "Queue next")
        interaction = FakeInteraction(command_name="ask", channel_id=222)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await button.callback(interaction)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(interaction.response.messages, ["This busy choice was already handled."])
        self.assertIn("busy_choice_already_handled action=queue_next", log_text)
        self.assertIn("target=thread-1", log_text)

    async def test_busy_choice_steer_defers_before_persistent_claim(self) -> None:
        original_claim_busy_choice_record = bot.claim_busy_choice_record
        observed_deferred: list[bool] = []
        message = FakeMessage()
        view = bot.BusyChoiceView(
            message,
            "please steer",
            target_thread_id="thread-1",
            choice_id="0123456789abcdef01234567",
        )
        button = next(item for item in view.children if getattr(item, "label", "") == "Steer now")
        interaction = FakeInteraction(command_name="ask", channel_id=222)

        try:
            def fake_claim_busy_choice_record(choice_id: str) -> bool:
                observed_deferred.append(interaction.response.deferred)
                return False

            bot.claim_busy_choice_record = fake_claim_busy_choice_record
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.claim_busy_choice_record = original_claim_busy_choice_record

        self.assertEqual(observed_deferred, [True])
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.followup.messages, ["This busy choice was already handled."])
        self.assertIn("busy_choice_already_handled action=steer_now", log_text)

    def test_fit_single_message_truncates_to_discord_limit(self) -> None:
        fitted = bot.fit_single_message("x" * 4100)
        self.assertLessEqual(len(fitted), bot.DISCORD_MAX_LEN)
        self.assertTrue(fitted.endswith("[truncated for Discord]"))

    def test_format_discord_command_label_truncates_and_flattens(self) -> None:
        label = bot.format_discord_command_label("x" * 100 + "\nboom")
        self.assertLessEqual(len(label), 80)
        self.assertNotIn("\n", label)
        self.assertTrue(label.endswith("..."))

    async def test_unhandled_component_interaction_gets_stale_button_notice(self) -> None:
        interaction = FakeInteraction(command_name="-", channel_id=222)
        interaction.type = bot.discord.InteractionType.component
        interaction.data = {"custom_id": "codex-busy-choice-old-button"}

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(
            interaction.response.messages,
            ["This Discord button is no longer active. Send the message again to get fresh controls."],
        )
        self.assertEqual(interaction.message.edits, [None])
        self.assertIn("component_interaction_unhandled_reported", log_text)
        self.assertIn("component_message_components_cleared context=unhandled_component", log_text)
        self.assertIn("custom_id=codex-busy-choice-old-button", log_text)

    async def test_persistent_approval_handles_restart_stale_view(self) -> None:
        original_submit = bot.submit_approval_reply
        submitted: list[tuple[str, str]] = []
        try:
            def fake_submit(target_thread_id, answer):
                submitted.append((target_thread_id, answer))
                return 0, "approved"

            bot.submit_approval_reply = fake_submit
            interaction = FakeInteraction(command_name="-", channel_id=222)
            interaction.type = bot.discord.InteractionType.component
            interaction.data = {"custom_id": "codex_approval:thread-1:2"}

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.submit_approval_reply = original_submit

        self.assertEqual(submitted, [("thread-1", "2")])
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.message.edits, [None])
        self.assertEqual(interaction.followup.messages, ["Approval submitted\n\napproved"])
        self.assertIn("approval_persistent user=242286902982606848 target=thread-1 answer_len=1", log_text)
        self.assertIn("approval_persistent_done exit=0 target=thread-1 answer_len=1", log_text)
        self.assertIn("component_message_components_cleared context=approval_persistent", log_text)
        self.assertNotIn("approved session", log_text)

    async def test_persistent_input_choice_handles_restart_stale_view(self) -> None:
        original_submit = bot.submit_input_reply
        submitted: list[tuple[str, str]] = []
        self.assertIsNone(bot.format_input_choice_custom_id("thread-1", "first choice"))
        custom_id = bot.format_input_choice_custom_id("thread-1", "choice-1")
        self.assertIsNotNone(custom_id)
        try:
            def fake_submit(target_thread_id, value):
                submitted.append((target_thread_id, value))
                return 0, "answered"

            bot.submit_input_reply = fake_submit
            interaction = FakeInteraction(command_name="-", channel_id=222)
            interaction.type = bot.discord.InteractionType.component
            interaction.data = {"custom_id": custom_id}

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.submit_input_reply = original_submit

        self.assertEqual(submitted, [("thread-1", "choice-1")])
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.message.edits, [None])
        self.assertEqual(interaction.followup.messages, ["Input submitted\n\nanswered"])
        self.assertIn("input_choice_persistent user=242286902982606848 target=thread-1 value_len=8", log_text)
        self.assertIn("input_choice_persistent_done exit=0 target=thread-1 value_len=8", log_text)
        self.assertIn("component_message_components_cleared context=input_choice_persistent", log_text)
        self.assertNotIn("choice-1", log_text)

    async def test_persistent_approval_replay_is_single_use_per_message(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        submitted: list[tuple[str, str]] = []

        def fake_submit(target_thread_id, answer):
            submitted.append((target_thread_id, answer))
            return 0, "approved"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                shared_message = FakeInteractionMessage()
                first = FakeInteraction(command_name="-", channel_id=222)
                first.type = bot.discord.InteractionType.component
                first.message = shared_message
                first.data = {"custom_id": "codex_approval:thread-1:2"}
                second = FakeInteraction(command_name="-", channel_id=222)
                second.type = bot.discord.InteractionType.component
                second.message = shared_message
                second.data = {"custom_id": "codex_approval:thread-1:3"}

                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    first_handled = await bot.handle_persistent_approval_interaction(
                        first,
                        "codex_approval:thread-1:2",
                        approval_submitter=fake_submit,
                    )
                    second_handled = await bot.handle_persistent_approval_interaction(
                        second,
                        "codex_approval:thread-1:3",
                        approval_submitter=fake_submit,
                    )
                log_text = log_path.read_text(encoding="utf-8")
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertTrue(first_handled)
        self.assertTrue(second_handled)
        self.assertEqual(submitted, [("thread-1", "2")])
        self.assertEqual(second.response.messages, ["This approval choice was already handled."])
        self.assertIn("approval_persistent_already_handled user=242286902982606848 target=thread-1", log_text)
        self.assertIn("component_message_components_cleared context=approval_persistent_already_handled", log_text)

    async def test_persistent_input_choice_replay_is_single_use_per_message(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        submitted: list[tuple[str, str]] = []
        first_custom_id = bot.format_input_choice_custom_id("thread-1", "choice-1")
        second_custom_id = bot.format_input_choice_custom_id("thread-1", "choice-2")
        self.assertIsNotNone(first_custom_id)
        self.assertIsNotNone(second_custom_id)

        def fake_submit(target_thread_id, value):
            submitted.append((target_thread_id, value))
            return 0, "answered"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                shared_message = FakeInteractionMessage()
                first = FakeInteraction(command_name="-", channel_id=222)
                first.type = bot.discord.InteractionType.component
                first.message = shared_message
                first.data = {"custom_id": first_custom_id}
                second = FakeInteraction(command_name="-", channel_id=222)
                second.type = bot.discord.InteractionType.component
                second.message = shared_message
                second.data = {"custom_id": second_custom_id}

                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    first_handled = await bot.handle_persistent_input_choice_interaction(
                        first,
                        str(first_custom_id),
                        input_submitter=fake_submit,
                    )
                    second_handled = await bot.handle_persistent_input_choice_interaction(
                        second,
                        str(second_custom_id),
                        input_submitter=fake_submit,
                    )
                log_text = log_path.read_text(encoding="utf-8")
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertTrue(first_handled)
        self.assertTrue(second_handled)
        self.assertEqual(submitted, [("thread-1", "choice-1")])
        self.assertEqual(second.response.messages, ["This input choice was already handled."])
        self.assertIn("input_choice_persistent_already_handled user=242286902982606848 target=thread-1", log_text)
        self.assertIn("component_message_components_cleared context=input_choice_persistent_already_handled", log_text)

    async def test_unhandled_component_interaction_skips_already_handled_response(self) -> None:
        interaction = FakeInteraction(command_name="-", channel_id=222)
        interaction.type = bot.discord.InteractionType.component
        interaction.data = {"custom_id": "codex-busy-choice-active-button"}
        await interaction.response.defer(thinking=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
            log_exists = log_path.exists()

        self.assertEqual(interaction.response.messages, [])
        self.assertFalse(log_exists)

    async def test_busy_choice_view_persists_custom_ids(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                message = FakeMessage()
                view = bot.make_busy_choice_view(
                    message,
                    "please steer",
                    target_thread_id="thread-1",
                    allow_steer=True,
                )
                custom_ids = {
                    getattr(item, "label", ""): getattr(item, "custom_id", "")
                    for item in view.children
                }
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertRegex(custom_ids["Steer now"], r"^codex_busy:[0-9a-f]{24}:steer$")
        self.assertRegex(custom_ids["Queue next"], r"^codex_busy:[0-9a-f]{24}:queue$")
        self.assertRegex(custom_ids["Ignore"], r"^codex_busy:[0-9a-f]{24}:ignore$")

    async def test_busy_choice_view_has_single_action_button_each(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                view = bot.make_busy_choice_view(
                    FakeMessage(),
                    "please steer",
                    target_thread_id="thread-1",
                    allow_steer=True,
                )
                labels = [getattr(item, "label", "") for item in view.children]
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertEqual(labels.count("Steer now"), 1)
        self.assertEqual(labels.count("Queue next"), 1)
        self.assertEqual(labels.count("Ignore"), 1)

    async def test_persistent_busy_choice_ignore_handles_restart_stale_view(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                message = FakeMessage()
                view = bot.make_busy_choice_view(
                    message,
                    "please ignore",
                    target_thread_id="thread-1",
                    allow_steer=True,
                )
                ignore_id = next(
                    getattr(item, "custom_id", "")
                    for item in view.children
                    if getattr(item, "label", "") == "Ignore"
                )
                choice_id, _action = bot.parse_busy_choice_custom_id(ignore_id)
                log_path = Path(temp_dir) / "discord-smoke.log"
                interaction = FakeInteraction(command_name="-", channel_id=222)
                interaction.type = bot.discord.InteractionType.component
                interaction.data = {"custom_id": ignore_id}

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
                log_text = log_path.read_text(encoding="utf-8")
                remaining = bot.get_busy_choice_record(choice_id)
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertEqual(interaction.response.messages, ["Ignored."])
        self.assertEqual(interaction.message.edits, [None])
        self.assertIsNone(remaining)
        self.assertIn("busy_choice_persistent_ignore", log_text)
        self.assertIn("component_message_components_cleared context=busy_choice_ignore", log_text)
        self.assertNotIn("please ignore", log_text)

    async def test_persistent_busy_choice_missing_record_clears_buttons(self) -> None:
        interaction = FakeInteraction(command_name="-", channel_id=222)
        interaction.type = bot.discord.InteractionType.component
        interaction.data = {"custom_id": "codex_busy:0123456789abcdef01234567:steer"}

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                handled = await bot.handle_persistent_busy_choice_interaction(
                    interaction,
                    "codex_busy:0123456789abcdef01234567:steer",
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertTrue(handled)
        self.assertEqual(
            interaction.response.messages,
            ["This Discord button is no longer active. Send the message again to get fresh controls."],
        )
        self.assertEqual(interaction.message.edits, [None])
        self.assertIn("busy_choice_persistent_missing action=steer", log_text)
        self.assertIn("component_message_components_cleared context=busy_choice_missing", log_text)

    async def test_persistent_busy_choice_already_acknowledged_logs_concise_marker(self) -> None:
        interaction = FakeInteraction(command_name="-", channel_id=222)
        interaction.type = bot.discord.InteractionType.component
        interaction.data = {"custom_id": "codex_busy:0123456789abcdef01234567:steer"}
        interaction.response = AlreadyAcknowledgedFakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(interaction.message.edits, [None])
        self.assertIn("component_interaction_persistent_handler_already_acknowledged", log_text)
        self.assertIn("custom_id=codex_busy:0123456789abcdef01234567:steer", log_text)
        self.assertNotIn("Traceback", log_text)

    async def test_persistent_busy_choice_already_responded_logs_concise_marker(self) -> None:
        interaction = FakeInteraction(command_name="-", channel_id=222)
        interaction.type = bot.discord.InteractionType.component
        interaction.data = {"custom_id": "codex_busy:0123456789abcdef01234567:steer"}
        interaction.response = AlreadyRespondedFakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(interaction.message.edits, [None])
        self.assertIn("component_interaction_persistent_handler_already_acknowledged", log_text)
        self.assertIn("custom_id=codex_busy:0123456789abcdef01234567:steer", log_text)
        self.assertNotIn("Traceback", log_text)

    async def test_persistent_busy_choice_defers_before_channel_resolution(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        original_resolve = bot.resolve_interaction_channel
        original_busy_state = bot.get_busy_state_for_thread
        original_enqueue = bot.enqueue_thread_ask
        observed_deferred: list[bool] = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                message = FakeMessage()
                view = bot.make_busy_choice_view(
                    message,
                    "please queue",
                    target_thread_id="thread-1",
                    allow_steer=True,
                )
                queue_id = next(
                    getattr(item, "custom_id", "")
                    for item in view.children
                    if getattr(item, "label", "") == "Queue next"
                )

                async def fake_resolve(interaction, channel_id):
                    observed_deferred.append(interaction.response.deferred)
                    return FakeTarget(channel_id=channel_id)

                async def fake_enqueue(*args, **kwargs):
                    return 1

                bot.resolve_interaction_channel = fake_resolve
                bot.get_busy_state_for_thread = lambda target_thread_id: ("busy", target_thread_id, "project:1")
                bot.enqueue_thread_ask = fake_enqueue
                interaction = FakeInteraction(command_name="-", channel_id=222)
                interaction.type = bot.discord.InteractionType.component
                interaction.data = {"custom_id": queue_id}
                log_path = Path(temp_dir) / "discord-smoke.log"

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
                log_text = log_path.read_text(encoding="utf-8")
            finally:
                bot.resolve_interaction_channel = original_resolve
                bot.get_busy_state_for_thread = original_busy_state
                bot.enqueue_thread_ask = original_enqueue
                bot.MIRROR_DB_PATH = old_db_path

        self.assertEqual(observed_deferred, [True])
        self.assertEqual(interaction.message.edits, [None])
        self.assertIn("component_message_components_cleared context=busy_choice_queue", log_text)

    async def test_discord_button_qa_exercises_button_handlers(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                fake_bot = SimpleNamespace()
                message = FakeMessage()
                message.channel = ReturningTarget(channel_id=222)
                log_path = Path(temp_dir) / "discord-smoke.log"

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    output = await bot.run_discord_button_qa(fake_bot, message)
                log_text = log_path.read_text(encoding="utf-8")
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertIn("ignore: ok", output)
        self.assertIn("claimed_record: ok", output)
        self.assertIn("missing_record: ok", output)
        self.assertIn("stale_cleanup: ok", output)
        self.assertIn("steer_success: ok", output)
        self.assertIn("approval_persistent: ok", output)
        self.assertIn("input_choice_persistent: ok", output)
        self.assertIn("result: ok", output)
        self.assertEqual(len(message.channel.sent_messages), 8)
        self.assertEqual(
            [content for content, _view in message.channel.messages].count(
                "Discord steering submitted.\nmessage: QA button steer success smoke"
            ),
            1,
        )
        self.assertEqual(sum(1 for sent in message.channel.sent_messages if sent.edits == [None]), 7)
        self.assertEqual(sum(1 for sent in message.channel.sent_messages if sent.edits == []), 1)
        self.assertIn("button_qa_done channel=222 user=242286902982606848 result=ok", log_text)

    async def test_persistent_busy_choice_denied_does_not_claim_record(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                message = FakeMessage()
                view = bot.make_busy_choice_view(
                    message,
                    "please queue",
                    target_thread_id="thread-1",
                    allow_steer=True,
                )
                queue_id = next(
                    getattr(item, "custom_id", "")
                    for item in view.children
                    if getattr(item, "label", "") == "Queue next"
                )
                choice_id, _action = bot.parse_busy_choice_custom_id(queue_id)
                log_path = Path(temp_dir) / "discord-smoke.log"
                interaction = FakeInteraction(command_name="-", channel_id=222)
                interaction.type = bot.discord.InteractionType.component
                interaction.data = {"custom_id": queue_id}
                interaction.user = SimpleNamespace(id=999)

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.report_unhandled_component_interaction(interaction, delay_sec=0)
                log_text = log_path.read_text(encoding="utf-8")
                remaining = bot.get_busy_choice_record(choice_id)
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertEqual(interaction.response.messages, ["Only the original sender can choose this."])
        self.assertIsNotNone(remaining)
        self.assertIn("busy_choice_persistent_denied", log_text)
        self.assertNotIn("please queue", log_text)

    async def test_persistent_steer_not_allowed_does_not_claim_queue_choice(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        original_busy_state = bot.get_busy_state_for_thread
        original_enqueue = bot.enqueue_thread_ask
        try:
            enqueue_calls: list[tuple[str, str | None, bool]] = []

            async def fake_enqueue(channel, prompt, target_thread_id, *, queued=False, **kwargs):
                enqueue_calls.append((prompt, target_thread_id, queued))
                return 1

            bot.get_busy_state_for_thread = lambda target_thread_id: ("busy", target_thread_id, "project:1")
            bot.enqueue_thread_ask = fake_enqueue

            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
                message = FakeMessage()
                view = bot.make_busy_choice_view(
                    message,
                    "please queue",
                    target_thread_id="thread-1",
                    allow_steer=False,
                )
                custom_ids = {
                    getattr(item, "label", ""): getattr(item, "custom_id", "")
                    for item in view.children
                    if isinstance(item, bot.discord.ui.Button)
                }
                choice_id, _action = bot.parse_busy_choice_custom_id(custom_ids["Queue next"])
                log_path = Path(temp_dir) / "discord-smoke.log"

                steer_interaction = FakeInteraction(command_name="-", channel_id=222)
                steer_interaction.type = bot.discord.InteractionType.component
                steer_interaction.data = {"custom_id": custom_ids["Steer now"]}
                steer_interaction.channel = message.channel

                queue_interaction = FakeInteraction(command_name="-", channel_id=222)
                queue_interaction.type = bot.discord.InteractionType.component
                queue_interaction.data = {"custom_id": custom_ids["Queue next"]}
                queue_interaction.channel = message.channel

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    steer_handled = await bot.handle_persistent_busy_choice_interaction(
                        steer_interaction,
                        custom_ids["Steer now"],
                    )
                    remaining_after_steer = bot.get_busy_choice_record(choice_id)
                    queue_handled = await bot.handle_persistent_busy_choice_interaction(
                        queue_interaction,
                        custom_ids["Queue next"],
                    )
                    remaining_after_queue = bot.get_busy_choice_record(choice_id)
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.MIRROR_DB_PATH = old_db_path
            bot.get_busy_state_for_thread = original_busy_state
            bot.enqueue_thread_ask = original_enqueue

        self.assertTrue(steer_handled)
        self.assertEqual(
            steer_interaction.response.messages,
            ["This message targets a different Codex thread. Queue it instead."],
        )
        self.assertIsNotNone(remaining_after_steer)
        self.assertTrue(queue_handled)
        self.assertEqual(queue_interaction.followup.messages, ["Queued at position 1."])
        self.assertEqual(enqueue_calls, [("please queue", "thread-1", True)])
        self.assertIsNone(remaining_after_queue)
        self.assertIn("busy_choice_persistent_steer_rejected", log_text)
        self.assertIn("busy_choice_persistent_queue", log_text)

    async def test_persistent_steer_duplicate_interaction_runs_once(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
                message = FakeMessage()
                view = bot.make_busy_choice_view(
                    message,
                    "please steer",
                    target_thread_id="thread-1",
                    allow_steer=True,
                )
                steer_id = next(
                    getattr(item, "custom_id", "")
                    for item in view.children
                    if getattr(item, "label", "") == "Steer now"
                )
                calls: list[tuple[str, str | None]] = []
                streamed: list[tuple[object, str | None]] = []

                def fake_steer(prompt: str, target_thread_id: str | None) -> bot.SteeringPromptResult:
                    calls.append((prompt, target_thread_id))
                    return bot.SteeringPromptResult(
                        0,
                        "[qa_delivery_verified]",
                        target_thread_id=target_thread_id,
                        target_ref=target_thread_id or "-",
                        session_path="qa-session.jsonl",
                        start_offset=0,
                    )

                async def fake_stream(channel, steering_result, target_thread_id: str | None) -> bool:
                    streamed.append((steering_result, target_thread_id))
                    return True

                first = FakeInteraction(command_name="-", channel_id=222)
                first.type = bot.discord.InteractionType.component
                first.data = {"custom_id": steer_id}
                first.channel = message.channel
                second = FakeInteraction(command_name="-", channel_id=222)
                second.type = bot.discord.InteractionType.component
                second.data = {"custom_id": steer_id}
                second.channel = message.channel
                log_path = Path(temp_dir) / "discord-smoke.log"

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    first_handled = await bot.handle_persistent_busy_choice_interaction(
                        first,
                        steer_id,
                        steering_runner=fake_steer,
                        steering_streamer=fake_stream,
                    )
                    second_handled = await bot.handle_persistent_busy_choice_interaction(
                        second,
                        steer_id,
                        steering_runner=fake_steer,
                        steering_streamer=fake_stream,
                    )
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.MIRROR_DB_PATH = old_db_path

        self.assertTrue(first_handled)
        self.assertTrue(second_handled)
        self.assertEqual(calls, [("please steer", "thread-1")])
        self.assertEqual(len(streamed), 1)
        self.assertEqual(first.response.defer_kwargs, [{"thinking": True, "ephemeral": True}])
        self.assertTrue(first.followup.messages[0].startswith("Steering sent"))
        self.assertEqual(message.channel.messages, [("Discord steering submitted.\nmessage: please steer", None)])
        self.assertEqual(
            second.response.messages,
            ["This Discord button is no longer active. Send the message again to get fresh controls."],
        )
        self.assertIn("steering_start_ack_sent target=thread-1", log_text)
        self.assertIn("busy_choice_persistent_steer_done exit=0", log_text)
        self.assertIn("busy_choice_persistent_missing action=steer", log_text)

    def test_cleanup_expired_busy_choices_returns_deleted_count(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.executemany(
                        """
                        INSERT INTO busy_choices (
                            choice_id, owner_user_id, channel_id, target_thread_id, prompt,
                            allow_steer, created_at, expires_at, claimed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            ("a" * 24, 1, 222, "thread-1", "expired", 1, 1.0, 2.0, None),
                            ("b" * 24, 1, 222, "thread-1", "claimed", 1, 1.0, 20.0, 3.0),
                            ("c" * 24, 1, 222, "thread-1", "active", 1, 1.0, 20.0, None),
                        ],
                    )

                deleted = bot.cleanup_expired_busy_choices(now=10.0)
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    remaining = conn.execute("SELECT choice_id FROM busy_choices").fetchall()
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertEqual(deleted, 2)
        self.assertEqual(remaining, [("c" * 24,)])

    def test_cleanup_expired_persistent_component_claims_returns_deleted_count(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO persistent_component_claims (claim_key, created_at, expires_at)
                        VALUES ('expired-a', 1, 2), ('expired-b', 1, 3), ('live', 1, 20)
                        """
                    )
                deleted = bot.cleanup_expired_persistent_component_claims(now=10.0)
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    remaining = conn.execute("SELECT claim_key FROM persistent_component_claims").fetchall()
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertEqual(deleted, 2)
        self.assertEqual(remaining, [("live",)])

    async def test_clear_stale_busy_choice_message_components_removes_missing_record(self) -> None:
        message = SimpleNamespace(
            id=123,
            channel=SimpleNamespace(id=222),
            components=[
                SimpleNamespace(
                    children=[
                        SimpleNamespace(custom_id="codex_busy:0123456789abcdef01234567:steer")
                    ]
                )
            ],
            edited_views=[],
        )

        async def fake_edit(view=None) -> None:
            message.edited_views.append(view)

        message.edit = fake_edit

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                cleared = await bot.clear_stale_busy_choice_message_components(message)

        self.assertTrue(cleared)
        self.assertEqual(message.edited_views, [None])

    async def test_clear_stale_busy_choice_message_components_keeps_active_record(self) -> None:
        choice_id = "0123456789abcdef01234567"
        with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO busy_choices (
                    choice_id, owner_user_id, channel_id, target_thread_id, prompt,
                    allow_steer, created_at, expires_at, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (choice_id, 1, 222, "thread-1", "active", 1, 1.0, time.time() + 60.0),
            )
        message = SimpleNamespace(
            id=124,
            channel=SimpleNamespace(id=222),
            components=[
                SimpleNamespace(children=[SimpleNamespace(custom_id=f"codex_busy:{choice_id}:steer")])
            ],
            edited_views=[],
        )

        async def fake_edit(view=None) -> None:
            message.edited_views.append(view)

        message.edit = fake_edit

        cleared = await bot.clear_stale_busy_choice_message_components(message)

        self.assertFalse(cleared)
        self.assertEqual(message.edited_views, [])

    async def test_on_ready_cleans_up_expired_busy_choices(self) -> None:
        original_cleanup = bot.cleanup_expired_busy_choices
        original_claim_cleanup = bot.cleanup_expired_persistent_component_claims
        calls: list[str] = []
        try:
            bot.cleanup_expired_busy_choices = lambda: 3
            bot.cleanup_expired_persistent_component_claims = lambda: 2

            async def fake_startup_diagnostics() -> None:
                calls.append("startup")

            fake_client = SimpleNamespace(
                user="bot#0001",
                guilds=[],
                startup_channel_id=None,
                log_startup_diagnostics=fake_startup_diagnostics,
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.CodexDiscordBot.on_ready(fake_client)
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.cleanup_expired_busy_choices = original_cleanup
            bot.cleanup_expired_persistent_component_claims = original_claim_cleanup

        self.assertEqual(calls, ["startup"])
        self.assertIn("busy_choice_cleanup_deleted count=3", log_text)
        self.assertIn("persistent_component_claim_cleanup_deleted count=2", log_text)

    def test_startup_probe_targets_include_allowed_and_mirror_channels(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_projects (
                            project_key, project_name, discord_channel_id, updated_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        ("c:/taxlab", "taxlab", 333, 20.0),
                    )
                    conn.execute(
                        """
                        INSERT INTO mirror_threads (
                            codex_thread_id, project_key, thread_title,
                            discord_channel_id, discord_thread_id, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("thread-1", "c:/taxlab", "title", 333, 444, 30.0),
                    )
                targets = bot.get_startup_probe_targets({111, 222}, 111)
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertEqual(
            targets,
            [
                ("startup", 111),
                ("allowed", 222),
                ("mirror_project", 333),
                ("mirror_thread", 444),
            ],
        )

    def test_discord_doctor_message_includes_adapter_diagnostics(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO persistent_component_claims (claim_key, created_at, expires_at)
                        VALUES ('doctor-active', 1, 9999999999), ('doctor-stale', 1, 2)
                        """
                    )
                fake_bot = SimpleNamespace(
                    enable_prefix_commands=True,
                    intents=SimpleNamespace(message_content=True),
                    _enable_debug_events=True,
                    allowed_channel_ids={222},
                    allowed_user_ids=set(),
                    startup_channel_id=222,
                    history_poll_seconds=15.0,
                    history_poll_bootstrap_lookback_seconds=120.0,
                    _history_poll_bootstrap_after="2026-06-03T06:21:10+00:00",
                    _history_poll_task=SimpleNamespace(done=lambda: False),
                    _history_poll_last_at="2026-06-03T06:23:10+00:00",
                    _history_poll_primed_channels={111, 222},
                    _slash_sync_status="ok",
                    _slash_sync_last_at="2026-06-03T06:23:07+00:00",
                    _slash_sync_commands="ask,doctor,new",
                )
                log_path = Path(temp_dir) / "discord-smoke.log"
                log_path.write_text(
                    "\n".join(
                        [
                            "[2026-06-03 13:59:57] ready user=codex#1234 guilds=1",
                            "[2026-06-03 13:59:58] socket_message_create channel=222 tracked=True source=client_channel_cache guild=1 author=3 bot=True content_len=49",
                            "[2026-06-03 13:59:59] socket_message_create channel=222 tracked=True source=client_channel_cache guild=1 author=3 bot=True content_len=49",
                            "[2026-06-03 14:00:00] socket_message_create channel=222 tracked=True source=client_channel_cache guild=1 author=2 bot=False content_len=12",
                            "[2026-06-03 14:00:01] message chat=222 user=2 prefix=False runner_busy=False codex_busy=idle target_source=mirror target=thread-1 text=sensitive prompt",
                            "[2026-06-03 14:00:02] busy_choice_sent reason=late_busy_failure target=thread-1 prompt_len=16",
                            "[2026-06-03 14:00:03] slash_ask_dispatch command=ask channel=222 user=2 target_source=mirror target=thread-1 prompt_len=9",
                            "[2026-06-03 14:00:04] slash_response_sent command=doctor title='Doctor' exit=0 chunks=1",
                            "[2026-06-03 14:00:05] socket_interaction_create channel=222 guild=1 user=2 type=2 command=ask",
                            "[2026-06-03 14:00:06] interaction_received type=application_command command=ask custom_id=- channel=222 user=2",
                            "[2026-06-03 14:00:07] interaction_received type=component command=- custom_id=codex_busy:abcd:queue channel=222 user=2",
                            "[2026-06-03 14:00:08] component_interaction_unhandled_reported custom_id=codex_busy:abcd:queue channel=222",
                            "[2026-06-03 14:00:09] button_qa_done channel=222 user=2 result=ok",
                            "[2026-06-03 14:00:10] steer_now_done exit=0 target=thread-1 elapsed_sec=6.12 output_len=42",
                        ]
                    ),
                    encoding="utf-8",
                )
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)), EnvPatch(
                    "DISCORD_ENABLE_QA_COMMANDS",
                    "1",
                ):
                    output = bot.build_discord_doctor_message(fake_bot, 222)
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertIn("Discord adapter diagnostics", output)
        self.assertIn("channel_id: 222", output)
        self.assertIn("message_content_enabled: True", output)
        self.assertIn("intent_message_content: True", output)
        self.assertIn("raw_debug_events: True", output)
        self.assertIn("qa_commands_enabled: True", output)
        self.assertIn("history_poll_seconds: 15.0", output)
        self.assertIn("history_poll_bootstrap_lookback_seconds: 120.0", output)
        self.assertIn("history_poll_bootstrap_after: 2026-06-03T06:21:10+00:00", output)
        self.assertIn("history_poll_alive: True", output)
        self.assertIn("history_poll_last_at: 2026-06-03T06:23:10+00:00", output)
        self.assertIn("history_poll_primed_channels: 2", output)
        self.assertIn("slash_sync_status: ok", output)
        self.assertIn("slash_sync_last_at: 2026-06-03T06:23:07+00:00", output)
        self.assertIn("slash_sync_commands: ask,doctor,new", output)
        self.assertIn("allowed_channels: 222", output)
        self.assertIn("last_ready_at: 2026-06-03 13:59:57", output)
        self.assertIn("last_gateway_event_at: 2026-06-03 14:00:05", output)
        self.assertIn("last_raw_interaction_at: 2026-06-03 14:00:05", output)
        self.assertIn("last_interaction_at: 2026-06-03 14:00:07", output)
        self.assertIn("last_component_at: 2026-06-03 14:00:08", output)
        self.assertIn("last_user_or_control_hook_at: 2026-06-03 14:00:08", output)
        self.assertIn("last_button_qa_at: 2026-06-03 14:00:09", output)
        self.assertIn("last_button_qa_result: ok", output)
        self.assertIn("persistent_component_claims_active: 1", output)
        self.assertIn("persistent_component_claims_stale: 1", output)
        self.assertIn("last_steering_button_at: 2026-06-03 14:00:10", output)
        self.assertIn("last_steering_button_exit: 0", output)
        self.assertIn("last_steering_button_elapsed_sec: 6.12", output)
        self.assertIn("Mirror check", output)
        self.assertIn("Expected live log sequence:", output)
        self.assertIn("Recent user/control hook events:", output)
        self.assertIn("Recent hook events:", output)
        self.assertIn("message_routed channel=222", output)
        self.assertIn("busy_choice_event reason=late_busy_failure", output)
        self.assertIn("slash_ask_dispatch channel=222 command=ask", output)
        self.assertIn("slash_response_sent channel=- command=doctor exit=0", output)
        self.assertIn("raw_interaction channel=222 type=2 command=ask", output)
        self.assertIn("interaction_received channel=222 type=component command=-", output)
        self.assertIn("component_event channel=222 custom_id=codex_busy:abcd:queue", output)
        user_section = output.split("Recent user/control hook events:", 1)[1].split("Recent hook events:", 1)[0]
        self.assertNotIn("bot=True", user_section)
        self.assertNotIn("sensitive prompt", output)

    async def test_discord_channel_history_sanitizes_message_content(self) -> None:
        class FakeHistoryChannel:
            def history(self, *, limit: int):
                async def iterator():
                    yield SimpleNamespace(
                        created_at=datetime.datetime(2026, 6, 3, 15, 12, tzinfo=datetime.timezone.utc),
                        author=SimpleNamespace(bot=False),
                        content="sensitive prompt",
                        type=SimpleNamespace(name="default"),
                    )

                return iterator()

        output = "\n".join(await bot.build_discord_channel_history_lines(FakeHistoryChannel()))

        self.assertIn("Recent channel history:", output)
        self.assertIn("2026-06-03T15:12:00+00:00 bot=False content_len=16 type=default", output)
        self.assertNotIn("sensitive prompt", output)

    async def test_discord_tracked_target_history_sanitizes_message_content(self) -> None:
        class FakeHistoryChannel:
            def __init__(self, messages) -> None:
                self.messages = messages

            def history(self, *, limit: int):
                async def iterator():
                    for message in self.messages[:limit]:
                        yield message

                return iterator()

        user_message = SimpleNamespace(
            created_at=datetime.datetime(2026, 6, 3, 15, 12, tzinfo=datetime.timezone.utc),
            author=SimpleNamespace(id=242, bot=False),
            content="sensitive prompt",
            type=SimpleNamespace(name="default"),
        )
        bot_message = SimpleNamespace(
            created_at=datetime.datetime(2026, 6, 3, 15, 13, tzinfo=datetime.timezone.utc),
            author=SimpleNamespace(id=151, bot=True),
            content="bot startup",
            type=SimpleNamespace(name="default"),
        )

        class FakeBot:
            allowed_channel_ids = {222}
            startup_channel_id = 111

            def get_cached_channel_or_thread(self, channel_id: int):
                channels = {
                    111: FakeHistoryChannel([bot_message]),
                    222: FakeHistoryChannel([bot_message, user_message]),
                }
                return channels.get(channel_id), "fake_cache"

        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                output = "\n".join(await bot.build_discord_tracked_target_user_history_lines(FakeBot()))
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertIn("Recent tracked target user history:", output)
        self.assertIn("startup channel=111 source=fake_cache latest_user=-", output)
        self.assertIn(
            "allowed channel=222 source=fake_cache "
            "latest_user_at=2026-06-03T15:12:00+00:00 user=242 content_len=16 type=default",
            output,
        )
        self.assertNotIn("sensitive prompt", output)

    async def test_history_poll_primes_then_processes_new_user_message_once(self) -> None:
        class FakeHistoryChannel(FakeTarget):
            def __init__(self) -> None:
                super().__init__(channel_id=333)
                self.history_messages: list[FakeMessage] = []

            def history(self, *, limit: int):
                async def iterator():
                    for message in self.history_messages[:limit]:
                        yield message

                return iterator()

        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_plain_ask = bot.handle_plain_ask
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        handled: list[tuple[str, str | None]] = []
        channel = FakeHistoryChannel()
        old_message = FakeMessage(content="old", channel_id=333, message_id=100)
        new_message = FakeMessage(content="please hook", channel_id=333, message_id=101)
        old_message.channel = channel
        new_message.channel = channel
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fake_handle_plain_ask(message, prompt, *, target_thread_id=None):
                handled.append((prompt, target_thread_id))

            bot.is_thread_runner_busy = runner_idle
            bot.handle_plain_ask = fake_handle_plain_ask

            async def process_message(message, *, source):
                await bot.CodexDiscordBot.process_discord_message(client, message, source=source)

            client = SimpleNamespace(
                _processed_message_ids={},
                _history_poll_primed_channels=set(),
                enable_prefix_commands=True,
                get_cached_channel_or_thread=lambda channel_id: (channel, "test_cache"),
                fetch_channel=lambda channel_id: (_ for _ in ()).throw(AssertionError("fetch not expected")),
                is_allowed_message_channel=lambda message_channel: True,
                is_allowed_user=lambda user_id: True,
                process_discord_message=process_message,
            )
            channel.history_messages = [old_message]

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.CodexDiscordBot.poll_history_channel(client, "allowed", 333)
                    await bot.CodexDiscordBot.poll_history_channel(client, "allowed", 333)
                    channel.history_messages = [new_message, old_message]
                    await bot.CodexDiscordBot.poll_history_channel(client, "allowed", 333)
                    await bot.CodexDiscordBot.on_message(client, new_message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(handled, [("please hook", "thread-1")])
            self.assertIn("history_poll_primed label=allowed channel=333", log_text)
            self.assertIn("history_poll_message channel=333", log_text)
            self.assertIn("message_received chat=333", log_text)
            self.assertIn("source=history_poll", log_text)
            self.assertIn("duplicate_message_skipped source=gateway chat=333 message=101", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_plain_ask = original_handle_plain_ask
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_history_poll_first_prime_processes_bootstrap_user_messages(self) -> None:
        class FakeHistoryChannel(FakeTarget):
            def __init__(self) -> None:
                super().__init__(channel_id=333)
                self.history_messages: list[FakeMessage] = []

            def history(self, *, limit: int):
                async def iterator():
                    for message in self.history_messages[:limit]:
                        yield message

                return iterator()

        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_plain_ask = bot.handle_plain_ask
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        handled: list[tuple[str, str | None]] = []
        channel = FakeHistoryChannel()
        cutoff = datetime.datetime(2026, 6, 3, 15, 0, tzinfo=datetime.timezone.utc)
        old_message = FakeMessage(content="old", channel_id=333, message_id=100)
        old_message.created_at = cutoff - datetime.timedelta(seconds=1)
        fresh_message = FakeMessage(content="bootstrap hook", channel_id=333, message_id=101)
        fresh_message.created_at = cutoff + datetime.timedelta(seconds=1)
        old_message.channel = channel
        fresh_message.channel = channel
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fake_handle_plain_ask(message, prompt, *, target_thread_id=None):
                handled.append((prompt, target_thread_id))

            bot.is_thread_runner_busy = runner_idle
            bot.handle_plain_ask = fake_handle_plain_ask

            async def process_message(message, *, source):
                await bot.CodexDiscordBot.process_discord_message(client, message, source=source)

            client = SimpleNamespace(
                _processed_message_ids={},
                _history_poll_primed_channels=set(),
                _history_poll_bootstrap_after=cutoff,
                enable_prefix_commands=True,
                get_cached_channel_or_thread=lambda channel_id: (channel, "test_cache"),
                fetch_channel=lambda channel_id: (_ for _ in ()).throw(AssertionError("fetch not expected")),
                is_allowed_message_channel=lambda message_channel: True,
                is_allowed_user=lambda user_id: True,
                process_discord_message=process_message,
            )
            channel.history_messages = [fresh_message, old_message]

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.CodexDiscordBot.poll_history_channel(client, "allowed", 333)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(handled, [("bootstrap hook", "thread-1")])
            self.assertIn("history_poll_primed label=allowed channel=333", log_text)
            self.assertIn("bootstrap_user_messages=1", log_text)
            self.assertIn("history_poll_message channel=333", log_text)
            self.assertIn("source=history_poll", log_text)
            self.assertNotIn("old", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_plain_ask = original_handle_plain_ask
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_history_poll_loop_continues_after_cycle_error(self) -> None:
        original_get_targets = bot.get_startup_probe_targets
        original_sleep = bot.asyncio.sleep
        calls: list[str] = []
        sleeps = 0

        def fake_get_targets(allowed_channel_ids, startup_channel_id, *, limit=50):
            calls.append("targets")
            if len(calls) == 1:
                raise RuntimeError("temporary db error")
            return [("allowed", 333)]

        async def fake_sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps >= 2:
                raise asyncio.CancelledError

        async def fake_poll(label, channel_id):
            calls.append(f"poll:{label}:{channel_id}")

        client = SimpleNamespace(
            is_closed=lambda: False,
            allowed_channel_ids={333},
            startup_channel_id=None,
            history_poll_seconds=0.01,
            _history_poll_last_at="-",
            poll_history_channel=fake_poll,
        )

        try:
            bot.get_startup_probe_targets = fake_get_targets
            bot.asyncio.sleep = fake_sleep
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with self.assertRaises(asyncio.CancelledError):
                        await bot.CodexDiscordBot.history_poll_loop(client)
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.get_startup_probe_targets = original_get_targets
            bot.asyncio.sleep = original_sleep

        self.assertEqual(calls, ["targets", "targets", "poll:allowed:333"])
        self.assertIn("history_poll_cycle_failed", log_text)
        self.assertIn("temporary db error", log_text)

    async def test_socket_message_create_logs_tracked_without_content(self) -> None:
        fake_client = SimpleNamespace(
            is_allowed_channel=lambda channel_id: channel_id == 222,
            is_allowed_message_channel=lambda channel: getattr(channel, "id", None) == 222,
            get_cached_channel_or_thread=lambda channel_id: (
                (SimpleNamespace(id=222), "test_cache") if channel_id == 222 else (None, "-")
            ),
        )
        fake_client.format_socket_interaction_user = (
            lambda data: bot.CodexDiscordBot.format_socket_interaction_user(fake_client, data)
        )
        fake_client.is_tracked_socket_message_channel = (
            lambda channel_id: bot.CodexDiscordBot.is_tracked_socket_message_channel(fake_client, channel_id)
        )
        async def fake_log_socket_payload(payload):
            await bot.CodexDiscordBot.log_socket_payload(fake_client, payload)

        fake_client.log_socket_payload = fake_log_socket_payload

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            payload = {
                "t": "MESSAGE_CREATE",
                "d": {
                    "channel_id": "222",
                    "guild_id": "111",
                    "content": "sensitive prompt",
                    "author": {"id": "999", "bot": False},
                },
            }
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.CodexDiscordBot.on_socket_response(fake_client, payload)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertIn("socket_message_create channel=222 tracked=True", log_text)
        self.assertIn("source=test_cache", log_text)
        self.assertIn("content_len=16", log_text)
        self.assertNotIn("sensitive prompt", log_text)

    async def test_socket_raw_receive_dispatches_gateway_payload(self) -> None:
        fake_client = SimpleNamespace(
            is_allowed_channel=lambda channel_id: channel_id == 222,
            is_allowed_message_channel=lambda channel: False,
            get_cached_channel_or_thread=lambda channel_id: (None, "-"),
        )
        fake_client.format_socket_interaction_user = (
            lambda data: bot.CodexDiscordBot.format_socket_interaction_user(fake_client, data)
        )
        fake_client.is_tracked_socket_message_channel = (
            lambda channel_id: bot.CodexDiscordBot.is_tracked_socket_message_channel(fake_client, channel_id)
        )
        async def fake_log_socket_payload(payload):
            await bot.CodexDiscordBot.log_socket_payload(fake_client, payload)

        fake_client.log_socket_payload = fake_log_socket_payload

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            payload = {
                "t": "MESSAGE_CREATE",
                "d": {
                    "channel_id": "222",
                    "guild_id": "111",
                    "content": "raw message",
                    "author": {"id": "999", "bot": False},
                },
            }
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.CodexDiscordBot.on_socket_raw_receive(fake_client, json.dumps(payload))
            log_text = log_path.read_text(encoding="utf-8")

        self.assertIn("socket_message_create channel=222 tracked=True", log_text)
        self.assertNotIn("raw message", log_text)

    async def test_socket_event_logging_dedupes_raw_and_response_hooks(self) -> None:
        fake_client = SimpleNamespace(
            is_allowed_channel=lambda channel_id: channel_id == 222,
            is_allowed_message_channel=lambda channel: False,
            get_cached_channel_or_thread=lambda channel_id: (None, "-"),
        )
        fake_client.format_socket_interaction_user = (
            lambda data: bot.CodexDiscordBot.format_socket_interaction_user(fake_client, data)
        )
        fake_client.is_tracked_socket_message_channel = (
            lambda channel_id: bot.CodexDiscordBot.is_tracked_socket_message_channel(fake_client, channel_id)
        )

        async def fake_log_socket_payload(payload):
            await bot.CodexDiscordBot.log_socket_payload(fake_client, payload)

        fake_client.log_socket_payload = fake_log_socket_payload

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            payload = {
                "t": "MESSAGE_CREATE",
                "s": 123,
                "d": {
                    "id": "555",
                    "channel_id": "222",
                    "guild_id": "111",
                    "content": "single log",
                    "author": {"id": "999", "bot": False},
                },
            }
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.CodexDiscordBot.on_socket_raw_receive(fake_client, json.dumps(payload))
                await bot.CodexDiscordBot.on_socket_response(fake_client, payload)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(log_text.count("socket_message_create channel=222 tracked=True"), 1)

    def test_discord_client_enables_debug_events_for_raw_socket_diagnostics(self) -> None:
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn("enable_debug_events=True", source)

    async def test_socket_message_create_untracked_omits_author_and_content_len(self) -> None:
        fake_client = SimpleNamespace(
            is_allowed_channel=lambda channel_id: False,
            is_allowed_message_channel=lambda channel: False,
            get_cached_channel_or_thread=lambda channel_id: (None, "-"),
        )
        fake_client.format_socket_interaction_user = (
            lambda data: bot.CodexDiscordBot.format_socket_interaction_user(fake_client, data)
        )
        fake_client.is_tracked_socket_message_channel = (
            lambda channel_id: bot.CodexDiscordBot.is_tracked_socket_message_channel(fake_client, channel_id)
        )
        async def fake_log_socket_payload(payload):
            await bot.CodexDiscordBot.log_socket_payload(fake_client, payload)

        fake_client.log_socket_payload = fake_log_socket_payload
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                log_path = Path(temp_dir) / "discord-smoke.log"
                payload = {
                    "t": "MESSAGE_CREATE",
                    "d": {
                        "channel_id": "333",
                        "guild_id": "111",
                        "content": "sensitive prompt",
                        "author": {"id": "999", "bot": False},
                    },
                }
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.CodexDiscordBot.on_socket_response(fake_client, payload)
                log_text = log_path.read_text(encoding="utf-8")
            finally:
                bot.MIRROR_DB_PATH = old_db_path

        self.assertIn("socket_message_create_untracked channel=333", log_text)
        self.assertNotIn("author=999", log_text)
        self.assertNotIn("content_len", log_text)
        self.assertNotIn("sensitive prompt", log_text)

    async def test_socket_interaction_create_logs_sanitized_command(self) -> None:
        fake_client = SimpleNamespace(
            is_allowed_channel=lambda channel_id: False,
        )
        fake_client.format_socket_interaction_user = (
            lambda data: bot.CodexDiscordBot.format_socket_interaction_user(fake_client, data)
        )
        async def fake_log_socket_payload(payload):
            await bot.CodexDiscordBot.log_socket_payload(fake_client, payload)

        fake_client.log_socket_payload = fake_log_socket_payload

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            payload = {
                "t": "INTERACTION_CREATE",
                "d": {
                    "channel_id": "222",
                    "guild_id": "111",
                    "type": 3,
                    "member": {"user": {"id": "999"}},
                    "data": {"custom_id": "codex_busy:abcdabcdabcdabcdabcdabcd:queue"},
                },
            }
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.CodexDiscordBot.on_socket_response(fake_client, payload)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertIn("socket_interaction_create channel=222", log_text)
        self.assertIn("user=999", log_text)
        self.assertIn("command=codex_busy:abcdabcdabcdabcdabcdabcd:queue", log_text)

    def test_help_readme_and_default_slash_commands_match(self) -> None:
        expected_commands = {
            "help",
            "list",
            "archived_list",
            "use",
            "status",
            "doctor",
            "where",
            "context",
            "usage",
            "runners",
            "mirror_check",
            "bridge_sync",
            "new",
            "ask",
            "ask_ipc",
        }

        help_text = bot.build_help()
        help_match = re.search(r"Slash commands: (.+)", help_text)
        self.assertIsNotNone(help_match)
        help_commands = set(re.findall(r"/([a-z_]+)", help_match.group(1)))
        self.assertEqual(help_commands, expected_commands)

        readme = Path("README.md").read_text(encoding="utf-8")
        readme_match = re.search(
            r"Registered Discord slash commands:\s*\n\s*-\s*(.+)",
            readme,
        )
        self.assertIsNotNone(readme_match)
        readme_commands = set(re.findall(r"/([a-z_]+)", readme_match.group(1)))
        self.assertEqual(readme_commands, expected_commands)

        source = Path(bot.__file__).read_text(encoding="utf-8")
        command_names = set(re.findall(r'@bot\.tree\.command\(name="([^"]+)"', source))
        self.assertEqual(command_names, expected_commands | {"qa_buttons"})
        self.assertIn("slash_new_dispatch", source)
        self.assertIn("slash_new_done", source)

    def test_qa_commands_are_hidden_unless_enabled(self) -> None:
        self.assertNotIn("!qa buttons", bot.build_help())
        self.assertNotIn("!steer", bot.build_help())
        with EnvPatch("DISCORD_ENABLE_QA_COMMANDS", "1"):
            help_text = bot.build_help()

        self.assertIn("!qa buttons", help_text)
        self.assertIn("!steer <prompt>", help_text)
        help_match = re.search(r"Slash commands: (.+)", help_text)
        self.assertIsNotNone(help_match)
        self.assertIn("qa_buttons", set(re.findall(r"/([a-z_]+)", help_match.group(1))))

    def test_main_requires_allowed_channels_unless_explicit_all(self) -> None:
        old_env = {key: os.environ.get(key) for key in os.environ if key.startswith("DISCORD_")}
        original_env_path = bot.ENV_PATH
        original_argv = sys.argv[:]
        try:
            for key in list(os.environ):
                if key.startswith("DISCORD_"):
                    os.environ.pop(key, None)
            os.environ["DISCORD_BOT_TOKEN"] = "fake-token"
            sys.argv = ["codex_discord_bot.py"]
            stdout = io.StringIO()
            with tempfile.TemporaryDirectory() as temp_dir:
                bot.ENV_PATH = Path(temp_dir) / "missing.env"
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)), redirect_stdout(stdout):
                    exit_code = bot.main()
                log_text = log_path.read_text(encoding="utf-8")
        finally:
            bot.ENV_PATH = original_env_path
            sys.argv = original_argv
            for key in list(os.environ):
                if key.startswith("DISCORD_"):
                    os.environ.pop(key, None)
            for key, value in old_env.items():
                if value is not None:
                    os.environ[key] = value

        self.assertEqual(exit_code, 1)
        self.assertIn("DISCORD_ALLOWED_CHANNEL_IDS", stdout.getvalue())
        self.assertIn("main_config_error reason=missing_allowed_channels", log_text)

    def test_runtime_instance_lock_blocks_second_holder(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows runtime mutex is only available on Windows")
        mutex_name = f"Local\\CodexDiscordBotTest_{os.getpid()}_{time.time_ns()}"
        old_runtime_lock_path = bot.RUNTIME_LOCK_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.RUNTIME_LOCK_PATH = Path(temp_dir) / "runtime.lock"
            try:
                with bot.acquire_runtime_instance_lock(mutex_name) as first:
                    self.assertTrue(first)
                    with bot.acquire_runtime_instance_lock(mutex_name) as second:
                        self.assertFalse(second)
                with bot.acquire_runtime_instance_lock(mutex_name) as third:
                    self.assertTrue(third)
                self.assertFalse(bot.RUNTIME_LOCK_PATH.exists())
            finally:
                bot.RUNTIME_LOCK_PATH = old_runtime_lock_path

    async def test_send_interaction_chunks_logs_and_sends(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            interaction = FakeInteraction(command_name="where", channel_id=222)
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.send_interaction_chunks(interaction, "hello", title="Where")

            self.assertEqual(interaction.followup.messages, ["hello"])
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("slash_response_start command=where", log_text)
            self.assertIn("slash_response_sent command=where", log_text)

    async def test_send_followup_chunks_splits_long_button_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.send_followup_chunks(
                    interaction,
                    "x" * 4100,
                    title="Steering",
                    exit_code=1,
                    log_prefix="button_response",
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertGreater(len(interaction.followup.messages), 1)
        self.assertTrue(all(len(message) <= bot.DISCORD_MAX_LEN for message in interaction.followup.messages))
        self.assertIn("button_response_start command=ask title='Steering' exit=1", log_text)
        self.assertIn("button_response_sent command=ask title='Steering' exit=1", log_text)

    async def test_send_followup_chunks_falls_back_to_channel_on_send_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            interaction.followup = FailingFollowup()
            interaction.channel = FakeTarget()
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.send_followup_chunks(
                    interaction,
                    "button result",
                    title="Steering",
                    exit_code=1,
                    log_prefix="button_response",
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(interaction.followup.messages, [])
        self.assertEqual(len(interaction.channel.messages), 1)
        content, view = interaction.channel.messages[0]
        self.assertIsNone(view)
        self.assertIn("Discord follow-up delivery failed; posting response here.", content)
        self.assertIn("button result", content)
        self.assertIn("button_response_failed command=ask title='Steering' exit=1", log_text)
        self.assertIn("error_type=RuntimeError", log_text)
        self.assertIn("button_response_fallback_sent command=ask title='Steering' exit=1", log_text)

    async def test_send_followup_chunks_falls_back_with_remaining_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            interaction.followup = FailingFollowup(fail_after=1)
            interaction.channel = FakeTarget()
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.send_followup_chunks(
                    interaction,
                    "x" * 4100,
                    title="Steering",
                    exit_code=1,
                    log_prefix="button_response",
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(len(interaction.followup.messages), 1)
        self.assertGreater(len(interaction.channel.messages), 0)
        fallback_text = "\n".join(content for content, _view in interaction.channel.messages)
        self.assertIn("posting remaining response here", fallback_text)
        self.assertIn("sent=1", log_text)

    async def test_send_direct_followup_falls_back_to_channel_with_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            interaction.followup = FailingFollowup()
            interaction.channel = FakeTarget()
            view = object()
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.send_direct_followup(
                    interaction,
                    "button view",
                    view=view,
                    log_prefix="button_followup",
                    context="steer_busy_failure",
                )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(interaction.followup.messages, [])
        self.assertEqual(len(interaction.channel.messages), 1)
        content, sent_view = interaction.channel.messages[0]
        self.assertIn("Discord follow-up delivery failed; posting response here.", content)
        self.assertIn("button view", content)
        self.assertIs(sent_view, view)
        self.assertIn("button_followup_failed command=ask context=steer_busy_failure", log_text)
        self.assertIn("button_followup_fallback_sent command=ask context=steer_busy_failure", log_text)

    async def test_on_message_logs_received_before_empty_content_ignore(self) -> None:
        client = SimpleNamespace(
            enable_prefix_commands=True,
            is_allowed_message_channel=lambda channel: True,
            is_allowed_user=lambda user_id: True,
        )
        message = FakeMessage(content="", channel_id=333)
        bot.EMPTY_CONTENT_NOTICE_LAST_SENT.clear()

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.CodexDiscordBot.on_message(client, message)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(len(message.channel.messages), 1)
        self.assertIn("Discord did not provide the text content", message.channel.messages[0][0])
        self.assertIn("message_received chat=333", log_text)
        self.assertIn("content_len=0", log_text)
        self.assertIn("ignored_message reason=empty_content chat=333", log_text)
        self.assertIn("empty_content_notice_sent chat=333", log_text)

    async def test_empty_content_notice_uses_channel_cooldown(self) -> None:
        bot.EMPTY_CONTENT_NOTICE_LAST_SENT.clear()
        first = FakeMessage(content="", channel_id=333)
        second = FakeMessage(content="", channel_id=333)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.maybe_send_empty_content_notice(first)
                await bot.maybe_send_empty_content_notice(second)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(len(first.channel.messages), 1)
        self.assertEqual(second.channel.messages, [])
        self.assertIn("empty_content_notice_sent chat=333", log_text)
        self.assertIn("empty_content_notice_skipped reason=cooldown chat=333", log_text)

    async def test_empty_content_notice_skips_non_text_payload(self) -> None:
        bot.EMPTY_CONTENT_NOTICE_LAST_SENT.clear()
        message = FakeMessage(content="", channel_id=333)
        message.attachments = [object()]

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.maybe_send_empty_content_notice(message)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(message.channel.messages, [])
        self.assertIn("empty_content_notice_skipped reason=non_text_payload chat=333", log_text)

    def test_strip_required_plain_ask_mentions_preserves_other_mentions(self) -> None:
        prompt, matched = bot.strip_required_plain_ask_mentions(
            "<@1500506752234422322> ask <@999> now",
            {1500506752234422322},
        )

        self.assertTrue(matched)
        self.assertEqual(prompt, "ask <@999> now")

    def test_strip_required_plain_ask_mentions_accepts_nickname_mention(self) -> None:
        prompt, matched = bot.strip_required_plain_ask_mentions(
            "<@!1500506752234422322> ask now",
            {1500506752234422322},
        )

        self.assertTrue(matched)
        self.assertEqual(prompt, "ask now")

    def test_strip_required_plain_ask_mentions_does_not_match_role_mentions(self) -> None:
        prompt, matched = bot.strip_required_plain_ask_mentions(
            "<@&1500506752234422322> ask now",
            {1500506752234422322},
        )

        self.assertFalse(matched)
        self.assertEqual(prompt, "<@&1500506752234422322> ask now")

    async def test_on_message_required_mention_strips_prompt_for_plain_ask(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_describe_project = bot.describe_mirrored_project_channel
        original_handle_plain_ask = bot.handle_plain_ask
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        calls: list[tuple[object, str, str | None]] = []
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.describe_mirrored_project_channel = lambda channel_id: None
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fake_handle_plain_ask(message, prompt, *, target_thread_id=None):
                calls.append((message, prompt, target_thread_id))

            bot.is_thread_runner_busy = runner_idle
            bot.handle_plain_ask = fake_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1500506752234422322},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="<@1500506752234422322> please run", channel_id=333)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_PLAIN_ASK_CONTEXT_FALLBACK", "0"):
                        await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(calls, [(message, "please run", "thread-1")])
            self.assertIn("text_len=10", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.describe_mirrored_project_channel = original_describe_project
            bot.handle_plain_ask = original_handle_plain_ask
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_on_message_required_mention_ignores_unmentioned_plain_ask(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_plain_ask = bot.handle_plain_ask
        try:
            def fail_get_mirrored(channel_id):
                raise AssertionError("unmentioned plain asks must not resolve a target")

            async def fail_handle_plain_ask(message, prompt, *, target_thread_id=None):
                raise AssertionError("unmentioned plain asks must not reach Codex")

            bot.get_mirrored_codex_thread_id = fail_get_mirrored
            bot.handle_plain_ask = fail_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1500506752234422322},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="plain channel chatter", channel_id=333)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_PLAIN_ASK_CONTEXT_FALLBACK", "0"):
                        await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(message.channel.messages, [])
            self.assertIn("ignored_message reason=required_mention_missing chat=333", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_plain_ask = original_handle_plain_ask

    async def test_on_message_context_fallback_accepts_unmentioned_plain_ask(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_describe_project = bot.describe_mirrored_project_channel
        original_handle_plain_ask = bot.handle_plain_ask
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        calls: list[tuple[object, str, str | None]] = []
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.describe_mirrored_project_channel = lambda channel_id: None
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fake_handle_plain_ask(message, prompt, *, target_thread_id=None):
                calls.append((message, prompt, target_thread_id))

            bot.is_thread_runner_busy = runner_idle
            bot.handle_plain_ask = fake_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1500506752234422322},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="codex explain this in Korean", channel_id=333)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_PLAIN_ASK_CONTEXT_FALLBACK", "1"):
                        await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(calls, [(message, "codex explain this in Korean", "thread-1")])
            self.assertIn("plain_ask_context_fallback chat=333", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.describe_mirrored_project_channel = original_describe_project
            bot.handle_plain_ask = original_handle_plain_ask
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_on_message_context_fallback_accepts_korean_discord_ops_chatter(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_describe_project = bot.describe_mirrored_project_channel
        original_handle_plain_ask = bot.handle_plain_ask
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        calls: list[tuple[object, str, str | None]] = []
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.describe_mirrored_project_channel = lambda channel_id: None
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fake_handle_plain_ask(message, prompt, *, target_thread_id=None):
                calls.append((message, prompt, target_thread_id))

            bot.is_thread_runner_busy = runner_idle
            bot.handle_plain_ask = fake_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1500506752234422322},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="디코 봇 응답 없어", channel_id=333)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_PLAIN_ASK_CONTEXT_FALLBACK", "1"):
                        await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(calls, [(message, "디코 봇 응답 없어", "thread-1")])
            self.assertIn("plain_ask_context_fallback chat=333", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.describe_mirrored_project_channel = original_describe_project
            bot.handle_plain_ask = original_handle_plain_ask
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_on_message_context_fallback_ignores_unmentioned_plain_chatter(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_plain_ask = bot.handle_plain_ask
        try:
            def fail_get_mirrored(channel_id):
                raise AssertionError("plain chatter must not resolve a target")

            async def fail_handle_plain_ask(message, prompt, *, target_thread_id=None):
                raise AssertionError("plain chatter must not reach Codex")

            bot.get_mirrored_codex_thread_id = fail_get_mirrored
            bot.handle_plain_ask = fail_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1500506752234422322},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="plain channel chatter", channel_id=333)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_PLAIN_ASK_CONTEXT_FALLBACK", "1"):
                        await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(message.channel.messages, [])
            self.assertIn("ignored_message reason=required_mention_missing chat=333", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_plain_ask = original_handle_plain_ask

    async def test_on_message_context_fallback_ignores_other_bot_mention_without_bridge_context(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_plain_ask = bot.handle_plain_ask
        try:
            def fail_get_mirrored(channel_id):
                raise AssertionError("other bot mentions without bridge context must not resolve a target")

            async def fail_handle_plain_ask(message, prompt, *, target_thread_id=None):
                raise AssertionError("other bot mentions without bridge context must not reach Codex")

            bot.get_mirrored_codex_thread_id = fail_get_mirrored
            bot.handle_plain_ask = fail_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1511380398914142379},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="<@1500506752234422322> ping", channel_id=333)
            message.raw_mentions = [1500506752234422322]
            message.mentions = [SimpleNamespace(id=1500506752234422322)]

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_PLAIN_ASK_CONTEXT_FALLBACK", "1"):
                        await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(message.channel.messages, [])
            self.assertIn("ignored_message reason=required_mention_missing chat=333", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_plain_ask = original_handle_plain_ask

    async def test_on_message_context_fallback_accepts_other_bot_mention_with_bridge_context(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_describe_project = bot.describe_mirrored_project_channel
        original_handle_plain_ask = bot.handle_plain_ask
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        calls: list[tuple[object, str, str | None]] = []
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.describe_mirrored_project_channel = lambda channel_id: None
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fake_handle_plain_ask(message, prompt, *, target_thread_id=None):
                calls.append((message, prompt, target_thread_id))

            bot.is_thread_runner_busy = runner_idle
            bot.handle_plain_ask = fake_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1511380398914142379},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(
                content="<@1500506752234422322> 잘아타스한테 티켓 다시 보내봐",
                channel_id=333,
            )
            message.raw_mentions = [1500506752234422322]
            message.mentions = [SimpleNamespace(id=1500506752234422322)]

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_PLAIN_ASK_CONTEXT_FALLBACK", "1"):
                        await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(calls, [(message, message.content, "thread-1")])
            self.assertIn("plain_ask_context_fallback chat=333", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.describe_mirrored_project_channel = original_describe_project
            bot.handle_plain_ask = original_handle_plain_ask
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_on_message_required_mention_only_prompts_for_content(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_plain_ask = bot.handle_plain_ask
        try:
            def fail_get_mirrored(channel_id):
                raise AssertionError("mention-only messages must not resolve a target")

            async def fail_handle_plain_ask(message, prompt, *, target_thread_id=None):
                raise AssertionError("mention-only messages must not reach Codex")

            bot.get_mirrored_codex_thread_id = fail_get_mirrored
            bot.handle_plain_ask = fail_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1500506752234422322},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="<@!1500506752234422322>", channel_id=333)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.CodexDiscordBot.on_message(client, message)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(message.channel.messages, [("Add a prompt after the mention.", None)])
            self.assertIn("ignored_message reason=mention_only_content chat=333", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_plain_ask = original_handle_plain_ask

    async def test_on_message_required_mention_does_not_gate_prefix_commands(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_prefix_command = bot.handle_prefix_command
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        calls: list[str] = []
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: None
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fake_handle_prefix_command(client, message, command):
                calls.append(command)

            bot.is_thread_runner_busy = runner_idle
            bot.handle_prefix_command = fake_handle_prefix_command
            client = SimpleNamespace(
                enable_prefix_commands=True,
                plain_ask_mention_user_ids={1500506752234422322},
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="!qa buttons", channel_id=333)

            await bot.CodexDiscordBot.on_message(client, message)

            self.assertEqual(calls, ["qa buttons"])
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_prefix_command = original_handle_prefix_command
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_on_message_project_parent_response_is_chunked(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_describe_project = bot.describe_mirrored_project_channel
        original_handle_plain_ask = bot.handle_plain_ask
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_busy_state = bot.get_busy_state_for_thread
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: None
            bot.describe_mirrored_project_channel = lambda channel_id: "x" * 4100
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", None, "")

            async def runner_idle(target_thread_id):
                return False

            async def fail_handle_plain_ask(message, prompt, *, target_thread_id=None):
                raise AssertionError("project parent messages must not fall back to selected thread")

            bot.is_thread_runner_busy = runner_idle
            bot.handle_plain_ask = fail_handle_plain_ask
            client = SimpleNamespace(
                enable_prefix_commands=True,
                is_allowed_message_channel=lambda channel: True,
                is_allowed_user=lambda user_id: True,
            )
            message = FakeMessage(content="please hook", channel_id=333)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.CodexDiscordBot.on_message(client, message)

            sent = [content for content, _view in message.channel.messages]
            self.assertGreater(len(sent), 1)
            self.assertTrue(all(len(content) <= bot.DISCORD_MAX_LEN for content in sent))
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.describe_mirrored_project_channel = original_describe_project
            bot.handle_plain_ask = original_handle_plain_ask
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_busy_state_for_thread = original_get_busy_state

    async def test_slash_error_handler_reports_before_initial_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.LoggingCommandTree.on_error(
                    SimpleNamespace(),
                    interaction,
                    bot.app_commands.AppCommandError("boom"),
                )

            self.assertEqual(
                interaction.response.messages,
                ["Discord slash command error. Check codex_discord_bot.log."],
            )
            self.assertEqual(interaction.followup.messages, [])
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("slash_command_error command=ask channel=222", log_text)
            self.assertIn("slash_command_error_sent command=ask response=initial", log_text)

    async def test_slash_error_handler_reports_after_defer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            await interaction.response.defer(thinking=True)
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.LoggingCommandTree.on_error(
                    SimpleNamespace(),
                    interaction,
                    bot.app_commands.AppCommandError("boom"),
                )

            self.assertEqual(interaction.response.messages, [])
            self.assertEqual(
                interaction.followup.messages,
                ["Discord slash command error. Check codex_discord_bot.log."],
            )
            self.assertEqual(interaction.followup.kwargs, [{"ephemeral": True}])
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("slash_command_error command=ask channel=222", log_text)
            self.assertIn("slash_command_error_sent command=ask response=followup", log_text)

    async def test_run_bridge_and_send_logs_and_sends(self) -> None:
        original_run_bridge_command = bot.run_bridge_command
        try:
            bot.run_bridge_command = lambda argv: (0, "ok")
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                target = FakeTarget()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    exit_code, output = await bot.run_bridge_and_send(target, ["status"], "Status")

                self.assertEqual(exit_code, 0)
                self.assertEqual(output, "ok")
                self.assertEqual(target.messages, [("Status\n\nok", None)])
                log_text = log_path.read_text(encoding="utf-8")
                self.assertIn("bridge_command_done title='Status' exit=0", log_text)
                self.assertIn("bridge_command_sent title='Status' exit=0", log_text)
        finally:
            bot.run_bridge_command = original_run_bridge_command

    def test_context_usage_detects_inferred_compaction_drop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            events = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "model_context_window": 200000,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "model_context_window": 200000,
                            "last_token_usage": {
                                "input_tokens": 100000,
                                "total_tokens": 101000,
                            },
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "model_context_window": 200000,
                            "last_token_usage": {
                                "input_tokens": 0,
                                "total_tokens": 13000,
                            },
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "model_context_window": 200000,
                            "last_token_usage": {
                                "input_tokens": 35000,
                                "total_tokens": 36000,
                            },
                        },
                    },
                },
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events),
                encoding="utf-8",
            )
            thread = bridge.ThreadInfo(
                id="thread-1",
                title="title",
                cwd=str(Path(temp_dir)),
                updated_at=1,
                rollout_path=str(session_path),
                model="gpt",
                reasoning_effort="high",
                tokens_used=1,
            )

            usage = bridge.get_thread_context_usage(thread)
            context_line = bot.format_context_usage_line(thread)

        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual(usage.inferred_compactions, 1)
        self.assertEqual(usage.last_compaction_before_input_tokens, 100000)
        self.assertEqual(usage.last_compaction_after_input_tokens, 35000)
        self.assertIn("compactions=1", context_line)

    def test_context_warning_stays_quiet_below_high_threshold(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_choose_thread = bridge.choose_thread
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                events = [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "model_context_window": 300000,
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "model_context_window": 300000,
                                "last_token_usage": {
                                    "input_tokens": 240000,
                                    "total_tokens": 240500,
                                },
                            },
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "model_context_window": 300000,
                                "last_token_usage": {
                                    "input_tokens": 0,
                                    "total_tokens": 13000,
                                },
                            },
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "model_context_window": 300000,
                                "last_token_usage": {
                                    "input_tokens": 40000,
                                    "total_tokens": 40500,
                                },
                            },
                        },
                    },
                ]
                session_path.write_text(
                    "\n".join(json.dumps(event) for event in events),
                    encoding="utf-8",
                )
                thread = bridge.ThreadInfo(
                    id="thread-1",
                    title="title",
                    cwd=str(Path(temp_dir)),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=1,
                )
                bot.resolve_target_ref = lambda target_thread_id: ("thread-1", "taxlab:1")
                bridge.choose_thread = lambda thread_id, _cwd=None: thread

                self.assertEqual(bot.build_context_warning("thread-1"), "")
                context_line = bot.format_context_usage_line(thread)

        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bridge.choose_thread = original_choose_thread

        self.assertIn("compactions=1", context_line)
        self.assertIn("archive_recommended=yes", context_line)

    def test_context_warning_starts_at_high_threshold(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_choose_thread = bridge.choose_thread
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                events = [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "model_context_window": 200000,
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "model_context_window": 200000,
                                "last_token_usage": {
                                    "input_tokens": 140000,
                                    "total_tokens": 140500,
                                },
                            },
                        },
                    },
                ]
                session_path.write_text(
                    "\n".join(json.dumps(event) for event in events),
                    encoding="utf-8",
                )
                thread = bridge.ThreadInfo(
                    id="thread-1",
                    title="title",
                    cwd=str(Path(temp_dir)),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=1,
                )
                bot.resolve_target_ref = lambda target_thread_id: ("thread-1", "taxlab:1")
                bridge.choose_thread = lambda thread_id, _cwd=None: thread

                warning = bot.build_context_warning("thread-1")

        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bridge.choose_thread = original_choose_thread

        self.assertIn("Context warning: 70.0% (high)", warning)

    async def test_target_busy_plain_ask_auto_queues_without_busy_choice(self) -> None:
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_busy_state = bot.get_busy_state_for_thread
        original_enqueue = bot.enqueue_thread_ask
        original_build_context_warning = bot.build_context_warning
        calls: list[tuple[str, str | None, bool, object]] = []
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_busy_state_for_thread = lambda target_thread_id: ("busy", "thread-1", "taxlab:1")

            async def fake_enqueue(channel, prompt, target_thread_id, *, queued=False, ack_sent=False, source_message=None):
                calls.append((prompt, target_thread_id, queued, source_message))
                return 1

            bot.enqueue_thread_ask = fake_enqueue
            bot.build_context_warning = lambda target_thread_id: ""
            message = FakeMessage()
            await bot.handle_plain_ask(message, "please steer", target_thread_id="thread-1")

            self.assertEqual(
                message.channel.messages,
                [("Queued in this Codex thread at position 1.\n\nCurrent target state: busy.", None)],
            )
            self.assertEqual(calls, [("please steer", "thread-1", True, message)])
        finally:
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.enqueue_thread_ask = original_enqueue
            bot.build_context_warning = original_build_context_warning

    async def test_stale_busy_plain_ask_auto_queues_without_steer_menu(self) -> None:
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_busy_state = bot.get_busy_state_for_thread
        original_get_stale_info = bot.get_stale_busy_steer_block_info
        original_enqueue = bot.enqueue_thread_ask
        original_build_context_warning = bot.build_context_warning
        calls: list[tuple[str, str | None, bool, object]] = []
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_busy_state_for_thread = lambda target_thread_id: ("busy", "thread-1", "taxlab:1")
            bot.get_stale_busy_steer_block_info = lambda target_thread_id: ("thread-1", "taxlab:1", 660.0)

            async def fake_enqueue(channel, prompt, target_thread_id, *, queued=False, ack_sent=False, source_message=None):
                calls.append((prompt, target_thread_id, queued, source_message))
                return 1

            bot.enqueue_thread_ask = fake_enqueue
            bot.build_context_warning = lambda target_thread_id: ""
            message = FakeMessage()
            await bot.handle_plain_ask(message, "please steer", target_thread_id="thread-1")

            self.assertEqual(
                message.channel.messages,
                [("Queued in this Codex thread at position 1.\n\nCurrent target state: busy.", None)],
            )
            self.assertEqual(calls, [("please steer", "thread-1", True, message)])
        finally:
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.get_stale_busy_steer_block_info = original_get_stale_info
            bot.enqueue_thread_ask = original_enqueue
            bot.build_context_warning = original_build_context_warning

    async def test_other_thread_busy_plain_ask_still_enters_target_ask_flow(self) -> None:
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_busy_state = bot.get_busy_state_for_thread
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_get_global_busy_threads = bot.get_global_busy_threads_for_target
        original_run_prompt_flow = bot.run_prompt_flow
        calls: list[tuple[str, str | None, object]] = []
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", "thread-1", "taxlab:1")

            async def runner_idle(target_thread_id):
                return False

            async def fake_run_prompt_flow(channel, prompt, *, source_message=None, target_thread_id=None):
                calls.append((prompt, target_thread_id, source_message))

            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                busy_thread = bridge.ThreadInfo(
                    id="other-thread",
                    title="Other",
                    cwd=str(temp_dir),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=0,
                )
                bot.is_thread_runner_busy = runner_idle
                bot.get_global_busy_threads_for_target = lambda target_thread_id: [busy_thread]
                bot.run_prompt_flow = fake_run_prompt_flow
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.handle_plain_ask(message, "please run", target_thread_id="thread-1")
                log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""

            self.assertEqual(message.channel.messages, [])
            self.assertEqual(calls, [("please run", "thread-1", message)])
            self.assertNotIn("global_busy_preflight reason=plain_ask_preflight target=thread-1", log_text)
        finally:
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.get_global_busy_threads_for_target = original_get_global_busy_threads
            bot.run_prompt_flow = original_run_prompt_flow

    async def test_runner_busy_plain_ask_uses_queue_flow_without_busy_choice(self) -> None:
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_busy_state = bot.get_busy_state_for_thread
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_run_prompt_flow = bot.run_prompt_flow
        calls: list[tuple[str, str | None, object]] = []
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", "thread-1", "taxlab:1")

            async def runner_busy(target_thread_id):
                return True

            async def fake_run_prompt_flow(channel, prompt, *, source_message=None, target_thread_id=None):
                calls.append((prompt, target_thread_id, source_message))

            bot.is_thread_runner_busy = runner_busy
            bot.run_prompt_flow = fake_run_prompt_flow
            message = FakeMessage()

            await bot.handle_plain_ask(message, "please queue", target_thread_id="thread-1")

            self.assertEqual(message.channel.messages, [])
            self.assertEqual(calls, [("please queue", "thread-1", message)])
        finally:
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.run_prompt_flow = original_run_prompt_flow

    async def test_pending_approval_plain_text_refreshes_buttons_without_submitting(self) -> None:
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_submit_interactive_reply = bot.submit_interactive_reply
        submitted: list[str] = []
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: (
                bot.INTERACTIVE_STATE_APPROVAL,
                "thread-1",
                "project:1",
            )

            async def fake_submit(*args):
                submitted.append(str(args[-1]))

            bot.submit_interactive_reply = fake_submit
            message = FakeMessage()

            await bot.handle_plain_ask(message, "new steering request", target_thread_id="thread-1")

            self.assertEqual(submitted, [])
            self.assertEqual(len(message.channel.messages), 1)
            content, view = message.channel.messages[0]
            self.assertIn("Waiting approval", content)
            self.assertIn("Pending approval", content)
            self.assertIsInstance(view, bot.ApprovalView)
            self.assertEqual(view.target_thread_id, "thread-1")
        finally:
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.submit_interactive_reply = original_submit_interactive_reply

    async def test_pending_approval_plain_numeric_reply_still_submits(self) -> None:
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_submit_interactive_reply = bot.submit_interactive_reply
        submitted: list[str] = []
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: (
                bot.INTERACTIVE_STATE_APPROVAL,
                "thread-1",
                "project:1",
            )

            async def fake_submit(channel, target_thread_id, target_ref, state, answer):
                submitted.append(answer)

            bot.submit_interactive_reply = fake_submit
            message = FakeMessage()

            await bot.handle_plain_ask(message, "approve", target_thread_id="thread-1")

            self.assertEqual(submitted, ["1"])
            self.assertEqual(message.channel.messages, [])
        finally:
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.submit_interactive_reply = original_submit_interactive_reply

    async def test_prefix_steer_sends_prompt_without_button_click(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_run_steering = bot.run_steering_prompt
        original_stream = bot.stream_steering_prompt_result_to_channel
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            observed: list[tuple[str, str | None]] = []
            streamed: list[tuple[object, str | None]] = []

            def fake_run_steering(prompt: str, target_thread_id: str | None) -> bot.SteeringPromptResult:
                observed.append((prompt, target_thread_id))
                return bot.SteeringPromptResult(
                    0,
                    "[qa_delivery_verified]",
                    target_thread_id=target_thread_id,
                    target_ref=target_thread_id or "-",
                    session_path="qa-session.jsonl",
                    start_offset=0,
                )

            async def fake_stream(channel, steering_result, target_thread_id: str | None) -> bool:
                streamed.append((steering_result, target_thread_id))
                return True

            bot.run_steering_prompt = fake_run_steering
            bot.stream_steering_prompt_result_to_channel = fake_stream
            message = FakeMessage()

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_ENABLE_QA_COMMANDS", "1"):
                        await bot.handle_prefix_command(SimpleNamespace(), message, "steer please steer now")

        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.run_steering_prompt = original_run_steering
            bot.stream_steering_prompt_result_to_channel = original_stream

        self.assertEqual(observed, [("please steer now", "thread-1")])
        self.assertEqual(len(streamed), 1)
        self.assertEqual(streamed[0][1], "thread-1")
        self.assertEqual(len(message.channel.messages), 1)
        self.assertEqual(message.channel.messages[0][0], "Steering sent\n\n[qa_delivery_verified]")

    async def test_prefix_steer_is_disabled_by_default(self) -> None:
        message = FakeMessage()

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.handle_prefix_command(SimpleNamespace(), message, "steer please steer now")

        self.assertEqual(
            message.channel.messages,
            [("Discord QA steering is disabled. Set DISCORD_ENABLE_QA_COMMANDS=1 to enable it.", None)],
        )

    async def test_prefix_bridge_sync_runs_refresh(self) -> None:
        original_refresh = bot.refresh_discord_bridge_session
        calls: list[tuple[object, int]] = []
        try:
            async def fake_refresh(fake_bot, *, limit=30):
                calls.append((fake_bot, limit))
                return "Discord bridge sync complete.\nselected_action: kept"

            bot.refresh_discord_bridge_session = fake_refresh
            message = FakeMessage()
            fake_bot = SimpleNamespace()

            await bot.handle_prefix_command(fake_bot, message, "bridge sync 17")

            self.assertEqual(calls, [(fake_bot, 17)])
            self.assertEqual(
                message.channel.messages,
                [
                    ("Discord bridge sync started.", None),
                    ("Discord bridge sync complete.\nselected_action: kept", None),
                ],
            )
        finally:
            bot.refresh_discord_bridge_session = original_refresh

    def test_refresh_codex_bridge_session_state_replaces_stale_selected_thread(self) -> None:
        original_sync_session_index = bridge.sync_session_index_with_state
        original_load_recent_threads = bridge.load_recent_threads
        original_get_selected_thread_id = bridge.get_selected_thread_id
        original_choose_thread = bridge.choose_thread
        original_set_selected_thread_id = bridge.set_selected_thread_id
        original_get_thread_workspace_ref = bridge.get_thread_workspace_ref
        selected_updates: list[str | None] = []
        try:
            thread = bridge.ThreadInfo(
                id="thread-1",
                title="title",
                cwd="C:\\repo",
                updated_at=1,
                rollout_path="session.jsonl",
                model="gpt",
                reasoning_effort="high",
                tokens_used=1,
            )
            bridge.sync_session_index_with_state = lambda: 1
            bridge.load_recent_threads = lambda limit=0: [thread]
            bridge.get_selected_thread_id = lambda: "stale-thread"
            bridge.choose_thread = lambda thread_id=None, cwd=None: thread
            bridge.set_selected_thread_id = lambda thread_id: selected_updates.append(thread_id)
            bridge.get_thread_workspace_ref = lambda selected, threads=None: "repo"

            state = bot.refresh_codex_bridge_session_state()

            self.assertEqual(state["selected_action"], "stale_replaced")
            self.assertEqual(state["selected_thread_id"], "thread-1")
            self.assertEqual(state["selected_ref"], "repo")
            self.assertEqual(selected_updates, ["thread-1"])
        finally:
            bridge.sync_session_index_with_state = original_sync_session_index
            bridge.load_recent_threads = original_load_recent_threads
            bridge.get_selected_thread_id = original_get_selected_thread_id
            bridge.choose_thread = original_choose_thread
            bridge.set_selected_thread_id = original_set_selected_thread_id
            bridge.get_thread_workspace_ref = original_get_thread_workspace_ref

    async def test_ask_busy_failure_reports_unaccepted_without_busy_choice(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        original_build_context_warning = bot.build_context_warning
        original_get_interactive_state = bot.get_interactive_state_for_thread
        calls: list[bool] = []
        try:
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                calls.append(force_while_busy)
                return (
                    1,
                    "\n".join(
                        [
                            "Ask failed (exit 1)",
                            "",
                            "ERROR: The selected thread is still busy. Wait, switch to another thread, or pass --force-while-busy.",
                        ]
                    ),
                )

            bot.run_ask_stream = fake_run_ask_stream
            bot.build_context_warning = lambda target_thread_id: ""

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_ASK_BUSY_RETRY_ATTEMPTS", "0"):
                        await bot.run_prompt_and_send(
                            message.channel,
                            "please steer",
                            ack_sent=True,
                            source_message=message,
                            target_thread_id="thread-1",
                        )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(len(message.channel.messages), 1)
            content, view = message.channel.messages[0]
            self.assertIn("Codex app did not accept this Discord message yet.", content)
            self.assertIn("retry_attempts: 0", content)
            self.assertIsNone(view)
            self.assertEqual(calls, [True])
            self.assertIn("ask_stream_busy_transport_failure kind=target target=thread-1", log_text)
            self.assertIn("ask_stream_busy_retry_exhausted target=thread-1 attempts=0", log_text)
            self.assertNotIn("busy_choice_sent reason=late_busy_failure", log_text)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream
            bot.build_context_warning = original_build_context_warning
            bot.get_interactive_state_for_thread = original_get_interactive_state

    async def test_ask_busy_retry_notice_uses_mapped_delivery_wording(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        original_build_context_warning = bot.build_context_warning
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_retry_delay = bot.get_ask_busy_retry_delay_seconds
        calls: list[bool] = []
        try:
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_ask_busy_retry_delay_seconds = lambda: 0

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                calls.append(force_while_busy)
                return (
                    1,
                    "\n".join(
                        [
                            "Ask failed (exit 1)",
                            "",
                            "ERROR: The selected thread is still busy. Wait, switch to another thread, or pass --force-while-busy.",
                        ]
                    ),
                )

            bot.run_ask_stream = fake_run_ask_stream
            bot.build_context_warning = lambda target_thread_id: ""

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_ASK_BUSY_RETRY_ATTEMPTS", "1"):
                        await bot.run_prompt_and_send(
                            message.channel,
                            "please steer",
                            ack_sent=True,
                            source_message=message,
                            target_thread_id="thread-1",
                        )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(len(message.channel.messages), 2)
            retry_notice, retry_view = message.channel.messages[0]
            final_status, final_view = message.channel.messages[1]
            self.assertIn("Retrying mapped-thread delivery up to 1 time(s).", retry_notice)
            self.assertNotIn("another turn", retry_notice)
            self.assertIsNone(retry_view)
            self.assertIn("Codex app did not accept this Discord message yet.", final_status)
            self.assertIsNone(final_view)
            self.assertEqual(calls, [True, True])
            self.assertIn("ask_stream_retry_done attempt=1", log_text)
            self.assertNotIn("busy_choice_sent reason=late_busy_failure", log_text)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream
            bot.build_context_warning = original_build_context_warning
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_ask_busy_retry_delay_seconds = original_get_retry_delay

    async def test_transport_busy_failure_does_not_show_steering_view(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        original_build_context_warning = bot.build_context_warning
        original_get_interactive_state = bot.get_interactive_state_for_thread
        try:
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                return (
                    1,
                    "\n".join(
                        [
                            "Ask failed (exit 1)",
                            "",
                            "ERROR: A Codex reply is still in progress. You can `open` other threads, but `ask` is blocked until it finishes. Busy thread(s): repo:1. Pass --force-while-busy to override.",
                        ]
                    ),
                )

            bot.run_ask_stream = fake_run_ask_stream
            bot.build_context_warning = lambda target_thread_id: ""

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    with EnvPatch("DISCORD_ASK_BUSY_RETRY_ATTEMPTS", "0"):
                        await bot.run_prompt_and_send(
                            message.channel,
                            "please steer",
                            ack_sent=True,
                            source_message=message,
                            target_thread_id="thread-1",
                        )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(len(message.channel.messages), 1)
            content, view = message.channel.messages[0]
            self.assertIn("Codex app did not accept this Discord message yet.", content)
            self.assertIn("retry_attempts: 0", content)
            self.assertIsNone(view)
            self.assertNotIn("Choose how to handle", content)
            self.assertIn("ask_stream_busy_transport_failure kind=global target=thread-1", log_text)
            self.assertIn("ask_stream_busy_retry_exhausted target=thread-1 attempts=0", log_text)
            self.assertNotIn("busy_choice_sent reason=late_busy_failure", log_text)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream
            bot.build_context_warning = original_build_context_warning
            bot.get_interactive_state_for_thread = original_get_interactive_state

    async def test_ask_stream_ipc_delivery_pending_does_not_send_failure(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        try:
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                return (
                    1,
                    "\n".join(
                        [
                            "target_thread: thread-1",
                            "ui_activation: ipc-thread-follower-start-turn",
                            "ERROR: Prompt delivery could not be confirmed in any recent Codex thread after IPC delivery. The transport reported success, but no matching user message was recorded.",
                        ]
                    ),
                )

            bot.run_ask_stream = fake_run_ask_stream

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.run_prompt_and_send(
                        message.channel,
                        "please run",
                        ack_sent=True,
                        source_message=message,
                        target_thread_id="thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(len(message.channel.messages), 1)
            content, view = message.channel.messages[0]
            self.assertIn("[delivery_pending]", content)
            self.assertIn("Do not resend", content)
            self.assertNotIn("Ask failed", content)
            self.assertNotIn("ERROR:", content)
            self.assertIsNone(view)
            self.assertIn("ask_stream_ipc_delivery_pending exit=1 target=thread-1", log_text)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream

    async def test_ask_stream_live_without_final_sends_fallback(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        try:
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                relay.feed_line("[commentary]")
                relay.feed_line("Still compacting context.")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[commentary]\nStill compacting context.\n\n[ready]"

            bot.run_ask_stream = fake_run_ask_stream

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.run_prompt_and_send(
                        message.channel,
                        "please steer",
                        ack_sent=True,
                        source_message=message,
                        target_thread_id="thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            sent = [content for content, _view in message.channel.messages]
            self.assertNotIn("Codex turn finished.", sent)
            self.assertTrue(any("In progress" in content for content in sent))
            self.assertTrue(any("Ask finished" in content for content in sent))
            self.assertTrue(any("[ready]" in content for content in sent))
            self.assertIn("ask_stream_no_final_fallback target=thread-1", log_text)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream

    async def test_ask_stream_no_final_is_suppressed_after_steering_handoff(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        old_handoffs = dict(bot.STEERING_HANDOFFS)
        try:
            bot.STEERING_HANDOFFS.clear()
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                bot.STEERING_HANDOFFS[bot.normalize_runner_key(target_thread_id)] = time.monotonic() + 1.0
                return 0, "[delivery_verified] taxlab:1"

            bot.run_ask_stream = fake_run_ask_stream

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.run_prompt_and_send(
                        message.channel,
                        "please steer",
                        ack_sent=True,
                        source_message=message,
                        target_thread_id="thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(message.channel.messages, [])
            self.assertIn("ask_stream_suppressed_after_steering target=thread-1", log_text)
        finally:
            bot.STEERING_HANDOFFS.clear()
            bot.STEERING_HANDOFFS.update(old_handoffs)
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream

    async def test_ask_stream_commentary_is_not_suppressed_after_steering_handoff(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        old_handoffs = dict(bot.STEERING_HANDOFFS)
        try:
            bot.STEERING_HANDOFFS.clear()
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                bot.STEERING_HANDOFFS[bot.normalize_runner_key(target_thread_id)] = time.monotonic() + 1.0
                relay.feed_line("[commentary]")
                relay.feed_line("checking live relay")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[commentary]\nchecking live relay\n\n[ready]"

            bot.run_ask_stream = fake_run_ask_stream

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.run_prompt_and_send(
                        message.channel,
                        "please steer",
                        ack_sent=True,
                        source_message=message,
                        target_thread_id="thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            sent = [content for content, _view in message.channel.messages]
            self.assertEqual(sent, ["In progress\n\nchecking live relay"])
            self.assertNotIn("discord_relay_suppressed_after_steering target=thread-1 mode=commentary", log_text)
            self.assertIn("ask_stream_suppressed_after_steering target=thread-1 sent_live=True", log_text)
        finally:
            bot.STEERING_HANDOFFS.clear()
            bot.STEERING_HANDOFFS.update(old_handoffs)
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream

    async def test_ask_stream_final_is_suppressed_after_steering_handoff(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        old_handoffs = dict(bot.STEERING_HANDOFFS)
        try:
            bot.STEERING_HANDOFFS.clear()
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                bot.STEERING_HANDOFFS[bot.normalize_runner_key(target_thread_id)] = time.monotonic() + 1.0
                relay.feed_line("[final_answer]")
                relay.feed_line("original final")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[final_answer]\noriginal final\n\n[ready]"

            bot.run_ask_stream = fake_run_ask_stream

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.run_prompt_and_send(
                        message.channel,
                        "please steer",
                        ack_sent=True,
                        source_message=message,
                        target_thread_id="thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(message.channel.messages, [])
            self.assertIn("discord_relay_suppressed_after_steering target=thread-1 mode=final", log_text)
            self.assertIn("ask_stream_suppressed_after_steering target=thread-1", log_text)
        finally:
            bot.STEERING_HANDOFFS.clear()
            bot.STEERING_HANDOFFS.update(old_handoffs)
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream

    async def test_run_prompt_and_send_uses_typing_indicator(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        try:
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
                relay.feed_line("[final_answer]")
                relay.feed_line("done")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[final_answer]\ndone\n\n[ready]"

            bot.run_ask_stream = fake_run_ask_stream

            message = FakeMessage()
            await bot.run_prompt_and_send(
                message.channel,
                "please run",
                ack_sent=True,
                source_message=message,
                target_thread_id="thread-1",
            )

            self.assertEqual(message.channel.typing_events, ["enter", "exit"])
            self.assertEqual(message.channel.messages, [("done", None), ("Codex turn finished.", None)])
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream

    async def test_busy_choice_send_falls_back_when_view_send_fails(self) -> None:
        original_build_context_warning = bot.build_context_warning
        try:
            bot.build_context_warning = lambda target_thread_id: ""
            message = FakeMessage()
            channel = ViewFailingTarget()
            message.channel = channel

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    sent_with_view = await bot.send_busy_choice_message(
                        channel,
                        message,
                        "please steer",
                        target_thread_id="thread-1",
                        allow_steer=True,
                        reason="late_busy_failure",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertFalse(sent_with_view)
            self.assertGreaterEqual(len(channel.messages), 1)
            fallback_text = "\n".join(content for content, view in channel.messages if view is None)
            self.assertIn("Codex app is still processing this mapped thread.", fallback_text)
            self.assertIn("Discord could not attach steering buttons.", fallback_text)
            self.assertIn("busy_choice_send_failed reason=late_busy_failure", log_text)
            self.assertIn("busy_choice_fallback_sent reason=late_busy_failure", log_text)
            self.assertNotIn("busy_choice_sent reason=late_busy_failure", log_text)
        finally:
            bot.build_context_warning = original_build_context_warning

    async def test_steer_now_success_attaches_watch(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_stream_steering = bot.stream_steering_prompt_result_to_channel
        calls: list[tuple[object, object, str | None]] = []
        try:
            steering_result = bot.SteeringPromptResult(
                0,
                "target_thread: thread-1\n[delivery_verified] taxlab:1",
                target_thread_id="thread-1",
                target_ref="taxlab:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            def fake_run_steering_prompt(prompt, target_thread_id):
                return steering_result

            async def fake_stream_steering(channel, result, target_thread_id):
                calls.append((channel, result, target_thread_id))
                await channel.send("steered final")
                return True

            bot.run_steering_prompt = fake_run_steering_prompt
            bot.stream_steering_prompt_result_to_channel = fake_stream_steering

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                view = bot.BusyChoiceView(message, "please steer", target_thread_id="thread-1")
                button = next(item for item in view.children if getattr(item, "label", "") == "Steer now")
                interaction = FakeInteraction()

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertTrue(interaction.response.deferred)
            self.assertEqual(interaction.response.defer_kwargs, [{"thinking": True, "ephemeral": True}])
            self.assertEqual(len(interaction.followup.messages), 1)
            self.assertEqual(interaction.followup.kwargs[0], {"ephemeral": True})
            self.assertIn("Steering sent", interaction.followup.messages[0])
            self.assertEqual(calls, [(message.channel, steering_result, "thread-1")])
            self.assertEqual(
                message.channel.messages,
                [
                    ("Discord steering submitted.\nmessage: please steer", None),
                    ("steered final", None),
                ],
            )
            self.assertIn("steering_start_ack_sent target=thread-1", log_text)
            self.assertIn("steer_now_sent exit=0 target=thread-1", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.stream_steering_prompt_result_to_channel = original_stream_steering

    def test_run_steering_prompt_treats_delayed_ipc_delivery_as_success(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_choose_thread = bridge.choose_thread
        original_snapshot = bridge.snapshot_recent_session_offsets
        original_run_ask = bot.run_ask
        original_wait = bridge.wait_for_prompt_delivery
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                thread = bridge.ThreadInfo(
                    id="thread-1",
                    title="title",
                    cwd=str(Path(temp_dir)),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=1,
                )
                recent_offsets = {"thread-1": (thread, session_path, 12)}
                waits: list[float] = []

                bot.resolve_target_ref = lambda target_thread_id: ("thread-1", "taxlab:1")
                bridge.choose_thread = lambda thread_id, _cwd=None: thread
                bridge.snapshot_recent_session_offsets = lambda limit=10, include_threads=None: recent_offsets
                ask_calls: list[dict[str, object]] = []

                def fake_run_ask(
                    prompt,
                    *,
                    force_while_busy=False,
                    wait=True,
                    target_thread_id=None,
                    timeout_sec=None,
                ):
                    ask_calls.append(
                        {
                            "force_while_busy": force_while_busy,
                            "wait": wait,
                            "target_thread_id": target_thread_id,
                            "timeout_sec": timeout_sec,
                        }
                    )
                    return (
                        1,
                        "ERROR: transport returned a nonzero exit, but the prompt may still be recorded.",
                    )

                def fake_wait(session_offsets, prompt, timeout_sec=4.0):
                    waits.append(timeout_sec)
                    return thread

                bot.run_ask = fake_run_ask
                bridge.wait_for_prompt_delivery = fake_wait

                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    result = bot.run_steering_prompt("please steer", "thread-1")

            self.assertEqual(result.exit_code, 0)
            self.assertIn("[delivery_verified]", result.output)
            self.assertEqual(result.target_thread_id, "thread-1")
            self.assertEqual(result.session_path, str(session_path))
            self.assertEqual(result.start_offset, 12)
            self.assertEqual(ask_calls[0]["wait"], False)
            self.assertEqual(ask_calls[0]["force_while_busy"], True)
            self.assertIsNotNone(ask_calls[0]["timeout_sec"])
            self.assertGreaterEqual(waits[-1], bot.STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bridge.choose_thread = original_choose_thread
            bridge.snapshot_recent_session_offsets = original_snapshot
            bot.run_ask = original_run_ask
            bridge.wait_for_prompt_delivery = original_wait

    def test_run_steering_prompt_keeps_watching_pending_ipc_delivery(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_choose_thread = bridge.choose_thread
        original_snapshot = bridge.snapshot_recent_session_offsets
        original_run_ask = bot.run_ask
        original_wait = bridge.wait_for_prompt_delivery
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                thread = bridge.ThreadInfo(
                    id="thread-1",
                    title="title",
                    cwd=str(Path(temp_dir)),
                    updated_at=1,
                    rollout_path=str(session_path),
                    model="gpt",
                    reasoning_effort="high",
                    tokens_used=1,
                )
                recent_offsets = {"thread-1": (thread, session_path, 12)}

                bot.resolve_target_ref = lambda target_thread_id: ("thread-1", "taxlab:1")
                bridge.choose_thread = lambda thread_id, _cwd=None: thread
                bridge.snapshot_recent_session_offsets = lambda limit=10, include_threads=None: recent_offsets

                def fake_run_ask(
                    prompt,
                    *,
                    force_while_busy=False,
                    wait=True,
                    target_thread_id=None,
                    timeout_sec=None,
                ):
                    return (
                        1,
                        "target_thread: thread-1\n"
                        "ui_activation: ipc-thread-follower-start-turn\n"
                        "ERROR: Prompt delivery could not be confirmed in any recent Codex thread after IPC delivery. "
                        "The transport reported success, but no matching user message was recorded.",
                    )

                bot.run_ask = fake_run_ask
                waits: list[float] = []

                def fake_wait(session_offsets, prompt, timeout_sec=4.0):
                    waits.append(timeout_sec)
                    return None

                bridge.wait_for_prompt_delivery = fake_wait

                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    result = bot.run_steering_prompt("please steer", "thread-1")
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(result.exit_code, 0)
            self.assertIn("[delivery_pending]", result.output)
            self.assertNotIn("ERROR:", result.output)
            self.assertNotIn("Prompt delivery could not be confirmed", result.output)
            self.assertEqual(result.target_thread_id, "thread-1")
            self.assertEqual(result.session_path, str(session_path))
            self.assertEqual(result.start_offset, 12)
            self.assertEqual(waits, [])
            self.assertIn("steering_ipc_delivery_pending exit=1 target=thread-1 confirm_timeout=0.0", log_text)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bridge.choose_thread = original_choose_thread
            bridge.snapshot_recent_session_offsets = original_snapshot
            bot.run_ask = original_run_ask
            bridge.wait_for_prompt_delivery = original_wait

    async def test_steering_watch_uses_finite_timeout(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        timeouts: list[float] = []
        try:
            def fake_run_watch(steering_result, relay, *, timeout_sec=0):
                timeouts.append(timeout_sec)
                relay.finish()
                return 2, ""

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            steering_result = bot.SteeringPromptResult(
                0,
                "[delivery_pending]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
                delivery_pending=True,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.stream_steering_prompt_result_to_channel(
                        target,
                        steering_result,
                        "thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(timeouts, [bot.STEERING_PENDING_WATCH_TIMEOUT_SECONDS])
            self.assertEqual(target.messages, [])
            self.assertIn("steer_watch_empty_failure_suppressed target=thread-1", log_text)

            target.messages.clear()
            steering_result.delivery_pending = False
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.stream_steering_prompt_result_to_channel(
                        target,
                        steering_result,
                        "thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(
                timeouts,
                [
                    bot.STEERING_PENDING_WATCH_TIMEOUT_SECONDS,
                    bot.STEERING_PENDING_WATCH_TIMEOUT_SECONDS,
                ],
            )
            self.assertEqual(target.messages, [])
            self.assertIn("steer_watch_empty_failure_suppressed target=thread-1", log_text)
        finally:
            bot.run_steering_watch_stream = original_run_watch

    async def test_steering_watch_suppresses_empty_success(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        try:
            def fake_run_watch(steering_result, relay, *, timeout_sec=0):
                relay.finish()
                return 0, ""

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            steering_result = bot.SteeringPromptResult(
                0,
                "[delivery_verified]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.stream_steering_prompt_result_to_channel(
                        target,
                        steering_result,
                        "thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(target.messages, [])
            self.assertIn("steer_watch_empty_success_suppressed target=thread-1", log_text)
        finally:
            bot.run_steering_watch_stream = original_run_watch

    async def test_steering_watch_reports_nonempty_failure(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        try:
            def fake_run_watch(steering_result, relay, *, timeout_sec=0):
                relay.finish()
                return 2, "watch failed with details"

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            steering_result = bot.SteeringPromptResult(
                0,
                "[delivery_verified]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            await bot.stream_steering_prompt_result_to_channel(
                target,
                steering_result,
                "thread-1",
            )

            self.assertEqual(len(target.messages), 1)
            self.assertIn("Steering watch failed (exit 2)", target.messages[0][0])
            self.assertIn("watch failed with details", target.messages[0][0])
        finally:
            bot.run_steering_watch_stream = original_run_watch

    async def test_steering_watch_live_final_does_not_send_done_copy(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        try:
            def fake_run_watch(steering_result, relay, *, timeout_sec=0):
                relay.feed_line("[final_answer]")
                relay.feed_line("steered final")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[final_answer]\nsteered final\n\n[ready]"

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            steering_result = bot.SteeringPromptResult(
                0,
                "[delivery_verified]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            await bot.stream_steering_prompt_result_to_channel(
                target,
                steering_result,
                "thread-1",
            )

            self.assertEqual(target.messages, [("steered final", None)])
        finally:
            bot.run_steering_watch_stream = original_run_watch

    async def test_post_approval_watch_streams_final_after_steering_handoff(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        old_handoffs = dict(bot.STEERING_HANDOFFS)
        try:
            bot.STEERING_HANDOFFS.clear()

            def fake_run_watch(watch_result, relay, *, timeout_sec=0):
                bot.mark_steering_handoff("thread-1")
                relay.feed_line("[final_answer]")
                relay.feed_line("approved follow-up")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[final_answer]\napproved follow-up\n\n[ready]"

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            watch_result = bot.SteeringPromptResult(
                0,
                "[approval_submitted]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    streamed = await bot.stream_post_approval_result_to_channel(
                        target,
                        watch_result,
                        "thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertTrue(streamed)
            self.assertEqual(target.messages, [("approved follow-up", None)])
            self.assertIn("approval_followup_watch_done exit=0 target=thread-1", log_text)
            self.assertNotIn("discord_relay_suppressed_after_steering", log_text)
        finally:
            bot.STEERING_HANDOFFS.clear()
            bot.STEERING_HANDOFFS.update(old_handoffs)
            bot.run_steering_watch_stream = original_run_watch

    async def test_steering_watch_does_not_suppress_handoff_before_watch_start(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        old_handoffs = dict(bot.STEERING_HANDOFFS)
        try:
            bot.STEERING_HANDOFFS.clear()
            bot.mark_steering_handoff("thread-1")

            def fake_run_watch(steering_result, relay, *, timeout_sec=0):
                relay.feed_line("[final_answer]")
                relay.feed_line("current steered final")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[final_answer]\ncurrent steered final\n\n[ready]"

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            steering_result = bot.SteeringPromptResult(
                0,
                "[delivery_verified]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.stream_steering_prompt_result_to_channel(
                        target,
                        steering_result,
                        "thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(target.messages, [("current steered final", None)])
            self.assertNotIn("steer_watch_suppressed_after_newer_handoff", log_text)
        finally:
            bot.STEERING_HANDOFFS.clear()
            bot.STEERING_HANDOFFS.update(old_handoffs)
            bot.run_steering_watch_stream = original_run_watch

    async def test_steering_watch_reports_timeout_failure(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        try:
            def fake_run_watch(steering_result, relay, *, timeout_sec=0):
                relay.feed_line("[timeout]")
                relay.finish()
                return 2, "[timeout]\nCodex is still working."

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            steering_result = bot.SteeringPromptResult(
                0,
                "[delivery_verified]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.stream_steering_prompt_result_to_channel(
                        target,
                        steering_result,
                        "thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(len(target.messages), 1)
            self.assertIn("Steering is still running in Codex.", target.messages[0][0])
            self.assertIn("Do not resend", target.messages[0][0])
            self.assertIn("steer_watch_timeout_reported target=thread-1", log_text)
        finally:
            bot.run_steering_watch_stream = original_run_watch

    async def test_older_steering_watch_suppresses_after_newer_handoff(self) -> None:
        original_run_watch = bot.run_steering_watch_stream
        old_handoffs = dict(bot.STEERING_HANDOFFS)
        try:
            bot.STEERING_HANDOFFS.clear()

            def fake_run_watch(steering_result, relay, *, timeout_sec=0):
                bot.STEERING_HANDOFFS[bot.normalize_runner_key("thread-1")] = time.monotonic() + 1.0
                relay.feed_line("[final_answer]")
                relay.feed_line("duplicate final")
                relay.feed_line("[ready]")
                relay.finish()
                return 0, "[final_answer]\nduplicate final\n\n[ready]"

            bot.run_steering_watch_stream = fake_run_watch
            target = FakeTarget()
            steering_result = bot.SteeringPromptResult(
                0,
                "[delivery_verified]",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.stream_steering_prompt_result_to_channel(
                        target,
                        steering_result,
                        "thread-1",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(target.messages, [])
            self.assertIn("steer_watch_suppressed_after_newer_handoff target=thread-1", log_text)
        finally:
            bot.STEERING_HANDOFFS.clear()
            bot.STEERING_HANDOFFS.update(old_handoffs)
            bot.run_steering_watch_stream = original_run_watch

    async def test_older_relay_suppresses_after_newer_relay_for_same_thread(self) -> None:
        old_generations = dict(bot.ACTIVE_DISCORD_RELAY_GENERATIONS)
        try:
            bot.ACTIVE_DISCORD_RELAY_GENERATIONS.clear()
            target = FakeTarget()
            loop = asyncio.get_running_loop()
            older = bot.DiscordAskRelay(loop, target, "thread-1", "project:1")
            bot.DiscordAskRelay(loop, target, "thread-1", "project:1")

            older.feed_line("[final_answer]")
            older.feed_line("stale final")
            older.feed_line("[ready]")
            older.finish()

            self.assertEqual(target.messages, [])
            self.assertTrue(older.suppressed_after_steering)
        finally:
            bot.ACTIVE_DISCORD_RELAY_GENERATIONS.clear()
            bot.ACTIVE_DISCORD_RELAY_GENERATIONS.update(old_generations)

    async def test_steer_now_busy_failure_reports_status_without_new_busy_view(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_build_context_warning = bot.build_context_warning
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_resolve_target_ref = bot.resolve_target_ref
        old_handoffs = dict(bot.STEERING_HANDOFFS)
        try:
            bot.STEERING_HANDOFFS.clear()
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_steering_prompt(prompt, target_thread_id):
                return (
                    1,
                    "ERROR: The selected thread is still busy. Wait, switch to another thread, or pass --force-while-busy.",
                )

            bot.run_steering_prompt = fake_run_steering_prompt
            bot.build_context_warning = lambda target_thread_id: ""

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                view = bot.BusyChoiceView(message, "please steer", target_thread_id="thread-1")
                button = next(item for item in view.children if getattr(item, "label", "") == "Steer now")
                interaction = FakeInteraction()

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertTrue(interaction.response.deferred)
            self.assertEqual(interaction.message.edits, [None])
            self.assertEqual(
                message.channel.messages,
                [("Discord steering submitted.\nmessage: please steer", None)],
            )
            self.assertEqual(len(interaction.followup.messages), 1)
            content = interaction.followup.messages[0]
            self.assertIn("Codex app did not accept this steering message yet.", content)
            self.assertIn("target: `taxlab:1`", content)
            self.assertNotIn("selected thread is still busy", content.lower())
            self.assertIn("steer_busy_status_sent reason=steer_busy_failure exit=1 target=thread-1", log_text)
            self.assertNotIn("busy_choice_sent reason=steer_busy_failure", log_text)
            self.assertIn("component_message_components_cleared context=busy_choice_steer", log_text)
            self.assertIn("steering_start_ack_sent target=thread-1", log_text)
            self.assertNotIn("prompt=please steer", log_text)
            self.assertEqual(bot.STEERING_HANDOFFS, {})
        finally:
            bot.STEERING_HANDOFFS.clear()
            bot.STEERING_HANDOFFS.update(old_handoffs)
            bot.run_steering_prompt = original_run_steering_prompt
            bot.build_context_warning = original_build_context_warning
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.resolve_target_ref = original_resolve_target_ref

    async def test_steer_now_blocks_stale_busy_without_sending_prompt(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_get_stale_info = bot.get_stale_busy_steer_block_info
        try:
            def fail_run_steering_prompt(prompt, target_thread_id):
                raise AssertionError("stale busy steering must not call Codex")

            bot.run_steering_prompt = fail_run_steering_prompt
            bot.get_stale_busy_steer_block_info = lambda target_thread_id: ("thread-1", "taxlab:1", 660.0)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                view = bot.BusyChoiceView(message, "please steer", target_thread_id="thread-1")
                button = next(item for item in view.children if getattr(item, "label", "") == "Steer now")
                interaction = FakeInteraction(command_name="ask", channel_id=222)

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertTrue(interaction.response.deferred)
            self.assertEqual(interaction.message.edits, [None])
            self.assertEqual(len(message.channel.messages), 1)
            content, view = message.channel.messages[0]
            self.assertIsNone(view)
            self.assertIn("has not produced new output recently", content)
            self.assertEqual(len(interaction.followup.messages), 1)
            self.assertIn("Steering was not sent", interaction.followup.messages[0])
            self.assertEqual(interaction.followup.kwargs, [{"ephemeral": True}])
            self.assertIn("stale_busy_steer_blocked reason=steer_now target=thread-1", log_text)
            self.assertNotIn("steering_start_ack_sent", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.get_stale_busy_steer_block_info = original_get_stale_info

    async def test_steer_now_waiting_input_failure_resends_app_menu(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_build_context_warning = bot.build_context_warning
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_resolve_target_ref = bot.resolve_target_ref
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_steering_prompt(prompt, target_thread_id):
                return (
                    1,
                    "ERROR: The selected thread is waiting on a follow-up choice or input in Codex Desktop. "
                    "Open the thread in the app and respond there first.",
                )

            bot.run_steering_prompt = fake_run_steering_prompt
            bot.build_context_warning = lambda target_thread_id: ""

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                view = bot.BusyChoiceView(message, "please steer", target_thread_id="thread-1")
                button = next(item for item in view.children if getattr(item, "label", "") == "Steer now")
                interaction = FakeInteraction()

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertTrue(interaction.response.deferred)
            self.assertEqual(interaction.message.edits, [None])
            self.assertEqual(len(interaction.followup.messages), 1)
            self.assertIn("Codex app menu was refreshed", interaction.followup.messages[0])
            self.assertEqual(len(message.channel.messages), 2)
            start_content, start_view = message.channel.messages[0]
            menu_content, menu_view = message.channel.messages[1]
            self.assertIn("Discord steering submitted", start_content)
            self.assertIsNone(start_view)
            self.assertIn("Waiting input", menu_content)
            self.assertIn("Pending input", menu_content)
            self.assertIsNone(menu_view)
            self.assertIn("codex_app_menu_sent reason=steer_busy_failure target=thread-1 state=waiting-input", log_text)
            self.assertNotIn("busy_choice_sent reason=steer_busy_failure", log_text)
            self.assertIn("component_message_components_cleared context=busy_choice_steer", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.build_context_warning = original_build_context_warning
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.resolve_target_ref = original_resolve_target_ref

    async def test_steer_now_busy_status_falls_back_when_followup_fails(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_build_context_warning = bot.build_context_warning
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_resolve_target_ref = bot.resolve_target_ref
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_steering_prompt(prompt, target_thread_id):
                return (
                    1,
                    "ERROR: The selected thread is still busy. Wait, switch to another thread.",
                )

            bot.run_steering_prompt = fake_run_steering_prompt
            bot.build_context_warning = lambda target_thread_id: ""

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                view = bot.BusyChoiceView(message, "please steer", target_thread_id="thread-1")
                button = next(item for item in view.children if getattr(item, "label", "") == "Steer now")
                interaction = FakeInteraction(command_name="ask", channel_id=222)
                interaction.followup = FailingFollowup()
                interaction.channel = FakeTarget()

                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(interaction.followup.messages, [])
            self.assertEqual(len(interaction.channel.messages), 1)
            content, fallback_view = interaction.channel.messages[0]
            self.assertIn("Discord follow-up delivery failed; posting response here.", content)
            self.assertIn("Codex app did not accept this steering message yet.", content)
            self.assertIsNone(fallback_view)
            self.assertIn("button_response_failed command=ask title='Steering'", log_text)
            self.assertIn("button_response_fallback_sent command=ask title='Steering'", log_text)
            self.assertIn("steer_busy_status_sent reason=steer_busy_failure exit=1 target=thread-1", log_text)
            self.assertNotIn("busy_choice_sent reason=steer_busy_failure", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.build_context_warning = original_build_context_warning
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.resolve_target_ref = original_resolve_target_ref

    async def test_approval_button_chunks_long_output(self) -> None:
        original_submit_approval_reply = bot.submit_approval_reply
        try:
            def fake_submit_approval_reply(target_thread_id, answer):
                return 0, "approved\n" + ("x" * 4100)

            bot.submit_approval_reply = fake_submit_approval_reply
            interaction = FakeInteraction(command_name="approval", channel_id=222)
            view = bot.ApprovalView("thread-1")
            button = next(item for item in view.children if getattr(item, "label", "") == "Approve")

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertTrue(interaction.response.deferred)
            self.assertGreater(len(interaction.followup.messages), 1)
            self.assertTrue(all(len(message) <= bot.DISCORD_MAX_LEN for message in interaction.followup.messages))
            self.assertIn("Approval submitted", interaction.followup.messages[0])
            self.assertIn("button_response_start command=approval title='Approval' exit=0", log_text)
            self.assertIn("approval_button_sent exit=0 target=thread-1", log_text)
            self.assertIn("approval_button user=242286902982606848 answer_len=1", log_text)
            self.assertIn("approval_button_done exit=0 target=thread-1 answer_len=1", log_text)
            self.assertNotIn("answer=1", log_text)
        finally:
            bot.submit_approval_reply = original_submit_approval_reply

    async def test_approval_button_starts_post_approval_watch(self) -> None:
        original_submit_approval_reply = bot.submit_approval_reply
        original_make_watch = bot.make_post_approval_watch_result
        original_stream_watch = bot.stream_post_approval_result_for_interaction
        calls: list[tuple[str, object, object | None]] = []
        watch_result = bot.SteeringPromptResult(
            0,
            "[approval_submitted]",
            target_thread_id="thread-1",
            target_ref="project:1",
            session_path="session.jsonl",
            start_offset=10,
        )
        try:
            def fake_submit_approval_reply(target_thread_id, answer):
                return 0, "approved"

            def fake_make_watch(target_thread_id):
                calls.append(("make", target_thread_id, None))
                return watch_result

            async def fake_stream_watch(interaction, watch, target_thread_id):
                calls.append(("stream", target_thread_id, watch))
                return True

            bot.submit_approval_reply = fake_submit_approval_reply
            bot.make_post_approval_watch_result = fake_make_watch
            bot.stream_post_approval_result_for_interaction = fake_stream_watch
            interaction = FakeInteraction(command_name="approval", channel_id=222)
            view = bot.ApprovalView("thread-1")
            button = next(item for item in view.children if getattr(item, "label", "") == "Approve")

            await button.callback(interaction)

            self.assertEqual(interaction.followup.messages, ["Approval submitted\n\napproved"])
            self.assertEqual(
                calls,
                [
                    ("make", "thread-1", None),
                    ("stream", "thread-1", watch_result),
                ],
            )
        finally:
            bot.submit_approval_reply = original_submit_approval_reply
            bot.make_post_approval_watch_result = original_make_watch
            bot.stream_post_approval_result_for_interaction = original_stream_watch

    async def test_plain_approval_reply_log_uses_answer_length(self) -> None:
        original_submit_approval_reply = bot.submit_approval_reply
        try:
            def fake_submit_approval_reply(target_thread_id, answer):
                return 0, "approved"

            bot.submit_approval_reply = fake_submit_approval_reply
            channel = FakeTarget()
            secret_answer = "approve this sensitive text"

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.submit_interactive_reply(
                        channel,
                        "thread-1",
                        "taxlab:1",
                        bot.INTERACTIVE_STATE_APPROVAL,
                        secret_answer,
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(channel.messages, [("Approval submitted\n\napproved", None)])
            self.assertIn("approval_reply_done exit=0 target=thread-1", log_text)
            self.assertIn(f"answer_len={len(secret_answer)}", log_text)
            self.assertNotIn(secret_answer, log_text)
        finally:
            bot.submit_approval_reply = original_submit_approval_reply

    async def test_queue_next_immediate_uses_runner_queue(self) -> None:
        original_get_busy_state = bot.get_busy_state_for_thread
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_enqueue_thread_ask = bot.enqueue_thread_ask
        original_run_prompt_flow = bot.run_prompt_flow
        calls: list[tuple[object, str, str | None, bool, bool, object]] = []
        try:
            bot.get_busy_state_for_thread = lambda target_thread_id: ("idle", target_thread_id, "taxlab:1")

            async def runner_idle(target_thread_id):
                return False

            async def fake_enqueue_thread_ask(
                channel,
                prompt,
                target_thread_id,
                *,
                queued=False,
                ack_sent=False,
                source_message=None,
            ):
                calls.append((channel, prompt, target_thread_id, queued, ack_sent, source_message))
                return 1

            async def fail_run_prompt_flow(*args, **kwargs):
                raise AssertionError("queue_next immediate should use enqueue_thread_ask")

            bot.is_thread_runner_busy = runner_idle
            bot.enqueue_thread_ask = fake_enqueue_thread_ask
            bot.run_prompt_flow = fail_run_prompt_flow

            message = FakeMessage()
            view = bot.BusyChoiceView(message, "please queue", target_thread_id="thread-1")
            button = next(item for item in view.children if getattr(item, "label", "") == "Queue next")
            interaction = FakeInteraction(command_name="ask", channel_id=222)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await button.callback(interaction)
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(
                calls,
                [(message.channel, "please queue", "thread-1", False, True, message)],
            )
            self.assertEqual(interaction.message.edits, [None])
            self.assertEqual(interaction.followup.messages, ["No active job now. Starting this message."])
            self.assertIn("queue_next_immediate user=242286902982606848", log_text)
            self.assertIn("component_message_components_cleared context=busy_choice_queue", log_text)
            self.assertIn("prompt_len=12", log_text)
            self.assertNotIn("prompt=please queue", log_text)
            self.assertIn("queue_next_immediate_enqueued user=242286902982606848", log_text)
        finally:
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.enqueue_thread_ask = original_enqueue_thread_ask
            bot.run_prompt_flow = original_run_prompt_flow

    async def test_thread_runner_job_failure_reports_short_channel_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            channel = FakeTarget()
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.report_thread_runner_job_failed({"channel": channel}, "thread-1")
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(
            channel.messages,
            [("Queued ask failed. Check codex_discord_bot.log.", None)],
        )
        self.assertIn("thread_runner_job_failure_reported target=thread-1", log_text)

    async def test_run_prompt_flow_chunks_long_context_warning(self) -> None:
        original_get_thread_runner = bot.get_thread_runner
        original_build_context_warning = bot.build_context_warning
        original_enqueue_thread_ask = bot.enqueue_thread_ask
        try:
            async def fake_get_thread_runner(target_thread_id):
                return {"active": False, "queue": asyncio.Queue()}

            async def fake_enqueue_thread_ask(
                channel,
                prompt,
                target_thread_id,
                *,
                queued=False,
                ack_sent=False,
                source_message=None,
            ):
                return 1

            bot.get_thread_runner = fake_get_thread_runner
            bot.build_context_warning = lambda target_thread_id: "x" * 4100
            bot.enqueue_thread_ask = fake_enqueue_thread_ask
            channel = FakeTarget()

            await bot.run_prompt_flow(channel, "please run", target_thread_id="thread-1")

            sent = [content for content, _view in channel.messages]
            self.assertGreater(len(sent), 1)
            self.assertTrue(all(len(content) <= bot.DISCORD_MAX_LEN for content in sent))
            self.assertTrue(sent[0].startswith("x"))
            self.assertNotIn("Ask received. Sending to Codex.", "\n".join(sent))
        finally:
            bot.get_thread_runner = original_get_thread_runner
            bot.build_context_warning = original_build_context_warning
            bot.enqueue_thread_ask = original_enqueue_thread_ask

    async def test_run_prompt_flow_queues_runner_busy_without_steer_view(self) -> None:
        original_get_thread_runner = bot.get_thread_runner
        original_build_context_warning = bot.build_context_warning
        original_enqueue_thread_ask = bot.enqueue_thread_ask
        calls: list[tuple[str, str | None, bool, bool, object | None]] = []
        try:
            async def fake_get_thread_runner(target_thread_id):
                return {"active": True, "queue": asyncio.Queue()}

            async def fake_enqueue_thread_ask(
                channel,
                prompt,
                target_thread_id,
                *,
                queued=False,
                ack_sent=False,
                source_message=None,
            ):
                calls.append((prompt, target_thread_id, queued, ack_sent, source_message))
                return 2

            bot.get_thread_runner = fake_get_thread_runner
            bot.build_context_warning = lambda target_thread_id: ""
            bot.enqueue_thread_ask = fake_enqueue_thread_ask
            channel = FakeTarget()
            source_message = FakeMessage()

            await bot.run_prompt_flow(
                channel,
                "please queue",
                source_message=source_message,
                target_thread_id="thread-1",
            )

            self.assertEqual(
                channel.messages,
                [("Queued in this Codex thread at position 2.", None)],
            )
            self.assertEqual(calls, [("please queue", "thread-1", True, False, source_message)])
        finally:
            bot.get_thread_runner = original_get_thread_runner
            bot.build_context_warning = original_build_context_warning
            bot.enqueue_thread_ask = original_enqueue_thread_ask

    async def test_run_prompt_flow_starts_distinct_target_threads_independently(self) -> None:
        original_run_prompt_and_send = bot.run_prompt_and_send
        original_build_context_warning = bot.build_context_warning
        target_ids = ["thread-a", "thread-b"]
        started: list[tuple[str | None, str, object]] = []
        both_started = asyncio.Event()
        release = asyncio.Event()
        try:
            async with bot.THREAD_RUNNERS_LOCK:
                for target_id in target_ids:
                    bot.THREAD_RUNNERS.pop(bot.normalize_runner_key(target_id), None)

            async def fake_run_prompt_and_send(
                channel,
                prompt,
                *,
                queued=False,
                ack_sent=False,
                source_message=None,
                target_thread_id=None,
            ):
                started.append((target_thread_id, prompt, channel))
                if len(started) == len(target_ids):
                    both_started.set()
                await both_started.wait()
                await release.wait()

            bot.run_prompt_and_send = fake_run_prompt_and_send
            bot.build_context_warning = lambda target_thread_id: ""
            channel_a = FakeTarget(channel_id=101)
            channel_b = FakeTarget(channel_id=102)

            tasks = [
                asyncio.create_task(bot.run_prompt_flow(channel_a, "first", target_thread_id="thread-a")),
                asyncio.create_task(bot.run_prompt_flow(channel_b, "second", target_thread_id="thread-b")),
            ]
            await asyncio.wait_for(both_started.wait(), timeout=1)

            self.assertCountEqual([target_thread_id for target_thread_id, _prompt, _channel in started], target_ids)
            self.assertEqual(channel_a.messages, [("Discord ask submitted.\nmessage: first", None)])
            self.assertEqual(channel_b.messages, [("Discord ask submitted.\nmessage: second", None)])
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        finally:
            release.set()
            for target_id in target_ids:
                runner = await bot.get_thread_runner(target_id)
                queue = runner.get("queue")
                if isinstance(queue, asyncio.Queue):
                    try:
                        await asyncio.wait_for(queue.join(), timeout=1)
                    except asyncio.TimeoutError:
                        pass
                task = runner.get("task")
                if isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            async with bot.THREAD_RUNNERS_LOCK:
                for target_id in target_ids:
                    bot.THREAD_RUNNERS.pop(bot.normalize_runner_key(target_id), None)
            bot.run_prompt_and_send = original_run_prompt_and_send
            bot.build_context_warning = original_build_context_warning

    async def test_run_prompt_flow_sends_ask_start_repeatback(self) -> None:
        original_get_thread_runner = bot.get_thread_runner
        original_build_context_warning = bot.build_context_warning
        original_enqueue_thread_ask = bot.enqueue_thread_ask
        calls: list[tuple[str, str | None, bool]] = []
        try:
            async def fake_get_thread_runner(target_thread_id):
                return {"active": False, "queue": asyncio.Queue()}

            async def fake_enqueue_thread_ask(
                channel,
                prompt,
                target_thread_id,
                *,
                queued=False,
                ack_sent=False,
                source_message=None,
            ):
                calls.append((prompt, target_thread_id, ack_sent))
                return 1

            bot.get_thread_runner = fake_get_thread_runner
            bot.build_context_warning = lambda target_thread_id: ""
            bot.enqueue_thread_ask = fake_enqueue_thread_ask
            channel = FakeTarget()

            await bot.run_prompt_flow(
                channel,
                "First sentence. second sentence\nthird line",
                target_thread_id="thread-1",
            )

            self.assertEqual(
                channel.messages,
                [("Discord ask submitted.\nmessage: First sentence.", None)],
            )
            self.assertEqual(calls, [("First sentence. second sentence\nthird line", "thread-1", True)])
        finally:
            bot.get_thread_runner = original_get_thread_runner
            bot.build_context_warning = original_build_context_warning
            bot.enqueue_thread_ask = original_enqueue_thread_ask

    async def test_thread_runner_accepts_send_capable_channel(self) -> None:
        original_run_prompt_and_send = bot.run_prompt_and_send
        calls: list[tuple[object, str, str | None, bool]] = []
        target_thread_id = "duck-channel-thread"
        try:
            async def fake_run_prompt_and_send(
                channel,
                prompt,
                *,
                queued=False,
                ack_sent=False,
                source_message=None,
                target_thread_id=None,
            ):
                calls.append((channel, prompt, target_thread_id, ack_sent))

            bot.run_prompt_and_send = fake_run_prompt_and_send
            channel = FakeTarget()

            await bot.enqueue_thread_ask(
                channel,
                "hello",
                target_thread_id,
                ack_sent=True,
            )
            runner = await bot.get_thread_runner(target_thread_id)
            queue = runner["queue"]
            self.assertIsInstance(queue, asyncio.Queue)
            await asyncio.wait_for(queue.join(), timeout=1)

            self.assertEqual(calls, [(channel, "hello", target_thread_id, True)])
        finally:
            bot.run_prompt_and_send = original_run_prompt_and_send
            runner = await bot.get_thread_runner(target_thread_id)
            task = runner.get("task")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            async with bot.THREAD_RUNNERS_LOCK:
                bot.THREAD_RUNNERS.pop(bot.normalize_runner_key(target_thread_id), None)

    async def test_interactive_approval_prompt_with_view_is_truncated(self) -> None:
        channel = FakeTarget()
        await bot.send_interactive_prompt(
            channel,
            "thread-1",
            "taxlab:1",
            bot.INTERACTIVE_STATE_APPROVAL,
            "x" * 4100,
            [],
        )

        self.assertEqual(len(channel.messages), 1)
        content, view = channel.messages[0]
        self.assertLessEqual(len(content), bot.DISCORD_MAX_LEN)
        self.assertTrue(content.endswith("[truncated for Discord]"))
        self.assertIsInstance(view, bot.ApprovalView)

    def test_busy_choice_message_is_single_discord_message(self) -> None:
        original_build_context_warning = bot.build_context_warning
        try:
            bot.build_context_warning = lambda target_thread_id: "warning " + ("w" * 900)
            content = bot.build_busy_choice_message("x" * 4100, "thread-1")

            self.assertLessEqual(len(content), bot.DISCORD_MAX_LEN)
            self.assertIn("[prompt truncated for Discord]", content)
            self.assertTrue(content.endswith("Choose the Discord action for this message."))
        finally:
            bot.build_context_warning = original_build_context_warning

    def test_prefix_bridge_action_builds_shared_argv(self) -> None:
        def fake_resolve(channel_id, ref):
            return ["--target", f"{channel_id}:{ref or '-'}"]

        status = commands.build_prefix_bridge_action(
            "status",
            "abc",
            222,
            resolve_target_args_func=fake_resolve,
        )
        open_abort = commands.build_prefix_bridge_action(
            "open_abort",
            "taxlab:1",
            222,
            resolve_target_args_func=fake_resolve,
        )
        missing_use = commands.build_prefix_bridge_action(
            "use",
            "",
            222,
            resolve_target_args_func=fake_resolve,
        )
        confirm_delete = commands.build_prefix_bridge_action(
            "confirm_delete_archive",
            "thread-1",
            222,
            resolve_target_args_func=fake_resolve,
        )

        self.assertEqual(status.argv, ["status", "--target", "222:abc"])
        self.assertEqual(open_abort.argv, ["open", "--abort", "taxlab:1"])
        self.assertEqual(missing_use.usage, "Usage: !use <ref>")
        self.assertEqual(confirm_delete.argv, ["delete_archive", "--confirm", "thread-1"])
        self.assertEqual(commands.parse_usage_days("bad").usage, "Usage: !usage [days]")
        self.assertEqual(commands.parse_bridge_sync_limit("bridge", "bad 1").usage, "Usage: !bridge sync [limit]")
        self.assertEqual(commands.parse_mirror_action("sync bad").usage, "Usage: !mirror sync [limit]")
        self.assertEqual(commands.parse_mirror_action("doctor").subcommand, "check")

    async def test_archive_list_alias_routes_to_archived_list(self) -> None:
        original_run_bridge_and_send = bot.run_bridge_and_send
        calls: list[tuple[list[str], str]] = []

        async def fake_run_bridge_and_send(target, argv, title, failure_title=None):
            calls.append((argv, title))
            await target.send("ok")
            return 0, "ok"

        try:
            bot.run_bridge_and_send = fake_run_bridge_and_send
            message = FakeMessage()
            await bot.handle_prefix_command(None, message, "archive_list 5")

            self.assertEqual(calls, [(["archived_list", "--limit", "5"], "Archived list")])
            self.assertEqual(message.channel.messages, [("ok", None)])
        finally:
            bot.run_bridge_and_send = original_run_bridge_and_send

    async def test_prefix_open_abort_routes_to_shared_bridge_action(self) -> None:
        original_run_bridge_and_send = bot.run_bridge_and_send
        calls: list[tuple[list[str], str]] = []

        async def fake_run_bridge_and_send(target, argv, title, failure_title=None):
            calls.append((argv, title))
            await target.send("ok")
            return 0, "ok"

        try:
            bot.run_bridge_and_send = fake_run_bridge_and_send
            message = FakeMessage()
            await bot.handle_prefix_command(None, message, "open_abort taxlab:1")

            self.assertEqual(calls, [(["open", "--abort", "taxlab:1"], "Open")])
            self.assertEqual(message.channel.messages, [("ok", None)])
        finally:
            bot.run_bridge_and_send = original_run_bridge_and_send

    async def test_unknown_prefix_command_response_is_bounded(self) -> None:
        message = FakeMessage()
        await bot.handle_prefix_command(None, message, "x" * 4100)

        self.assertEqual(len(message.channel.messages), 1)
        content, view = message.channel.messages[0]
        self.assertLessEqual(len(content), 100)
        self.assertIsNone(view)
        self.assertTrue(content.startswith("Unknown command: !"))
        self.assertTrue(content.endswith("..."))

    async def test_new_thread_flow_uses_resolved_cwd_and_mirrors(self) -> None:
        original_resolve_cwd = bot.resolve_discord_new_thread_cwd
        original_resolve_project_channel = bot.resolve_discord_new_thread_project_channel_id
        original_run_bridge_command = bot.run_bridge_command
        original_mirror_single = bot.mirror_single_codex_thread
        original_choose_thread = bot.bridge.choose_thread
        argv_seen: list[str] = []
        mirror_calls: list[tuple[str, int | None]] = []
        try:
            bot.resolve_discord_new_thread_cwd = lambda channel_id: r"C:\taxlab"
            bot.resolve_discord_new_thread_project_channel_id = lambda channel_id, project_key: 777
            bot.bridge.choose_thread = lambda thread_id, ref: bot.bridge.ThreadInfo(
                id=thread_id,
                title="new",
                cwd=r"C:\taxlab",
                updated_at=1,
                rollout_path="",
                model="",
                reasoning_effort="",
                tokens_used=0,
            )

            def fake_run_bridge_command(argv):
                argv_seen.extend(argv)
                return 0, "target_thread: thread-new\ncwd: C:\\taxlab"

            async def fake_mirror_single_codex_thread(
                fake_bot,
                thread_id,
                *,
                preferred_project_channel_id=None,
            ):
                mirror_calls.append((thread_id, preferred_project_channel_id))
                return SimpleNamespace(id=999)

            bot.run_bridge_command = fake_run_bridge_command
            bot.mirror_single_codex_thread = fake_mirror_single_codex_thread

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    exit_code, output = await bot.run_discord_new_thread(
                        SimpleNamespace(),
                        222,
                        "start here",
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(argv_seen, ["new", "--cwd", r"C:\taxlab", "start here"])
            self.assertEqual(mirror_calls, [("thread-new", 777)])
            self.assertIn("target_thread: thread-new", output)
            self.assertIn("Mirrored Discord thread: <#999>", output)
        finally:
            bot.resolve_discord_new_thread_cwd = original_resolve_cwd
            bot.resolve_discord_new_thread_project_channel_id = original_resolve_project_channel
            bot.run_bridge_command = original_run_bridge_command
            bot.mirror_single_codex_thread = original_mirror_single
            bot.bridge.choose_thread = original_choose_thread

    async def test_new_thread_failure_does_not_mirror(self) -> None:
        original_resolve_cwd = bot.resolve_discord_new_thread_cwd
        original_run_bridge_command = bot.run_bridge_command
        original_mirror_single = bot.mirror_single_codex_thread
        argv_seen: list[str] = []
        mirror_calls: list[str] = []
        try:
            bot.resolve_discord_new_thread_cwd = lambda channel_id: None

            def fake_run_bridge_command(argv):
                argv_seen.extend(argv)
                return 1, "ERROR: cannot create thread"

            async def fake_mirror_single_codex_thread(
                fake_bot,
                thread_id,
                *,
                preferred_project_channel_id=None,
            ):
                mirror_calls.append(thread_id)
                return SimpleNamespace(id=999)

            bot.run_bridge_command = fake_run_bridge_command
            bot.mirror_single_codex_thread = fake_mirror_single_codex_thread

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    exit_code, output = await bot.run_discord_new_thread(
                        SimpleNamespace(),
                        222,
                        "start here",
                    )
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(exit_code, 1)
            self.assertEqual(argv_seen, ["new", "start here"])
            self.assertEqual(mirror_calls, [])
            self.assertIn("New failed (exit 1)", output)
            self.assertNotIn("Mirrored Discord thread:", output)
            self.assertIn("new_thread_cwd channel=222 cwd=default", log_text)
            self.assertNotIn("new_thread_mirrored", log_text)
        finally:
            bot.resolve_discord_new_thread_cwd = original_resolve_cwd
            bot.run_bridge_command = original_run_bridge_command
            bot.mirror_single_codex_thread = original_mirror_single

    def test_bridge_new_default_timeout_handles_slow_local_state_persistence(self) -> None:
        original_load_recent_threads = bridge.load_recent_threads
        original_spawn_runner = bridge.spawn_background_new_thread_runner
        original_resolve_cwd = bridge.resolve_new_thread_cwd
        original_wait_delivery = bridge.wait_for_prompt_delivery
        original_set_selected = bridge.set_selected_thread_id
        original_sync_session_index = bridge.sync_session_index_with_state
        original_time = bridge.time.time
        original_sleep = bridge.time.sleep

        class FakeRunner:
            pid = 1234

            def poll(self) -> None:
                return None

        selected_ids: list[str] = []
        clock = {"now": 0.0}
        old_thread = bridge.ThreadInfo(
            id="old-thread",
            title="old",
            cwd=r"C:\project",
            updated_at=1,
            rollout_path="",
            model="",
            reasoning_effort="",
            tokens_used=0,
        )

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                session_path = Path(temp_dir) / "session.jsonl"
                session_path.write_text("", encoding="utf-8")
                new_thread = bridge.ThreadInfo(
                    id="new-thread",
                    title="new",
                    cwd=str(Path(temp_dir)),
                    updated_at=2,
                    rollout_path=str(session_path),
                    model="",
                    reasoning_effort="",
                    tokens_used=0,
                )

                def fake_load_recent_threads(limit=20):
                    if clock["now"] >= 9.0:
                        return [new_thread, old_thread]
                    return [old_thread]

                bridge.load_recent_threads = fake_load_recent_threads
                bridge.spawn_background_new_thread_runner = lambda prompt, cwd: FakeRunner()
                bridge.resolve_new_thread_cwd = lambda cwd: str(Path(temp_dir))
                bridge.wait_for_prompt_delivery = lambda session_offsets, prompt, timeout_sec=4.0: new_thread
                bridge.set_selected_thread_id = selected_ids.append
                bridge.sync_session_index_with_state = lambda: 2
                bridge.time.time = lambda: clock["now"]
                bridge.time.sleep = lambda seconds: clock.__setitem__("now", clock["now"] + seconds)

                args = bridge.build_parser().parse_args(["new", "--cwd", str(Path(temp_dir)), "start here"])
                self.assertEqual(args.create_timeout, 30.0)

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = bridge.command_new(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(selected_ids, ["new-thread"])
            self.assertIn("target_thread: new-thread", stdout.getvalue())
        finally:
            bridge.load_recent_threads = original_load_recent_threads
            bridge.spawn_background_new_thread_runner = original_spawn_runner
            bridge.resolve_new_thread_cwd = original_resolve_cwd
            bridge.wait_for_prompt_delivery = original_wait_delivery
            bridge.set_selected_thread_id = original_set_selected
            bridge.sync_session_index_with_state = original_sync_session_index
            bridge.time.time = original_time
            bridge.time.sleep = original_sleep

    async def test_slash_new_dispatch_logs_and_sends_response(self) -> None:
        original_run_discord_new_thread = bot.run_discord_new_thread
        calls: list[tuple[object, int | None, str]] = []
        try:
            async def fake_run_discord_new_thread(fake_bot, channel_id, prompt):
                calls.append((fake_bot, channel_id, prompt))
                return 0, "New\n\ntarget_thread: thread-new"

            bot.run_discord_new_thread = fake_run_discord_new_thread
            fake_bot = SimpleNamespace()
            interaction = FakeInteraction(command_name="new", channel_id=222)

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.handle_slash_new(fake_bot, interaction, "start here")
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(calls, [(fake_bot, 222, "start here")])
            self.assertEqual(interaction.followup.messages, ["New\n\ntarget_thread: thread-new"])
            self.assertEqual(interaction.followup.kwargs, [{}])
            self.assertIn("slash_new_dispatch channel=222", log_text)
            self.assertIn("user=242286902982606848", log_text)
            self.assertIn("prompt_len=10", log_text)
            self.assertIn("slash_new_done channel=222 exit=0", log_text)
            self.assertIn("slash_response_start command=new title='New' exit=0", log_text)
            self.assertIn("slash_response_sent command=new title='New' exit=0", log_text)
        finally:
            bot.run_discord_new_thread = original_run_discord_new_thread

    async def test_slash_ask_routes_to_existing_ask_flow(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_handle_plain_ask = bot.handle_plain_ask
        calls: list[tuple[object, str, str | None]] = []

        async def fake_handle_plain_ask(message, prompt, *, target_thread_id=None):
            calls.append((message, prompt, target_thread_id))

        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.handle_plain_ask = fake_handle_plain_ask
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            interaction.channel = FakeTarget()

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.handle_slash_ask(interaction, "please run")
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(interaction.followup.messages, ["Ask handling posted in this channel."])
            self.assertEqual(interaction.followup.kwargs, [{"ephemeral": True}])
            self.assertEqual(len(calls), 1)
            source_message, prompt, target_thread_id = calls[0]
            self.assertEqual(prompt, "please run")
            self.assertEqual(target_thread_id, "thread-1")
            self.assertIs(source_message.channel, interaction.channel)
            self.assertIs(source_message.author, interaction.user)
            self.assertIn("slash_ask_dispatch command=ask channel=222", log_text)
            self.assertIn("target_source=mirror target=thread-1", log_text)
            self.assertIn("prompt_len=10", log_text)
            self.assertIn("slash_ask_ack_sent command=ask channel=222", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.handle_plain_ask = original_handle_plain_ask

    async def test_slash_ask_blocks_project_parent_fallback(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_describe_project = bot.describe_mirrored_project_channel
        original_handle_plain_ask = bot.handle_plain_ask
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: None
            bot.describe_mirrored_project_channel = (
                lambda channel_id: "`taxlab` project channel has multiple Codex threads."
            )

            async def fail_handle_plain_ask(message, prompt, *, target_thread_id=None):
                raise AssertionError("project parent slash ask must not fall back to selected thread")

            bot.handle_plain_ask = fail_handle_plain_ask
            interaction = FakeInteraction(command_name="ask", channel_id=333)
            interaction.channel = FakeTarget()

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.handle_slash_ask(interaction, "please run")
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(
                interaction.followup.messages,
                ["`taxlab` project channel has multiple Codex threads."],
            )
            self.assertIn("slash_ask_blocked command=ask channel=333", log_text)
            self.assertIn("reason=project_parent", log_text)
            self.assertNotIn("slash_ask_dispatch", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.describe_mirrored_project_channel = original_describe_project
            bot.handle_plain_ask = original_handle_plain_ask

    async def test_slash_ask_target_busy_auto_queues_without_steer_view(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_busy_state = bot.get_busy_state_for_thread
        original_build_context_warning = bot.build_context_warning
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        original_run_prompt_flow = bot.run_prompt_flow
        calls: list[tuple[object, str, str | None, object | None]] = []
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_busy_state_for_thread = lambda target_thread_id: ("busy", "thread-1", "taxlab:1")
            bot.build_context_warning = lambda target_thread_id: ""

            async def runner_idle(target_thread_id):
                return False

            bot.is_thread_runner_busy = runner_idle

            async def fake_run_prompt_flow(channel, prompt, *, source_message=None, target_thread_id=None):
                calls.append((channel, prompt, target_thread_id, source_message))

            bot.run_prompt_flow = fake_run_prompt_flow
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            interaction.channel = FakeTarget()

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.handle_slash_ask(interaction, "please steer")
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(interaction.followup.messages, ["Ask handling posted in this channel."])
            self.assertEqual(
                interaction.channel.messages,
                [("Queued in this Codex thread at position 1.\n\nCurrent target state: busy.", None)],
            )
            self.assertEqual(calls, [])
            self.assertIn("slash_ask_dispatch command=ask channel=222", log_text)
            self.assertIn("target_busy_auto_queued target=thread-1", log_text)
            self.assertNotIn("busy_choice_sent reason=codex_busy_preflight", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.build_context_warning = original_build_context_warning
            bot.is_thread_runner_busy = original_is_thread_runner_busy
            bot.run_prompt_flow = original_run_prompt_flow

    def test_filter_mirrorable_threads_ignores_deleted_workspace_projects(self) -> None:
        original_global_state_path = bot.bridge.GLOBAL_STATE_PATH
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                temp_path = Path(temp_dir)
                saved = temp_path / "saved"
                deleted = temp_path / "deleted"
                state_path = temp_path / "global.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "project-order": [str(saved)],
                            "electron-saved-workspace-roots": [str(saved)],
                        }
                    ),
                    encoding="utf-8",
                )
                bot.bridge.GLOBAL_STATE_PATH = state_path
                threads = [
                    bot.bridge.ThreadInfo(
                        id="saved-thread",
                        title="saved",
                        cwd=str(saved),
                        updated_at=1,
                        rollout_path="saved.jsonl",
                        model="gpt",
                        reasoning_effort="high",
                        tokens_used=1,
                    ),
                    bot.bridge.ThreadInfo(
                        id="deleted-thread",
                        title="deleted",
                        cwd=str(deleted),
                        updated_at=2,
                        rollout_path="deleted.jsonl",
                        model="gpt",
                        reasoning_effort="high",
                        tokens_used=1,
                    ),
                    bot.bridge.ThreadInfo(
                        id="projectless-thread",
                        title="chat",
                        cwd="",
                        updated_at=3,
                        rollout_path="chat.jsonl",
                        model="gpt",
                        reasoning_effort="high",
                        tokens_used=1,
                    ),
                ]

                filtered = bot.filter_mirrorable_threads(threads)

            self.assertEqual(
                [thread.id for thread in filtered],
                ["saved-thread", "projectless-thread"],
            )
        finally:
            bot.bridge.GLOBAL_STATE_PATH = original_global_state_path

    def test_new_thread_cwd_prefers_mirrored_thread_cwd(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        original_choose_thread = bot.bridge.choose_thread
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                expected_cwd = str(Path(temp_dir) / "taxlab")
                bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_threads (
                            codex_thread_id, project_key, thread_title,
                            discord_channel_id, discord_thread_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("thread-1", expected_cwd, "title", 111, 222, 1.0),
                    )
                bot.bridge.choose_thread = lambda thread_id, cwd: SimpleNamespace(cwd=expected_cwd)

                self.assertEqual(bot.resolve_discord_new_thread_cwd(222), expected_cwd)
        finally:
            bot.MIRROR_DB_PATH = old_db_path
            bot.bridge.choose_thread = original_choose_thread

    def test_new_thread_cwd_falls_back_to_project_channel_path(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            project_path = Path(temp_dir) / "project"
            project_path.mkdir()
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_projects (
                            project_key, project_name, discord_channel_id, updated_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (str(project_path), "project", 333, 1.0),
                    )

                self.assertEqual(bot.resolve_discord_new_thread_cwd(333), str(project_path))
            finally:
                bot.MIRROR_DB_PATH = old_db_path

    def test_new_thread_project_channel_prefers_invoking_thread_parent(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_threads (
                            codex_thread_id, project_key, thread_title,
                            discord_channel_id, discord_thread_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("thread-1", r"c:\taxlab", "title", 111, 222, 1.0),
                    )

                self.assertEqual(
                    bot.resolve_discord_new_thread_project_channel_id(222, r"c:\taxlab"),
                    111,
                )
                self.assertIsNone(
                    bot.resolve_discord_new_thread_project_channel_id(222, r"c:\other")
                )
            finally:
                bot.MIRROR_DB_PATH = old_db_path

    def test_new_thread_project_channel_matches_normalized_invoking_thread_parent(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_threads (
                            codex_thread_id, project_key, thread_title,
                            discord_channel_id, discord_thread_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        ("thread-1", r"c:\taxlab", "title", 111, 222, 1.0),
                    )

                self.assertEqual(
                    bot.resolve_discord_new_thread_project_channel_id(222, r"\\?\C:\taxlab"),
                    111,
                )
            finally:
                bot.MIRROR_DB_PATH = old_db_path

    def test_new_thread_project_channel_accepts_project_parent_channel(self) -> None:
        old_db_path = bot.MIRROR_DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            bot.MIRROR_DB_PATH = Path(temp_dir) / "mirror.sqlite"
            try:
                bot.init_mirror_db()
                with sqlite3.connect(bot.MIRROR_DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO mirror_projects (
                            project_key, project_name, discord_channel_id, updated_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (r"c:\taxlab", "taxlab", 111, 1.0),
                    )

                self.assertEqual(
                    bot.resolve_discord_new_thread_project_channel_id(111, r"c:\taxlab"),
                    111,
                )
            finally:
                bot.MIRROR_DB_PATH = old_db_path

    async def test_delete_stale_project_channels_deletes_mirror_text_channel(self) -> None:
        original_text_channel = bot.discord.TextChannel

        class FakeTextChannel:
            def __init__(self) -> None:
                self.id = 111
                self.category_id = 999
                self.topic = "Codex project mirror: stale"
                self.deleted_reasons: list[str] = []

            async def delete(self, reason: str) -> None:
                self.deleted_reasons.append(reason)

        channel = FakeTextChannel()
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 111 else None,
            fetch_channel=lambda channel_id: channel,
        )
        category = SimpleNamespace(id=999)
        try:
            bot.discord.TextChannel = FakeTextChannel
            result = await bot.delete_stale_project_channels(
                guild,
                category,
                [("stale-project", "stale", 111)],
            )
        finally:
            bot.discord.TextChannel = original_text_channel

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(channel.deleted_reasons), 1)
        self.assertIn("stale-project", channel.deleted_reasons[0])

    async def test_delete_stale_project_channels_skips_non_mirror_text_channel(self) -> None:
        original_text_channel = bot.discord.TextChannel

        class FakeTextChannel:
            def __init__(self) -> None:
                self.id = 111
                self.category_id = 123
                self.topic = "general"
                self.deleted = False

            async def delete(self, reason: str) -> None:
                self.deleted = True

        channel = FakeTextChannel()
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 111 else None,
            fetch_channel=lambda channel_id: channel,
        )
        category = SimpleNamespace(id=999)
        try:
            bot.discord.TextChannel = FakeTextChannel
            result = await bot.delete_stale_project_channels(
                guild,
                category,
                [("stale-project", "stale", 111)],
            )
        finally:
            bot.discord.TextChannel = original_text_channel

        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(channel.deleted)

    async def test_discord_ask_relay_sends_quiet_progress_notice(self) -> None:
        target = FakeTarget()
        relay = bot.DiscordAskRelay(
            asyncio.get_running_loop(),
            target,
            "thread-1",
            "project:1",
            quiet_notice_delay_sec=0.01,
        )

        relay.feed_line("[waiting_for_final_answer]")
        await asyncio.sleep(0.05)
        await asyncio.to_thread(relay.finish)

        self.assertEqual(len(target.messages), 1)
        self.assertIn("Codex is still working.", target.messages[0][0])
        self.assertIn("compacts context", target.messages[0][0])
        self.assertTrue(relay.quiet_notice_sent)
        self.assertFalse(relay.sent_live)

    async def test_discord_ask_relay_default_quiet_progress_notice_is_disabled(self) -> None:
        target = FakeTarget()
        relay = bot.DiscordAskRelay(
            asyncio.get_running_loop(),
            target,
            "thread-1",
            "project:1",
        )

        relay.feed_line("[waiting_for_final_answer]")
        await asyncio.sleep(0.05)
        await asyncio.to_thread(relay.finish)

        self.assertEqual(target.messages, [])
        self.assertFalse(relay.quiet_notice_sent)
        self.assertFalse(relay.sent_live)

    async def test_discord_ask_relay_cancels_quiet_notice_after_final(self) -> None:
        target = FakeTarget()
        relay = bot.DiscordAskRelay(
            asyncio.get_running_loop(),
            target,
            "thread-1",
            "project:1",
            quiet_notice_delay_sec=0.05,
        )

        relay.feed_line("[waiting_for_final_answer]")
        relay.feed_line("[final_answer]")
        relay.feed_line("done")
        await asyncio.to_thread(relay.finish)
        await asyncio.sleep(0.08)

        self.assertEqual(target.messages, [("done", None)])
        self.assertFalse(relay.quiet_notice_sent)
        self.assertTrue(relay.sent_live)

    async def test_discord_ask_relay_preserves_send_order(self) -> None:
        original_send_chunks = bot.send_chunks
        sent: list[str] = []
        try:
            async def fake_send_chunks(channel, text):
                if str(text).startswith("In progress"):
                    await asyncio.sleep(0.05)
                sent.append(text)

            bot.send_chunks = fake_send_chunks
            target = FakeTarget()
            relay = bot.DiscordAskRelay(
                asyncio.get_running_loop(),
                target,
                "thread-1",
                "project:1",
                quiet_notice_delay_sec=-1,
            )

            relay.feed_line("[commentary]")
            relay.feed_line("checking order")
            relay.feed_line("[final_answer]")
            relay.feed_line("done")
            relay.feed_line("[ready]")
            await asyncio.to_thread(relay.finish)

            self.assertEqual(sent, ["In progress\n\nchecking order", "done"])
            self.assertTrue(relay.sent_live)
            self.assertTrue(relay.saw_final)
        finally:
            bot.send_chunks = original_send_chunks

    async def test_steering_watch_streams_final_to_discord_relay(self) -> None:
        original_watch_for_final_answer = bridge.watch_for_final_answer
        try:
            def fake_watch_for_final_answer(
                session_path,
                start_offset,
                timeout_sec,
                include_commentary,
                stream_live=False,
                stream_label="",
                stream_callback=None,
            ):
                if stream_live and stream_callback is not None:
                    for line in ["[final_answer]", "steered done", ""]:
                        stream_callback(line)
                return {
                    "status": "final",
                    "commentary": [],
                    "final_answer": "steered done",
                    "streamed_live": True,
                    "final_streamed_live": True,
                }

            bridge.watch_for_final_answer = fake_watch_for_final_answer
            target = FakeTarget()
            relay = bot.DiscordAskRelay(
                asyncio.get_running_loop(),
                target,
                "thread-1",
                "project:1",
                quiet_notice_delay_sec=0.05,
            )
            steering_result = bot.SteeringPromptResult(
                0,
                "Steering sent",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            exit_code, _output = await asyncio.to_thread(
                bot.run_steering_watch_stream,
                steering_result,
                relay,
                timeout_sec=1,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(target.messages, [("steered done", None)])
            self.assertTrue(relay.sent_live)
            self.assertTrue(relay.saw_final)
        finally:
            bridge.watch_for_final_answer = original_watch_for_final_answer

    async def test_steering_watch_streams_commentary_to_discord_relay(self) -> None:
        original_watch_for_final_answer = bridge.watch_for_final_answer
        try:
            def fake_watch_for_final_answer(
                session_path,
                start_offset,
                timeout_sec,
                include_commentary,
                stream_live=False,
                stream_label="",
                stream_callback=None,
            ):
                if stream_live and stream_callback is not None:
                    for line in ["[commentary]", "checking files", "", "[final_answer]", "done", ""]:
                        stream_callback(line)
                return {
                    "status": "final",
                    "commentary": ["checking files"],
                    "final_answer": "done",
                    "streamed_live": True,
                    "final_streamed_live": True,
                }

            bridge.watch_for_final_answer = fake_watch_for_final_answer
            target = FakeTarget()
            relay = bot.DiscordAskRelay(
                asyncio.get_running_loop(),
                target,
                "thread-1",
                "project:1",
                quiet_notice_delay_sec=0.05,
            )
            steering_result = bot.SteeringPromptResult(
                0,
                "Steering sent",
                target_thread_id="thread-1",
                target_ref="project:1",
                session_path="session.jsonl",
                start_offset=10,
            )

            exit_code, _output = await asyncio.to_thread(
                bot.run_steering_watch_stream,
                steering_result,
                relay,
                timeout_sec=1,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                target.messages,
                [("In progress\n\nchecking files", None), ("done", None)],
            )
            self.assertTrue(relay.sent_live)
            self.assertTrue(relay.saw_final)
        finally:
            bridge.watch_for_final_answer = original_watch_for_final_answer

    def test_bridge_marks_final_streamed_live_separately(self) -> None:
        original_read_new_session_events = bridge.read_new_session_events
        try:
            def fake_read_new_session_events(session_path, cursor):
                if cursor:
                    return [], cursor
                return [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "commentary",
                            "content": [{"type": "output_text", "text": "working"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    },
                ], 1

            bridge.read_new_session_events = fake_read_new_session_events
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = bridge.watch_for_final_answer(
                    Path("unused.jsonl"),
                    0,
                    timeout_sec=1,
                    include_commentary=True,
                    stream_live=True,
                )

            self.assertTrue(result["streamed_live"])
            self.assertTrue(result["final_streamed_live"])
            self.assertIn("[commentary]", stdout.getvalue())
            self.assertIn("[final_answer]", stdout.getvalue())
        finally:
            bridge.read_new_session_events = original_read_new_session_events


if __name__ == "__main__":
    unittest.main()
