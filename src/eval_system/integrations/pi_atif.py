"""Convert Pi Coding Agent native JSONL sessions to ATIF v1.7."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)


def _timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Pi message timestamps are Unix epoch milliseconds.
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _text_parts(content: Any, *, keys: tuple[str, ...] = ("text",)) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
    return "\n".join(parts)


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    if value is None:
        return {}
    return {"value": value}


def _metrics(message: dict[str, Any]) -> Metrics | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    direct_input = usage.get("input") or 0
    cached = usage.get("cacheRead") or 0
    output = usage.get("output") or 0
    cost = usage.get("cost") or {}
    extra = {
        "cache_write_tokens": usage.get("cacheWrite"),
        "reasoning_tokens": usage.get("reasoning"),
        "total_tokens": usage.get("totalTokens"),
    }
    extra = {key: value for key, value in extra.items() if value is not None}
    return Metrics(
        prompt_tokens=direct_input + cached or None,
        completion_tokens=output or None,
        cached_tokens=cached or None,
        cost_usd=cost.get("total") if isinstance(cost, dict) else None,
        extra=extra or None,
    )


def read_pi_events(session_file: str | Path) -> list[dict[str, Any]]:
    """Read valid object events from a Pi session JSONL file."""
    events: list[dict[str, Any]] = []
    with Path(session_file).open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def convert_pi_session_to_atif(
    session_file: str | Path,
    *,
    agent_version: str = "unknown",
    fallback_model_name: str | None = None,
) -> Trajectory | None:
    """Convert one Pi v3 native session into a validated ATIF trajectory.

    Assistant messages remain one ATIF step each. Tool results that follow an
    assistant message are attached to that same step through ``source_call_id``.
    The native JSONL remains the lossless authority; ATIF is the cross-agent
    projection.
    """
    events = read_pi_events(session_file)
    if not events:
        return None

    session = next((event for event in events if event.get("type") == "session"), {})
    model_change = next((event for event in events if event.get("type") == "model_change"), {})
    thinking_change = next(
        (event for event in events if event.get("type") == "thinking_level_change"), {}
    )
    session_id = session.get("id") or Path(session_file).stem
    default_model = model_change.get("modelId") or fallback_model_name
    provider = model_change.get("provider")
    reasoning_effort = thinking_change.get("thinkingLevel")

    steps: list[Step] = []
    pending_calls: dict[str, int] = {}
    totals = {"prompt": 0, "completion": 0, "cached": 0, "cost": 0.0}
    saw_cost = False
    api_names: set[str] = set()
    providers: set[str] = set()

    for event in events:
        if event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        timestamp = _timestamp(message.get("timestamp")) or _timestamp(event.get("timestamp"))

        if role == "user":
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    timestamp=timestamp,
                    source="user",
                    message=_text_parts(message.get("content")),
                    extra={"pi_event_id": event.get("id")} if event.get("id") else None,
                )
            )
            continue

        if role == "assistant":
            content = message.get("content") or []
            text = _text_parts(content, keys=("text",))
            reasoning = _text_parts(content, keys=("thinking",))
            tool_calls: list[ToolCall] = []
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "toolCall":
                        continue
                    call_id = str(item.get("id") or "")
                    tool_calls.append(
                        ToolCall(
                            tool_call_id=call_id,
                            function_name=str(item.get("name") or ""),
                            arguments=_arguments(item.get("arguments")),
                        )
                    )

            metrics = _metrics(message)
            if metrics:
                totals["prompt"] += metrics.prompt_tokens or 0
                totals["completion"] += metrics.completion_tokens or 0
                totals["cached"] += metrics.cached_tokens or 0
                if metrics.cost_usd is not None:
                    totals["cost"] += metrics.cost_usd
                    saw_cost = True
            if isinstance(message.get("api"), str):
                api_names.add(message["api"])
            if isinstance(message.get("provider"), str):
                providers.add(message["provider"])

            extra = {
                "pi_event_id": event.get("id"),
                "response_id": message.get("responseId"),
                "stop_reason": message.get("stopReason"),
                "raw_stop_reason": message.get("rawStopReason"),
                "api": message.get("api"),
                "provider": message.get("provider"),
            }
            extra = {key: value for key, value in extra.items() if value is not None}
            step = Step(
                step_id=len(steps) + 1,
                timestamp=timestamp,
                source="agent",
                message=text,
                model_name=message.get("model") or default_model,
                reasoning_effort=reasoning_effort,
                reasoning_content=reasoning or None,
                tool_calls=tool_calls or None,
                metrics=metrics,
                llm_call_count=1,
                extra=extra or None,
            )
            steps.append(step)
            step_index = len(steps) - 1
            for tool_call in tool_calls:
                if tool_call.tool_call_id:
                    pending_calls[tool_call.tool_call_id] = step_index
            continue

        if role == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            result = ObservationResult(
                source_call_id=call_id or None,
                content=_text_parts(message.get("content")),
                extra={
                    key: value
                    for key, value in {
                        "tool_name": message.get("toolName"),
                        "is_error": message.get("isError"),
                        "timestamp": _timestamp(message.get("timestamp")),
                        "pi_event_id": event.get("id"),
                    }.items()
                    if value is not None
                }
                or None,
            )
            step_index = pending_calls.pop(call_id, None)
            if step_index is not None:
                step = steps[step_index]
                current = list(step.observation.results) if step.observation else []
                current.append(result)
                step.observation = Observation(results=current)
            else:
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        timestamp=timestamp,
                        source="system",
                        message="Pi tool result without a matching tool call",
                        observation=Observation(results=[result]),
                    )
                )

    if not steps:
        return None

    extra = {
        "native_format": "pi-session-v3",
        "native_session_file": Path(session_file).name,
        "cwd": session.get("cwd"),
        "provider": provider or (sorted(providers)[0] if len(providers) == 1 else None),
        "apis": sorted(api_names),
        "thinking_level": reasoning_effort,
    }
    final_extra = {
        "provider": extra.get("provider"),
        "apis": sorted(api_names),
    }
    final_extra = {key: value for key, value in final_extra.items() if value}
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=str(session_id),
        agent=Agent(
            name="pi",
            version=agent_version or "unknown",
            model_name=default_model,
            extra={key: value for key, value in extra.items() if value is not None},
        ),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=totals["prompt"] or None,
            total_completion_tokens=totals["completion"] or None,
            total_cached_tokens=totals["cached"] or None,
            total_cost_usd=totals["cost"] if saw_cost else None,
            total_steps=len(steps),
            extra=final_extra or None,
        ),
    )
