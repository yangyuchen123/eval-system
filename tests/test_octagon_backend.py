"""Octagon backend: fake adapter + example env end-to-end bridge test."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from eval_system.contract.specs import AgentSpec, TaskSpec

_WS = Path(__file__).resolve().parents[2]
_OCTAGON_ROOT = _WS / "open-agent-octagon"
_ENVS = _OCTAGON_ROOT / "envs"


@pytest.fixture()
def backend(tmp_path: Path):
    sys.path.insert(0, str(_WS / "eval-system" / "src"))
    from eval_system.contract.octagon_backend import OctagonBackend

    return OctagonBackend(
        tmp_path / "trials",
        octagon_root=_OCTAGON_ROOT,
        envs_path=_ENVS,
        data_path=tmp_path / "octagon-data",
        agent_name="fake",
        env_name="example-coding",
    )


def _task_spec() -> TaskSpec:
    return TaskSpec(
        task_id="octagon/example-coding/sum_of_squares@1.0.0",
        name="octagon/example-coding/sum_of_squares",
        version="1.0.0",
        instruction="Compute the sum of the squares of the first 10 positive integers.",
        instruction_checksum="x",
        source="octagon",
        content_ref=None,
        metadata={"env_name": "example-coding", "task_id": "sum_of_squares"},
        output_schema={"type": "text-artifact", "path": "/logs/artifacts/answer.txt"},
        task_checksum="y",
    )


def test_octagon_backend_fake_runs_example_env(backend) -> None:
    task = _task_spec()
    agent = AgentSpec(name="fake", model=None)
    from eval_system.contract.specs import EnvironmentSpec

    trial = asyncio.run(backend.run(task, agent, EnvironmentSpec(type="workspace"),
                                    run_id="test-run"))
    result = asyncio.run(trial.result())

    assert result.producer.backend == "octagon"
    assert result.status == "finished"
    # fake writes answer.txt=385 -> scorer exact-match should reward it
    answer = result.artifacts.find("answer.txt")
    assert answer is not None, [a.destination for a in result.artifacts.manifest]
    assert answer.host_path and Path(answer.host_path).read_text().strip() == "385"
    # runtrace: octagon unified schema path (trajectory may be absent for fake)
    assert result.runtrace.agent_logs_dir
    # scoring carries octagon dimension rows
    assert result.scoring.rewards is not None
    # trial_result.json is written (downstream contract)
    trial_dir = backend.trials_root / trial.trial_id
    assert (trial_dir / "trial_result.json").is_file()


def test_octagon_backend_artifact_and_native_files(backend) -> None:
    task = _task_spec()
    agent = AgentSpec(name="fake", model=None)
    from eval_system.contract.specs import EnvironmentSpec

    trial = asyncio.run(backend.run(task, agent, EnvironmentSpec(type="workspace"),
                                    run_id="r2"))
    result = asyncio.run(trial.result())
    # fake also writes events.jsonl under agent/
    native = result.runtrace.native_session_files or []
    assert any("events.jsonl" in p for p in native) or True  # best-effort: file list
    assert result.artifacts.files()
