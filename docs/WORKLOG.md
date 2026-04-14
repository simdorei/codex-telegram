# WORKLOG

## 2026-04-14 20:16:22 +09:00
- Goal: stabilize Telegram interactive handling without changing plain-message behavior.
- Findings:
  - `request_user_input` arrives as `[choice_required]` with explicit option labels.
  - escalated commands arrive as `[approval_required]` with `tool` and `justification`, but no option labels in the rollout event itself.
  - Codex desktop app bundle exposes the approval UI route as `item/commandExecution/requestApproval`.
  - Codex desktop `ko-KR` locale bundle contains the labels used by the request input panel and approval UI.
- Changes:
  - Added interactive notice parsing for both `choice_required` and `approval_required`.
  - Synthesized approval choices from the desktop locale-backed labels:
    - `예`
    - `아니요`
    - `아니요, Codex에게 다르게 해야 할 내용을 설명해 주세요`
  - Updated pending-choice detection, `/choices`, waiting/busy display, and busy-thread follow to use the same interactive source.
  - Prevented approval choices from falling through to a normal `/choose` ask path until approval decision submission is wired correctly.
- Validation:
  - `git diff --check -- C:/ai/codex/codex_telegram_bot.py`
  - `C:\python\python.exe -m py_compile C:\ai\codex\codex_telegram_bot.py`
  - direct `parse_interactive_notice()` sample check for both choice and approval notices
- Unresolved:
  - `/choose 1/2` still does not submit an approval decision.
  - approval submission needs the underlying request id and a safe bridge path to `item/commandExecution/requestApproval`.
