"""Runtime state helpers for Discord runner and relay bookkeeping."""

from __future__ import annotations

import asyncio
import time


def normalize_runner_key(target_thread_id: str | None) -> str:
    return target_thread_id or "__selected__"


def mark_steering_handoff(
    handoffs: dict[str, float],
    target_thread_id: str | None,
    *,
    now_func=time.monotonic,
) -> float:
    handoff_at = now_func()
    handoffs[normalize_runner_key(target_thread_id)] = handoff_at
    return handoff_at


def had_steering_handoff_since(
    handoffs: dict[str, float],
    target_thread_id: str | None,
    started_at: float,
) -> bool:
    return handoffs.get(normalize_runner_key(target_thread_id), 0.0) > started_at


def register_discord_relay(generations: dict[str, int], target_thread_id: str | None) -> int:
    key = normalize_runner_key(target_thread_id)
    generation = generations.get(key, 0) + 1
    generations[key] = generation
    return generation


def is_discord_relay_stale(
    generations: dict[str, int],
    target_thread_id: str | None,
    generation: int,
) -> bool:
    return generations.get(normalize_runner_key(target_thread_id), 0) > generation


async def build_runners_message(
    runners: dict[str, dict[str, object]],
    runners_lock: object,
    *,
    resolve_target_ref_func,
) -> str:
    async with runners_lock:
        items = list(runners.items())
    if not items:
        return "No active Discord runner queues."
    lines = ["Discord runner queues"]
    for key, runner in items:
        queue = runner.get("queue")
        queue_size = queue.qsize() if isinstance(queue, asyncio.Queue) else 0
        target_thread_id = str(runner.get("target_thread_id") or "").strip() or None
        _thread_id, target_ref = resolve_target_ref_func(target_thread_id)
        lines.append(
            f"- {target_ref}: active={bool(runner.get('active'))} queued={queue_size} key={key[:8]}"
        )
    return "\n".join(lines)
