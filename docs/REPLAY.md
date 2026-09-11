# Frozen stage replay

The replay CLI is a thin adapter over existing EvalSystem, AgentEval and Benchmark Forge modules. It never starts Harbor or Pi.

## Canonical fixtures

```bash
python -m eval_system.integrations replay fixture-seal draft.json fixture.json
python -m eval_system.integrations replay fixture-validate fixture.json
python -m eval_system.integrations replay fixture-set-build canonical/ fixture-set.json
python -m eval_system.integrations replay fixture-set-validate fixture-set.json
```

## Stage replay

```bash
# Export frozen trial inputs and execute the existing AgentEval CLI.
python -m eval_system.integrations replay agent-eval FIXTURE \
  --output-root run/replay/agent-eval -- \
  eval --cases run/replay/agent-eval/inputs/cases.json \
  --outputs run/replay/agent-eval/inputs/outputs.json \
  --case-package examples.demo.agent_demo \
  --run-root run/replay/agent-eval/result

# A fixture may contain an AgentEval recipe such as calibration_metrics.
python -m eval_system.integrations replay agent-eval CALIBRATION_FIXTURE \
  --output-root run/replay/calibration

python -m eval_system.integrations replay feedback FIXTURE --output run/replay/feedback.jsonl

python -m eval_system.integrations replay kb FIXTURE \
  --output-kb run/replay/kb.sqlite3 \
  --output run/replay/retrieval.json \
  --query 'cross module integration'

python -m eval_system.integrations replay ir FIXTURE --output run/replay/ir.json

python -m eval_system.integrations replay design FIXTURE \
  --output-manifest run/replay/design.json -- \
  --goal 'candidate design' --target-size 1 \
  --artifact-root run/replay/design --source procedural --design-only
```

Use `--plan-only` instead of `--design-only` to stop after Allocation and write the existing `BenchmarkPlanCandidate` projection to `plan.json`.

## Experiments and promotion

A Fast experiment must bind at least one fixture and explicitly forbid Harbor and Pi. If a fixture entry includes `path`, validation loads it and checks its ID and digest.

```bash
python -m eval_system.integrations replay experiment-validate experiment.json
python -m eval_system.integrations replay promote experiment.json results.json --output decision.json
```

Every stage produces `closed_loop.stage_replay.v1` with input/result digests and explicit `harbor_invoked=false`, `pi_invoked=false`. AgentEval also writes `cache_stats.json`; replay manifests include it when the forwarded command has `--run-root`.
