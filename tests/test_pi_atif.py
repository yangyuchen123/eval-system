from __future__ import annotations

import json
from pathlib import Path

from eval_system.integrations.harbor_agents import OpenRouterPi
from eval_system.integrations.pi_atif import convert_pi_session_to_atif


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_pi_native_session_converts_to_atif(tmp_path: Path) -> None:
    session = tmp_path / "session.jsonl"
    _write_jsonl(session, [
        {"type": "session", "version": 3, "id": "session-1", "timestamp": "2026-08-27T00:00:00Z", "cwd": "/workspace"},
        {"type": "model_change", "provider": "openrouter", "modelId": "deepseek/deepseek-v4-flash-0731"},
        {"type": "thinking_level_change", "thinkingLevel": "high"},
        {"type": "message", "id": "u1", "message": {"role": "user", "content": [{"type": "text", "text": "Do the task"}], "timestamp": 1787788800000}},
        {"type": "message", "id": "a1", "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Need to inspect the file."},
                {"type": "text", "text": "I will inspect it."},
                {"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "/workspace/input.json"}},
            ],
            "api": "openai-completions", "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash-0731",
            "usage": {"input": 100, "output": 20, "cacheRead": 30, "cacheWrite": 4, "reasoning": 5, "totalTokens": 150, "cost": {"total": 0.001}},
            "stopReason": "toolUse", "timestamp": 1787788801000, "responseId": "resp-1",
        }},
        {"type": "message", "id": "t1", "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "read", "content": [{"type": "text", "text": "contents"}], "isError": False, "timestamp": 1787788801100}},
        {"type": "message", "id": "a2", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Done"}],
            "api": "openai-completions", "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash-0731",
            "usage": {"input": 50, "output": 10, "cacheRead": 10, "cacheWrite": 0, "reasoning": 0, "totalTokens": 70, "cost": {"total": 0.0005}},
            "stopReason": "stop", "timestamp": 1787788802000,
        }},
    ])

    trajectory = convert_pi_session_to_atif(session, agent_version="0.84.3")
    assert trajectory is not None
    assert trajectory.schema_version == "ATIF-v1.7"
    assert trajectory.session_id == "session-1"
    assert trajectory.agent.name == "pi"
    assert trajectory.agent.model_name == "deepseek/deepseek-v4-flash-0731"
    assert len(trajectory.steps) == 3

    tool_step = trajectory.steps[1]
    assert tool_step.source == "agent"
    assert tool_step.reasoning_content == "Need to inspect the file."
    assert tool_step.reasoning_effort == "high"
    assert tool_step.tool_calls and tool_step.tool_calls[0].function_name == "read"
    assert tool_step.observation is not None
    assert tool_step.observation.results[0].source_call_id == "call-1"
    assert tool_step.observation.results[0].content == "contents"
    assert tool_step.metrics is not None
    assert tool_step.metrics.prompt_tokens == 130
    assert tool_step.metrics.cached_tokens == 30

    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 190
    assert trajectory.final_metrics.total_completion_tokens == 30
    assert trajectory.final_metrics.total_cached_tokens == 40
    assert trajectory.final_metrics.total_cost_usd == 0.0015


def test_openrouter_pi_declares_atif_support() -> None:
    assert OpenRouterPi.SUPPORTS_ATIF is True
