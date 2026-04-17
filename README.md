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

## Current Version

- Patch level: `2026.04.18-1`
- This README reflects the current bridge and Telegram patch set in this workspace.

## Current Patch Summary

Compared with the last committed baseline, the current patch set adds or stabilizes:

- thread creation from bridge and Telegram via `new` and `/new`
- archive move flow via `archive`, `/archive`, and `archived_list`
- archived-thread deletion flow via `delete_archive`, `/delete_archive`, and `/confirm_delete_archive`
- target-thread binding without opening UI via `use` and `/use`
- Codex Desktop executable discovery and restart via `discover_codex`, `/discover_codex`, `restart_codex`, and `/restart_codex`
- Telegram live approval handling for `waiting-approval` threads with visible `1 / 2 / 3` choices
- Telegram follow-up delivery after approval replies so the post-approval result message is forwarded back into chat

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
CODEX_DESKTOP_EXE=
PYTHON_EXE=
CODEX_BRIDGE_AUTO_START_TELEGRAM=1
```

Important variables:

- `TELEGRAM_BOT_TOKEN`: required for Telegram mode
- `TELEGRAM_ALLOWED_CHAT_IDS`: optional allowlist of Telegram chat IDs
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
TELEGRAM_BOT_TOKEN=123456:example-token
TELEGRAM_ALLOWED_CHAT_IDS=123456789
CODEX_HOME=C:\Users\your_user\.codex
PYTHON_EXE=C:\python\python.exe
CODEX_BRIDGE_AUTO_START_TELEGRAM=1
```

After editing `.env`, restart the running bridge/bot process so the new values are loaded.

Advanced overrides used by the bridge:

- `CODEX_STATE_DB`
- `CODEX_GLOBAL_STATE`
- `CODEX_SESSION_INDEX`
- `CODEX_BRIDGE_STATE`

To find your Telegram chat ID:

1. Send any message to your bot.
2. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`.
3. Read `message.chat.id`.

## Telegram-First Flow

Recommended Telegram workflow:

1. `/list`
2. `/use <ref>`
3. Send plain text or `/ask <prompt>`

권장 텔레그램 흐름:

1. `/list`
2. `/use <ref>`
3. 일반 텍스트 또는 `/ask <prompt>` 전송

Plain text messages are treated like `/ask <message>`, except for interactive states:

- if the selected thread is `waiting-input`, plain text replies to that prompt
- if the selected thread is `waiting-approval`, reply with the visible `1`, `2`, or `3` option

일반 텍스트 메시지는 기본적으로 `/ask <message>`처럼 처리되지만, 상호작용 상태에서는 다르게 동작합니다.

- 선택한 스레드가 `waiting-input`이면 일반 텍스트가 그 입력에 대한 답변으로 전달됩니다.
- 선택한 스레드가 `waiting-approval`이면 `1`, `3`, `cancel`로 응답합니다.

Telegram no longer uses `/open`. Use `/use` to bind the target thread, then ask against that selected thread.

텔레그램에서는 더 이상 `/open`을 쓰지 않습니다. `/use`로 대상 스레드를 선택한 뒤 그 스레드에 질문을 보냅니다.

## Telegram Menu

| Command | English | 한국어 |
| --- | --- | --- |
| `/list [limit]` | Show recent active threads and states such as `idle`, `busy`, `waiting-input`, `waiting-approval`. | 최근 활성 스레드와 상태(`idle`, `busy`, `waiting-input`, `waiting-approval`)를 보여줍니다. |
| `/archived_list [limit]` | Show archived threads. | 보관된 스레드를 보여줍니다. |
| `/new <prompt>` | Create a new thread and send the first prompt. | 새 스레드를 만들고 첫 질문을 보냅니다. |
| `/archive [ref]` | Archive the selected thread or a specific ref. | 선택된 스레드 또는 지정한 ref를 보관합니다. |
| `/delete_archive <ref>` | Preview local archived-thread deletion. | 로컬 보관 스레드 삭제 전 미리보기를 보여줍니다. |
| `/confirm_delete_archive <ref>` | Actually delete the archived thread locally. | 보관 스레드를 로컬에서 실제 삭제합니다. |
| `/use <ref>` | Persist the default target thread without opening UI. | UI를 열지 않고 기본 대상 스레드를 선택합니다. |
| `/status [ref]` | Show status for the current or specified thread. | 현재 또는 지정한 스레드 상태를 보여줍니다. |
| `/doctor` | Print bridge diagnostics. | 브리지 진단 정보를 출력합니다. |
| `/ask <prompt>` | Send a prompt through the default IPC path. | 기본 IPC 경로로 질문을 보냅니다. |
| `/ask_ipc <prompt>` | Alias of `/ask`. | `/ask`의 별칭입니다. |
| `/restart_bot` | Restart only the Telegram bot process. | 텔레그램 봇 프로세스만 재시작합니다. |
| `/chatid` | Show the current Telegram chat id. | 현재 텔레그램 chat id를 보여줍니다. |

Telegram notes:

- Default Telegram `ask` uses IPC, not UI paste.
- If an older thread is not currently loaded by Codex Desktop, IPC can still fail once. Open that thread once in the app and retry.
- Live `waiting-approval` prompts are shown in Telegram with visible `1 / 2 / 3` options.
- Current tested Telegram approval flow is the live `commandExecution` prompt path.

추가 메모:

- 텔레그램의 기본 `ask`는 UI 복붙이 아니라 IPC를 사용합니다.
- 아주 오래된 스레드가 데스크톱 앱에 아직 로드되지 않은 경우, 앱에서 한 번 열어 준 뒤 다시 시도해야 할 수 있습니다.
- 승인 옵션 `2`(다시 묻지 않기 포함 승인)는 아직 Codex Desktop에서 처리해야 합니다.
- 텔레그램에서의 승인 완료는 아직 실험적입니다. 스레드가 계속 `waiting-approval`이면 데스크톱에서 마무리하세요.

Additional Telegram commands:

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
- `open <ref>`
- `open --abort <ref>`
- `use <ref>`
- `new "..."`
- `archive <ref>`
- `delete_archive <ref>`
- `ask "..."`
- `status`
- `doctor`
- `tail --only-new`

Default REPL behavior:

- plain text is treated like `ask --stream --include-commentary "..."`
- default `ask` uses IPC
- `open` changes the visible Codex thread
- `use` only changes the persisted target thread

`list` output fields:

- `ctx last/peak`: latest input context vs. historical peak input context
- `used`: cumulative `tokens_used`
- `rec archive`: shown when `used >= 50M` or either context value reaches `200k`

## Public Repo Notes

- `.env` is ignored by Git.
- `*.log` is ignored by Git.
- `requirements.txt` is intentionally empty because the project uses only the Python standard library.

## Log Rotation

Managed runtime logs:

- `codex_telegram_bot.log`
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
