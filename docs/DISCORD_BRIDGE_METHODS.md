# Discord Bridge Methods

This note records the Codex Desktop bridge transports currently available in this workspace.

## 1. IPC named pipe

- Code path: `codex_desktop_bridge.py`
- Pipe: `\\.\pipe\codex-ipc`
- Main methods:
  - `thread-follower-start-turn`
  - `thread-follower-submit-user-input`
  - approval decision submit methods used by `submit_approval_decision_via_ipc`
- Current Discord use:
  - default `ask` path invokes bridge `ask --ipc --ipc-recover-ui`
  - approval/input follow-up handling uses IPC state and submit helpers
- Strength:
  - best live session integration when the target thread owner client is loaded
  - supports approval/input state inspection and response submission
- Failure mode:
  - owner client discovery can fail when the Codex UI has not loaded the target thread
  - bridge already has UI recovery and local sidecar fallback for `ask` start-turn

## 2. Local app-server sidecar

- Code path: `CodexAppServerSidecar`
- Launch: `codex.exe app-server`
- JSON-RPC methods currently used:
  - `initialize`
  - `thread/start`
  - `thread/read`
  - `thread/resume`
  - `turn/start`
  - `turn/interrupt`
  - `thread/backgroundTerminals/clean`
  - `thread/archive`
- Current Discord/bridge use:
  - `new` creates threads through the sidecar runner
  - `archive` uses sidecar thread archive
  - busy status can read/resume target thread through sidecar
  - `ask --ipc` falls back to `start_turn_via_sidecar` when IPC owner-client discovery fails
- Strength:
  - does not need the Electron UI owner client for start-turn once the app-server can attach
  - good candidate for making Discord ask less dependent on IPC
- Current gap:
  - approval and follow-up input submission are still IPC/UI oriented
  - sidecar start-turn needs delivery verification against session JSONL before Discord can trust it

## 3. Foreground UI automation

- Code path:
  - `activate_thread_in_ui`
  - `verify_thread_in_ui`
  - `send_prompt_to_codex`
  - `submit_permission_approval_via_ui_row_select`
- Mechanism:
  - Windows UI automation, clipboard, mouse/keyboard, and visible Codex window focus
- Current use:
  - bridge `ask --ui` legacy path
  - fallback approval row selection in specific permission approval cases
- Strength:
  - works even when IPC owner-client routing is unavailable, if the visible UI is correct
- Risk:
  - foreground focus and UI layout make it less stable for unattended Discord control

## 4. Debug app-server send-message-v2

- Code path: `spawn_background_new_thread_runner`
- Launch:
  - `codex.exe debug app-server send-message-v2 <prompt>`
- Current use:
  - background `new` runner creates and persists a new thread with the first prompt
- Strength:
  - useful for fresh thread creation without visible UI paste
- Current gap:
  - scoped to new-thread creation in current code, not a general mapped-thread ask path

## Practical Direction

For Discord, the strongest non-IPC direction is the local app-server sidecar:

1. Keep IPC as the primary path for approval/input and live owner-client state.
2. Make app-server sidecar an explicit ask transport option, not only an IPC failure fallback.
3. Keep session JSONL delivery verification mandatory after sidecar `turn/start`.
4. Keep foreground UI automation as last-resort/manual recovery, not the default live QA path.

