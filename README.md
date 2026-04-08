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

## Repository Layout

- `codex_desktop_bridge.py`: local thread discovery, window activation, ask/watch flow
- `codex_telegram_bot.py`: Telegram adapter that runs the bridge in-process
- `codex-bridge.cmd`: main launcher
- `codex-telegram-bot.cmd`: Telegram-only launcher
- `.env.example`: local environment template

## Quick Start

1. Clone the repository.
2. Copy `.env.example` to `.env`.
3. Fill in `TELEGRAM_BOT_TOKEN` if you want Telegram control.
4. Start the Codex Desktop app and sign in.
5. Run:

```powershell
.\codex-bridge.cmd
```

If `TELEGRAM_BOT_TOKEN` is configured in `.env`, `codex-bridge.cmd` also starts the Telegram adapter automatically.

Optional launcher flags:

- `.\codex-bridge.cmd --no-bot`
- `.\codex-bridge.cmd --bot-only`

## Environment Variables

Example `.env`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=
CODEX_HOME=
PYTHON_EXE=
CODEX_BRIDGE_AUTO_START_TELEGRAM=1
```

Important variables:

- `TELEGRAM_BOT_TOKEN`: required for Telegram mode
- `TELEGRAM_ALLOWED_CHAT_IDS`: optional allowlist of Telegram chat IDs
- `CODEX_HOME`: override default Codex state directory if needed
- `PYTHON_EXE`: force a specific Python interpreter
- `CODEX_BRIDGE_AUTO_START_TELEGRAM`: set `0` to disable bot auto-start

Advanced overrides used by the bridge:

- `CODEX_STATE_DB`
- `CODEX_GLOBAL_STATE`
- `CODEX_SESSION_INDEX`
- `CODEX_BRIDGE_STATE`

To find your Telegram chat ID:

1. Send any message to your bot.
2. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`.
3. Read `message.chat.id`.

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

## Public Repo Notes

- `.env` is ignored by Git.
- `*.log` is ignored by Git.
- `requirements.txt` is intentionally empty of packages because the project uses only the Python standard library.

## Troubleshooting

If an old folder such as `codex-desktop-bridge` still appears in the Codex app after you removed it from the workspace list, the usual causes are:

- the physical folder still exists on disk
- old threads still have that folder saved as their `cwd`
- Codex keeps recent workspace roots and recent thread history separately

Removing a workspace root in the app does not necessarily delete old thread metadata.

## Known Limits

- This is not an official API.
- It depends on the Codex Desktop UI and local state layout.
- App updates can break parts of the automation.
- Switching threads during a reply may abort that reply.
