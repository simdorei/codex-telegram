import asyncio
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
    def __init__(self, channel_id: int = 222, parent_id: int | None = None) -> None:
        self.messages: list[tuple[str, object | None]] = []
        self.id = channel_id
        self.parent_id = parent_id

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
        self.type = bot.discord.InteractionType.application_command
        self.data: dict[str, object] = {}


class FakeMessage:
    def __init__(self, content: str = "", channel_id: int = 222) -> None:
        self.channel = FakeTarget(channel_id=channel_id)
        self.author = SimpleNamespace(id=242286902982606848, bot=False)
        self.content = content


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
        self.assertIn("component_interaction_unhandled_reported", log_text)
        self.assertIn("custom_id=codex-busy-choice-old-button", log_text)

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
        self.assertIsNone(remaining)
        self.assertIn("busy_choice_persistent_ignore", log_text)
        self.assertNotIn("please ignore", log_text)

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

    async def test_socket_message_create_logs_tracked_without_content(self) -> None:
        fake_client = SimpleNamespace(
            is_allowed_channel=lambda channel_id: channel_id == 222,
        )
        fake_client.format_socket_interaction_user = (
            lambda data: bot.CodexDiscordBot.format_socket_interaction_user(fake_client, data)
        )

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
        self.assertIn("content_len=16", log_text)
        self.assertNotIn("sensitive prompt", log_text)

    async def test_socket_message_create_untracked_omits_author_and_content_len(self) -> None:
        fake_client = SimpleNamespace(
            is_allowed_channel=lambda channel_id: False,
        )
        fake_client.format_socket_interaction_user = (
            lambda data: bot.CodexDiscordBot.format_socket_interaction_user(fake_client, data)
        )
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
        self.assertIn("slash_new_dispatch", source)
        self.assertIn("slash_new_done", source)

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

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "discord-smoke.log"
            with EnvPatch("CODEX_DISCORD_LOG_PATH", str(log_path)):
                await bot.CodexDiscordBot.on_message(client, message)
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(message.channel.messages, [])
        self.assertIn("message_received chat=333", log_text)
        self.assertIn("content_len=0", log_text)
        self.assertIn("ignored_message reason=empty_content chat=333", log_text)

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
            self.assertIn("prompt_len=12", log_text)
            self.assertNotIn("prompt=please steer", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.build_context_warning = original_build_context_warning

    async def test_steer_now_waiting_input_failure_resends_busy_choice_view(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_build_context_warning = bot.build_context_warning
        try:
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
            self.assertEqual(len(interaction.followup.messages), 1)
            content, followup_view = interaction.followup.messages[0]
            self.assertIn("This Codex thread is already working.", content)
            self.assertIsInstance(followup_view, bot.BusyChoiceView)
            self.assertEqual(followup_view.target_thread_id, "thread-1")
            self.assertNotIn("waiting on a follow-up", content.lower())
            self.assertIn("busy_choice_sent reason=steer_busy_failure target=thread-1", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.build_context_warning = original_build_context_warning

    async def test_steer_now_busy_failure_falls_back_when_followup_fails(self) -> None:
        original_run_steering_prompt = bot.run_steering_prompt
        original_build_context_warning = bot.build_context_warning
        try:
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
            self.assertIn("This Codex thread is already working.", content)
            self.assertIsInstance(fallback_view, bot.BusyChoiceView)
            self.assertEqual(fallback_view.target_thread_id, "thread-1")
            self.assertIn("button_followup_failed command=ask context=steer_busy_failure", log_text)
            self.assertIn("button_followup_fallback_sent command=ask context=steer_busy_failure", log_text)
            self.assertIn("busy_choice_sent reason=steer_busy_failure target=thread-1", log_text)
        finally:
            bot.run_steering_prompt = original_run_steering_prompt
            bot.build_context_warning = original_build_context_warning

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
            self.assertEqual(interaction.followup.messages, ["No active job now. Starting this message."])
            self.assertIn("queue_next_immediate user=242286902982606848", log_text)
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

    async def test_run_prompt_flow_chunks_long_context_ack(self) -> None:
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
            self.assertTrue(sent[0].startswith("Ask received. Sending to Codex."))
        finally:
            bot.get_thread_runner = original_get_thread_runner
            bot.build_context_warning = original_build_context_warning
            bot.enqueue_thread_ask = original_enqueue_thread_ask

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
            self.assertTrue(content.endswith("Choose how to handle this message for this thread."))
        finally:
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


if __name__ == "__main__":
    unittest.main()
