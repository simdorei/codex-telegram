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
- `cli/codex_cli_telegram_bot.py`: Telegram adapter for `codex exec` and `codex exec resume`
- `codex-bridge.cmd`: main launcher
- `codex-telegram-bot.cmd`: Telegram-only launcher
- `cli/codex-cli-telegram-bot.cmd`: Codex CLI Telegram launcher
- `.env.example`: local environment template
- `cli/.env`: separate CLI Telegram bot environment copy

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
CODEX_CLI_WORKDIR=C:\taxlab
CODEX_EXE=
CODEX_CLI_FULL_AUTO=0
CODEX_CLI_SKIP_GIT_REPO_CHECK=0
```

Important variables:

- `TELEGRAM_BOT_TOKEN`: required for Telegram mode
- `TELEGRAM_ALLOWED_CHAT_IDS`: optional allowlist of Telegram chat IDs
- `CODEX_HOME`: override default Codex state directory if needed
- `PYTHON_EXE`: force a specific Python interpreter
- `CODEX_BRIDGE_AUTO_START_TELEGRAM`: set `0` to disable bot auto-start
- `CODEX_CLI_WORKDIR`: workspace path for new Codex CLI sessions
- `CODEX_EXE`: override the Codex CLI executable name or path
- `CODEX_CLI_FULL_AUTO`: set `1` to run CLI asks with `--full-auto`
- `CODEX_CLI_SKIP_GIT_REPO_CHECK`: set `1` to pass `--skip-git-repo-check`

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

- `/list [limit]`
- `/use <ref>`
- `/status [ref]`
- `/doctor`
- `/ask <prompt>`
- `/ask_ipc <prompt> (alias)`
- `/abort`
- `/chatid`

Plain text Telegram messages are treated like `/ask <message>`.

Current Telegram flow:

1. `/list`
2. `/use <ref>`
3. Send `/ask <prompt>` or plain text

Current default Telegram `ask` uses IPC instead of UI paste. Telegram no longer exposes `/open`; use `/use` to bind the target thread. Very old threads may need to be opened once in the Codex Desktop app before IPC can address them directly.

## Codex CLI Telegram Bot

Files:

- `cli/codex_cli_telegram_bot.py`
- `cli/codex-cli-telegram-bot.cmd`
- `cli/.env`

Start it with:

```powershell
.\cli\codex-cli-telegram-bot.cmd
```

The CLI bot reads `cli\.env`, requires both `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_ALLOWED_CHAT_IDS`, and keeps one saved Codex CLI session per Telegram
chat. Run `/sessions`, choose one with `/use <number>`, then plain text
messages continue the selected session. Use `/new` to arm a fresh session.
You can keep a different bot token here from the desktop bridge's root `.env`.

CLI bot commands:

- `/sessions`
- `/use <number|session_id>`
- `/ask <prompt>`
- `/reset`
- `/new`
- `/session`
- `/cwd`
- `/chatid`

If Windows cannot find `codex`, the CLI bot will try `wsl.exe` automatically.
You can force that path with `CODEX_BACKEND=wsl` in `cli\.env`. Optional WSL
overrides are `CODEX_WSL_WORKDIR`, `CODEX_WSL_CODEX_HOME`, and
`CODEX_WSL_DISTRO`.

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

## 한국어 안내

### 개요

이 프로젝트는 `Codex CLI`가 아니라 `Codex Desktop 앱`을 Windows에서 다루기 위한 비공식 브리지입니다.

동작 방식은 두 가지를 조합합니다.

1. `CODEX_HOME` 또는 `%USERPROFILE%\.codex` 아래의 로컬 상태 파일 읽기
2. 실행 중인 `Codex` 창과 내부 IPC를 이용한 제어

### 요구사항

- Windows
- 로그인된 Codex Desktop 앱
- Python 3.11 이상
- Codex 앱과 같은 Windows 사용자 세션

추가 Python 패키지는 필요하지 않습니다.

### 빠른 시작

1. 저장소를 클론합니다.
2. `.env.example`을 `.env`로 복사합니다.
3. 텔레그램 제어를 쓸 경우 `TELEGRAM_BOT_TOKEN`을 입력합니다.
4. Codex Desktop 앱을 실행하고 로그인합니다.
5. 아래 명령으로 시작합니다.

```powershell
.\codex-bridge.cmd
```

`.env`에 `TELEGRAM_BOT_TOKEN`이 있으면 `codex-bridge.cmd`가 텔레그램 봇도 같이 올립니다.

선택 실행 옵션:

- `.\codex-bridge.cmd --no-bot`
- `.\codex-bridge.cmd --bot-only`

### 주요 파일

- `codex_desktop_bridge.py`: 로컬 thread 탐색, window 활성화, ask/watch 흐름
- `codex_telegram_bot.py`: 브리지를 같은 프로세스 안에서 호출하는 텔레그램 어댑터
- `cli/codex_cli_telegram_bot.py`: `codex exec`, `codex exec resume` 기반 CLI 텔레그램 어댑터
- `codex-bridge.cmd`: 메인 실행기
- `codex-telegram-bot.cmd`: 텔레그램 봇만 실행
- `cli/codex-cli-telegram-bot.cmd`: CLI 텔레그램 봇 실행기

### 주요 환경 변수

- `TELEGRAM_BOT_TOKEN`: 텔레그램 모드에서 필수
- `TELEGRAM_ALLOWED_CHAT_IDS`: 허용할 텔레그램 chat id 목록
- `CODEX_HOME`: Codex 상태 디렉터리를 수동 지정할 때 사용
- `PYTHON_EXE`: 특정 Python 실행 파일 강제 지정
- `CODEX_BRIDGE_AUTO_START_TELEGRAM`: `0`이면 봇 자동 실행 비활성화
- `CODEX_CLI_WORKDIR`: 새 Codex CLI 세션의 기본 작업 폴더
- `CODEX_EXE`: Codex CLI 실행 파일 이름 또는 경로
- `CODEX_CLI_FULL_AUTO`: `1`이면 CLI ask에 `--full-auto` 전달
- `CODEX_CLI_SKIP_GIT_REPO_CHECK`: `1`이면 `--skip-git-repo-check` 전달

### 브리지 REPL 명령

- `list`
- `open <ref>`
- `open --abort <ref>`
- `use <ref>`
- `ask "..."`
- `status`
- `doctor`

브리지 REPL의 기본 `ask`는 foreground 모드입니다.

- 현재 터미널을 점유한 채 응답이 끝날 때까지 기다립니다.
- 기본값으로는 commentary 스트리밍 없이 최종 답변만 출력합니다.
- `--ipc`, `--stream`, `--include-commentary` 같은 옵션은 필요할 때만 켭니다.

### 텔레그램 사용법

현재 텔레그램 쪽 기본 흐름은 `open`이 아니라 `use -> ask`입니다.

사용 가능한 명령:

- `/list [limit]`
- `/use <ref>`
- `/status [ref]`
- `/doctor`
- `/ask <prompt>`
- `/ask_ipc <prompt> (별칭)`
- `/abort`
- `/chatid`

일반 텍스트 메시지는 `/ask <message>`처럼 처리됩니다.

권장 흐름:

1. `/list`
2. `/use <ref>`
3. `/ask <prompt>` 또는 일반 텍스트 전송

현재 텔레그램의 기본 `ask`는 UI 복붙이 아니라 IPC를 사용합니다. 그래서 텔레그램 표면에서는 `/open`을 더 이상 쓰지 않고, `/use`로 target thread만 선택합니다.

주의:

- 아주 오래된 thread는 Codex Desktop 앱에서 한 번 열어 owner client를 만들어야 IPC가 바로 붙을 수 있습니다.
- 다른 thread가 busy여도 IPC ask는 가능하지만, 선택한 target thread 자체가 busy이면 차단됩니다.

### Thread Reference 예시

workspace에 최근 thread가 여러 개 있으면 아래처럼 표시됩니다.

- `ai:1`
- `ai:2`
- `taxlab`

예시:

```powershell
list
use ai:2
ask "Test"
```

### Codex CLI 텔레그램 봇

CLI용 텔레그램 봇은 별도 파일과 별도 `.env`를 사용합니다.

- `cli/codex_cli_telegram_bot.py`
- `cli/codex-cli-telegram-bot.cmd`
- `cli/.env`

실행:

```powershell
.\cli\codex-cli-telegram-bot.cmd
```

이 봇은 텔레그램 chat마다 별도 Codex CLI 세션을 유지합니다. `/sessions`로 목록을 보고 `/use <번호>`로 선택한 뒤 일반 텍스트를 보내면 그 세션에 이어서 질문합니다. 새 세션은 `/new`로 시작합니다.

### 공개 저장소 관련

- `.env`는 Git에 포함되지 않습니다.
- `*.log`는 Git에 포함되지 않습니다.
- `requirements.txt`에 패키지가 없는 이유는 표준 라이브러리만 사용하기 때문입니다.

### 트러블슈팅

삭제한 예전 폴더가 여전히 보이면 보통 원인은 아래 중 하나입니다.

- 실제 폴더가 디스크에 남아 있음
- 예전 thread의 `cwd`가 그 폴더로 남아 있음
- Codex가 workspace root와 recent thread 기록을 별도로 유지함

즉 앱에서 workspace를 제거해도 옛 thread 메타데이터가 자동 삭제되지는 않습니다.

### 제한사항

- 공식 API가 아닙니다.
- Codex Desktop UI와 로컬 상태 파일 형식에 의존합니다.
- 앱 업데이트로 일부 자동화가 깨질 수 있습니다.
- 응답 중 thread를 바꾸면 진행 중인 reply가 중단될 수 있습니다.
