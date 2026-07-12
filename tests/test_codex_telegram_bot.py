import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import codex_telegram_bot as bot


class TelegramOnlyBotTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.message = {
            "chat": {"id": 123, "type": "private"},
            "message_id": 7,
        }

    def test_help_exposes_only_recovery_commands(self) -> None:
        help_text = bot.build_help_message()

        for command in ("/list", "/use", "/status", "/ask"):
            self.assertIn(command, help_text)
        for command in ("/new", "/archive", "/delete_archive", "/restart_codex"):
            self.assertNotIn(command, help_text)

    @patch.object(bot, "log_line")
    @patch.object(bot, "send_bridge_command_result")
    @patch.object(bot, "send_text")
    def test_removed_command_is_not_forwarded_to_bridge(
        self,
        send_text,
        send_bridge_command_result,
        _log_line,
    ) -> None:
        bot.handle_message("token", {**self.message, "text": "/new prompt"}, {123})

        send_bridge_command_result.assert_not_called()
        self.assertIn("Unknown command", send_text.call_args.args[2])

    @patch.object(bot, "log_line")
    @patch.object(bot, "start_or_queue_ask")
    @patch.object(bot, "resolve_selected_target", return_value=("thread-id", "1", "project"))
    @patch.object(bot, "get_active_job_summary", return_value="")
    @patch.object(bot, "maybe_submit_waiting_input_reply", return_value=False)
    @patch.object(bot, "resolve_interactive_reply_target", return_value=(None, "", ""))
    def test_plain_text_targets_selected_existing_thread(
        self,
        _resolve_interactive,
        _submit_input,
        _active_summary,
        _selected_target,
        start_or_queue_ask,
        _log_line,
    ) -> None:
        bot.handle_message("token", {**self.message, "text": "continue"}, {123})

        start_or_queue_ask.assert_called_once_with(
            "token",
            123,
            "continue",
            7,
            "",
            "thread-id",
            "1",
            "project",
        )
        _resolve_interactive.assert_called_once_with(123, "continue", "thread-id", "1", "project")
        _submit_input.assert_called_once_with("token", 123, "continue", 7, "thread-id", "1")

    @patch.object(bot, "log_line")
    @patch.object(bot, "start_or_queue_ask")
    @patch.object(bot, "resolve_selected_target", return_value=("thread-id", "1", "project"))
    @patch.object(bot, "get_active_job_summary", return_value="")
    @patch.object(bot, "maybe_submit_waiting_input_reply", return_value=True)
    @patch.object(
        bot,
        "resolve_interactive_reply_target",
        return_value=("thread-id", "1", "project"),
    )
    def test_plain_text_answers_waiting_thread_before_starting_ask(
        self,
        _resolve_interactive,
        submit_input,
        _active_summary,
        _selected_target,
        start_or_queue_ask,
        _log_line,
    ) -> None:
        bot.handle_message("token", {**self.message, "text": "1"}, {123})

        submit_input.assert_called_once_with("token", 123, "1", 7, "thread-id", "1")
        start_or_queue_ask.assert_not_called()

    @patch.object(bot, "log_line")
    @patch.object(bot, "start_or_queue_ask")
    @patch.object(bot, "resolve_selected_target", return_value=("thread-id", "1", "project"))
    @patch.object(bot, "get_active_job_summary", return_value="")
    @patch.object(bot, "maybe_submit_waiting_input_reply", return_value=True)
    @patch.object(
        bot,
        "resolve_interactive_reply_target",
        return_value=("thread-id", "1", "project"),
    )
    def test_ask_command_answers_waiting_thread_before_starting_ask(
        self,
        _resolve_interactive,
        submit_input,
        _active_summary,
        _selected_target,
        start_or_queue_ask,
        _log_line,
    ) -> None:
        bot.handle_message("token", {**self.message, "text": "/ask 1"}, {123})

        submit_input.assert_called_once_with("token", 123, "1", 7, "thread-id", "1")
        start_or_queue_ask.assert_not_called()

    @patch.object(bot, "log_line")
    @patch.object(bot, "maybe_follow_selected_thread")
    @patch.object(bot, "should_attach_follow_after_command", return_value=True)
    @patch.object(bot, "send_bridge_command_result", return_value=(0, "thread_id: thread-id"))
    @patch.object(bot, "resolve_selected_target", return_value=("old-id", "2", "old"))
    @patch.object(bot, "get_active_job_summary", return_value="")
    def test_use_follows_selected_busy_thread(
        self,
        _active_summary,
        _selected_target,
        _bridge_result,
        _should_follow,
        maybe_follow,
        _log_line,
    ) -> None:
        bot.handle_message("token", {**self.message, "text": "/use 1"}, {123})

        maybe_follow.assert_called_once_with("token", 123, 7)

    def test_env_example_is_telegram_only(self) -> None:
        env_example = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")

        self.assertIn("TELEGRAM_BOT_TOKEN=", env_example)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=", env_example)
        self.assertNotIn("DISCORD_", env_example)

    def test_load_local_env_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("\ufeffTELEGRAM_BOT_TOKEN=test-token\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                bot.load_local_env(env_path)
                self.assertEqual(os.environ.get("TELEGRAM_BOT_TOKEN"), "test-token")

    def test_empty_allowed_chat_ids_allow_all(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bot.get_allowed_chat_ids(), set())

    def test_follow_forwards_ongoing_and_final_responses(self) -> None:
        events = [
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "phase": "commentary"},
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "phase": "final_answer"},
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.jsonl"
            session_path.touch()
            thread = SimpleNamespace(rollout_path=str(session_path))

            with (
                patch.object(bot.bridge, "choose_thread", return_value=thread),
                patch.object(bot.bridge, "get_last_user_and_assistant_messages", return_value=("", "")),
                patch.object(bot.bridge, "read_new_session_events", return_value=(events, 1)),
                patch.object(bot.bridge, "extract_message_text", side_effect=["working", "done"]),
                patch.object(bot, "get_current_interactive_prompt", return_value=("", "")),
                patch.object(bot, "send_text") as send_text,
            ):
                bot.follow_thread_output(
                    "token",
                    123,
                    "thread-id",
                    "1",
                    7,
                    threading.Event(),
                    timeout_sec=1,
                )

        forwarded = [call.args[2] for call in send_text.call_args_list]
        self.assertEqual(forwarded, ["In progress (1)\n\nworking", "done"])


if __name__ == "__main__":
    unittest.main()
