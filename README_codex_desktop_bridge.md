# Codex Desktop Bridge

Unofficial Windows bridge for controlling the Codex Desktop app without using Codex CLI.

It works by combining:

1. Local Codex state files from `CODEX_HOME` or `%USERPROFILE%\.codex`
2. UI automation against the currently running `Codex` window

## Requirements

- Windows
- Codex Desktop app installed and signed in
- Python 3.11 or newer
- Same Windows user session as the running Codex app

No third-party Python packages are required.

## What `~/.codex` Means

`~/.codex` means the current user's Codex data folder.

- Windows example: `%USERPROFILE%\.codex`
- Example expanded path: `C:\Users\username\.codex`

Important files inside it:

- `state_*.sqlite`: thread metadata
- `session_index.jsonl`: thread name index
- `sessions\...\rollout-*.jsonl`: conversation event logs

## Files

- [codex_desktop_bridge.py](codex_desktop_bridge.py)
- [codex-bridge.cmd](codex-bridge.cmd)

## Start

```powershell
cd C:\ai\codex
.\codex-bridge.cmd
```

If `TELEGRAM_BOT_TOKEN` is configured, `codex-bridge.cmd` also starts the Telegram adapter in a separate minimized console.

Optional launcher flags:

- `.\codex-bridge.cmd --no-bot`
- `.\codex-bridge.cmd --bot-only`

## Environment Variables

You can override default paths when a machine stores Codex data somewhere else.

- `CODEX_HOME`
- `CODEX_STATE_DB`
- `CODEX_SESSION_INDEX`
- `CODEX_GLOBAL_STATE`
- `CODEX_BRIDGE_STATE`
- `PYTHON_EXE`

Example:

```powershell
$env:CODEX_HOME = 'D:\portable\codex-data'
$env:PYTHON_EXE = 'C:\Python312\python.exe'
.\codex-bridge.cmd
```

Optional:

- `CODEX_BRIDGE_AUTO_START_TELEGRAM=0`

## Main Commands

- `list`: show recent active threads
- `archived_list`: show archived threads
- `open <ref>`: select and open a thread
- `open --abort <ref>`: abort the current reply, then switch threads
- `use <ref>`: persist the target thread without opening UI
- `new "..."`: create a new thread and send the first prompt
- `archive <ref>`: archive a thread
- `delete_archive <ref>`: preview or delete an archived thread locally
- `ask "..."`: send a prompt to the selected thread
- `status`: show selected thread details
- `tail --only-new`: watch raw session events
- `doctor`: print environment and detection diagnostics

## Telegram Adapter

Files:

- [codex_telegram_bot.py](codex_telegram_bot.py)
- [codex-telegram-bot.cmd](codex-telegram-bot.cmd)

Required environment variable:

- `TELEGRAM_BOT_TOKEN`

Recommended environment variable:

- `TELEGRAM_ALLOWED_CHAT_IDS`

Add them to the local `.env` file in the same folder as the scripts:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ALLOWED_CHAT_IDS=your_chat_id
```

Example:

```powershell
$env:TELEGRAM_BOT_TOKEN = '123456:example-token'
$env:TELEGRAM_ALLOWED_CHAT_IDS = '123456789'
.\codex-telegram-bot.cmd
```

Telegram commands:

- `/list [limit]`
- `/archived_list [limit]`
- `/new <prompt>`
- `/archive [ref]`
- `/delete_archive <ref>`
- `/confirm_delete_archive <ref>`
- `/use <ref>`
- `/status [ref]`
- `/doctor`
- `/ask <prompt>`
- `/ask_ipc <prompt> (alias)`
- `/restart_bot`
- `/chatid`

Plain text messages are treated like `/ask <message>`.

Recommended Telegram flow:

1. `/list`
2. `/use <ref>`
3. Send `/ask <prompt>` or plain text

Telegram no longer exposes `/open`; use `/use` to bind the target thread first.

## Thread References

When a workspace has multiple recent threads, the bridge labels them as:

- `ai:1`
- `ai:2`
- `taxlab`

Example:

```powershell
list
use ai:2
ask "Test"
```

If a workspace name is ambiguous, the bridge will tell you which refs to use.

## Default Behavior

Default REPL `ask` uses background IPC:

- it avoids UI paste by default
- it can stream commentary when requested
- plain text in the REPL is treated like `ask --stream --include-commentary "..."`

Telegram `ask` also uses IPC by default.

`list` shows:

- `ctx last/peak`: latest input context and historical peak input context
- `used`: cumulative `tokens_used`
- `rec archive`: shown when `used >= 50M` or either context value reaches `200k`

## Busy Safety

Changing threads while Codex is replying can stop the current reply.

Because of that:

- `open <ref>` is blocked while another reply is busy
- `open --abort <ref>` forces the switch after aborting the current reply

## Doctor

`doctor` is a diagnostic command for deployment and support.

It prints:

- Python version and executable
- `CODEX_HOME`
- detected `state_*.sqlite`
- session index path
- global state path
- whether a Codex window is visible
- selected thread id
- busy threads

Use it when another user says "it does not work on my PC".

## Known Limits

- This is not an official API.
- It depends on the Codex Desktop UI and local state layout.
- App updates can break parts of the automation.
- Switching threads during a reply may abort that reply.

## Recommended Repo Layout

- Keep `codex_desktop_bridge.py` as the core bridge
- Keep `codex_telegram_bot.py` as a separate adapter layer
- Keep deployment notes in this README
