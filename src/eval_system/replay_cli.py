"""Unified thin CLI for replaying one existing stage at a time."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Any

from .replay import copy_kb_snapshot, load_fixture, seal_fixture, write_stage_manifest, kb_logical_digest, path_digest, canonical_digest, validate_experiment, promotion_decision, build_fixture_set, validate_fixture_set


def _ref(fixture, name: str, required: bool = True) -> Path | None:
    item = fixture.ref(name, required=required)
    return Path(item.path) if item else None


def _calibration_replay(fixture, out: Path) -> dict[str, Any]:
    comparison = _ref(fixture, "human_comparison")
    data = json.loads(comparison.read_text(encoding="utf-8"))
    from agenteval.meta_eval.metrics import score_metrics, stability_metrics
    human = float(data["reference"]["human_mean"])
    schemes = {}
    for name, payload in data.get("schemes", {}).items():
        rounds = payload.get("rounds", [])
        judgments = [{"score": row.get("coordination_score"), "status": row.get("status"), "evidence_refs": []} for row in rounds]
        predicted = [row["score"] for row in judgments if row.get("score") is not None]
        stability = stability_metrics(judgments)
        stability["pairwise_evidence_jaccard"] = None
        stability["exact_claim_identity_agreement"] = None
        metrics = score_metrics(predicted, [human] * len(predicted))
        mean_score = sum(predicted) / len(predicted) if predicted else None
        schemes[name] = {
            "per_round_metrics_to_constant_human_mean": metrics,
            "mean_abs_error_to_human_mean": abs(mean_score - human) if mean_score is not None else None,
            "stability": stability,
        }
    result = {"schema_version":"agenteval.calibration_replay.v1","fixture_id":fixture.fixture_id,"human_reference":human,"schemes":schemes,"source_ref":str(comparison)}
    target=out/"calibration-replay.json"; target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return {"executed":True,"mode":"calibration_metrics","result_ref":str(target),"result_digest":canonical_digest(result)}


def _agent_eval(args) -> int:
    fixture = load_fixture(args.fixture)
    if "agent-eval" not in fixture.allowed_stages: raise SystemExit("fixture forbids agent-eval replay")
    from eval_system.integrations.agent_eval import trial_results_to_agent_eval
    trial = _ref(fixture, "trial_root", False) or _ref(fixture, "trial", False)
    out = Path(args.output_root); out.mkdir(parents=True, exist_ok=True)
    exported = trial_results_to_agent_eval(trial, out / "inputs") if trial else {}
    results: dict[str, Any] = {"exported": {k: str(v) for k,v in exported.items()}, "executed": False}
    recipe = (fixture.bindings.get("replay_recipes") or {}).get("agent-eval") if isinstance(fixture.bindings, dict) else None
    if not args.agenteval_args and isinstance(recipe, dict) and recipe.get("mode") == "calibration_metrics":
        results.update(_calibration_replay(fixture, out))
    if args.agenteval_args:
        forwarded = list(args.agenteval_args)
        if forwarded and forwarded[0] == "--": forwarded.pop(0)
        from agenteval.cli import main as agenteval_main
        results["exit_code"] = agenteval_main(forwarded); results["executed"] = True
        if "--run-root" in forwarded:
            run_root = Path(forwarded[forwarded.index("--run-root") + 1])
            cache_ref = run_root / "cache_stats.json"
            if cache_ref.is_file():
                results["cache_stats_ref"] = str(cache_ref)
                results["cache_stats"] = json.loads(cache_ref.read_text(encoding="utf-8"))
    stage_inputs = {"trial": str(trial) if trial else None, "trial_digest": path_digest(trial) if trial else None, "fixture_input_digest": fixture.input_digest, "recipe": recipe}
    if isinstance(recipe, dict) and recipe.get("source_ref"):
        recipe_ref = _ref(fixture, str(recipe["source_ref"]), False)
        stage_inputs["recipe_source_ref"] = str(recipe_ref) if recipe_ref else None
        stage_inputs["recipe_source_digest"] = path_digest(recipe_ref) if recipe_ref else None
    write_stage_manifest(fixture, "agent-eval", out / "replay-manifest.json", inputs=stage_inputs, results=results)
    return int(results.get("exit_code") or 0)


def _feedback(args) -> int:
    fixture = load_fixture(args.fixture)
    if "feedback" not in fixture.allowed_stages: raise SystemExit("fixture forbids feedback replay")
    from eval_system.integrations.feedback import load_runtime_samples, build_feedback_events, write_feedback_events
    trial = _ref(fixture, "trial_root") or _ref(fixture, "trial")
    summary = _ref(fixture, "evaluation_summary")
    report = json.loads(summary.read_text(encoding="utf-8"))
    events = build_feedback_events(load_runtime_samples(trial), report, report_path=summary, low_threshold=args.low_threshold, high_threshold=args.high_threshold)
    output = Path(args.output); write_feedback_events(output, events)
    write_stage_manifest(fixture, "feedback", output.with_suffix(output.suffix+".replay.json"), inputs={"trial_digest":path_digest(trial),"summary_digest":path_digest(summary)}, results={"feedback_ref":str(output),"event_count":len(events),"feedback_digest":path_digest(output)})
    return 0


def _kb(args) -> int:
    fixture = load_fixture(args.fixture)
    if "kb" not in fixture.allowed_stages: raise SystemExit("fixture forbids kb replay")
    source = _ref(fixture, "kb_snapshot"); target = copy_kb_snapshot(source, args.output_kb)
    from benchmark_forge.octagon.knowledge import OctagonKnowledgeBase, DESIGN_FEEDBACK_SOURCE_KINDS
    kb = OctagonKnowledgeBase(target)
    feedback = Path(args.feedback) if args.feedback else _ref(fixture, "feedback", False)
    indexed = kb.index_feedback_file(feedback, replace=args.replace_feedback) if feedback else 0
    kinds = args.source_kind or DESIGN_FEEDBACK_SOURCE_KINDS
    result = kb.context(args.query, role=args.role, source_kinds=kinds, limit=args.limit)
    output = Path(args.output); output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    write_stage_manifest(fixture,"kb",output.with_suffix(output.suffix+".replay.json"),inputs={"source_kb_byte_digest":path_digest(source),"source_kb_logical_digest":kb_logical_digest(source),"feedback_digest":path_digest(feedback) if feedback else None,"query":args.query,"source_kinds":kinds},results={"indexed":indexed,"retrieval_ref":str(output),"retrieval_digest":canonical_digest(result),"output_kb_byte_digest":path_digest(target),"output_kb_logical_digest":kb_logical_digest(target)})
    return 0


def _design(args) -> int:
    fixture=load_fixture(args.fixture)
    if "design" not in fixture.allowed_stages: raise SystemExit("fixture forbids design replay")
    forwarded=list(args.benchmark_forge_args)
    if forwarded and forwarded[0]=="--": forwarded.pop(0)
    if not any(v in forwarded for v in ("--design-only","--plan-only")): forwarded.append("--design-only")
    from benchmark_forge.cli import main as forge_main
    result=forge_main(forwarded)
    write_stage_manifest(fixture,"design",Path(args.output_manifest),inputs={"forwarded_args":forwarded},results={"exit_code":result or 0})
    return int(result or 0)


def _ir(args) -> int:
    fixture=load_fixture(args.fixture)
    if "ir" not in fixture.allowed_stages: raise SystemExit("fixture forbids ir replay")
    from benchmark_forge.environment_ir import EnvironmentIR, validate_ir_contract_bindings
    from benchmark_forge.domain import ExecutableTaskContract
    ir_path=Path(args.ir) if args.ir else _ref(fixture,"ir")
    ir=EnvironmentIR.model_validate_json(ir_path.read_text(encoding="utf-8"))
    if not ir.frozen or ir.ir_checksum != ir.semantic_checksum(): raise ValueError("IR is not checksum-valid and frozen")
    contract_path=Path(args.contract) if args.contract else _ref(fixture,"contract",False)
    if contract_path:
        contract=ExecutableTaskContract.model_validate_json(contract_path.read_text(encoding="utf-8")); validate_ir_contract_bindings(contract,ir)
    write_stage_manifest(fixture,"ir",args.output,inputs={"ir":str(ir_path),"ir_digest":path_digest(ir_path),"contract":str(contract_path) if contract_path else None,"contract_digest":path_digest(contract_path) if contract_path else None},results={"valid":True,"semantic_checksum":ir.semantic_checksum(),"ir_checksum":ir.ir_checksum})
    return 0


def _promote(args) -> int:
    exp=validate_experiment(args.experiment); results=json.loads(Path(args.results).read_text(encoding="utf-8")); decision=promotion_decision(exp,results); Path(args.output).write_text(json.dumps(decision,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(decision["status"]); return 0


def main(argv: list[str] | None=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    forwarded: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        forwarded, argv = argv[split + 1:], argv[:split]
    p=argparse.ArgumentParser(prog="replay"); sub=p.add_subparsers(dest="command",required=True)
    s=sub.add_parser("fixture-seal"); s.add_argument("draft"); s.add_argument("output"); s.set_defaults(func=lambda a:(seal_fixture(a.draft,a.output),print(a.output),0)[2])
    s=sub.add_parser("fixture-validate"); s.add_argument("fixture"); s.set_defaults(func=lambda a:(load_fixture(a.fixture),print("valid"),0)[2])
    s=sub.add_parser("fixture-set-build"); s.add_argument("root"); s.add_argument("output"); s.add_argument("--set-id",default="canonical-replay-v1"); s.add_argument("--missing-category",action="append",default=[]); s.set_defaults(func=lambda a:(build_fixture_set(a.root,a.output,set_id=a.set_id,missing_categories=a.missing_category),print(a.output),0)[2])
    s=sub.add_parser("fixture-set-validate"); s.add_argument("fixture_set"); s.set_defaults(func=lambda a:(validate_fixture_set(a.fixture_set),print("valid"),0)[2])
    s=sub.add_parser("agent-eval"); s.add_argument("fixture"); s.add_argument("--output-root",required=True); s.set_defaults(func=_agent_eval, agenteval_args=[])
    s=sub.add_parser("feedback"); s.add_argument("fixture"); s.add_argument("--output",required=True); s.add_argument("--low-threshold",type=float,default=.4); s.add_argument("--high-threshold",type=float,default=.9); s.set_defaults(func=_feedback)
    s=sub.add_parser("kb"); s.add_argument("fixture"); s.add_argument("--output-kb",required=True); s.add_argument("--output",required=True); s.add_argument("--query",required=True); s.add_argument("--feedback"); s.add_argument("--replace-feedback",action="store_true"); s.add_argument("--source-kind",action="append"); s.add_argument("--role",default="design"); s.add_argument("--limit",type=int,default=6); s.set_defaults(func=_kb)
    s=sub.add_parser("design"); s.add_argument("fixture"); s.add_argument("--output-manifest",required=True); s.set_defaults(func=_design, benchmark_forge_args=[])
    s=sub.add_parser("ir"); s.add_argument("fixture"); s.add_argument("--ir"); s.add_argument("--contract"); s.add_argument("--output",required=True); s.set_defaults(func=_ir)
    s=sub.add_parser("experiment-validate"); s.add_argument("experiment"); s.set_defaults(func=lambda a:(validate_experiment(a.experiment),print("valid"),0)[2])
    s=sub.add_parser("promote"); s.add_argument("experiment"); s.add_argument("results"); s.add_argument("--output",required=True); s.set_defaults(func=lambda a:_promote(a))
    args = p.parse_args(argv)
    if args.command == "agent-eval": args.agenteval_args = forwarded
    elif args.command == "design": args.benchmark_forge_args = forwarded
    elif forwarded: p.error("forwarded arguments after -- are only valid for agent-eval/design")
    return args.func(args)
