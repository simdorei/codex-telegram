# Discord Bridge Refactor Plan

## Rule

Refactor in small slices only:

1. Inspect the call path and monkeypatch/test dependencies.
2. Move one cohesive helper group.
3. Keep the `codex_discord_bot.py` public function name as a wrapper when tests or runtime code call it directly.
4. Run unit tests and compile checks.
5. Restart the Discord bot task.
6. Verify through the existing Chrome Discord tab with a harmless command that exercises the changed path.

## Completed Slices

- Text/env helpers: `codex_discord_text.py`
- Component custom ID helpers: `codex_discord_components.py`
- Discord hook/file logging helpers: `codex_discord_logging.py`
- SQLite mirror/persistence helpers: `codex_discord_store.py`
- Bridge subprocess helpers: `codex_discord_bridge_process.py`
- Context and usage message builders: `codex_discord_context.py`
- Runner/relay state helpers: `codex_discord_runtime.py`
- Interactive notice parser: `codex_discord_interactive.py`
- Codex thread state resolver helpers: `codex_discord_thread_state.py`
- Discord diagnostics/history builders: `codex_discord_diagnostics.py`
- Project/path helpers: `codex_discord_projects.py`
- Busy-thread formatting helpers: `codex_discord_busy.py`
- Mirror status/list builders: `codex_discord_mirror_status.py`
- Steering result/delivery helpers: `codex_discord_steering.py`
- Help text builder: `codex_discord_help.py`
- Runner queue/state loop: `codex_discord_runner.py`
- Ask/watch stream relay core and stream runners: `codex_discord_stream.py`
- Prefix/slash command argv and parse helpers: `codex_discord_commands.py`

## Next Safe Slices

- Prefix/slash command dispatch adapters:
  - Keep command registration in `codex_discord_bot.py`.
  - Pure argv construction and limit/subcommand parsing are already in `codex_discord_commands.py`.
  - Move dispatch branches only if they can stay thin wrappers around existing bot functions.
  - Live QA: matching prefix command, for example `!context`, `!usage 1`, or `!mirror check`.

- Post-stream Discord response wrappers:
  - Candidate functions: `stream_steering_prompt_result_to_channel`,
    `stream_post_approval_result_to_channel`, and interaction follow-up wrappers.
  - These still own Discord fallback sends and should move only with focused tests for timeout/fallback messages.

## Bridge Method Direction

- Use IPC for approval/input while Codex still requires owner-client interactive state.
- Use app-server sidecar as the main non-IPC transport candidate for ask/new/read flows.
- Use JSONL as delivery verification and recovery evidence, not as a direct write transport.
- Keep UI automation as a last-resort fallback.
