"""Thin replay manifests and stage entrypoints over existing closed-loop modules."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA = "closed_loop.replay_fixture.v1"
STAGES = ("agent-eval", "feedback", "kb", "design", "ir")


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def path_digest(path: str | Path) -> str:
    path = Path(path).expanduser().resolve()
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.read_bytes())
    elif path.is_dir():
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            h.update(item.relative_to(path).as_posix().encode())
            h.update(b"\0")
            h.update(item.read_bytes())
            h.update(b"\0")
    else:
        raise FileNotFoundError(path)
    return "sha256:" + h.hexdigest()


def kb_logical_digest(path: str | Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT env_id, source_path, source_kind, chunk_id, content_hash "
                "FROM documents ORDER BY env_id, source_path, source_kind, chunk_id"
            ).fetchall()
    except sqlite3.Error:
        return None
    return canonical_digest(rows)


class FrozenRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str
    path: str
    sha256: str | None = None
    required: bool = True
    logical_digest: str | None = None


class RepoBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str
    commit: str | None = None
    dirty: bool | None = None
    dirty_state_digest: str | None = None


class FrozenReplayFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["closed_loop.replay_fixture.v1"] = SCHEMA
    fixture_id: str
    fixture_version: str = "1"
    status: Literal["draft", "sealed", "deprecated"] = "draft"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    purpose: str
    tags: list[str] = Field(default_factory=list)
    learning_eligibility: dict[str, bool] = Field(default_factory=dict)
    allowed_stages: list[str] = Field(default_factory=lambda: list(STAGES))
    forbidden_claims: list[str] = Field(default_factory=list)
    refs: list[FrozenRef]
    repos: list[RepoBinding] = Field(default_factory=list)
    bindings: dict[str, Any] = Field(default_factory=dict)
    input_digest: str | None = None
    output_digest: str | None = None
    fixture_digest: str | None = None

    @model_validator(mode="after")
    def validate_stages(self) -> "FrozenReplayFixture":
        unknown = set(self.allowed_stages) - set(STAGES)
        if unknown:
            raise ValueError(f"unknown replay stages: {sorted(unknown)}")
        return self

    def ref(self, name: str, *, required: bool = True) -> FrozenRef | None:
        found = next((item for item in self.refs if item.name == name), None)
        if found is None and required:
            raise KeyError(f"fixture {self.fixture_id} has no ref {name!r}")
        return found


def _repo_binding(name: str, path: str) -> RepoBinding:
    root = Path(path).expanduser().resolve()
    try:
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        status = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain=v1"], text=True)
        entries = []
        for line in status.splitlines():
            rel = line[3:]
            file = root / rel
            entries.append({"status": line[:2], "path": rel, "digest": path_digest(file) if file.exists() else None})
        return RepoBinding(name=name, path=str(root), commit=commit, dirty=bool(entries), dirty_state_digest=canonical_digest(entries))
    except (OSError, subprocess.SubprocessError):
        return RepoBinding(name=name, path=str(root))


def seal_fixture(draft: str | Path, output: str | Path) -> FrozenReplayFixture:
    source = Path(draft).expanduser().resolve()
    fixture = FrozenReplayFixture.model_validate_json(source.read_text(encoding="utf-8"))
    base = source.parent
    for ref in fixture.refs:
        target = Path(ref.path).expanduser()
        if not target.is_absolute():
            target = (base / target).resolve()
        ref.path = str(target)
        if not target.exists():
            if ref.required:
                raise FileNotFoundError(target)
            continue
        ref.sha256 = path_digest(target)
        if ref.kind == "kb_snapshot":
            ref.logical_digest = kb_logical_digest(target)
    fixture.repos = [_repo_binding(repo.name, repo.path) for repo in fixture.repos]
    fixture.status = "sealed"
    payload = fixture.model_dump(mode="json", exclude={"fixture_digest"})
    inputs = [r.model_dump(mode="json") for r in fixture.refs if r.kind in {"benchmark", "task_spec", "ir", "trial", "trace", "artifact", "rubric", "config", "kb_snapshot", "retrieval"}]
    outputs = [r.model_dump(mode="json") for r in fixture.refs if r.kind in {"evaluation", "evidence", "feedback", "design"}]
    fixture.input_digest = canonical_digest(inputs)
    fixture.output_digest = canonical_digest(outputs)
    fixture.fixture_digest = canonical_digest(payload | {"input_digest": fixture.input_digest, "output_digest": fixture.output_digest})
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fixture.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return fixture


def load_fixture(path: str | Path, *, verify: bool = True) -> FrozenReplayFixture:
    fixture = FrozenReplayFixture.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if verify:
        for ref in fixture.refs:
            target = Path(ref.path)
            if not target.exists():
                if ref.required:
                    raise FileNotFoundError(target)
                continue
            actual = path_digest(target)
            if actual != ref.sha256:
                raise ValueError(f"digest mismatch for {ref.name}: expected {ref.sha256}, got {actual}")
        expected = fixture.fixture_digest
        clone = fixture.model_copy(update={"fixture_digest": None})
        actual = canonical_digest(clone.model_dump(mode="json", exclude={"fixture_digest"}))
        # fixture digest was sealed after input/output digests were filled.
        if expected != actual:
            raise ValueError(f"fixture digest mismatch: expected {expected}, got {actual}")
    return fixture


def write_stage_manifest(fixture: FrozenReplayFixture, stage: str, output: str | Path, *, inputs: dict[str, Any], results: dict[str, Any]) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "closed_loop.stage_replay.v1", "stage": stage,
        "fixture_id": fixture.fixture_id, "fixture_digest": fixture.fixture_digest,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "harbor_invoked": False, "pi_invoked": False,
        "inputs": inputs, "input_digest": canonical_digest(inputs),
        "results": results, "result_digest": canonical_digest(results),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def copy_kb_snapshot(source: str | Path, output: str | Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target

class ReplayExperiment(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["closed_loop.replay_experiment.v1"] = "closed_loop.replay_experiment.v1"
    experiment_id: str
    hypothesis: str
    changed_component: str
    changed_variable: dict[str, Any]
    frozen_components: list[str]
    fixture_set: list[dict[str, str]]
    loop_tier: Literal["fast", "medium", "full"]
    forbidden_invocations: dict[str, bool] = Field(default_factory=dict)
    promotion: dict[str, Any] = Field(default_factory=dict)


def validate_experiment(path: str | Path) -> ReplayExperiment:
    exp = ReplayExperiment.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if exp.loop_tier == "fast":
        for expensive in ("harbor", "pi"):
            if exp.forbidden_invocations.get(expensive) is not True:
                raise ValueError(f"fast experiment must forbid {expensive}")
    if not exp.fixture_set:
        raise ValueError("experiment requires at least one frozen fixture")
    base = Path(path).resolve().parent
    for item in exp.fixture_set:
        fixture_path = item.get("path")
        if not fixture_path:
            continue
        resolved = Path(fixture_path).expanduser()
        if not resolved.is_absolute(): resolved = (base / resolved).resolve()
        fixture = load_fixture(resolved)
        if fixture.fixture_id != item.get("fixture_id") or fixture.fixture_digest != item.get("fixture_digest"):
            raise ValueError(f"experiment fixture binding mismatch: {item.get('fixture_id')}")
    return exp


def promotion_decision(exp: ReplayExperiment, results: dict[str, Any]) -> dict[str, Any]:
    fast_passed = bool(results.get("fast_passed"))
    medium_trigger = bool(results.get("benchmark_changed") or results.get("environment_changed") or results.get("agent_behavior_required") or results.get("empirical_difficulty_required"))
    if not fast_passed:
        status = "fast_failed"
    elif medium_trigger and not results.get("medium_passed"):
        status = "medium_required"
    elif results.get("full_requested") and not results.get("full_passed"):
        status = "full_required"
    else:
        status = "ready_for_promotion" if not results.get("full_passed") else "promoted"
    return {"experiment_id": exp.experiment_id, "status": status, "medium_trigger": medium_trigger, "evidence": results}

class FixtureSetEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture_id: str
    path: str
    fixture_digest: str
    purpose: str
    tags: list[str] = Field(default_factory=list)
    allowed_stages: list[str] = Field(default_factory=list)


class CanonicalFixtureSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["closed_loop.fixture_set.v1"] = "closed_loop.fixture_set.v1"
    set_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    fixtures: list[FixtureSetEntry]
    missing_categories: list[str] = Field(default_factory=list)
    set_digest: str | None = None


def build_fixture_set(root: str | Path, output: str | Path, *, set_id: str = "canonical-replay-v1", missing_categories: list[str] | None = None) -> CanonicalFixtureSet:
    root = Path(root).resolve(); entries=[]
    for path in sorted(root.glob("*.fixture.json")):
        fixture=load_fixture(path)
        entries.append(FixtureSetEntry(fixture_id=fixture.fixture_id,path=str(path),fixture_digest=str(fixture.fixture_digest),purpose=fixture.purpose,tags=fixture.tags,allowed_stages=fixture.allowed_stages))
    result=CanonicalFixtureSet(set_id=set_id,fixtures=entries,missing_categories=missing_categories or [])
    result.set_digest=canonical_digest(result.model_dump(mode="json",exclude={"set_digest"}))
    target=Path(output); target.parent.mkdir(parents=True,exist_ok=True); target.write_text(result.model_dump_json(indent=2)+"\n",encoding="utf-8")
    return result


def validate_fixture_set(path: str | Path) -> CanonicalFixtureSet:
    source=Path(path); result=CanonicalFixtureSet.model_validate_json(source.read_text(encoding="utf-8"))
    for entry in result.fixtures:
        fixture=load_fixture(entry.path)
        if fixture.fixture_id != entry.fixture_id or fixture.fixture_digest != entry.fixture_digest:
            raise ValueError(f"fixture-set binding mismatch: {entry.fixture_id}")
    actual=canonical_digest(result.model_dump(mode="json",exclude={"set_digest"}))
    if actual != result.set_digest: raise ValueError(f"fixture set digest mismatch: expected {result.set_digest}, got {actual}")
    return result
