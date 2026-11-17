"""C4: are AI-written tests any good? Agents write tests for the reference implementations; mutation testing scores them
against the public examples and the held-out suites of the dark factory lab.

  uv run python -m sdlc.c4 write --tag c4-pilot --models haiku,terra,gemini --trials 1 --cap 5 --cap-tags c1-pilot,c4-pilot --resume
  uv run python -m sdlc.c4 score --tag c4-pilot          # mutation-test every finished suite (and the baselines), no model calls
  uv run python -m sdlc.c4 report --tag c4-pilot

The writer sees SPEC.md (the full spec), the reference product file and the public examples (tests/helpers.py,
tests/test_public.py), and may run anything in the sandbox. It must write tests/test_agent.py. Scoring uses its
test_agent.py with the pristine helpers.py. Tests that fail on the reference (which passes every held-out scenario) are
counted as invalid and deselected before mutation testing, as a mutation tool requires a green suite.
"""
import argparse
import datetime as dt
import json
import shutil
import subprocess
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from . import df, llm, mutate, paths

OUT = paths.RESULTS / "c4"
RUNS = OUT / "runs.jsonl"
SCORES = OUT / "scores.jsonl"
RAW = paths.RESULTS / "raw" / "c4"
SUITES = paths.ROOT / "suites" / "c4"
MAX_TURNS = 40
RUN_CAP = {"claude-haiku-5-5": 0.50, "claude-sonnet-5-5": 3.00, "claude-opus-5-5": 6.00, "gpt-6.1-terra": 1.50,
           "gpt-6-luna": 0.30, "gemini-3.5-flash-lite": 0.60}

SYSTEM = ("You are a software engineer working in a sandbox. The workspace root is the current directory. Python 3.13, "
          "pytest, standard library only, no network.")
TASK = """The workspace holds a product ({entry}) and its specification (SPEC.md). tests/helpers.py and tests/test_public.py \
show how the product is driven as a black box in tests.

Write a thorough pytest test suite for this product in tests/test_agent.py. The suite will guard the product against \
regressions in future changes: it must pass on the current implementation, which is correct, and it should fail if any \
behaviour that SPEC.md defines is changed or broken. You may import from tests/helpers.py but must not modify it or \
test_public.py; put any extra helpers inside test_agent.py. Run your tests with `python -m pytest tests -q`. When you are \
done, reply with a short summary and no tool call."""


def prepare(run_id, spec):
    ws = paths.RUNTIME / "ws" / run_id
    if ws.exists():
        shutil.rmtree(ws)
    (ws / "tests").mkdir(parents=True)
    shutil.copy(df.config.SPECS / spec / "SPEC.md", ws / "SPEC.md")
    shutil.copy(df.config.REFERENCE / spec / "v1" / df.ENTRY[spec], ws / df.ENTRY[spec])
    for f in (df.config.SPECS / spec / "public").glob("*.py"):
        shutil.copy(f, ws / "tests" / f.name)
    for p in [ws, *ws.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    return ws


def load(path):
    return [json.loads(x) for x in open(path) if x.strip()] if path.exists() else []


def write_one(tag, spec, model, trial, budget, backend=None, run_cap=None):
    short = model.split("-")[1] if model.startswith("claude") else model.replace(".", "")
    run_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{spec}-tests-{short}-t{trial}"
    raw = RAW / run_id
    raw.mkdir(parents=True, exist_ok=True)
    ws = prepare(run_id, spec)
    meter = llm.Meter(tag, run_id, budget)
    transcript = df.agent.Transcript(raw / "transcript.jsonl")
    rec = {"run_id": run_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "tag": tag, "spec": spec,
           "model": model, "family": llm.family(model), "trial": trial, "error": None}
    t0 = time.time()
    info = None
    try:
        with df.Sandbox(ws) as sb:
            b = backend or llm.backend_for(model, budget)
            msgs = [{"role": "user", "content": TASK.format(entry=df.ENTRY[spec])}]
            info = df.agent.agent_loop(b, meter, "writer", sb, SYSTEM, msgs, ["write_file", "read_file", "list_files", "run"],
                                       MAX_TURNS, transcript, run_cap or RUN_CAP.get(model, 1.0))
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        transcript.log(stage="harness", role="error", error=traceback.format_exc())
    rec["elapsed_s"] = round(time.time() - t0, 1)
    rec["writer"] = {x: info[x] for x in ("turns", "tool_calls", "stopped_by", "last_pytest")} if info else None
    rec["usage"] = {"model_calls": meter.calls, "cost_usd": round(meter.cost, 6), "stages": meter.stages}
    shipped = raw / "workspace"
    if shipped.exists():
        shutil.rmtree(shipped)
    shutil.copytree(ws, shipped, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.rmtree(ws, ignore_errors=True)
    tf = shipped / "tests" / "test_agent.py"
    rec["test_file"] = tf.exists()
    if rec["error"] is None and not tf.exists():
        rec["error_kind"] = "no test_agent.py"      # a finished run that produced nothing scores 0, it is not retried
    # keep the suite in the repo (small, and every C4 number is computed from it)
    if tf.exists():
        dst = SUITES / run_id
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy(tf, dst / "test_agent.py")
        rec["suite_path"] = str(dst / "test_agent.py") if not dst.is_relative_to(paths.ROOT) else str((dst / "test_agent.py").relative_to(paths.ROOT))
    rec["helpers_modified"] = (shipped / "tests" / "helpers.py").read_bytes() != (df.config.SPECS / spec / "public" / "helpers.py").read_bytes()
    rec["product_modified"] = (shipped / df.ENTRY[spec]).read_bytes() != (df.config.REFERENCE / spec / "v1" / df.ENTRY[spec]).read_bytes()
    OUT.mkdir(parents=True, exist_ok=True)
    with open(RUNS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def junit_run(spec, suite_files):
    """Run a suite once against the reference with JUnit output. Returns per-test outcomes and the pytest wall time."""
    work = paths.RUNTIME / "c4junit" / uuid.uuid4().hex[:10]
    app, suite, out = work / "app", work / "suite", work / "out"
    for d in (app, suite, out):
        d.mkdir(parents=True)
    shutil.copy(df.config.REFERENCE / spec / "v1" / df.ENTRY[spec], app / df.ENTRY[spec])
    for f in suite_files:
        shutil.copy(f, suite / Path(f).name)
    for p in [work, *work.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    t0 = time.time()
    subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{app}:/app:ro", "-v", f"{suite}:/suite:ro",
                    "-v", f"{out}:/out", "-e", "APP_DIR=/app", "-w", "/suite", mutate.IMAGE, "python", "-m", "pytest", "/suite",
                    "-q", "-p", "no:cacheprovider", "--timeout", "30", "--junitxml", "/out/j.xml"],
                   capture_output=True, text=True, timeout=1800)
    wall = time.time() - t0
    tests = []
    if (out / "j.xml").exists():
        root = ET.parse(out / "j.xml").getroot()
        for c in root.iter("testcase"):
            ok = c.find("failure") is None and c.find("error") is None and c.find("skipped") is None
            parts = (c.get("classname") or "").split(".")   # module, then any test classes (suite files sit flat in /suite)
            nodeid = "::".join([parts[0] + ".py", *parts[1:], c.get("name")]) if parts[0] else c.get("name")
            tests.append({"nodeid": nodeid, "ok": ok, "time": float(c.get("time") or 0)})
        suites = root if root.tag == "testsuite" else root.find("testsuite")
        ptime = float(suites.get("time") or 0) if suites is not None else None
    else:
        ptime = None
    shutil.rmtree(work, ignore_errors=True)
    return {"tests": tests, "pytest_s": ptime, "wall_s": round(wall, 2)}


def baseline_suites(spec):
    pub, hold = df.config.SPECS / spec / "public", df.config.HOLDOUT / spec / "v1"
    return {"public": [pub / "helpers.py", pub / "test_public.py"], "holdout": [hold / "helpers.py", hold / "test_holdout.py"]}


def score_suite(spec, suite_id, files, kind, extra=None):
    j = junit_run(spec, files)
    invalid = [t["nodeid"] for t in j["tests"] if not t["ok"]]
    ids = mutate.sample(spec, None)
    if len(j["tests"]) - len(invalid) == 0:      # nothing runs green on the reference: the suite kills nothing
        s = {"valid": True, "killed": 0, "total": len(ids), "score": 0.0, "note": "no valid tests"}
    else:
        res = mutate.run_suite_on_mutants(spec, files, ids, deselect=invalid)
        s = mutate.score(res, ids)
    rec = {"suite_id": suite_id, "spec": spec, "kind": kind, "tests": len(j["tests"]), "valid_tests": len(j["tests"]) - len(invalid),
           "invalid_tests": invalid, "pytest_s": j["pytest_s"], "wall_s": j["wall_s"], **(extra or {}),
           "mutation": {k: v for k, v in s.items() if k != "status"}, "status": s.get("status")}
    with open(SCORES, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def cmd_score(a):
    done = {r["suite_id"] for r in load(SCORES)}
    for spec in df.SPEC_NAMES:
        for kind, files in baseline_suites(spec).items():
            sid = f"{kind}-{spec}"
            if sid not in done:
                r = score_suite(spec, sid, files, kind)
                print(f"{sid}: {r['valid_tests']} tests, mutation {r['mutation'].get('killed')}/{r['mutation'].get('total')}")
    for r in load(RUNS):
        if r["tag"] != a.tag or r["error"] or r["run_id"] in done:
            continue
        pub = df.config.SPECS / r["spec"] / "public" / "helpers.py"
        extra = {"tag": r["tag"], "model": r["model"], "family": r["family"], "trial": r["trial"], "cost_usd": r["usage"]["cost_usd"]}
        if not r.get("suite_path"):
            rec = {"suite_id": r["run_id"], "spec": r["spec"], "kind": "agent", "tests": 0, "valid_tests": 0, "invalid_tests": [],
                   "pytest_s": None, "wall_s": None, **extra,
                   "mutation": {"valid": True, "killed": 0, "total": len(mutate.sample(r["spec"], None)), "score": 0.0}}
            with open(SCORES, "a") as f:
                f.write(json.dumps(rec) + "\n")
            continue
        rec = score_suite(r["spec"], r["run_id"], [pub, paths.ROOT / r["suite_path"]], "agent", extra)
        m = rec["mutation"]
        print(f"{r['run_id']}: {rec['valid_tests']}/{rec['tests']} valid tests, mutation {m.get('killed')}/{m.get('total')}")


def cmd_write(a):
    tags = a.cap_tags.split(",") if a.cap_tags else [a.tag]
    budget = llm.Budget(a.cap, tags)
    models = [llm.ALIASES.get(m, m) for m in a.models.split(",")]
    done = {(r["tag"], r["spec"], r["model"], r["trial"]) for r in load(RUNS) if r["error"] is None} if a.resume else set()
    for trial in range(a.first_trial, a.first_trial + a.trials):
        for model in models:
            for spec in a.specs.split(","):
                if (a.tag, spec, model, trial) in done:
                    print(f"skip  {spec} {model} t{trial} (done)")
                    continue
                s = llm.spent(tags)
                rc = a.run_cap or RUN_CAP.get(model, 1.0)
                if s + rc > a.cap:
                    print(f"STOP: spent ${s:.2f} of ${a.cap:.2f}; the next run (cap ${rc:.2f}) could exceed it.")
                    return
                print(f"write {spec} {model} t{trial}  (spent ${s:.2f})", flush=True)
                r = write_one(a.tag, spec, model, trial, budget, run_cap=a.run_cap)
                print(f"  {'ERROR ' + r['error'][:200] if r['error'] else ''}file={r['test_file']} last_pytest={(r['writer'] or {}).get('last_pytest')} "
                      f"${r['usage']['cost_usd']:.3f} {r['elapsed_s']:.0f}s calls={r['usage']['model_calls']}", flush=True)
    print(f"finished. spent ${llm.spent(tags):.2f} on {tags}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["write", "score", "report"])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--specs", default=",".join(df.SPEC_NAMES))
    ap.add_argument("--models", default="haiku")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--first-trial", type=int, default=1)
    ap.add_argument("--cap", type=float)
    ap.add_argument("--cap-tags")
    ap.add_argument("--run-cap", type=float)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    if a.cmd == "write":
        if a.cap is None:
            ap.error("--cap is required for write")
        cmd_write(a)
    elif a.cmd == "score":
        cmd_score(a)
    else:
        from .report import c4_report
        c4_report(a.tag)


if __name__ == "__main__":
    main()
