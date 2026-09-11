"""Project AgentEval results back into Benchmark Forge's existing event shape.

This is intentionally a thin bridge: it does not invent a second diagnosis
model or database.  The output is a JSONL stream of the existing
``BenchmarkEvent`` fields (event_id/event_type/role/message/payload/timestamp),
which Benchmark Forge can index in its existing SQLite knowledge store.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from eval_system.schema import AgentRunInfo, ArtifactFile, Artifacts, EvalSample, EvalScenario, RunTrace, Scoring


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _score(row: dict[str, Any]) -> float | None:
    value = row.get("score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _diagnosis(sample: EvalSample, score: float | None, *, low_threshold: float, high_threshold: float) -> dict[str, Any]:
    runtime_status = str((sample.scoring.verifier_result or {}).get("status") or "")
    if sample.scoring.exception or runtime_status in {"failed", "cancelled", "canceled"}:
        return {
            "status": "needs_review",
            "suspected_owner": "infrastructure",
            "confidence": 0.55,
            "reason": "runtime reported an exception or non-completed status; separate runtime failure from benchmark/Judge failure",
            "recommended_action": "inspect runtime result, trace coverage, and artifact collection before changing benchmark or rubric",
        }
    # A large verifier/Judge disagreement is itself a review signal even when
    # the aggregate Judge score sits inside the ordinary band.  Keep ownership
    # conservative: the evidence may indicate verifier strictness, rubric
    # mismatch, benchmark output ambiguity, or Judge error.
    reward = sample.scoring.reward
    if reward is not None and score is not None and abs(float(reward) - score) >= 0.5:
        return {
            "status": "needs_review",
            "suspected_owner": "benchmark_or_scoring",
            "confidence": 0.65,
            "reason": f"verifier reward ({float(reward):.3g}) and AgentEval score ({score:.3g}) materially disagree",
            "recommended_action": "inspect per-rubric subscores, artifact representation, verifier acceptance rules, and Judge evidence before changing task difficulty",
        }
    if score is not None and score < low_threshold:
        return {
            "status": "needs_review",
            "suspected_owner": "unknown",
            "confidence": 0.4,
            "reason": "low AgentEval score; distinguish candidate failure, benchmark difficulty, rubric/scoring, and runtime evidence issues",
            "recommended_action": "human review of artifact, runtrace evidence chain, rubric, and task difficulty",
        }
    if score is not None and score > high_threshold:
        return {
            "status": "needs_review",
            "suspected_owner": "unknown",
            "confidence": 0.4,
            "reason": "high AgentEval score; check genuine capability, rubric looseness, benchmark loopholes, and task simplicity",
            "recommended_action": "human review of artifact, rubric coverage, exploitability, and task difficulty",
        }
    return {
        "status": "no_anomaly_detected",
        "suspected_owner": "none",
        "confidence": 0.3,
        "reason": "score is inside the configured review band and runtime is terminal",
        "recommended_action": "retain as routine observation; do not write a difficulty correction without additional evidence",
    }



_SOURCE_CONTEXT_ALIASES = {
    "smoke": "smoke", "smoke_test": "smoke", "validation": "validation",
    "synthetic": "validation", "test": "validation", "debug": "debug",
    "demo": "validation", "meta_eval": "meta_eval", "meta-eval": "meta_eval",
    "calibration": "meta_eval", "production": "production", "formal": "production",
}

_ROUTING = {
    "infrastructure": ("infrastructure_incident", ["eval-system", "harbor-adapter", "engineering"]),
    "infrastructure_contract": ("infrastructure_incident", ["eval-system", "task-materializer", "verifier-adapter", "engineering"]),
    "evaluator": ("evaluator_calibration_case", ["agent-eval", "rubric-calibration", "evaluator-meta-eval"]),
    "benchmark_validity": ("benchmark_design_lesson", ["benchagent", "benchmark-validity"]),
    "difficulty": ("difficulty_observation", ["benchagent", "difficulty-learning"]),
    "capability_pattern": ("capability_failure_pattern", ["benchagent", "capability-gap-learning"]),
    "routine_observation": ("feedback_observation", ["evaluation-observability"]),
    "unqualified_observation": ("feedback_observation", ["human-review"]),
}


def _metadata(sample: EvalSample) -> dict[str, Any]:
    config = sample.scenario.config if isinstance(sample.scenario.config, dict) else {}
    nested = config.get("benchmark_metadata")
    merged = dict(nested) if isinstance(nested, dict) else {}
    merged.update({key: value for key, value in config.items() if key != "benchmark_metadata"})
    return merged


def _source_context(sample: EvalSample) -> dict[str, Any]:
    metadata = _metadata(sample)
    explicit = next((metadata.get(key) for key in (
        "feedback_source_context", "source_context", "run_purpose",
        "benchmark_stage", "evaluation_mode",
    ) if metadata.get(key) is not None), None)
    if explicit is not None:
        raw = str(explicit).strip().lower().replace(" ", "_")
        return {"kind": _SOURCE_CONTEXT_ALIASES.get(raw, raw), "explicit": True, "signals": [str(explicit)]}
    values = [
        metadata.get("benchmark_id"), metadata.get("query_id"), metadata.get("source"),
        sample.scenario.task_id, sample.scenario.task_name, sample.agent.trial_name,
    ]
    text = " ".join(str(value).lower() for value in values if value is not None)
    patterns = [
        ("smoke", r"(?:^|[^a-z])smoke(?:[^a-z]|$)"),
        ("debug", r"(?:^|[^a-z])debug(?:[^a-z]|$)"),
        ("meta_eval", r"meta[-_ ]?eval|calibration"),
        ("validation", r"(?:^|[^a-z])(validation|synthetic|demo|test)(?:[^a-z]|$)"),
        ("production", r"(?:^|[^a-z])(production|formal)(?:[^a-z]|$)"),
    ]
    for kind, pattern in patterns:
        if re.search(pattern, text):
            return {"kind": kind, "explicit": False, "signals": [part for part in values if part is not None]}
    return {"kind": "unknown", "explicit": False, "signals": []}


def _artifact_preview(sample: EvalSample, *, max_bytes: int = 512) -> str | None:
    for artifact in sample.artifacts.files():
        if artifact.type != "file":
            continue
        try:
            path = Path(artifact.host_path)
            if path.is_file() and path.stat().st_size <= max_bytes:
                return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _representation_mismatch_evidence(sample: EvalSample, score: float | None) -> dict[str, Any]:
    instruction = sample.scenario.instruction or ""
    artifact = _artifact_preview(sample)
    reward = sample.scoring.reward
    disagreement = reward is not None and score is not None and abs(float(reward) - score) >= 0.5
    option_task = bool(re.search(r"(?im)^options?:\s*$", instruction))
    numeric_artifact = bool(artifact and re.fullmatch(r"\d+", artifact))
    output_ambiguous = option_task and "option text" not in instruction.lower() and "option number" not in instruction.lower() and "option index" not in instruction.lower()
    return {
        "detected": bool(disagreement and option_task and numeric_artifact and output_ambiguous),
        "verifier_judge_disagreement": disagreement,
        "multiple_choice_instruction": option_task,
        "numeric_artifact": numeric_artifact,
        "instruction_representation_ambiguous": output_ambiguous,
        "artifact_preview": artifact,
    }


def _pattern_counts(metadata: dict[str, Any]) -> tuple[int, int, float]:
    pattern = metadata.get("capability_pattern")
    if not isinstance(pattern, dict):
        pattern = metadata.get("feedback_pattern") if isinstance(metadata.get("feedback_pattern"), dict) else None
    if not isinstance(pattern, dict):
        pattern = metadata.get("feedback_evidence") if isinstance(metadata.get("feedback_evidence"), dict) else {}
    try:
        runs = int(pattern.get("run_count", 0))
    except (TypeError, ValueError):
        runs = 0
    try:
        tasks = int(pattern.get("task_count", 0))
    except (TypeError, ValueError):
        tasks = 0
    try:
        confidence = float(pattern.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return runs, tasks, confidence


def _qualification(
    sample: EvalSample, row: dict[str, Any], diagnosis: dict[str, Any], score: float | None,
) -> dict[str, Any]:
    metadata = _metadata(sample)
    source_context = _source_context(sample)
    runtime_status = str((sample.scoring.verifier_result or {}).get("status") or ("failed" if sample.scoring.exception else "completed"))
    run_valid = not sample.scoring.exception and runtime_status not in {"failed", "cancelled", "canceled"}
    evaluator_calibrated = metadata.get("evaluator_calibrated") is True
    benchmark_validated = metadata.get("benchmark_validated") is True
    evaluator_trustworthy = score is not None and bool(row.get("skills") or row.get("metrics")) and evaluator_calibrated
    representation = _representation_mismatch_evidence(sample, score)
    benchmark_valid: bool | None = False if representation["detected"] else (True if benchmark_validated else None)
    runs, tasks, pattern_confidence = _pattern_counts(metadata)
    capability_pattern_declared = isinstance(metadata.get("capability_pattern"), dict) or isinstance(metadata.get("feedback_pattern"), dict)
    repeated_pattern_observed = capability_pattern_declared and runs >= 2 and tasks >= 2 and pattern_confidence >= 0.7
    repeated_pattern = repeated_pattern_observed and run_valid and evaluator_trustworthy and benchmark_valid is True
    difficulty_evidence = runs >= 3 and tasks >= 2 and pattern_confidence >= 0.7 and run_valid and evaluator_trustworthy and benchmark_valid is True
    source_learning_allowed = source_context["kind"] == "production"

    promotion = metadata.get("feedback_promotion") if isinstance(metadata.get("feedback_promotion"), dict) else {}
    promoted = promotion.get("approved") is True
    promoted_type = str(promotion.get("feedback_type") or "")

    if not run_valid:
        feedback_type = "infrastructure"
        reason = "run/runtime is not trustworthy; route to infrastructure debugging before any learning"
    elif source_context["kind"] == "meta_eval":
        feedback_type = "evaluator"
        reason = "source context is evaluator calibration/meta-evaluation"
    elif representation["detected"]:
        if source_learning_allowed or (promoted and promoted_type == "benchmark_validity"):
            feedback_type = "benchmark_validity"
            reason = "production benchmark has evidence-backed instruction/artifact/verifier representation ambiguity"
        else:
            feedback_type = "infrastructure_contract"
            reason = "representation/verifier contract defect occurred in smoke/validation/debug context and is engineering evidence, not benchmark-distribution learning"
    elif promoted and promoted_type in {"benchmark_validity", "difficulty", "capability_pattern", "evaluator"}:
        feedback_type = promoted_type
        reason = "explicitly promoted by feedback metadata after review"
    elif repeated_pattern:
        feedback_type = "capability_pattern"
        reason = "repeated high-confidence pattern spans multiple tasks and runs"
    elif diagnosis.get("suspected_owner") in {"scoring", "evaluator", "judge"}:
        feedback_type = "evaluator"
        reason = "diagnosis attributes the anomaly to evaluator/scoring behavior"
    elif score is not None and (score < 0.4 or score > 0.9):
        feedback_type = "difficulty" if difficulty_evidence and source_learning_allowed and evaluator_trustworthy and benchmark_valid is not False else "unqualified_observation"
        reason = "difficulty requires repeated evidence plus valid runtime, evaluator and benchmark" if feedback_type == "difficulty" else "single-run score anomaly is recorded but is not sufficient difficulty evidence"
    elif diagnosis.get("status") == "no_anomaly_detected":
        feedback_type = "routine_observation"
        reason = "no anomaly detected; retain for observability only"
    else:
        feedback_type = "unqualified_observation"
        reason = "evidence does not yet isolate infrastructure, evaluator, benchmark validity, difficulty or repeated capability ownership"

    design_eligible = feedback_type in {"benchmark_validity", "difficulty", "capability_pattern"} and (source_learning_allowed or promoted)
    difficulty_eligible = feedback_type == "difficulty" and difficulty_evidence and (source_learning_allowed or promoted)
    evaluator_eligible = feedback_type == "evaluator"
    infrastructure_eligible = feedback_type in {"infrastructure", "infrastructure_contract"}
    source_kind, consumers = _ROUTING[feedback_type]
    return {
        "source_context": source_context,
        "qualification": {
            "status": "qualified" if any((design_eligible, difficulty_eligible, evaluator_eligible, infrastructure_eligible)) else "record_only",
            "run_valid": run_valid,
            "runtime_status": runtime_status,
            "evaluator_trustworthy": evaluator_trustworthy,
            "evaluator_calibrated": evaluator_calibrated,
            "benchmark_valid": benchmark_valid,
            "benchmark_validated": benchmark_validated,
            "source_learning_allowed": source_learning_allowed,
            "difficulty_evidence_sufficient": difficulty_evidence,
            "capability_pattern_evidence_sufficient": repeated_pattern,
            "promotion": promotion or None,
            "reason": reason,
        },
        "classification": {
            "feedback_type": feedback_type,
            "suspected_owner": diagnosis.get("suspected_owner"),
            "confidence": diagnosis.get("confidence"),
            "representation_mismatch_evidence": representation,
        },
        "learning_eligibility": {
            "eligible_for_benchmark_design": design_eligible,
            "eligible_for_difficulty_learning": difficulty_eligible,
            "eligible_for_evaluator_calibration": evaluator_eligible,
            "eligible_for_infrastructure_debugging": infrastructure_eligible,
        },
        "routing": {
            "knowledge_domain": source_kind,
            "knowledge_source_kind": source_kind,
            "target_components": consumers,
            "design_role_visible": design_eligible,
            "record": True,
        },
    }


def load_runtime_samples(root: str | Path) -> list[EvalSample]:
    """Load Harbor v1 trials or eval-system v2 TrialResult directories."""
    from eval_system.loader import HarborEvalLoader

    samples = list(HarborEvalLoader(root).iter_samples())
    if samples:
        return samples
    from eval_system.contract.trial import load_spec, read_trial_result
    base = Path(root).expanduser()
    trial_dirs = sorted({path.parent for path in base.rglob("trial_result.json")}) if base.is_dir() else []
    result: list[EvalSample] = []
    for trial_dir in trial_dirs:
        trial = read_trial_result(trial_dir)
        task = load_spec(trial_dir, "task")
        agent = load_spec(trial_dir, "agent")
        if task is None or agent is None:
            continue
        entries = [ArtifactFile(source=item.source, destination=item.destination, type=item.type, status=item.status, service=item.service, host_path=item.host_path, size_bytes=item.size_bytes) for item in trial.artifacts.manifest]
        result.append(EvalSample(
            sample_id=trial.trial_id, trial_dir=str(trial_dir),
            scenario=EvalScenario(scenario_id=task.task_id, task_name=task.name, task_id=task.task_id, instruction=task.instruction, config=task.metadata),
            agent=AgentRunInfo(name=agent.name, version=agent.version, model=agent.model, provider=agent.provider, trial_name=trial.trial_id),
            artifacts=Artifacts(root=str(trial_dir / "artifacts"), manifest=entries),
            runtrace=RunTrace(agent_logs_dir=str(trial_dir / "agent"), trajectory=None, agent_context=trial.runtrace.agent_context),
            scoring=Scoring(rewards=trial.scoring.rewards, reward=trial.scoring.reward, verifier_result=trial.scoring.verifier_result, exception=trial.scoring.exception, timings=trial.scoring.timings, step_results=trial.scoring.step_results),
        ))
    return result

def build_feedback_events(
    samples: Iterable[EvalSample],
    report: dict[str, Any],
    *,
    report_path: str | Path | None = None,
    low_threshold: float = 0.4,
    high_threshold: float = 0.9,
    include_normal: bool = True,
    backend: str = "harbor",
) -> list[dict[str, Any]]:
    """Build existing BenchmarkEvent-shaped feedback records from a report."""
    report_by_case = {str(row.get("case_id")): row for row in report.get("cases", []) if isinstance(row, dict)}
    events: list[dict[str, Any]] = []
    for sample in samples:
        row = report_by_case.get(sample.sample_id)
        if row is None:
            continue
        score = _score(row)
        diagnosis = _diagnosis(sample, score, low_threshold=low_threshold, high_threshold=high_threshold)
        if not include_normal and diagnosis["status"] == "no_anomaly_detected":
            continue
        skills = row.get("skills") or {}
        qualified = _qualification(sample, row, diagnosis, score)
        payload = {
            "feedback_kind": "iteration_feedback",
            "feedback_schema_version": "evaluation_feedback.v2",
            "benchmark_id": sample.scenario.config.get("benchmark_id") or sample.scenario.config.get("query_id"),
            "query_id": sample.scenario.config.get("query_id"),
            "item_id": sample.scenario.config.get("item_id"),
            "subtask_id": sample.scenario.config.get("subtask_id"),
            "sample_index": sample.scenario.config.get("sample_index"),
            "task_id": sample.scenario.task_id,
            "case_id": sample.sample_id,
            "agent": sample.agent.model_dump(mode="json"),
            "runtime": {
                "backend": backend,
                "trial_dir": sample.trial_dir,
                "status": (sample.scoring.verifier_result or {}).get("status") or ("failed" if sample.scoring.exception else "completed"),
                "reward": sample.scoring.reward,
                "exception": sample.scoring.exception,
            },
            "evaluation": {
                "score": score,
                "skills": skills,
                "report_path": str(report_path) if report_path else None,
            },
            "evidence": {
                "runtrace": sample.runtrace.trajectory_path or str(Path(sample.trial_dir) / "agent" / "trajectory.json"),
                "artifacts": [artifact.model_dump(mode="json") for artifact in sample.artifacts.manifest],
                "verifier": str(Path(sample.trial_dir) / "verifier"),
            },
            "diagnosis": diagnosis,
            **qualified,
        }
        event_id = "evaluation_feedback_" + _hash({"case_id": sample.sample_id, "payload": payload})[:24]
        events.append({
            "event_id": event_id,
            "event_type": "evaluation_feedback",
            "role": "agent-eval",
            "message": f"AgentEval score={score!r} for {sample.scenario.task_id}; diagnosis={diagnosis['status']}",
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    return events


def write_feedback_events(path: str | Path, events: Iterable[dict[str, Any]]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = list(events)
    target.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return target
