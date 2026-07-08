# Codex Discord Frontend Harness

A Windows-local Discord frontend harness for operating the Codex app/web workflow remotely, with mention-gated asks, thread-aware steering, busy-state protection, structured not-sent handling, and deployable QA logs.

This repository currently lives under the legacy `codex-bridge-telegram` name, but the primary product is an unofficial Windows-local Discord frontend harness for operating signed-in Codex app/web work from Discord. It is not a Codex CLI harness and it is not an official mobile Codex replacement. It is an operator wrapper for a private Windows machine where the local Codex app/web session remains the trusted execution surface.

Windows 로컬 Codex 앱/웹 작업을 Discord에서 안전하게 원격 운영하기 위한 frontend harness + bridge입니다. 단순 채팅봇도, CLI 래퍼도 아니라, 멘션 기반 요청, thread별 스티어링, target thread busy와 다른 thread busy 분리, 전송 실패 보장, QA 로그를 제공하는 운영 프론트엔드입니다.

The target environment is deliberately narrow:

- Windows host machine
- Codex app/web session installed or reachable, signed in, awake, and running in the same Windows user session
- private Discord server or allowlisted Discord channels

It works by combining:

1. Discord routing, buttons, slash commands, and thread mirrors
2. A Windows frontend harness for Codex state, locks, preflight, and QA evidence
3. Local Codex state files from `CODEX_HOME` or `%USERPROFILE%\.codex`
4. UI/IPC delivery against the currently running `Codex` Desktop window

```text
Discord Bot
  -> Bridge Router
     - mention gate and context fallback
     - !/slash commands
     - Discord thread mapping
     - buttons and user-facing UI
  -> Windows Frontend Harness
     - Codex app/web runtime status
     - process/run-state lock and target thread state
     - ask / steer / retry / cancel preflight
     - structured status events
     - QA marker/log evidence
     - stale cmd/zombie and stale busy detection
  -> Codex app / Codex web surface
```

## Repository Introduction

This is a Windows-only local Discord frontend harness for operating Codex from Discord. It is designed for users who already keep a signed-in Codex app/web session running on a Windows workstation and want a Discord control plane for starting work, routing messages to the right Codex thread, approving tool prompts, checking context pressure, and keeping project threads mirrored while away from the desktop.

Short repo description:

> Windows-local Codex Discord frontend harness with per-thread routing, live progress streaming, approvals, busy-state protection, diagnostics, QA evidence, and optional legacy Telegram support.

This project is not an official OpenAI API or replacement for the Codex app. It is a local Discord frontend around a signed-in Codex app/web session and local Codex state. Treat it as an operator tool for a private Discord server, not as a public multi-user hosted service.

## Release Readiness

Current status: Windows/Discord release candidate for private operators after configuring tokens, allowlists, and live QA markers. It is suitable for a public repository release when presented as an unsupported Windows-local operator tool, not as a hosted service or official Codex client.

The current Discord-first patch set has been validated with:

- unit tests for Discord handlers, app-menu recovery, bridge subprocess streaming, and IPC delivery diagnostics
- Python compile checks for the changed bridge and Discord modules
- live web Discord QA for busy-state recovery, where a message sent during an active Codex turn produced structured Discord status instead of a raw `Ask failed` message
- live Discord flow evidence for start ACK, live progress messages, final answer delivery, and `Codex turn finished.`
- live Discord Desktop QA via Windows Computer Use text accessibility, where `!qa buttons` produced `button_qa_done channel=1511945919707217992 user=242286902982606848 result=ok`
- visible/headless launcher duplicate-start checks, where repeat starts logged `already_running process_scan` and kept a single Discord bot process
- scheduled-task restart verification for the Discord bot runtime

Operational recommendation:

- deploy only in an allowlisted private Discord server or channel
- keep `.env` local and never commit tokens
- keep `CODEX_BRIDGE_AUTO_START_TELEGRAM=0` unless you explicitly want the optional Telegram adapter
- expect occasional breakage after Codex app/web updates because this wrapper depends on local state layout, IPC discovery, and UI behavior
- require each target Windows operator host to run the release QA matrix before trusting it for unattended remote operation

## Frontend Harness Quality Gate

The bridge is considered usable for general release only when this matrix passes with live Discord marker evidence:

- unmentioned plain message in a gated channel: ignored without Codex ask/session/busy UI/typing unless context fallback is enabled
- configured user mention `<@id>` or `<@!id>`: accepted and stripped from the prompt
- role mention `<@&id>`: does not satisfy the user mention gate
- `!` commands and slash commands: unaffected by mention gating
- context fallback enabled: unmentioned bridge/Codex requests are accepted; other-bot mentions remain ignored unless they include a bridge/Codex keyword
- mapped Discord thread ask: enters that thread's send queue and attempts delivery to the target Codex app/web thread instead of being blocked by a preflight busy-choice screen
- Codex app transport busy after delivery attempt: logs `ask_stream_busy_transport_failure`, mirrors approval/input when `codex_app_menu_sent` is available, otherwise retries and ends with `ask_stream_busy_retry_exhausted` plus a no-buttons status message
- Discord runner busy while target Codex thread is idle: queues the ask in that Discord thread without showing `Steer now`
- `Steer now` click: removes the original busy-choice buttons and posts a public `Discord steering submitted` ACK before submitting the steering prompt
- `Steer now` busy failure: does not create a second `Steer now` button; it mirrors an exposed Codex app menu or reports that the steering message was not accepted yet
- `Steer now` delivery: records the steering prompt in the target Codex thread and reports `steer_now_done exit=0`
- `Steer now` relay: streams the resulting Codex commentary/final answer back to the mapped Discord thread and records `steer_watch_done`
- `Steer now` timeout: if Codex accepts the steering prompt but no final answer is captured before the watch timeout, Discord posts an explicit still-running notice instead of going silent
- stale target busy: if the target Codex thread has not produced local output past the stale-steer threshold, Discord does not send another steering prompt and posts an explicit not-sent notice instead of showing another `Steer now`
- other Codex thread busy while target thread is idle: does not block the mapped Discord thread; Discord sends the ask with `--force-while-busy`
- duplicate launch guard: repeat launcher, watchdog, or scheduled-task starts keep one Discord websocket owner and log duplicate attempts instead of creating a second active bot
- stale completed sessions: excluded from busy diagnostics
- stale non-interactive sessions: excluded from busy diagnostics after the stale threshold
- pending approval/input: remains busy and re-shows the appropriate control
- bot-authored status/final messages: ignored and do not loop back into Codex

Release QA should use the Discord Desktop app as the Computer Use surface. Chrome Discord can be useful for manual inspection, but Chrome URL-confidence guards can block automated input even when the user is logged in. Discord Desktop text accessibility avoids that browser-specific failure mode and was used for the latest live QA marker.

Release validation evidence should include:

```powershell
py -3 -m unittest tests.test_codex_discord_bot
py -3 -m py_compile codex_desktop_bridge.py codex_discord_bot.py codex_discord_busy.py codex_windows_harness.py tests\test_codex_discord_bot.py
py -3 .\codex_windows_harness.py runtime
py -3 .\codex_windows_harness.py preflight --thread-id <codex-thread-id>
git diff --check
```

Live Discord Desktop QA evidence:

- temporarily set `DISCORD_ENABLE_QA_COMMANDS=1`
- restart the Discord adapter
- send `!qa buttons` from the Discord Desktop app in a mirrored Codex thread
- confirm the Discord reply contains `Discord button QA ... result: ok`
- confirm `codex_discord_bot.log` contains `button_qa_done channel=<thread_id> user=<user_id> result=ok`
- set `DISCORD_ENABLE_QA_COMMANDS=0` again and restart the adapter headless

Busy/steering release evidence should include these log markers from the same run:

- `message ... target=<thread-id>` for each Discord message being tested
- `ask_stream_done exit=... target=<thread-id>` for each delivery attempt
- busy transport case only: `ask_stream_busy_transport_failure kind=target|global target=<thread-id>`
- app menu case only: `codex_app_menu_sent reason=... target=<thread-id> state=waiting-approval|waiting-input`
- no-menu busy case only: `ask_stream_busy_retry_exhausted target=<thread-id> attempts=<n>`
- exactly one Discord bot process owns the Discord websocket before and after the test messages
- `component_message_components_cleared context=busy_choice_steer`
- no second steering menu after a busy steering failure: `steer_busy_status_sent reason=steer_busy_failure`
- `steering_start_ack_sent target=<thread-id>`
- `steer_now user=<user-id> target=<thread-id>`
- `steer_now_done exit=0 target=<thread-id>`
- `steer_now_sent exit=0 target=<thread-id>`
- `steer_watch_done ... final=True` or live Discord final-answer messages for the same target thread
- timeout case only: `steer_watch_timeout_reported` plus a public `Steering is still running in Codex.` message

## What This Adds Beyond Mobile Codex

OpenAI's mobile Codex preview is useful for staying connected to Codex work from a phone, including starting or continuing threads, answering questions, changing direction, approving actions, and reviewing context from a connected host. This wrapper focuses on a different gap: using Discord as a Windows-local frontend for a signed-in Codex app/web session.

Extra capabilities this wrapper provides for that workflow:

- Discord project/channel mirroring for local Codex workspaces and threads
- stable Discord thread-to-Codex-thread routing so plain messages land in the intended local thread
- parent-channel protection when multiple Codex threads exist in the same Discord project channel
- live progress streaming from Codex commentary into Discord, not just final-answer forwarding
- post-delivery retry/status handling when Codex app transport is busy, plus approval/input controls when the app exposes a menu
- Discord buttons for approval prompts and input-choice recovery
- `!context`, `!usage`, and archive recommendations based on local thread state and token/context pressure
- local archive, archived-list, and archived-thread deletion flows exposed through Discord commands
- `!mirror sync`, `!mirror list`, and `!mirror check` diagnostics for keeping Discord mirrors aligned with local Codex state
- `!doctor`, startup diagnostics, history polling, and runtime logs for operating the bridge as a service
- Windows Codex Desktop discovery and restart commands from Discord
- optional smoke-test commands for Discord buttons and steering flows in a test channel

Official references:

- Codex overview and use cases: <https://developers.openai.com/codex/explore/>
- Codex access with ChatGPT plans: <https://help.openai.com/en/articles/11369540-getting-started-with-codex>
- ChatGPT mobile release notes for Codex remote access: <https://help.openai.com/en/articles/6825453-chatgpt-apps-on-ios-and-android>

## Requirements

- Windows
- Codex app/web session installed or reachable and signed in
- Python 3.11 or newer
- Same Windows user session as the running Codex app

The Telegram adapter uses only the Python standard library. The Discord adapter requires `discord.py`; install dependencies with:

```powershell
py -3 -m pip install -r requirements.txt
```

## Repository Layout

- `codex_desktop_bridge.py`: local thread discovery, window activation, ask/watch flow
- `codex_windows_harness.py`: Windows-local frontend harness preflight/runtime status layer
- `codex_discord_bot.py`: Discord adapter with project/channel and thread mirroring
- `codex_telegram_bot.py`: optional Telegram adapter for the legacy flow
- `codex-bridge.cmd`: main launcher
- `codex-discord-bot.cmd`: Discord-only launcher
- `codex-discord-bot-headless.vbs`: hidden Discord launcher for daily operation
- `codex-telegram-bot.cmd`: Telegram-only launcher
- `.env.example`: local environment template

## Current Version

- Patch level: `2026.06.05-1`
- This README reflects the current bridge and Discord-focused patch set in this workspace.

## Current Patch Summary

Compared with the last committed baseline, the current patch set adds or stabilizes:

- Discord-first thread creation, archive, targeting, and Codex Desktop discovery/restart flows
- Discord project/channel mirroring, per-thread routing, queued steering, approval/input buttons, and mirror diagnostics
- Discord `Steer now` streaming, typing indicators while Codex is working, and `!context` compaction/status visibility
- Windows frontend harness preflight that keeps target-thread busy separate from other-thread busy for Discord routing
- mention-gated plain asks via `DISCORD_PLAIN_ASK_MENTION_USER_IDS`, with optional context fallback for bridge/Codex-directed chatter
- after `restart_codex`, reopen the target thread with `open <ref>` before asking if you need visible-thread/live IPC recovery

## Quick Start

1. Clone the repository.
2. Copy `.env.example` to `.env`.
3. Fill in `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, and `DISCORD_ALLOWED_CHANNEL_IDS` for Discord control.
4. Fill in `TELEGRAM_BOT_TOKEN` only if you also want optional Telegram control.
5. Start the Codex Desktop app and sign in.
6. Run:

```powershell
.\codex-bridge.cmd
```

Start the Discord adapter when `DISCORD_BOT_TOKEN` is configured:

```powershell
.\codex-discord-bot.cmd
```

For daily operation without a dangling console window, launch the headless Discord adapter:

```powershell
wscript.exe .\codex-discord-bot-headless.vbs
```

The visible `.cmd` launcher prints the script path and log path before running. The headless launcher records startup attempts in `discord_launcher.log`, starts a Windows tray icon for run-state visibility, and the bot runtime writes to `codex_discord_bot.log` or `CODEX_DISCORD_LOG_PATH`.

The tray icon is intentionally thin: it watches `.codex_discord_bot.runtime.lock`, shows whether the Discord bridge is running, and exposes quick actions to open the bot log, open the bridge folder, or request a bot restart.

If the headless bot was started through the scheduled watchdog with elevated rights, request a clean restart by creating `.codex_discord_bot.restart` and running the scheduled task again:

```powershell
New-Item -ItemType File .\.codex_discord_bot.restart -Force
Start-ScheduledTask -TaskName 'Codex Discord Bot'
```

The watchdog removes the marker, stops the runtime PID from `.codex_discord_bot.runtime.lock`, and starts a fresh headless adapter. The expected launcher log markers are `watchdog_restart_requested`, `watchdog_restart_stop`, and then `watchdog_start_missing`/`headless_launch`.

Optional launcher flags:

- `.\codex-bridge.cmd --no-bot`
- `.\codex-bridge.cmd --bot-only`
- set `CODEX_BRIDGE_AUTO_START_TELEGRAM=1` only when you explicitly want the main launcher to start Telegram too

## Environment Variables

Example `.env`:

```env
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_ALLOWED_CHANNEL_IDS=
DISCORD_ALLOWED_USER_IDS=
DISCORD_ALLOW_ALL_CHANNELS=0
DISCORD_STARTUP_CHANNEL_ID=
DISCORD_STARTUP_NOTIFY=1
DISCORD_ENABLE_MESSAGE_CONTENT=1
DISCORD_PLAIN_ASK_MENTION_USER_IDS=
DISCORD_PLAIN_ASK_CONTEXT_FALLBACK=0
DISCORD_PLAIN_ASK_CONTEXT_KEYWORDS=codex,코덱스,bridge,브릿지,discord,디스코드,디코,bot,봇,응답,message,메시지,메세지,채팅,thread,스레드,queue,큐,steer,스티어,patch,패치,qa,하네스,harness,잘아타스
DISCORD_ENABLE_QA_COMMANDS=0
DISCORD_HISTORY_POLL_SECONDS=15
DISCORD_HISTORY_BOOTSTRAP_LOOKBACK_SECONDS=120
DISCORD_STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS=25
DISCORD_STEERING_PENDING_WATCH_TIMEOUT_SECONDS=600
DISCORD_STALE_BUSY_STEER_BLOCK_SECONDS=600
DISCORD_ASK_BUSY_RETRY_ATTEMPTS=3
DISCORD_ASK_BUSY_RETRY_DELAY_SECONDS=8
CODEX_DISCORD_LOG_PATH=
CODEX_HOME=
CODEX_DESKTOP_EXE=
PYTHON_EXE=
CODEX_BRIDGE_AUTO_START_TELEGRAM=0
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=
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
- `DISCORD_PLAIN_ASK_MENTION_USER_IDS`: optional comma-separated Discord user IDs that must be mentioned before plain messages are forwarded to Codex. The matching mention is stripped from the prompt; `!` commands and slash commands are not gated.
- `DISCORD_PLAIN_ASK_CONTEXT_FALLBACK`: set `1` to let unmentioned plain messages through when they look like bridge/Codex requests. Messages that mention another bot are still ignored unless they also contain one of the context keywords.
- `DISCORD_PLAIN_ASK_CONTEXT_KEYWORDS`: comma-separated lower-signal words used by context fallback, such as `codex`, `bridge`, `discord`, `harness`, the bridge bot display name, or local Korean keywords you add in your own `.env`
- `DISCORD_ENABLE_QA_COMMANDS`: set `1` to expose Discord QA smoke commands such as `!qa buttons` and `/qa_buttons`
- `DISCORD_HISTORY_POLL_SECONDS`: optional fallback interval for checking recent allowed/mirrored channel history; set `0` to disable
- `DISCORD_HISTORY_BOOTSTRAP_LOOKBACK_SECONDS`: optional startup lookback window for recent history polling
- `DISCORD_STEERING_DELIVERY_CONFIRM_TIMEOUT_SECONDS`: optional extra wait for delayed Codex IPC steering delivery confirmation
- `DISCORD_STEERING_PENDING_WATCH_TIMEOUT_SECONDS`: optional max wait for a steering watch after IPC accepted delivery but local recording lagged
- `DISCORD_STALE_BUSY_STEER_BLOCK_SECONDS`: optional age threshold for a busy target thread with no new local output; after this, Discord blocks additional steering prompts and reports not-sent guidance instead of stacking work into a stuck turn
- `DISCORD_ASK_BUSY_RETRY_ATTEMPTS`: retry count after Codex app transport reports busy but no approval/input menu is available
- `DISCORD_ASK_BUSY_RETRY_DELAY_SECONDS`: delay between those post-delivery busy retries
- `CODEX_DISCORD_LOG_PATH`: optional Discord adapter log path override, useful for smoke tests or isolated diagnostics
- `CODEX_HOME`: override default Codex state directory if needed
- `CODEX_DESKTOP_EXE`: optional override for the Codex Desktop app executable. `/discover_codex` or `/restart_codex` auto-save it into `.env` when discovery succeeds.
- `PYTHON_EXE`: force a specific Python interpreter
- `CODEX_BRIDGE_AUTO_START_TELEGRAM`: set `1` to make `codex-bridge.cmd` auto-start the optional Telegram adapter

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
DISCORD_PLAIN_ASK_MENTION_USER_IDS=
DISCORD_PLAIN_ASK_CONTEXT_FALLBACK=0
CODEX_HOME=C:\Users\your_user\.codex
PYTHON_EXE=C:\python\python.exe
CODEX_BRIDGE_AUTO_START_TELEGRAM=0
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

- `/help`, `/list`, `/archived_list`, `/use`, `/status`, `/doctor`, `/where`, `/context`, `/usage`, `/runners`, `/mirror_check`, `/bridge_sync`, `/new`, `/ask`, `/ask_ipc`

Optional Discord QA commands:

- set `DISCORD_ENABLE_QA_COMMANDS=1` to expose `!qa buttons` and `/qa_buttons`
- use them only in a test channel; they create temporary Discord button messages and clear their controls during the smoke test
- button QA covers busy-choice `Ignore`, stale/missing busy controls, synthetic `Steer now`, persistent approval, and persistent input-choice handlers without submitting a real Codex prompt
- `!steer <prompt>` is also exposed only when QA commands are enabled; it is a text-path smoke test for the same steering backend, not the normal user workflow
- persistent approval/input fallback buttons are single-use per Discord message, so replayed or double-clicked restart buttons do not submit twice
- startup cleanup removes stale busy-choice buttons whose backing DB records expired or were already handled, while preserving active in-flight controls
- `/doctor` reports whether QA commands are enabled, persistent component claim counts, the last button QA result, and last steering-button elapsed time from the Discord log

Messages inside a mirrored Discord thread are sent to that Codex thread. If a Discord project channel has multiple Codex threads, plain messages in the parent channel are blocked so they do not accidentally fall back to the selected Codex thread.

## Frontend Harness CLI

`codex_windows_harness.py` is the structured Windows-local status and preflight layer for the Discord frontend. Discord owns routing, commands, buttons, and mirrored-thread UX; the harness owns local Codex state decisions that should not be hidden inside Discord UI code.

Runtime probe:

```powershell
py -3 .\codex_windows_harness.py runtime
```

Ask preflight for a known Codex thread:

```powershell
py -3 .\codex_windows_harness.py preflight --thread-id <codex-thread-id>
```

Preflight routes:

- `route=ask`, `accepted=true`: target thread is idle
- `route=target_busy`, `accepted=false`, `can_steer=true`: diagnostic evidence that the target thread is already working; Discord plain asks still attempt mapped-thread delivery first and only mirror a Codex app menu after the app exposes one

When other Codex threads are busy but the mapped target thread is idle, preflight still returns `route=ask` and `accepted=true`. The other busy threads remain in `global_busy_threads` as diagnostic evidence only; Discord sends the ask to the mapped target with `--force-while-busy` instead of reporting `not sent`.

Discord runner busy is also separate from target Codex busy. If the mapped target thread is idle but the bridge is already processing a Discord ask for that thread, the next ask is queued in the same thread and does not show `Steer now`.

Stale target busy is handled as a separate safety case. If the local Codex session still looks busy but has not written new output past `DISCORD_STALE_BUSY_STEER_BLOCK_SECONDS`, Discord reports that the message was not sent as another steering prompt and points the operator to the Codex app, `!open_abort <thread-ref>`, or `!new <prompt>`.

The optional CLI probe reports `codex_cli_status` as `ok`, `not_found`, `permission_denied`, or `unavailable`. This is diagnostic only. The release target is the Codex app/web frontend flow through Discord, not Codex CLI execution.

## Optional Telegram Flow

The Telegram adapter remains available for legacy or mobile workflows, but Discord is the primary adapter and the main bridge launcher does not start Telegram unless `CODEX_BRIDGE_AUTO_START_TELEGRAM=1` is set.

Recommended optional Telegram workflow:

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
- It depends on Codex app/web internals and local state layout.
- App updates can break IPC discovery or UI automation.
- The Discord adapter must run as a singleton. A stale pre-mutex headless process can create duplicate Discord messages until that process is terminated.
- `Steer now` is a frontend steering operation against the active local Codex app/web session. It is release-ready only after live QA proves both Codex recording and Discord final relay on the target Windows host.
- If Codex accepts a steering prompt but the Codex turn itself does not produce a final answer before the watch timeout, Discord reports `Steering is still running in Codex.`; final relay still depends on the local Codex session finishing the turn.
- If a target thread remains busy without local output beyond the stale-steer threshold, Discord blocks additional steering prompts to avoid piling more work into the stuck turn.
- Switching visible threads while Codex is replying can still affect the active reply.
- Codex CLI execution is not the production path; the harness only probes CLI availability for diagnostics.
