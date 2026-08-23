"""从 Harbor 落盘结果（result.json / config.json / lock.json）提取三个 Spec。

纯 stdlib + pydantic，不依赖 harbor 包 —— 这样 contract 层可被任意后端复用。
任务目录（instruction / environment hash）不可访问时降级（空指令 + None hash）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from eval_system.contract.specs import (
    AgentSpec,
    ContentRef,
    EnvironmentSpec,
    ImageRef,
    Resources,
    TaskSpec,
    content_checksum,
    model_checksum,
)

# 与 Harbor environment_content_hash 相同的算法（sha256，截断 32 hex，
# 使 environment_hash 与 Harbor 镜像名 hb__<hex> 可直接对照）。
_CONTENT_HASH_IGNORE = frozenset({".DS_Store", ".git", "__pycache__"})
ENVIRONMENT_HASH_LEN = 32


def environment_content_hash(environment_dir: Path, truncate: int = ENVIRONMENT_HASH_LEN) -> str:
    """复刻 Harbor 的 environment_content_hash：环境目录内容 hash。"""
    candidates: list[tuple[str, Path]] = []
    for path in environment_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(environment_dir)
        if _CONTENT_HASH_IGNORE & set(rel.parts):
            continue
        candidates.append((rel.as_posix(), path))
    if not candidates:
        return hashlib.sha256(environment_dir.name.encode()).hexdigest()[:truncate]
    candidates.sort(key=lambda item: item[0])
    digest = hashlib.sha256()
    for rel_posix, path in candidates:
        data = path.read_bytes()
        digest.update(len(rel_posix.encode()).to_bytes(4, "big"))
        digest.update(rel_posix.encode())
        digest.update(len(data).to_bytes(4, "big"))
        digest.update(data)
    return digest.hexdigest()[:truncate]


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_instruction(task_dir: Path | None) -> str:
    if task_dir is None:
        return ""
    try:
        path = task_dir / "instruction.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _read_task_toml_metadata(task_dir: Path | None) -> dict[str, Any]:
    if task_dir is None:
        return {}
    try:
        import tomllib

        data = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        return data.get("metadata") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _read_task_resources(task_dir: Path | None) -> dict[str, Any]:
    if task_dir is None:
        return {}
    try:
        import tomllib

        data = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        env = data.get("environment") or {}
        return {k: env[k] for k in ("cpus", "memory_mb", "storage_mb", "gpus") if k in env}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# ---------------------------------------------------------------------------
# 提取三个 Spec
# ---------------------------------------------------------------------------
def extract_specs(
    result: dict[str, Any],
    config: dict[str, Any],
    lock: dict[str, Any] | None = None,
    *,
    task_dir: Path | None = None,
) -> dict[str, Any]:
    """从 Harbor trial 的三个 JSON 提取 {task, agent, environment} Spec。"""
    return {
        "task": extract_task_spec(result, config, lock, task_dir=task_dir),
        "agent": extract_agent_spec(result, config),
        "environment": extract_environment_spec(config, lock, task_dir=task_dir),
    }


def extract_task_spec(
    result: dict[str, Any],
    config: dict[str, Any],
    lock: dict[str, Any] | None = None,
    *,
    task_dir: Path | None = None,
) -> TaskSpec:
    lock_task = (lock or {}).get("task") or {}
    cfg_task = config.get("task") or {}

    name = result.get("task_name") or lock_task.get("name") or cfg_task.get("name") or "unknown"
    version = lock_task.get("version") or cfg_task.get("ref") or "0.0.0"
    task_id = f"{name}@{version}"

    if task_dir is None and lock_task.get("path"):
        task_dir = Path(lock_task["path"])
    if task_dir is None and cfg_task.get("path"):
        task_dir = Path(cfg_task["path"])

    instruction = _read_instruction(task_dir)

    # task_checksum：优先 lock.task.digest（Harbor 已算的内容 hash），否则 result
    task_checksum = lock_task.get("digest") or result.get("task_checksum") or ""

    # content_ref：按 lock.task.type / config.task 推断
    content_ref = _build_content_ref(lock_task, cfg_task)

    return TaskSpec(
        task_id=task_id,
        name=name,
        version=version,
        instruction=instruction,
        instruction_checksum=content_checksum(instruction),
        source=result.get("source"),
        content_ref=content_ref,
        task_checksum=task_checksum,
        metadata=_read_task_toml_metadata(task_dir),
        output_schema=None,
    )


def _build_content_ref(lock_task: dict, cfg_task: dict) -> ContentRef | None:
    task_type = lock_task.get("type") or ""
    if task_type == "git" or cfg_task.get("git_url"):
        return ContentRef(
            type="git",
            url=cfg_task.get("git_url") or lock_task.get("git_url"),
            commit=cfg_task.get("git_commit_id") or lock_task.get("commit"),
            path=cfg_task.get("path") or lock_task.get("path"),
        )
    if task_type == "package" or cfg_task.get("name") and not cfg_task.get("path"):
        return ContentRef(
            type="registry",
            name=lock_task.get("name") or cfg_task.get("name"),
            ref=lock_task.get("version") or cfg_task.get("ref"),
        )
    path = cfg_task.get("path") or lock_task.get("path")
    if path:
        return ContentRef(type="path", path=path)
    return None


def extract_agent_spec(result: dict[str, Any], config: dict[str, Any]) -> AgentSpec:
    agent_info = result.get("agent_info") or {}
    model_info = agent_info.get("model_info") or {}
    cfg_agent = config.get("agent") or {}

    name = agent_info.get("name") or cfg_agent.get("name") or "unknown"
    spec = AgentSpec(
        name=name,
        version=agent_info.get("version"),
        model=model_info.get("name") or cfg_agent.get("model_name"),
        provider=model_info.get("provider"),
        config=cfg_agent,
        config_hash="",  # 由下面的 model_checksum 回填
        supports_atif=False,
    )
    spec.config_hash = model_checksum(spec)
    return spec


def extract_environment_spec(
    config: dict[str, Any],
    lock: dict[str, Any] | None = None,
    *,
    task_dir: Path | None = None,
) -> EnvironmentSpec:
    lock_env = (lock or {}).get("environment") or {}
    cfg_env = config.get("environment") or {}
    cfg_task = config.get("task") or {}

    env_type = lock_env.get("type") or cfg_env.get("type") or "docker"

    # image_ref：优先环境目录 Dockerfile，其次声明的 docker_image
    image_ref: ImageRef | None = None
    if task_dir is not None and (task_dir / "environment" / "Dockerfile").is_file():
        image_ref = ImageRef(type="dockerfile", path=str(task_dir / "environment"))
    elif cfg_env.get("docker_image") or cfg_task.get("docker_image"):
        image_ref = ImageRef(
            type="image", tag=cfg_env.get("docker_image") or cfg_task.get("docker_image")
        )

    resources = Resources(**_read_task_resources(task_dir))
    if cfg_env.get("timeout_sec"):
        resources.timeout_sec = float(cfg_env["timeout_sec"])

    environment_hash = None
    if task_dir is not None and (task_dir / "environment").is_dir():
        environment_hash = "sha256:" + environment_content_hash(task_dir / "environment")

    return EnvironmentSpec(
        type=env_type,
        image_ref=image_ref,
        resources=resources,
        mcp_servers=list(cfg_env.get("mcp_servers") or []),
        environment_hash=environment_hash,
    )
