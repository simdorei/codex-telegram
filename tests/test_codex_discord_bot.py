import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import codex_discord_bot as bot


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str) -> None:
        self.messages.append(content)


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str) -> None:
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, command_name: str = "help", channel_id: int = 12345) -> None:
        self.command = SimpleNamespace(name=command_name)
        self.channel_id = channel_id
        self.followup = FakeFollowup()
        self.user = SimpleNamespace(id=242286902982606848)
        self.channel = None


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
                self.assertEqual(target.messages, ["Status\n\nok"])
                log_text = log_path.read_text(encoding="utf-8")
                self.assertIn("bridge_command_done title='Status' exit=0", log_text)
                self.assertIn("bridge_command_sent title='Status' exit=0", log_text)
        finally:
            bot.run_bridge_command = original_run_bridge_command


if __name__ == "__main__":
    unittest.main()
