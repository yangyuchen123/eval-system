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
