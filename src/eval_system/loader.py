"""Offline loader: Harbor jobs/trials directories -> unified EvalSample.

Usage:
    loader = HarborEvalLoader(Path("~/.harbor/jobs"))
    for sample in loader.iter_samples(): ...

Accepts either:
  * a jobs root  (contains <job>/<trial>/ dirs, e.g. ~/.harbor/jobs)
  * a job dir    (contains <trial>/ dirs directly)
  * a single trial dir (has result.json)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from eval_system.schema import (
    AgentRunInfo,
    ArtifactFile,
    Artifacts,
    EvalSample,
    EvalScenario,
    RunTrace,
    Scoring,
)

logger = logging.getLogger(__name__)

_ARTIFACT_MANIFEST = "manifest.json"
_TRAJECTORY = "trajectory.json"
_BRIDGE_TRAJECTORY = "bridge-trajectory.json"
_VERIFIER_STDOUT = "test-stdout.txt"
_VERIFIER_STDERR = "test-stderr.txt"


def _is_trial_dir(path: Path) -> bool:
    """A trial dir has result.json AND trial.log (job dirs also have result.json)."""
    return (
        path.is_dir()
        and (path / "result.json").is_file()
        and (path / "trial.log").is_file()
    )


def _is_job_dir(path: Path) -> bool:
    return path.is_dir() and (path / "job.log").exists() or (
        path.is_dir() and any(_is_trial_dir(p) for p in path.iterdir())
    )


def _safe_json(path: Path) -> dict | list | None:
    if not path.is_file():
        logger.debug("Missing file: %s", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot parse %s: %s", path, exc)
        return None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


class HarborEvalLoader:
    """Scans Harbor output and normalizes each trial into an EvalSample."""

    def __init__(self, path: str | Path):
        self.root = Path(path).expanduser()

    # -- discovery ----------------------------------------------------------
    def discover_trial_dirs(self, *, recursive: bool = True) -> list[Path]:
        """Find trial directories under the root.

        If the root itself is a trial dir, returns it. If it is a job dir,
        returns its trial children. Otherwise walks recursively for dirs
        containing result.json.
        """
        root = self.root
        if _is_trial_dir(root):
            return [root]

        if recursive:
            return sorted(
                p for p in root.rglob("*") if p.is_dir() and _is_trial_dir(p)
            )
        return sorted(p for p in root.iterdir() if _is_trial_dir(p))

    # -- loading ------------------------------------------------------------
    def iter_samples(self, *, recursive: bool = True) -> Iterator[EvalSample]:
        for trial_dir in self.discover_trial_dirs(recursive=recursive):
            sample = self.load_trial(trial_dir)
            if sample is not None:
                yield sample

    def load_trial(self, trial_dir: str | Path) -> EvalSample | None:
        """Load one trial dir into an EvalSample. Returns None on no result.json."""
        trial_dir = Path(trial_dir)
        result = _safe_json(trial_dir / "result.json")
        if result is None:
            return None

        config = _safe_json(trial_dir / "config.json") or {}

        return EvalSample(
            sample_id=trial_dir.name,
            trial_dir=str(trial_dir.resolve()),
            scenario=self._build_scenario(trial_dir, result, config),
            agent=self._build_agent(result),
            artifacts=self._build_artifacts(trial_dir),
            runtrace=self._build_runtrace(trial_dir, result),
            scoring=self._build_scoring(trial_dir, result),
            raw_result=result,
            is_regrade=bool(config.get("source_trial")),
        )

    # -- builders -----------------------------------------------------------
    def _build_scenario(self, trial_dir: Path, result: dict, config: dict) -> EvalScenario:
        # Prefer eval-system's normalized TaskSpec when present.  Harbor's raw
        # result.task_id for local tasks is a path object and loses benchmark
        # provenance, while specs/task.json preserves the stable task_id and
        # benchagent metadata needed by the feedback loop.
        task_spec = _safe_json(trial_dir / "specs" / "task.json") or {}
        task_name = task_spec.get("name") or result.get("task_name")
        raw_task_id = task_spec.get("task_id") or result.get("task_id")
        task_id = _stringify_task_id(raw_task_id)
        scenario_id = task_id or (
            f"{task_name}@{config.get('task', {}).get('ref')}"
            if task_name else "unknown"
        )
        instruction = task_spec.get("instruction") or self._load_instruction(config)
        scenario_config = dict(config)
        metadata = task_spec.get("metadata")
        if isinstance(metadata, dict):
            scenario_config["benchmark_metadata"] = metadata
            for key, value in metadata.items():
                scenario_config.setdefault(key, value)
        return EvalScenario(
            scenario_id=scenario_id,
            task_name=task_name,
            task_id=task_id,
            source=task_spec.get("source") or result.get("source"),
            instruction=instruction,
            task_checksum=task_spec.get("task_checksum") or result.get("task_checksum"),
            config=scenario_config,
        )

    @staticmethod
    def _load_instruction(config: dict) -> str | None:
        """Best-effort: read instruction.md from a local task dir."""
        task = config.get("task") or {}
        path = task.get("path")
        if not path:
            return None
        instruction_path = Path(path).expanduser() / "instruction.md"
        try:
            if instruction_path.is_file():
                return instruction_path.read_text(encoding="utf-8")
        except OSError:
            pass
        return None

    @staticmethod
    def _build_agent(result: dict) -> AgentRunInfo:
        agent_info = result.get("agent_info") or {}
        model_info = agent_info.get("model_info") or {}
        return AgentRunInfo(
            name=agent_info.get("name", "unknown"),
            version=agent_info.get("version"),
            model=model_info.get("name"),
            provider=model_info.get("provider"),
            trial_name=result.get("trial_name", ""),
            trial_uri=result.get("trial_uri"),
            started_at=_iso(result.get("started_at")),
            finished_at=_iso(result.get("finished_at")),
        )

    def _build_artifacts(self, trial_dir: Path) -> Artifacts:
        artifacts_dir = trial_dir / "artifacts"
        manifest = _safe_json(artifacts_dir / _ARTIFACT_MANIFEST) or []
        entries: list[ArtifactFile] = []
        if isinstance(manifest, list):
            for entry in manifest:
                if not isinstance(entry, dict):
                    continue
                destination = entry.get("destination", "")
                host_path = artifacts_dir / destination.removeprefix("artifacts/")
                entries.append(
                    ArtifactFile(
                        source=entry.get("source", ""),
                        destination=destination,
                        type=entry.get("type", "file"),
                        status=entry.get("status", "unknown"),
                        service=entry.get("service"),
                        host_path=str(host_path),
                        size_bytes=(
                            _file_size(host_path) if host_path.exists() else None
                        ),
                    )
                )
        return Artifacts(root=str(artifacts_dir), manifest=entries).expand_directories()

    def _build_runtrace(self, trial_dir: Path, result: dict) -> RunTrace:
        agent_dir = trial_dir / "agent"
        trajectory_path = agent_dir / _TRAJECTORY
        trajectory = _safe_json(trajectory_path) if trajectory_path.is_file() else None

        # Harbor may collect a Pi native session without invoking the custom
        # agent post-run hook (or the hook may fail closed).  Reuse the
        # lossless native JSONL as a read-side fallback so downstream
        # AgentEval still receives the ATIF runtrace.  This does not replace
        # the native files; it only materializes the existing projection.
        if trajectory is None:
            pi_sessions = sorted((agent_dir / "pi" / "sessions").glob("*.jsonl"))
            if pi_sessions:
                try:
                    from eval_system.integrations.pi_atif import convert_pi_session_to_atif

                    agent_info = result.get("agent_info") or {}
                    model_info = agent_info.get("model_info") or {}
                    converted = convert_pi_session_to_atif(
                        pi_sessions[-1],
                        agent_version=str(agent_info.get("version") or "unknown"),
                        fallback_model_name=model_info.get("name"),
                    )
                    if converted is not None:
                        trajectory = converted.to_json_dict()
                        trajectory_path.write_text(
                            json.dumps(trajectory, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                except Exception:
                    # Native session remains authoritative; malformed or
                    # future Pi events must not make trial loading fail.
                    trajectory = None

        native_files: list[str] = list()
        if agent_dir.is_dir():
            native_files = sorted(
                str(p.relative_to(agent_dir))
                for p in agent_dir.rglob("*")
                if p.is_file() and p.name not in (_TRAJECTORY,)
            )

        bridge_path = trial_dir / "user-agent" / _BRIDGE_TRAJECTORY
        bridge = _safe_json(bridge_path) if bridge_path.is_file() else None

        return RunTrace(
            agent_logs_dir=str(agent_dir),
            trajectory=trajectory if isinstance(trajectory, dict) else None,
            trajectory_path=str(trajectory_path) if trajectory_path.is_file() else None,
            native_session_files=native_files,
            agent_context=result.get("agent_result"),
            bridge_trajectory=bridge if isinstance(bridge, dict) else None,
        )

    def _build_scoring(self, trial_dir: Path, result: dict) -> Scoring:
        verifier_dir = trial_dir / "verifier"
        verifier_result = result.get("verifier_result")
        rewards = verifier_result.get("rewards") if verifier_result else None

        def _read(name: str) -> str | None:
            path = verifier_dir / name
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

        exception = result.get("exception_info")
        timings = {
            key: result.get(key)
            for key in (
                "environment_setup",
                "agent_setup",
                "agent_execution",
                "verifier",
            )
            if result.get(key) is not None
        }

        return Scoring(
            rewards=rewards,
            reward=_primary_reward(rewards),
            test_stdout=_read(_VERIFIER_STDOUT),
            test_stderr=_read(_VERIFIER_STDERR),
            verifier_result=verifier_result,
            exception=exception,
            timings=timings,
            step_results=result.get("step_results"),
        )


# -- helpers ----------------------------------------------------------------
def _stringify_task_id(task_id: object) -> str | None:
    if task_id is None:
        return None
    if isinstance(task_id, dict):
        return json.dumps(task_id, sort_keys=True)
    return str(task_id)


def _iso(value: object) -> str | None:
    return str(value) if value is not None else None


def _primary_reward(rewards: object) -> float | None:
    if not isinstance(rewards, dict):
        return None
    reward = rewards.get("reward")
    if reward is None and rewards:
        reward = next(iter(rewards.values()))
    if reward is None:
        return None
    try:
        return float(reward)
    except (TypeError, ValueError):
        return None
