"""Adapters from benchagent ``evaluation.json`` to eval-system contracts.

The adapter deliberately keeps benchmark production independent from Harbor:
benchagent emits data, while this module turns each sample into a TaskSpec and,
optionally, a Harbor-compatible task directory.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from eval_system.contract.specs import ContentRef, TaskSpec, content_checksum


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "task"


def _instruction(sample: dict[str, Any]) -> str:
    parts = []
    if sample.get("context"):
        parts.append(f"Context:\n{sample['context']}")
    parts.append(f"Question:\n{sample.get('question', '')}")
    options = sample.get("options") or []
    if options:
        parts.append("Options:\n" + "\n".join(f"{i}. {v}" for i, v in enumerate(options)))
    parts.append(
        "Write only the final answer to /logs/artifacts/answer.txt. "
        "Do not include analysis in that file."
    )
    return "\n\n".join(parts)


def benchmark_to_task_specs(path: str | Path, *, task_root: str | Path | None = None) -> list[TaskSpec]:
    """Convert benchagent samples into immutable execution input contracts."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    query = payload.get("query") or payload.get("spec", {}).get("user_query") or {}
    samples = payload.get("samples") or []
    specs: list[TaskSpec] = []
    for index, sample in enumerate(samples):
        subtask = _safe(str(sample.get("subtask_id", "default")))
        sample_index = sample.get("sample_index", index)
        version = "1.0.0"
        task_id = f"bench/{_safe(str(query.get('id', 'query')))}/{subtask}/{sample_index}@{version}"
        instruction = _instruction(sample)
        answer = sample.get("answer")
        metadata = {
            "source": "benchagent",
            "query_id": query.get("id"),
            "subtask_id": sample.get("subtask_id"),
            "sample_index": sample_index,
            "answer": answer,
            "answer_type": sample.get("answer_type", "multiple_choice"),
            "options": sample.get("options") or [],
            "context": sample.get("context"),
            "media": sample.get("media") or [],
            "artifact_path": "/logs/artifacts/answer.txt",
        }
        task_name = task_id.split("@", 1)[0]
        content_ref = None
        if task_root is not None:
            content_ref = ContentRef(type="path", path=str(Path(task_root) / _safe(task_name)))
        specs.append(TaskSpec(
            task_id=task_id,
            name=task_name,
            version=version,
            instruction=instruction,
            instruction_checksum=content_checksum(instruction),
            source=str(sample.get("dataset_id") or "benchagent"),
            content_ref=content_ref,
            metadata=metadata,
            output_schema={
                "type": "text-artifact",
                "path": "/logs/artifacts/answer.txt",
                "answer_type": metadata["answer_type"],
            },
            task_checksum=content_checksum(json.dumps(
                {"task_id": task_id, "instruction": instruction, "metadata": metadata},
                ensure_ascii=False, sort_keys=True,
            )),
        ))
    return specs


def materialize_benchmark_tasks(
    evaluation_json: str | Path,
    output_root: str | Path,
    *,
    clean: bool = False,
) -> list[Path]:
    """Write one Harbor task directory per benchmark sample.

    The expected answer lives in ``tests/expected.json`` and is consumed only by
    the verifier. This is intended for generated benchmark staging directories;
    production deployments should mount the verifier side separately if the
    task answer must remain hidden from the agent.
    """
    payload = json.loads(Path(evaluation_json).read_text(encoding="utf-8"))
    specs = benchmark_to_task_specs(evaluation_json, task_root=output_root)
    samples = payload.get("samples") or []
    root = Path(output_root)
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for spec, sample in zip(specs, samples, strict=True):
        task_dir = root / _safe(spec.name)
        (task_dir / "tests").mkdir(parents=True, exist_ok=True)
        (task_dir / "environment").mkdir(parents=True, exist_ok=True)
        (task_dir / "solution").mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text(spec.instruction + "\n", encoding="utf-8")
        (task_dir / "tests" / "expected.json").write_text(
            json.dumps({"answer": sample.get("answer")}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (task_dir / "tests" / "test.sh").write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "actual=/logs/artifacts/answer.txt\n"
            "expected=/tests/expected.json\n"
            "test -f \"$actual\" || { echo 0 > /logs/verifier/reward.txt; exit 1; }\n"
            "ACTUAL=\"$actual\" EXPECTED=\"$expected\" python3 - <<'PY'\n"
            "import json, os, pathlib\n"
            "actual=pathlib.Path(os.environ['ACTUAL']).read_text().strip()\n"
            "expected=json.loads(pathlib.Path(os.environ['EXPECTED']).read_text())['answer']\n"
            "ok=actual == str(expected).strip()\n"
            "pathlib.Path('/logs/verifier/reward.txt').write_text('1' if ok else '0')\n"
            "raise SystemExit(0 if ok else 1)\n"
            "PY\n",
            encoding="utf-8",
        )
        (task_dir / "tests" / "test.sh").chmod(0o755)
        (task_dir / "environment" / "Dockerfile").write_text(
            "FROM python:3.12-slim\n", encoding="utf-8"
        )
        (task_dir / "task.toml").write_text(
            "schema_version = \"1.4\"\n\n"
            f"[task]\nname = \"{spec.name}\"\nversion = \"{spec.version}\"\n"
            "authors = []\nkeywords = [\"benchagent\"]\n\n"
            "[metadata]\nsource = \"benchagent\"\n"
            f"query_id = \"{spec.metadata.get('query_id', '')}\"\n\n"
            "[verifier]\ntimeout_sec = 120.0\n\n"
            "[agent]\ntimeout_sec = 300.0\n\n"
            "[environment]\nbuild_timeout_sec = 600.0\ncpus = 1\nmemory_mb = 2048\n"
            "storage_mb = 10240\ngpus = 0\nmcp_servers = []\n",
            encoding="utf-8",
        )
        paths.append(task_dir)
    return paths
