"""Export TrialResult artifacts into AgentEval's neutral input format."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval_system.contract.harbor_backend import build_trial_result
from eval_system.contract.trial import read_trial_result, load_spec


def _read_artifact(result: Any, trial_dir: Path) -> tuple[str, str]:
    preferred = ["logs/artifacts/report.md", "logs/artifacts/answer.txt", "output.txt"]
    for name in preferred:
        for entry in result.artifacts.manifest:
            destination = entry.destination.lstrip("/").removeprefix("artifacts/")
            if destination == name or destination.endswith("/" + name):
                path = Path(entry.host_path)
                if path.is_file():
                    return name, path.read_text(encoding="utf-8", errors="replace")
    for entry in result.artifacts.manifest:
        path = Path(entry.host_path)
        if entry.status == "ok" and entry.type == "file" and path.is_file():
            return entry.destination, path.read_text(encoding="utf-8", errors="replace")
    return "", ""


def trial_results_to_agent_eval(
    trials_root: str | Path,
    output_dir: str | Path,
    *,
    include_failed: bool = True,
) -> dict[str, Path]:
    """Create ``cases.json`` and ``outputs.json`` consumable by AgentEval CLI."""
    root = Path(trials_root)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    # Harbor's native output is result.json. trial_result.json is the
    # normalized eval-system contract and may not exist until the adapter
    # materializes it. Discover both forms and deduplicate directories.
    trial_dirs = sorted({
        p.parent for p in root.rglob("trial_result.json")
    } | {
        p.parent for p in root.rglob("result.json")
    })
    for trial_dir in trial_dirs:
        if (trial_dir / "trial_result.json").is_file():
            result = read_trial_result(trial_dir)
        else:
            result = build_trial_result(trial_dir)
        if not include_failed and result.status != "finished":
            continue
        task = load_spec(trial_dir, "task")
        artifact_path, output = _read_artifact(result, trial_dir)
        metadata = dict(task.metadata) if task is not None else {}
        case_id = result.trial_id
        cases.append({
            "case_id": case_id,
            "task": task.instruction if task is not None else case_id,
            "expected": {
                "answer": metadata.get("answer"),
                "artifact_path": artifact_path,
                "reward": result.scoring.reward,
                "status": result.status,
            },
            "context": {
                "trial_dir": str(trial_dir),
                "runtrace": result.runtrace.model_dump(mode="json"),
                "scoring": result.scoring.model_dump(mode="json"),
                "artifacts": [a.model_dump(mode="json") for a in result.artifacts.manifest],
            },
        })
        outputs[case_id] = output
    cases_path = out / "cases.json"
    outputs_path = out / "outputs.json"
    cases_path.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outputs_path.write_text(json.dumps(outputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"cases": cases_path, "outputs": outputs_path}
