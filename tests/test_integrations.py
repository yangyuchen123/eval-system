from __future__ import annotations

import json
from pathlib import Path

from eval_system.integrations.benchagent import (
    benchmark_to_task_specs,
    materialize_benchmark_tasks,
)


def test_benchagent_sample_materializes_harbor_task(tmp_path: Path) -> None:
    source = tmp_path / "evaluation.json"
    source.write_text(json.dumps({
        "query": {"id": "demo"},
        "samples": [{
            "subtask_id": "qa",
            "sample_index": 0,
            "context": "context",
            "question": "question",
            "options": ["a", "b"],
            "answer": "a",
            "answer_type": "multiple_choice",
        }],
    }), encoding="utf-8")

    root = tmp_path / "tasks"
    paths = materialize_benchmark_tasks(source, root)
    specs = benchmark_to_task_specs(source, task_root=root)

    assert len(paths) == 1
    assert specs[0].content_ref is not None
    assert Path(specs[0].content_ref.path) == paths[0]
    assert (paths[0] / "instruction.md").is_file()
    assert (paths[0] / "tests/test.sh").is_file()
    assert json.loads((paths[0] / "tests/expected.json").read_text())["answer"] == "a"
    assert specs[0].metadata["feedback_source_context"] == "validation"


def test_feedback_bridge_preserves_benchmark_event_shape(tmp_path: Path) -> None:
    from eval_system.integrations.feedback import build_feedback_events, write_feedback_events
    from eval_system.schema import EvalSample, EvalScenario, AgentRunInfo, Artifacts, RunTrace, Scoring

    sample = EvalSample(
        sample_id="trial-1",
        trial_dir=str(tmp_path),
        scenario=EvalScenario(scenario_id="bench/demo", task_name="bench/demo", task_id="bench/demo/0", instruction="do it"),
        agent=AgentRunInfo(name="pi", model="test-model", trial_name="trial-1"),
        artifacts=Artifacts(root=str(tmp_path), manifest=[]),
        runtrace=RunTrace(agent_logs_dir=str(tmp_path), trajectory={"steps": []}),
        scoring=Scoring(rewards={"reward": 0}, reward=0),
    )
    report = {"cases": [{"case_id": "trial-1", "score": 0.2, "skills": {"judge": {"score": 0.2}}}]}
    events = build_feedback_events([sample], report)
    assert len(events) == 1
    assert events[0]["event_type"] == "evaluation_feedback"
    assert events[0]["payload"]["diagnosis"]["status"] == "needs_review"
    target = write_feedback_events(tmp_path / "feedback.jsonl", events)
    assert target.is_file()


def test_harbor_loader_prefers_normalized_task_spec_metadata(tmp_path: Path) -> None:
    from eval_system.loader import HarborEvalLoader

    trial = tmp_path / "trial"
    (trial / "specs").mkdir(parents=True)
    (trial / "agent").mkdir()
    (trial / "artifacts").mkdir()
    (trial / "verifier").mkdir()
    (trial / "result.json").write_text(json.dumps({
        "task_name": "benchagent/raw-name",
        "task_id": {"path": "/tmp/raw-task"},
        "trial_name": "trial",
        "agent_info": {"name": "pi", "model_info": {}},
        "exception_info": None,
    }))
    (trial / "config.json").write_text(json.dumps({"task": {"path": "/tmp/raw-task"}}))
    (trial / "trial.log").write_text("")
    (trial / "specs" / "task.json").write_text(json.dumps({
        "task_id": "bench/smoke/qa/3@1.0.0",
        "name": "bench/smoke/qa/3",
        "instruction": "gold instruction",
        "task_checksum": "sha256:task",
        "metadata": {"benchmark_id": "smoke", "query_id": "smoke", "subtask_id": "qa", "sample_index": 3},
    }))
    sample = HarborEvalLoader(trial).load_trial(trial)
    assert sample is not None
    assert sample.scenario.task_id == "bench/smoke/qa/3@1.0.0"
    assert sample.scenario.instruction == "gold instruction"
    assert sample.scenario.config["benchmark_id"] == "smoke"
    assert sample.scenario.config["sample_index"] == 3


def test_feedback_flags_verifier_judge_disagreement(tmp_path: Path) -> None:
    from eval_system.integrations.feedback import build_feedback_events
    from eval_system.schema import EvalSample, EvalScenario, AgentRunInfo, Artifacts, RunTrace, Scoring

    sample = EvalSample(
        sample_id="trial-divergence", trial_dir=str(tmp_path),
        scenario=EvalScenario(scenario_id="bench/demo", task_name="bench/demo", task_id="bench/demo/0"),
        agent=AgentRunInfo(name="pi", trial_name="trial-divergence"),
        artifacts=Artifacts(root=str(tmp_path), manifest=[]),
        runtrace=RunTrace(agent_logs_dir=str(tmp_path)),
        scoring=Scoring(rewards={"reward": 0.0}, reward=0.0),
    )
    report = {"cases": [{"case_id": "trial-divergence", "score": 0.75, "skills": {}}]}
    event = build_feedback_events([sample], report)[0]
    diagnosis = event["payload"]["diagnosis"]
    assert diagnosis["status"] == "needs_review"
    assert diagnosis["suspected_owner"] == "benchmark_or_scoring"
    assert "materially disagree" in diagnosis["reason"]


def _feedback_sample(tmp_path: Path, *, context: str, reward: float = 0.0, instruction: str = "do it", artifact: str | None = None):
    from eval_system.schema import EvalSample, EvalScenario, AgentRunInfo, ArtifactFile, Artifacts, RunTrace, Scoring

    manifest = []
    if artifact is not None:
        path = tmp_path / "answer.txt"
        path.write_text(artifact, encoding="utf-8")
        manifest.append(ArtifactFile(
            source="/logs/artifacts/answer.txt", destination="artifacts/answer.txt",
            type="file", status="ok", host_path=str(path), size_bytes=len(artifact),
        ))
    return EvalSample(
        sample_id=f"trial-{context}", trial_dir=str(tmp_path),
        scenario=EvalScenario(
            scenario_id=f"bench/{context}", task_name=f"bench/{context}", task_id=f"bench/{context}/0",
            instruction=instruction, config={"feedback_source_context": context, "benchmark_id": f"bench-{context}"},
        ),
        agent=AgentRunInfo(name="pi", trial_name=f"trial-{context}"),
        artifacts=Artifacts(root=str(tmp_path), manifest=manifest),
        runtrace=RunTrace(agent_logs_dir=str(tmp_path)),
        scoring=Scoring(rewards={"reward": reward}, reward=reward),
    )


def test_smoke_representation_feedback_is_recorded_but_not_learning_eligible(tmp_path: Path) -> None:
    from eval_system.integrations.feedback import build_feedback_events

    sample = _feedback_sample(
        tmp_path, context="smoke", reward=0.0, artifact="0",
        instruction=("Question:\nWhat agrees?\n\nOptions:\n0. Full answer\n1. Other\n\n"
                     "Write only the final answer to /logs/artifacts/answer.txt."),
    )
    report = {"cases": [{"case_id": sample.sample_id, "score": 0.75, "skills": {"judge": {"score": 0.75}}}]}
    event = build_feedback_events([sample], report)[0]
    payload = event["payload"]
    assert payload["classification"]["feedback_type"] == "infrastructure_contract"
    assert payload["source_context"]["kind"] == "smoke"
    assert payload["learning_eligibility"] == {
        "eligible_for_benchmark_design": False,
        "eligible_for_difficulty_learning": False,
        "eligible_for_evaluator_calibration": False,
        "eligible_for_infrastructure_debugging": True,
    }
    assert payload["routing"]["knowledge_source_kind"] == "infrastructure_incident"
    assert payload["routing"]["design_role_visible"] is False


def test_single_high_score_is_not_difficulty_learning(tmp_path: Path) -> None:
    from eval_system.integrations.feedback import build_feedback_events

    sample = _feedback_sample(tmp_path, context="production", reward=1.0)
    report = {"cases": [{"case_id": sample.sample_id, "score": 1.0, "skills": {"judge": {"score": 1.0}}}]}
    payload = build_feedback_events([sample], report)[0]["payload"]
    assert payload["classification"]["feedback_type"] == "unqualified_observation"
    assert payload["learning_eligibility"]["eligible_for_difficulty_learning"] is False
    assert payload["routing"]["design_role_visible"] is False


def test_meta_eval_feedback_routes_to_evaluator_calibration(tmp_path: Path) -> None:
    from eval_system.integrations.feedback import build_feedback_events

    sample = _feedback_sample(tmp_path, context="meta_eval", reward=0.0)
    report = {"cases": [{"case_id": sample.sample_id, "score": 0.8, "skills": {"judge": {"score": 0.8}}}]}
    payload = build_feedback_events([sample], report)[0]["payload"]
    assert payload["classification"]["feedback_type"] == "evaluator"
    assert payload["learning_eligibility"]["eligible_for_evaluator_calibration"] is True
    assert payload["learning_eligibility"]["eligible_for_benchmark_design"] is False
    assert payload["routing"]["knowledge_source_kind"] == "evaluator_calibration_case"


def test_repeated_production_capability_pattern_can_be_design_eligible(tmp_path: Path) -> None:
    from eval_system.integrations.feedback import build_feedback_events

    sample = _feedback_sample(tmp_path, context="production", reward=0.5)
    sample.scenario.config.update({
        "capability_pattern": {"run_count": 4, "task_count": 3, "confidence": 0.85},
        "benchmark_validated": True,
        "evaluator_calibrated": True,
    })
    report = {"cases": [{"case_id": sample.sample_id, "score": 0.5, "skills": {"judge": {"score": 0.5}}}]}
    payload = build_feedback_events([sample], report)[0]["payload"]
    assert payload["classification"]["feedback_type"] == "capability_pattern"
    assert payload["learning_eligibility"]["eligible_for_benchmark_design"] is True
    assert payload["routing"]["knowledge_source_kind"] == "capability_failure_pattern"
    assert payload["routing"]["design_role_visible"] is True


def test_repeated_trusted_production_scores_can_be_difficulty_feedback(tmp_path: Path) -> None:
    from eval_system.integrations.feedback import build_feedback_events

    sample = _feedback_sample(tmp_path, context="production", reward=1.0)
    sample.scenario.config.update({
        "feedback_evidence": {"run_count": 5, "task_count": 3, "confidence": 0.9},
        "benchmark_validated": True,
        "evaluator_calibrated": True,
    })
    report = {"cases": [{"case_id": sample.sample_id, "score": 1.0, "skills": {"judge": {"score": 1.0}}}]}
    payload = build_feedback_events([sample], report)[0]["payload"]
    assert payload["classification"]["feedback_type"] == "difficulty"
    assert payload["learning_eligibility"]["eligible_for_difficulty_learning"] is True
    assert payload["learning_eligibility"]["eligible_for_benchmark_design"] is True
    assert payload["routing"]["knowledge_source_kind"] == "difficulty_observation"


def test_replay_fixture_seal_and_validate(tmp_path: Path) -> None:
    from eval_system.replay import FrozenReplayFixture, FrozenRef, seal_fixture, load_fixture
    artifact = tmp_path / "artifact.txt"; artifact.write_text("frozen", encoding="utf-8")
    draft = tmp_path / "draft.json"
    draft.write_text(FrozenReplayFixture(
        fixture_id="fx", purpose="test", refs=[FrozenRef(name="artifact", kind="artifact", path=str(artifact))]
    ).model_dump_json(indent=2), encoding="utf-8")
    sealed = tmp_path / "sealed.json"; seal_fixture(draft, sealed)
    loaded = load_fixture(sealed)
    assert loaded.status == "sealed" and loaded.fixture_digest and loaded.refs[0].sha256
    artifact.write_text("changed", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="digest mismatch"):
        load_fixture(sealed)


def test_fast_experiment_requires_expensive_stage_guards(tmp_path: Path) -> None:
    from eval_system.replay import validate_experiment
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps({
        "experiment_id":"EXP-1", "hypothesis":"classifier improves", "changed_component":"feedback",
        "changed_variable":{"baseline":"a","candidate":"b"}, "frozen_components":["trial"],
        "fixture_set":[{"fixture_id":"fx","fixture_digest":"sha256:x"}], "loop_tier":"fast",
        "forbidden_invocations":{"harbor":True,"pi":True}
    }), encoding="utf-8")
    assert validate_experiment(path).experiment_id == "EXP-1"


def test_fixture_set_and_experiment_bind_fixture_digest(tmp_path: Path) -> None:
    from eval_system.replay import (
        FrozenReplayFixture, FrozenRef, seal_fixture, build_fixture_set,
        validate_fixture_set, validate_experiment,
    )
    artifact = tmp_path / "artifact.txt"; artifact.write_text("frozen", encoding="utf-8")
    draft = tmp_path / "draft.json"
    draft.write_text(FrozenReplayFixture(
        fixture_id="fx-set", purpose="set-test",
        refs=[FrozenRef(name="artifact", kind="artifact", path=str(artifact))],
    ).model_dump_json(indent=2), encoding="utf-8")
    canonical = tmp_path / "canonical"; canonical.mkdir()
    fixture_path = canonical / "fx-set.fixture.json"
    fixture = seal_fixture(draft, fixture_path)
    set_path = tmp_path / "fixture-set.json"
    built = build_fixture_set(canonical, set_path, missing_categories=["too_easy"])
    assert validate_fixture_set(set_path).set_digest == built.set_digest
    experiment = tmp_path / "experiment.json"
    experiment.write_text(json.dumps({
        "experiment_id":"EXP-bind", "hypothesis":"binding is checked",
        "changed_component":"feedback", "changed_variable":{"baseline":"a","candidate":"b"},
        "frozen_components":["trial"], "loop_tier":"fast",
        "forbidden_invocations":{"harbor":True,"pi":True},
        "fixture_set":[{"fixture_id":fixture.fixture_id,"fixture_digest":fixture.fixture_digest,"path":str(fixture_path)}],
    }), encoding="utf-8")
    assert validate_experiment(experiment).experiment_id == "EXP-bind"
    payload = json.loads(experiment.read_text()); payload["fixture_set"][0]["fixture_digest"] = "sha256:wrong"
    experiment.write_text(json.dumps(payload), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="fixture binding mismatch"):
        validate_experiment(experiment)
