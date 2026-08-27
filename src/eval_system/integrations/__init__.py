"""Adapters connecting benchmark production, execution and scoring."""

from .agent_eval import trial_results_to_agent_eval
from .benchagent import benchmark_to_task_specs, materialize_benchmark_tasks

__all__ = [
    "benchmark_to_task_specs",
    "materialize_benchmark_tasks",
    "trial_results_to_agent_eval",
]
