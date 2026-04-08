# AGENTS.md

## Goal
Make the smallest safe change that solves the requested problem without breaking existing behavior.

## Working Rules
- Do not refactor unrelated code.
- Do not rename functions, classes, files, routes, or environment variables unless explicitly required.
- Preserve existing public interfaces and behavior unless the task explicitly asks to change them.
- Prefer minimal, local patches over broad rewrites.
- Before editing, inspect the relevant call flow, imports, config usage, and side effects.
- If the root cause is uncertain, do not guess. First identify the likely cause from code evidence.
- If multiple files may be affected, explain the dependency briefly before patching.

## Patch Policy
- One patch should solve one clear problem.
- Avoid opportunistic cleanup.
- Avoid touching formatting in unrelated lines.
- Keep diffs small and easy to review.
- Reuse existing patterns in the codebase instead of introducing new styles.

## Safety Checks Before Editing
Always check:
1. entry point
2. calling path
3. config/env dependencies
4. template or frontend/backend coupling
5. error handling and logging impact
6. encoding / locale-sensitive code if text is involved

## Validation
After changes, always run the relevant checks when available:
- tests
- lint
- type check
- minimal local run for the changed path

If a command cannot be run, say so explicitly.
If a test fails, report whether it is caused by your patch or pre-existing.

## Output Format
When finishing a task, provide:
1. what changed
2. why it changed
3. files changed
4. validation run
5. remaining risks or assumptions

## Do Not
- Do not modify unrelated files.
- Do not introduce new dependencies unless explicitly requested.
- Do not change database schema, route paths, auth flow, or deployment config unless required by the task.
- Do not remove logs or telemetry unless explicitly requested.
- Do not silently change legacy behavior.

## For Bug Fixes
For bug fixes, follow this order:
1. reproduce mentally from code
2. identify root cause
3. patch minimally
4. verify no adjacent flow is broken
5. summarize exact behavioral change

## For Existing Projects
Prefer consistency with the existing project over idealized architecture.

## Anti-Breakage Rules
- Do not make speculative fixes.
- Do not patch multiple suspected causes at once.
- If the issue may come from state/session/config/template mismatch, inspect all linked files before editing.
- Preserve backward compatibility unless explicitly told otherwise.
- For production bugs, prefer a narrow defensive fix over a broad cleanup.
- If the codebase already has a service/module pattern, follow it instead of creating a new structure.
- When editing a handler, verify the paired template, JS, config, and service layer.
- When editing auth/session logic, verify redirect flow, CSRF/state handling, and environment-specific config.
