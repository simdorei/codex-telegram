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

## Files

- `codex_desktop_bridge.py`
- `codex_telegram_bot.py`
- `codex-bridge.cmd`
- `codex-telegram-bot.cmd`

## Start

```powershell
.\codex-bridge.cmd
```

If `TELEGRAM_BOT_TOKEN` is configured in `.env`, `codex-bridge.cmd` also starts the Telegram adapter automatically.

Optional launcher flags:

- `.\codex-bridge.cmd --no-bot`
- `.\codex-bridge.cmd --bot-only`

## .env

Create a local `.env` file next to the scripts.

Example:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=
CODEX_HOME=
PYTHON_EXE=
CODEX_BRIDGE_AUTO_START_TELEGRAM=1
```

`TELEGRAM_ALLOWED_CHAT_IDS` is optional but recommended.

To find your chat ID:

1. Send any message to your bot
2. Open:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Read `message.chat.id`

## Main REPL Commands

- `list`
- `open <ref>`
- `open --abort <ref>`
- `ask "..."`
- `status`
- `doctor`

Default `ask` is foreground mode:

- it streams commentary
- it prints the final answer
- it keeps the prompt occupied until the reply ends

## Telegram Commands

- `/list`
- `/open <ref>`
- `/open_abort <ref>`
- `/status [ref]`
- `/doctor`
- `/ask <prompt>`
- `/abort`
- `/chatid`

Plain text Telegram messages are treated like `/ask <message>`.

## Thread References

When a workspace has multiple recent threads, the bridge labels them as:

- `ai:1`
- `ai:2`
- `taxlab`

Example:

```powershell
list
open ai:2
ask "Test"
```

## Known Limits

- This is not an official API.
- It depends on the Codex Desktop UI and local state layout.
- App updates can break parts of the automation.
- Switching threads during a reply may abort that reply.
