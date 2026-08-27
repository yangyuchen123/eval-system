"""Small Harbor agent compatibility adapters used by eval-system."""
from __future__ import annotations

from typing import Any

from harbor.agents.installed.codex import Codex


class _OpenRouterModelName(str):
    """Preserve OpenRouter's vendor/model id through Harbor Codex's final split.

    Harbor 0.22's Codex adapter strips every prefix with ``split('/')[-1]``.
    For ``openrouter/deepseek/model`` that incorrectly sends only ``model``.
    Keep provider inference (``split('/', 1)``) unchanged while making the
    final unrestricted split return ``deepseek/model``.
    """

    def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
        if sep == "/" and maxsplit == -1 and self.startswith("openrouter/"):
            return ["openrouter", self.removeprefix("openrouter/")]
        return super().split(sep, maxsplit)


class OpenRouterCodex(Codex):
    """Codex adapter that sends the full OpenRouter model identifier."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.model_name:
            self.model_name = _OpenRouterModelName(self.model_name)

from harbor.agents.installed.pi import Pi
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.utils.trajectory_utils import format_trajectory_json

from eval_system.integrations.pi_atif import convert_pi_session_to_atif


class OpenRouterPi(Pi):
    """Pi adapter with preinstalled setup and Pi-native to ATIF conversion.

    Harbor 0.22's stock Pi adapter always installs NVM and Pi during every
    trial and does not emit ATIF. This adapter skips installation when Pi is
    already present and projects its lossless native JSONL session to ATIF.
    """

    SUPPORTS_ATIF = True

    async def install(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(command="command -v pi >/dev/null 2>&1")
        if result.return_code == 0:
            return
        await super().install(environment)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        session_files = sorted((self.logs_dir / "pi" / "sessions").glob("*.jsonl"))
        if not session_files:
            self.logger.debug("No Pi native session found for ATIF conversion")
            return
        try:
            trajectory = convert_pi_session_to_atif(
                session_files[-1],
                agent_version=self._version or "unknown",
                fallback_model_name=self.model_name,
            )
        except Exception:
            self.logger.exception("Failed to convert Pi native session to ATIF")
            return
        if trajectory is None:
            return
        trajectory_path = self.logs_dir / "trajectory.json"
        trajectory_path.write_text(
            format_trajectory_json(trajectory.to_json_dict()), encoding="utf-8"
        )
        if trajectory.final_metrics:
            metrics = trajectory.final_metrics
            context.n_input_tokens = metrics.total_prompt_tokens or 0
            context.n_cache_tokens = metrics.total_cached_tokens or 0
            context.n_output_tokens = metrics.total_completion_tokens or 0
            context.cost_usd = metrics.total_cost_usd
