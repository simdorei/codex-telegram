import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import codex_discord_bot as bot


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.kwargs: list[dict[str, object]] = []

    async def send(self, content: str, view=None, **kwargs) -> None:
        self.messages.append(content if view is None else (content, view))
        self.kwargs.append(kwargs)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deferred = False
        self.done = False

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append(content)
        self.done = True

    async def defer(self, thinking: bool = False) -> None:
        self.deferred = True
        self.done = True

    def is_done(self) -> bool:
        return self.done


class FakeInteractionMessage:
    def __init__(self) -> None:
        self.edits: list[object | None] = []

    async def edit(self, view=None) -> None:
        self.edits.append(view)


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object | None]] = []

    async def send(self, content: str, view=None) -> None:
        self.messages.append((content, view))


class FakeInteraction:
    def __init__(self, command_name: str = "help", channel_id: int = 12345) -> None:
        self.command = SimpleNamespace(name=command_name)
        self.channel_id = channel_id
        self.followup = FakeFollowup()
        self.response = FakeResponse()
        self.user = SimpleNamespace(id=242286902982606848)
        self.channel = None
        self.message = FakeInteractionMessage()


class FakeMessage:
    def __init__(self) -> None:
        self.channel = FakeTarget()
        self.author = SimpleNamespace(id=242286902982606848)


class FakeBot:
    def is_allowed_user(self, user_id: int | None) -> bool:
        return True

    def is_allowed_channel(self, channel_id: int | None) -> bool:
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
    def test_log_path_override_writes_to_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                bot.log_line("isolated_smoke_log")
                self.assertEqual(bot.get_log_path(), log_path)

            self.assertTrue(log_path.exists())
            self.assertIn("isolated_smoke_log", log_path.read_text(encoding="utf-8"))

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

    def test_choice_views_claim_once_and_disable_buttons(self) -> None:
        input_view = bot.InputChoiceView("thread-1", [("1", "First"), ("2", "Second")])
        self.assertTrue(input_view.claim())
        self.assertFalse(input_view.claim())
        self.assertTrue(all(getattr(item, "disabled", False) for item in input_view.children))

        message = SimpleNamespace(author=SimpleNamespace(id=1), channel=None)
        busy_view = bot.BusyChoiceView(message, "prompt", target_thread_id="thread-1")
        self.assertTrue(busy_view.claim())
        self.assertFalse(busy_view.claim())
        self.assertTrue(all(getattr(item, "disabled", False) for item in busy_view.children))

    def test_help_readme_and_registered_slash_commands_match(self) -> None:
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
        self.assertEqual(command_names, expected_commands)

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

    async def test_busy_plain_ask_shows_busy_choice_view(self) -> None:
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_busy_state = bot.get_busy_state_for_thread
        original_build_context_warning = bot.build_context_warning
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        try:
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_busy_state_for_thread = lambda target_thread_id: ("busy", "thread-1", "taxlab:1")
            bot.build_context_warning = lambda target_thread_id: ""

            async def runner_idle(target_thread_id):
                return False

            bot.is_thread_runner_busy = runner_idle
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                message = FakeMessage()
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.handle_plain_ask(message, "please steer", target_thread_id="thread-1")
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(len(message.channel.messages), 1)
            content, view = message.channel.messages[0]
            self.assertIn("This Codex thread is already working.", content)
            self.assertIn("please steer", content)
            self.assertIsInstance(view, bot.BusyChoiceView)
            self.assertEqual(view.target_thread_id, "thread-1")
            self.assertIn("busy_choice_sent reason=codex_busy_preflight target=thread-1", log_text)
        finally:
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.build_context_warning = original_build_context_warning
            bot.is_thread_runner_busy = original_is_thread_runner_busy

    async def test_ask_busy_failure_shows_busy_choice_view(self) -> None:
        original_resolve_target_ref = bot.resolve_target_ref
        original_run_ask_stream = bot.run_ask_stream
        original_build_context_warning = bot.build_context_warning
        try:
            bot.resolve_target_ref = lambda target_thread_id: (target_thread_id, "taxlab:1")

            def fake_run_ask_stream(prompt, relay, *, force_while_busy=False, wait=True, target_thread_id=None):
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
            self.assertIn("This Codex thread is already working.", content)
            self.assertIn("please steer", content)
            self.assertIsInstance(view, bot.BusyChoiceView)
            self.assertEqual(view.target_thread_id, "thread-1")
            self.assertIn("busy_choice_sent reason=late_busy_failure target=thread-1", log_text)
        finally:
            bot.resolve_target_ref = original_resolve_target_ref
            bot.run_ask_stream = original_run_ask_stream
            bot.build_context_warning = original_build_context_warning

    async def test_steer_now_busy_failure_resends_busy_choice_view(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_build_context_warning = bot.build_context_warning
        try:
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
            self.assertEqual(len(interaction.followup.messages), 1)
            content, followup_view = interaction.followup.messages[0]
            self.assertIn("This Codex thread is already working.", content)
            self.assertIn("please steer", content)
            self.assertIsInstance(followup_view, bot.BusyChoiceView)
            self.assertEqual(followup_view.target_thread_id, "thread-1")
            self.assertNotIn("selected thread is still busy", content.lower())
            self.assertIn("busy_choice_sent reason=steer_busy_failure target=thread-1", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.build_context_warning = original_build_context_warning

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

    async def test_new_thread_flow_uses_resolved_cwd_and_mirrors(self) -> None:
        original_resolve_cwd = bot.resolve_discord_new_thread_cwd
        original_run_bridge_command = bot.run_bridge_command
        original_mirror_single = bot.mirror_single_codex_thread
        argv_seen: list[str] = []
        try:
            bot.resolve_discord_new_thread_cwd = lambda channel_id: r"C:\taxlab"

            def fake_run_bridge_command(argv):
                argv_seen.extend(argv)
                return 0, "target_thread: thread-new\ncwd: C:\\taxlab"

            async def fake_mirror_single_codex_thread(fake_bot, thread_id):
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
            self.assertIn("target_thread: thread-new", output)
            self.assertIn("Mirrored Discord thread: <#999>", output)
        finally:
            bot.resolve_discord_new_thread_cwd = original_resolve_cwd
            bot.run_bridge_command = original_run_bridge_command
            bot.mirror_single_codex_thread = original_mirror_single

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

            async def fake_mirror_single_codex_thread(fake_bot, thread_id):
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

    async def test_slash_ask_busy_view_uses_interaction_user_owner(self) -> None:
        original_get_mirrored = bot.get_mirrored_codex_thread_id
        original_get_interactive_state = bot.get_interactive_state_for_thread
        original_get_busy_state = bot.get_busy_state_for_thread
        original_build_context_warning = bot.build_context_warning
        original_is_thread_runner_busy = bot.is_thread_runner_busy
        try:
            bot.get_mirrored_codex_thread_id = lambda channel_id: "thread-1"
            bot.get_interactive_state_for_thread = lambda target_thread_id: ("", None, "")
            bot.get_busy_state_for_thread = lambda target_thread_id: ("busy", "thread-1", "taxlab:1")
            bot.build_context_warning = lambda target_thread_id: ""

            async def runner_idle(target_thread_id):
                return False

            bot.is_thread_runner_busy = runner_idle
            interaction = FakeInteraction(command_name="ask", channel_id=222)
            interaction.channel = FakeTarget()

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "discord-smoke.log"
                with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                    await bot.handle_slash_ask(interaction, "please steer")
                log_text = log_path.read_text(encoding="utf-8")

            self.assertEqual(interaction.followup.messages, ["Ask handling posted in this channel."])
            self.assertEqual(len(interaction.channel.messages), 1)
            content, view = interaction.channel.messages[0]
            self.assertIn("This Codex thread is already working.", content)
            self.assertIsInstance(view, bot.BusyChoiceView)
            self.assertIs(view.message.author, interaction.user)
            self.assertIs(view.message.channel, interaction.channel)
            self.assertEqual(view.target_thread_id, "thread-1")
            self.assertIn("slash_ask_dispatch command=ask channel=222", log_text)
            self.assertIn("busy_choice_sent reason=codex_busy_preflight target=thread-1", log_text)
        finally:
            bot.get_mirrored_codex_thread_id = original_get_mirrored
            bot.get_interactive_state_for_thread = original_get_interactive_state
            bot.get_busy_state_for_thread = original_get_busy_state
            bot.build_context_warning = original_build_context_warning
            bot.is_thread_runner_busy = original_is_thread_runner_busy

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


if __name__ == "__main__":
    unittest.main()
