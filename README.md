# codex-bridge-telegram

Unofficial Windows bridge and Telegram control layer for the Codex Desktop app without using Codex CLI.

This repository is the `codex-bridge-telegram` project: a local Codex Desktop bridge plus Telegram and Discord bot adapters for thread control, archive actions, approval handling, and mirrored mobile workflows.

It works by combining:

1. Local Codex state files from `CODEX_HOME` or `%USERPROFILE%\.codex`
2. UI automation against the currently running `Codex` window

## Requirements

- Windows
- Codex Desktop app installed and signed in
- Python 3.11 or newer
- Same Windows user session as the running Codex app

The Telegram adapter uses only the Python standard library. The Discord adapter requires `discord.py`; install dependencies with:

```powershell
py -3 -m pip install -r requirements.txt
```

## Repository Layout

- `codex_desktop_bridge.py`: local thread discovery, window activation, ask/watch flow
- `codex_telegram_bot.py`: Telegram adapter for the `codex-bridge-telegram` flow
- `codex_discord_bot.py`: Discord adapter with project/channel and thread mirroring
- `codex-bridge.cmd`: main launcher
- `codex-telegram-bot.cmd`: Telegram-only launcher
- `codex-discord-bot.cmd`: Discord-only launcher
- `.env.example`: local environment template

## Current Version

- Patch level: `2026.06.03-1`
- This README reflects the current bridge, Telegram, and Discord patch set in this workspace.

## Current Patch Summary

Compared with the last committed baseline, the current patch set adds or stabilizes:

- thread creation from bridge and Telegram via `new` and `/new`
- archive move flow via `archive`, `/archive`, and `archived_list`
- archived-thread deletion flow via `delete_archive`, `/delete_archive`, and `/confirm_delete_archive`
- split thread targeting between `use`/`/use` and `open`/`/open`
- Codex Desktop executable discovery and restart via `discover_codex`, `/discover_codex`, `restart_codex`, and `/restart_codex`
- Telegram live approval handling for `waiting-approval` threads with visible `1 / 2 / 3` choices
- Discord project/channel mirroring, per-thread routing, queued steering, approval/input buttons, and mirror diagnostics
- Discord `Steer now` streaming, typing indicators while Codex is working, and `!context` compaction/status visibility
- Telegram follow-up delivery after approval replies so the post-approval result message is forwarded back into chat
- Telegram follow mode now forwards approval prompts directly instead of requiring a manual `/list` and `/use` refresh
- after `/restart_codex`, reopen the target thread with `/open <ref>` before asking if you need visible-thread/live IPC recovery

## Quick Start

1. Clone the repository.
2. Copy `.env.example` to `.env`.
3. Fill in `TELEGRAM_BOT_TOKEN` if you want Telegram control.
4. Fill in `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, and `DISCORD_ALLOWED_CHANNEL_IDS` if you want Discord control.
5. Start the Codex Desktop app and sign in.
6. Run:

```powershell
.\codex-bridge.cmd
```

If `TELEGRAM_BOT_TOKEN` is configured in `.env`, `codex-bridge.cmd` also starts the Telegram adapter automatically.

Start the Discord adapter separately when `DISCORD_BOT_TOKEN` is configured:

```powershell
.\codex-discord-bot.cmd
```

Optional launcher flags:

- `.\codex-bridge.cmd --no-bot`
- `.\codex-bridge.cmd --bot-only`

## Environment Variables

Example `.env`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_ALLOWED_CHANNEL_IDS=
DISCORD_ALLOWED_USER_IDS=
DISCORD_ALLOW_ALL_CHANNELS=0
DISCORD_STARTUP_CHANNEL_ID=
DISCORD_STARTUP_NOTIFY=1
DISCORD_ENABLE_MESSAGE_CONTENT=1
DISCORD_ENABLE_QA_COMMANDS=0
DISCORD_HISTORY_POLL_SECONDS=15
DISCORD_HISTORY_BOOTSTRAP_LOOKBACK_SECONDS=120
DISCORD_STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS=25
DISCORD_STEERING_PENDING_WATCH_TIMEOUT_SECONDS=120
CODEX_DISCORD_LOG_PATH=
CODEX_HOME=
CODEX_DESKTOP_EXE=
PYTHON_EXE=
CODEX_BRIDGE_AUTO_START_TELEGRAM=1
```

Important variables:

- `TELEGRAM_BOT_TOKEN`: required for Telegram mode
- `TELEGRAM_ALLOWED_CHAT_IDS`: optional allowlist of Telegram chat IDs
- `DISCORD_BOT_TOKEN`: required for Discord mode
- `DISCORD_GUILD_ID`: optional guild/server ID for faster slash-command sync
- `DISCORD_ALLOWED_CHANNEL_IDS`: allowlist of Discord channel/thread IDs; required unless `DISCORD_ALLOW_ALL_CHANNELS=1`
- `DISCORD_ALLOWED_USER_IDS`: optional allowlist of Discord user IDs
- `DISCORD_ALLOW_ALL_CHANNELS`: set `1` only for a private test server where every channel is safe for Codex control
- `DISCORD_STARTUP_CHANNEL_ID`: optional channel ID for startup notifications
- `DISCORD_STARTUP_NOTIFY`: set `1` to send an online notification at startup
- `DISCORD_ENABLE_MESSAGE_CONTENT`: set `0` to disable prefix/plain-message handling and use slash commands only
- `DISCORD_ENABLE_QA_COMMANDS`: set `1` to expose Discord QA smoke commands such as `!qa buttons` and `/qa_buttons`
- `DISCORD_HISTORY_POLL_SECONDS`: optional fallback interval for checking recent allowed/mirrored channel history; set `0` to disable
- `DISCORD_HISTORY_BOOTSTRAP_LOOKBACK_SECONDS`: optional startup lookback window for recent history polling
- `DISCORD_STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS`: optional extra wait for delayed Codex IPC steering delivery confirmation
- `DISCORD_STEERING_PENDING_WATCH_TIMEOUT_SECONDS`: optional max wait for a steering watch after IPC accepted delivery but local recording lagged
- `CODEX_DISCORD_LOG_PATH`: optional Discord adapter log path override, useful for smoke tests or isolated diagnostics
- `CODEX_HOME`: override default Codex state directory if needed
- `CODEX_DESKTOP_EXE`: optional override for the Codex Desktop app executable. `/discover_codex` or `/restart_codex` auto-save it into `.env` when discovery succeeds.
- `PYTHON_EXE`: force a specific Python interpreter
- `CODEX_BRIDGE_AUTO_START_TELEGRAM`: set `0` to disable bot auto-start

### Find `CODEX_HOME` and `PYTHON_EXE` on Windows

`CODEX_HOME` (default):

```powershell
Join-Path $env:USERPROFILE '.codex'
Test-Path (Join-Path $env:USERPROFILE '.codex')
```

If that folder exists and you use the default Codex data location, you can either:

- leave `CODEX_HOME=` empty, or
- set it explicitly (recommended for multi-PC setup).

`PYTHON_EXE`:

```powershell
py -3 -c "import sys; print(sys.executable)"
```

If `py` is not available:

```powershell
python -c "import sys; print(sys.executable)"
```

Example `.env` with explicit paths:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=123456789
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_ALLOWED_CHANNEL_IDS=
DISCORD_ALLOWED_USER_IDS=
DISCORD_ALLOW_ALL_CHANNELS=0
CODEX_HOME=C:\Users\your_user\.codex
PYTHON_EXE=C:\python\python.exe
CODEX_BRIDGE_AUTO_START_TELEGRAM=1
```

After editing `.env`, restart the running bridge/bot process so the new values are loaded.
Put your real bot token only in the local `.env` file. Do not commit token values.

Advanced overrides used by the bridge:

- `CODEX_STATE_DB`
- `CODEX_GLOBAL_STATE`
- `CODEX_SESSION_INDEX`
- `CODEX_BRIDGE_STATE`

To find your Telegram chat ID:

1. Send any message to your bot.
2. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`.
3. Read `message.chat.id`.

## Discord Mirror Flow

Start the Discord adapter:

```powershell
.\codex-discord-bot.cmd
```

Useful Discord commands:

- `!help`: show the current Discord command list
- `!list [limit]`, `!archived_list [limit]` / `!archive_list [limit]`: show active or archived Codex threads
- `!new <prompt>`: create a new Codex thread and send the first prompt
- `!archive [ref]`, `!delete_archive <ref>`, `!confirm_delete_archive <ref>`: archive and local archived-thread deletion flow
- `!use <ref>`, `!open <ref>`, `!open_abort <ref>`: select or open Codex threads
- `!status [ref]`, `!doctor`, `!discover_codex`, `!restart_codex`: Discord/bridge diagnostics and Codex Desktop maintenance
- `!context [all]`, `!usage [days]`, `!runners`, `!chatid`: Discord/Codex utility status
- `!mirror sync [limit]`: create/update Discord project channels and thread mirrors from local Codex threads
- `!mirror list [limit]`: show the current local mirror map
- `!mirror check`: verify missing, stale, or wrong-project mappings
- `!where`: show which Codex thread the current Discord channel/thread maps to
- `!approval`: re-show approval buttons if the mapped Codex thread is waiting for approval
- `!ask <prompt>`: send a prompt to the mapped or selected Codex thread

Registered Discord slash commands:

- `/help`, `/list`, `/archived_list`, `/use`, `/status`, `/doctor`, `/where`, `/context`, `/usage`, `/runners`, `/mirror_check`, `/new`, `/ask`, `/ask_ipc`

Optional Discord QA commands:

- set `DISCORD_ENABLE_QA_COMMANDS=1` to expose `!qa buttons` and `/qa_buttons`
- use them only in a test channel; they create temporary Discord button messages and clear their controls during the smoke test
- button QA covers busy-choice `Ignore`, stale/missing busy controls, synthetic `Steer now`, persistent approval, and persistent input-choice handlers without submitting a real Codex prompt
- `!steer <prompt>` is also exposed only when QA commands are enabled; it is a text-path smoke test for the same steering backend, not the normal user workflow
- persistent approval/input fallback buttons are single-use per Discord message, so replayed or double-clicked restart buttons do not submit twice
- startup cleanup removes stale busy-choice buttons whose backing DB records expired or were already handled, while preserving active in-flight controls
- `/doctor` reports whether QA commands are enabled, persistent component claim counts, the last button QA result, and last steering-button elapsed time from the Discord log

Messages inside a mirrored Discord thread are sent to that Codex thread. If a Discord project channel has multiple Codex threads, plain messages in the parent channel are blocked so they do not accidentally fall back to the selected Codex thread.

## Telegram-First Flow

Recommended Telegram workflow:

1. `/list`
2. `/use <ref>`
3. Optional: `/open <ref>` if you need the visible Codex UI thread, or if you just ran `/restart_codex`
4. Send plain text or `/ask <prompt>`

Plain text messages are treated like `/ask <message>`, except for interactive states:

- if the selected thread is `waiting-input`, plain text replies to that prompt
- if the selected thread is `waiting-approval`, reply with the visible `1`, `2`, or `3` option

`/use` only stores the default target thread. `/open` actually opens that thread in the visible Codex UI.

After `/restart_codex`, run `/open <ref>` again before asking if you want live IPC / follow / approval visibility to recover reliably. `/use` alone does not reopen the Codex UI thread after restart.

## Telegram Menu

| Command | Description |
| --- | --- |
| `/list [limit]` | Show recent active threads and states such as `idle`, `busy`, `waiting-input`, `waiting-approval`. |
| `/archived_list [limit]` | Show archived threads. |
| `/new <prompt>` | Create a new thread and send the first prompt. |
| `/archive [ref]` | Archive the selected thread or a specific ref. |
| `/delete_archive <ref>` | Preview local archived-thread deletion. |
| `/confirm_delete_archive <ref>` | Actually delete the archived thread locally. |
| `/use <ref>` | Persist the default target thread without opening UI. |
| `/status [ref]` | Show status for the current or specified thread. |
| `/doctor` | Print adapter and bridge diagnostics. |
| `/ask <prompt>` | Send a prompt through the default IPC path. |
| `/ask_ipc <prompt>` | Alias of `/ask`. |
| `/discover_codex` | Discover the Codex Desktop executable and persist it into `.env`. |
| `/restart_codex` | Restart the Codex Desktop app using the saved executable path. |
| `/restart_bot` | Restart only the Telegram bot process. |
| `/chatid` | Show the current Telegram chat id. |

Telegram notes:

- Default Telegram `ask` uses IPC, not UI paste.
- If an older thread is not currently loaded by Codex Desktop, IPC can still fail once. Open that thread once in the app and retry.
- `/use <ref>` keeps the target binding only. `/open <ref>` is the command that reopens the visible Codex UI thread.
- After `/restart_codex`, reopen the target thread with `/open <ref>` before asking if you need live IPC / follow / approval recovery.
- Live `waiting-approval` prompts are shown in Telegram with visible `1 / 2 / 3` options.
- When a followed thread enters `waiting-approval`, the approval prompt is forwarded into Telegram directly.
- Current tested Telegram approval flow is the live `commandExecution` prompt path.

Additional Telegram commands:

- `/open <ref>`: open the target thread in the visible Codex UI and make it the selected thread
- `/open_abort <ref>`: abort the current reply if needed, then open the target thread in the visible Codex UI
- `/discover_codex`: discover the Codex Desktop executable and persist `CODEX_DESKTOP_EXE` into `.env`
- `/restart_codex`: restart the Codex Desktop app using the discovered `CODEX_DESKTOP_EXE` path

## Thread References

When multiple recent threads exist in the same workspace, refs look like:

- `ai:1`
- `ai:2`
- `taxlab`
- `other`

Example:

```powershell
list
use ai:2
ask "Test"
```

## Bridge Shell Commands

Main REPL commands:

- `list`
- `archived_list`
- `discover_codex`
- `restart_codex`
- `open <ref>`
- `open --abort <ref>`
- `use <ref>`
- `new "..."`
- `archive <ref>`
- `delete_archive <ref>`
- `approval_reply <answer> [ref]`
- `ask "..."`
- `status`
- `doctor`
- `tail --only-new`
- `focus`
- `help`
- `exit`

Default REPL behavior:

- plain text is treated like `ask --stream --include-commentary "..."`
- default `ask` uses IPC
- `open` changes the visible Codex thread
- `use` only changes the persisted target thread
- after `restart_codex`, use `open <ref>` again if you need visible-thread/live IPC recovery

`list` output fields:

- `ctx last/peak`: latest input context vs. historical peak input context
- `used`: cumulative `tokens_used`
- `rec archive`: shown when `used >= 50M` or either context value reaches `200k`

## Public Repo Notes

- `.env` is ignored by Git.
- `*.log` is ignored by Git.
- Telegram mode uses only the Python standard library.
- Discord mode requires `discord.py`, tracked in `requirements.txt`.

## Log Rotation

Managed runtime logs:

- `codex_telegram_bot.log`
- `codex_discord_bot.log`
- `discord_launcher.log`
- `_ipc_probe_log.jsonl`

Rotation rule:

- if the current file would exceed `500 KB`, the previous `.bak` file is deleted
- the current file is moved to `<name>.bak`
- a new current file is created

## Troubleshooting

If an old folder such as `codex-desktop-bridge` still appears in Codex Desktop after you removed it from the workspace list, the usual causes are:

- the physical folder still exists on disk
- old threads still have that folder saved as their `cwd`
- Codex keeps recent workspace roots and recent thread history separately

Removing a workspace root in the app does not necessarily delete old thread metadata.

If `new`, `archive`, or archived-thread deletion updates the local state but the Codex Desktop sidebar still shows the old list, click the thread pane once or restart the app. The local state updates first; the visible sidebar can lag until the UI refreshes.

## Known Limits

- This is not an official API.
- It depends on Codex Desktop internals and local state layout.
- App updates can break IPC discovery or UI automation.
- Switching visible threads while Codex is replying can still affect the active reply.
