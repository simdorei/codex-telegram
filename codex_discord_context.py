"""Context and usage message builders for the Discord bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def format_context_usage_line(thread: object, *, bridge_module: object) -> str:
    context_usage = bridge_module.get_thread_context_usage(thread)
    if context_usage is None:
        return "context: -"
    status = bridge_module.describe_thread_context_usage(context_usage)
    archive_hint = "yes" if bridge_module.should_recommend_archive(thread, context_usage) else "no"
    compaction_hint = f"compactions={context_usage.inferred_compactions}"
    if context_usage.inferred_compactions:
        compaction_hint += (
            f" last={bridge_module.format_token_k(context_usage.last_compaction_before_input_tokens)}"
            f"->{bridge_module.format_token_k(context_usage.last_compaction_after_input_tokens)}"
        )
    return (
        f"context: {context_usage.usage_ratio * 100:.1f}% ({status}) "
        f"last={bridge_module.format_token_k(context_usage.last_input_tokens)} "
        f"peak={bridge_module.format_token_k(context_usage.peak_input_tokens)} "
        f"window={bridge_module.format_token_k(context_usage.model_context_window)} "
        f"{compaction_hint} "
        f"archive_recommended={archive_hint}"
    )


def build_context_warning(
    target_thread_id: str | None,
    *,
    bridge_module: object,
    resolve_target_ref_func,
    log_func,
) -> str:
    try:
        resolved_thread_id, _target_ref = resolve_target_ref_func(target_thread_id)
        if not resolved_thread_id:
            return ""
        thread = bridge_module.choose_thread(resolved_thread_id, None)
        context_usage = bridge_module.get_thread_context_usage(thread)
    except Exception as exc:
        log_func(f"context_warning_unavailable target={target_thread_id or '-'} error={exc}")
        return ""
    if context_usage is None:
        return ""
    status = bridge_module.describe_thread_context_usage(context_usage)
    archive_recommended = bridge_module.should_recommend_archive(thread, context_usage)
    has_compaction_history = context_usage.inferred_compactions > 0
    if status not in {"high", "critical"}:
        return ""
    compaction_note = ""
    if has_compaction_history:
        compaction_note = (
            f" compactions={context_usage.inferred_compactions}"
            f" last={bridge_module.format_token_k(context_usage.last_compaction_before_input_tokens)}"
            f"->{bridge_module.format_token_k(context_usage.last_compaction_after_input_tokens)}."
        )
    return (
        f"Context warning: {context_usage.usage_ratio * 100:.1f}% ({status}), "
        f"archive_recommended={'yes' if archive_recommended else 'no'}."
        f"{compaction_note} "
        "Use `!context` to inspect, or `!new <prompt>` to continue in a fresh mirrored thread."
    )


def build_context_message(
    channel_id: int | None = None,
    *,
    all_threads: bool = False,
    limit: int = 10,
    bridge_module: object,
    get_mirrored_codex_thread_id_func,
    resolve_selected_target_func,
) -> str:
    if not all_threads:
        target_thread_id = get_mirrored_codex_thread_id_func(channel_id)
        if not target_thread_id:
            selected_thread_id, _target_ref = resolve_selected_target_func()
            target_thread_id = selected_thread_id
        if not target_thread_id:
            return "No Codex thread target found."
        try:
            thread = bridge_module.choose_thread(target_thread_id, None)
        except Exception as exc:
            return f"Context unavailable.\n\nERROR: {exc}"
        return "\n".join(
            [
                "Context status",
                f"thread_ref: {bridge_module.get_thread_workspace_ref(thread)}",
                f"title: {bridge_module.get_thread_ui_name(thread.id, thread) or thread.title or '-'}",
                format_context_usage_line(thread, bridge_module=bridge_module),
                f"tokens_used_total: {bridge_module.format_token_k(thread.tokens_used)}",
            ]
        )

    threads = bridge_module.load_recent_threads(limit=max(1, min(50, limit)))
    lines = ["Context status"]
    for thread in threads:
        title = bridge_module.get_thread_ui_name(thread.id, thread) or thread.title or thread.id[:8]
        lines.append(
            f"- {bridge_module.get_thread_workspace_ref(thread)} / {title}: "
            f"{format_context_usage_line(thread, bridge_module=bridge_module)}; "
            f"total={bridge_module.format_token_k(thread.tokens_used)}"
        )
    return "\n".join(lines)


def parse_event_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_window_minutes(value: object, *, bridge_module: object) -> str:
    minutes = bridge_module.coerce_nonnegative_int(value)
    if minutes <= 0:
        return "-"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def format_rate_limit_reset(value: object, *, bridge_module: object) -> str:
    reset_at = bridge_module.coerce_nonnegative_int(value)
    if reset_at <= 0:
        return "-"
    return bridge_module.format_timestamp(reset_at)


def format_rate_limit_line(label: str, value: object, *, bridge_module: object, format_percent_func) -> str:
    if not isinstance(value, dict):
        return f"{label}: -"
    return (
        f"{label}: used={format_percent_func(value.get('used_percent'))} "
        f"window={format_window_minutes(value.get('window_minutes'), bridge_module=bridge_module)} "
        f"resets={format_rate_limit_reset(value.get('resets_at'), bridge_module=bridge_module)}"
    )


def build_weekly_usage_message(days: int = 7, *, bridge_module: object, format_percent_func) -> str:
    days = max(1, min(30, days))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sessions_dir = bridge_module.CODEX_HOME / "sessions"
    if not sessions_dir.exists():
        return f"Local usage estimate unavailable: sessions directory not found at {sessions_dir}"

    turns = 0
    token_events = 0
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    files_scanned = 0
    recent_threads: set[str] = set()
    latest_rate_limits: dict[str, object] | None = None
    latest_rate_limits_at: datetime | None = None

    for session_path in sessions_dir.rglob("*.jsonl"):
        files_scanned += 1
        try:
            for event in bridge_module.iter_session_events(session_path):
                moment = parse_event_timestamp(event.get("timestamp"))
                if moment is None or moment < cutoff:
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if event.get("type") == "session_meta":
                    thread_id = str(payload.get("id") or "").strip()
                    if thread_id:
                        recent_threads.add(thread_id)
                    continue
                if event.get("type") != "event_msg":
                    continue
                event_type = payload.get("type")
                if event_type == "task_started":
                    turns += 1
                    turn_id = str(payload.get("turn_id") or "").strip()
                    if turn_id:
                        recent_threads.add(turn_id)
                    continue
                if event_type != "token_count":
                    continue
                info = payload.get("info") or {}
                if not isinstance(info, dict):
                    continue
                rate_limits = payload.get("rate_limits")
                if isinstance(rate_limits, dict) and (
                    latest_rate_limits_at is None or moment > latest_rate_limits_at
                ):
                    latest_rate_limits = rate_limits
                    latest_rate_limits_at = moment
                last_usage = info.get("last_token_usage") or {}
                if not isinstance(last_usage, dict):
                    continue
                token_events += 1
                event_input = bridge_module.coerce_nonnegative_int(last_usage.get("input_tokens"))
                event_total = bridge_module.coerce_nonnegative_int(last_usage.get("total_tokens"))
                input_tokens += event_input
                total_tokens += event_total
                output_tokens += max(0, event_total - event_input)
        except Exception:
            continue

    lines = [f"Codex usage ({days}d local scan)"]
    if latest_rate_limits:
        seen_at = (
            latest_rate_limits_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if latest_rate_limits_at
            else "-"
        )
        lines.extend(
            [
                "Latest rate limits",
                f"seen_at: {seen_at}",
                f"plan: {latest_rate_limits.get('plan_type') or '-'}",
                f"limit_id: {latest_rate_limits.get('limit_id') or '-'}",
                format_rate_limit_line(
                    "primary",
                    latest_rate_limits.get("primary"),
                    bridge_module=bridge_module,
                    format_percent_func=format_percent_func,
                ),
                format_rate_limit_line(
                    "secondary",
                    latest_rate_limits.get("secondary"),
                    bridge_module=bridge_module,
                    format_percent_func=format_percent_func,
                ),
                f"credits: {latest_rate_limits.get('credits') or '-'}",
                f"reached: {latest_rate_limits.get('rate_limit_reached_type') or '-'}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "Latest rate limits",
                "not found in recent local token_count events",
                "",
            ]
        )

    lines.extend(
        [
            "Local token estimate",
            f"turns: {turns}",
            f"token_events: {token_events}",
            f"total_tokens: {bridge_module.format_token_k(total_tokens)}",
            f"input_tokens: {bridge_module.format_token_k(input_tokens)}",
            f"output_tokens_est: {bridge_module.format_token_k(output_tokens)}",
            f"recent_threads_seen: {len(recent_threads)}",
            f"session_files_scanned: {files_scanned}",
        ]
    )
    return "\n".join(lines)
