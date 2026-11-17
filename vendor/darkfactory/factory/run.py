"""Run the factory: spec in, shipped code out, graded on held-out scenarios.

  uv run python -m factory.run --tag pilot --specs splitr --designs oneshot,loop,pce --models haiku --change --cap 3 --resume
"""
import argparse
import datetime as dt
import json
import shutil
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from . import config, gaming, grade, llm
from .agent import Transcript
from .designs import DESIGNS
from .sandbox import Sandbox


def load_runs():
    if not config.RUNS.exists():
        return []
    return [json.loads(x) for x in open(config.RUNS) if x.strip()]


def cell_key(r):
    return (r["tag"], r["spec"], r.get("spec_level", "full"), r["design"], r["model"], r["trial"], r["round"])


def prepare_workspace(run_id, spec, parent=None, level="full"):
    ws = config.RUNTIME / "ws" / run_id
    if ws.exists():
        shutil.rmtree(ws)
    if parent:
        shutil.copytree(config.RAW / parent / "workspace", ws)
        shutil.copy(config.SPECS / spec / "CHANGE.md", ws / "CHANGE.md")
        (ws / "tests").mkdir(exist_ok=True)
        shutil.copy(config.SPECS / spec / "public_v2" / "test_public_v2.py", ws / "tests" / "test_public_v2.py")
    else:
        (ws / "tests").mkdir(parents=True)
        shutil.copy(config.SPECS / spec / ("BRIEF.md" if level == "brief" else "SPEC.md"), ws / "SPEC.md")
        for f in (config.SPECS / spec / "public").glob("*.py"):
            shutil.copy(f, ws / "tests" / f.name)
    for p in [ws, *ws.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    return ws


def one_run(tag, spec, design, model, trial, rnd, parent, backend=None, level="full", run_cap=None, oracle=None):
    run_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{spec}-{design}-{model.split('-')[1] if '-' in model else model}-{level}-r{rnd}-t{trial}"
    raw = config.RAW / run_id
    raw.mkdir(parents=True, exist_ok=True)
    ws = prepare_workspace(run_id, spec, parent, level)
    meter = llm.Meter(tag, run_id)
    transcript = Transcript(raw / "transcript.jsonl")
    rec = {"run_id": run_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "tag": tag, "spec": spec, "spec_level": level,
           "design": design, "model": model, "trial": trial, "round": rnd, "parent_run_id": parent, "error": None}
    t0 = time.time()
    try:
        with Sandbox(ws) as sb:
            ctx = SimpleNamespace(backend=backend or llm.backend_for(model), meter=meter, sandbox=sb, transcript=transcript,
                                  change=rnd == 2, cost_cap=run_cap or config.RUN_COST_CAP.get(model, 1.0),
                                  oracle=oracle or (llm.backend_for(config.ORACLE_MODEL) if design == "ask" else None),
                                  full_spec=(config.SPECS / spec / "SPEC.md").read_text())
            out = DESIGNS[design](ctx)
        rec["factory"] = {"iterations": out["iterations"], "eval_rounds": out.get("eval_rounds"),
                          "eval_verdicts": out.get("eval_verdicts"), "evaluator_touched_src": out.get("evaluator_touched_src"),
                          "questions": out.get("questions"),
                          "stages": {k: {x: v[x] for x in ("turns", "tool_calls", "stopped_by", "last_pytest")}
                                     for k, v in out["stages"].items()}}
    except Exception as e:  # API failure, Docker failure: keep the record, do not count the cell as done
        rec["error"] = f"{type(e).__name__}: {e}"
        transcript.log(stage="harness", role="error", error=traceback.format_exc())
    rec["elapsed_s"] = round(time.time() - t0, 1)
    rec["usage"] = {"model_calls": meter.calls, "cost_usd": round(meter.cost, 6), "stages": meter.stages}
    shipped = raw / "workspace"
    if shipped.exists():
        shutil.rmtree(shipped)
    shutil.copytree(ws, shipped, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.rmtree(ws, ignore_errors=True)
    if rec["error"] is None:
        t1 = time.time()
        rec["grade"] = grade.grade(spec, shipped, change=rnd == 2)
        rec["gaming"] = gaming.check(spec, shipped, rnd == 2, raw / "transcript.jsonl")
        rec["code"] = gaming.loc(shipped)
        rec["grade_s"] = round(time.time() - t1, 1)
        g = rec["grade"]
        rec["gap"] = round(g["public"]["rate"] - g["holdout"]["rate"], 4)
        if rnd == 2:   # survival: of the held-out scenarios the parent codebase passed, how many still pass after the change
            prec = next(r for r in load_runs() if r["run_id"] == parent)
            base = [n for n in prec["grade"]["holdout"]["names"] if n not in prec["grade"]["holdout"]["failed"]
                    and n in g["regression"]["names"]]
            kept = [n for n in base if n not in g["regression"]["failed"]]
            rec["survival"] = {"kept": len(kept), "base": len(base), "broken": sorted(set(base) - set(kept))}
    with open(config.RUNS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def summary_line(r):
    if r["error"]:
        return f"  ERROR {r['error'][:150]}"
    g = r["grade"]
    extra = ""
    if "by_class" in g["holdout"]:
        extra = " [" + " ".join(f"{k} {v['passed']}/{v['total']}" for k, v in sorted(g["holdout"]["by_class"].items(), reverse=True)) + "]"
    if r["factory"].get("questions") is not None:
        extra += f" questions={r['factory']['questions']}"
    if r["round"] == 2:
        extra += f" survival {r['survival']['kept']}/{r['survival']['base']}"
        extra += f" regression {g['regression']['passed']}/{g['regression']['total']} change {g['change']['passed']}/{g['change']['total']}"
    return (f"  public {g['public']['passed']}/{g['public']['total']} holdout {g['holdout']['passed']}/{g['holdout']['total']}"
            f"{extra} gamed={r['gaming']['gamed']} ${r['usage']['cost_usd']:.3f} {r['elapsed_s']:.0f}s iters={r['factory']['iterations']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--specs", default=",".join(config.SPEC_NAMES))
    ap.add_argument("--designs", default=",".join(config.DESIGNS))
    ap.add_argument("--models", default="haiku")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--change", action="store_true", help="also run the change round on every shipped codebase")
    ap.add_argument("--cap", type=float, required=True, help="hard spend cap in USD for this tag")
    ap.add_argument("--spec-level", choices=["full", "brief"], default="full",
                    help="full: the precise SPEC.md. brief: the short ticket (BRIEF.md); holdout still checks the full intent")
    ap.add_argument("--run-cap", type=float, help="per-run cost cap in USD (default: per model, see config)")
    ap.add_argument("--resume", action="store_true", help="skip cells that already have a finished record")
    a = ap.parse_args()
    models = [config.MODEL_ALIASES.get(m, m) for m in a.models.split(",")]
    done = {cell_key(r): r for r in load_runs() if r["error"] is None} if a.resume else {}
    cells = [(s, d, m, t) for t in range(1, a.trials + 1) for m in models for s in a.specs.split(",") for d in a.designs.split(",")]
    for spec, design, model, trial in cells:
        parent = None
        for rnd in (1, 2) if a.change else (1,):
            key = (a.tag, spec, a.spec_level, design, model, trial, rnd)
            if key in done:
                print(f"skip  {spec} {design} {model} t{trial} r{rnd} (done)")
                parent = done[key]["run_id"]
                continue
            spent = llm.spent(a.tag)
            if spent + (a.run_cap or config.RUN_COST_CAP.get(model, 1.0)) > a.cap:
                print(f"STOP: spent ${spent:.2f} of ${a.cap:.2f} cap; the next run could exceed it. Re-run with --resume to continue.")
                return
            if rnd == 2 and parent is None:
                break
            print(f"run   {spec}/{a.spec_level} {design} {model} t{trial} r{rnd}  (spent ${spent:.2f})", flush=True)
            rec = one_run(a.tag, spec, design, model, trial, rnd, parent, level=a.spec_level, run_cap=a.run_cap)
            print(summary_line(rec), flush=True)
            parent = rec["run_id"] if rec["error"] is None else None
    print(f"finished. spent ${llm.spent(a.tag):.2f} on tag {a.tag}")


if __name__ == "__main__":
    main()
