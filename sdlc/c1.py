"""C1: does a spec make the code better? Brief ticket in, four ways to get to code, graded on the held-out suites.

Conditions (same model, same system prompt, same four tools, same sandbox; only the steps before coding differ):
  none     the coder gets the ticket and the examples and builds (the dark factory `loop` design)
  plan     an agent first writes a free-form plan.md (no template), then a fresh coder builds from it
  speckit  GitHub Spec Kit v1.2.6 commands in order: /speckit.specify, /speckit.plan, /speckit.tasks, /speckit.implement
  kiro     Kiro-style spec: requirements.md (user stories, EARS criteria), design.md, tasks.md, then implement the tasks.
           Kiro itself cannot run headless on an arbitrary model, so this follows its documented document structure
           with prompts written for this lab (sdlc/c1.py KIRO_*).
Every phase starts with a fresh context (as after /clear); documents carry the state, as in both tools' own guidance.
The run is unattended: questions and approval gates get the default answer (stated in the system prompt).

  uv run python -m sdlc.c1 --tag c1-pilot --conditions none,plan,speckit,kiro --models haiku --trials 1 --cap 5 --resume
"""
import argparse
import datetime as dt
import json
import shutil
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from . import df, llm, paths, speckit

OUT = paths.RESULTS / "c1"
RUNS = OUT / "runs.jsonl"
RAW = paths.RESULTS / "raw" / "c1"
CORPUS_C1 = paths.CORPUS / "c1"
CONDITIONS = ["none", "plan", "speckit", "kiro"]
TOOLS = ["write_file", "read_file", "list_files", "run"]
MAX_TURNS = 40
RUN_CAP = {"claude-haiku-5-5": 0.80, "claude-sonnet-5-5": 4.00, "claude-opus-5-5": 8.00, "gpt-6.1-terra": 2.00,
           "gpt-6-luna": 0.40, "gemini-3.5-flash-lite": 1.00}

COMMON = ("The product must be the single file {entry} in the workspace root; the customer's acceptance examples in tests/ "
          "show how it is run (`python -m pytest tests -q`) and are read-only. "
          "This run is unattended: nobody will answer questions or approve steps. Where your instructions say to ask the user, "
          "wait for confirmation or stop for review, take the recommended or default option, note it, and continue.")
# Coding phase, identical in all four conditions: the dark factory's build framing and rules.
SYSTEM_CODE = df.designs.FACTORY + "\n\n" + df.designs.RULES + "\n\n" + COMMON
# Document phases (plan, Spec Kit specify/plan/tasks, Kiro requirements/design/tasks): a neutral engineer framing, so the
# build-and-ship framing does not push the agent to skip the step. The pilot showed it does (see PLAN.md).
SYSTEM_DOCS = ("You are a software engineer working in a repository. The workspace root is the current directory. Python 3.13, "
               "standard library only, no network. Do only what the current instruction asks; later steps will follow it. "
               + COMMON)
KICKOFF = df.designs._coder_kickoff(SimpleNamespace(change=False))   # the dark factory loop design's own kickoff
TECH = ("Python 3.13, standard library only, no third-party packages, no network. The product is the single file {entry} "
        "in the repository root; the customer's acceptance examples in tests/ run it from there "
        "(`python -m pytest tests -q`). No persistent storage beyond what the ticket asks for.")

PLAN_WRITER = ("Read SPEC.md (the customer's ticket) and the examples in tests/. Before any code is written, write plan.md in the "
               "workspace root: how you will build the product, every rule and edge case it must handle, and a decision for "
               "each point the ticket leaves open. Do not write product code. When plan.md is written, reply with a one-line "
               "summary and no tool call.")
PLAN_CODER = KICKOFF.replace("Build it,", "plan.md holds the plan written for this product; follow it. Build it,")

KIRO_DIR = ".kiro/specs/{name}"
KIRO_REQUIREMENTS = """You are running the requirements phase of a spec-driven workflow (Kiro style). SPEC.md is the customer's \
ticket; tests/ holds the customer's acceptance examples. Do not write code.

Write {dir}/requirements.md with:
- an Introduction: what the product is and who uses it;
- numbered Requirements. Each has a User Story ("As a <role>, I want <capability>, so that <benefit>") and numbered \
Acceptance Criteria in EARS notation, one testable behaviour per line, using these patterns:
  - WHEN <trigger> THE SYSTEM SHALL <response>
  - IF <unwanted condition> THEN THE SYSTEM SHALL <response>
  - WHILE <state> THE SYSTEM SHALL <response>
  - WHERE <feature is included> THE SYSTEM SHALL <response>
  - THE SYSTEM SHALL <response>   (always true)
Cover the normal behaviour, every input format, every error case and every boundary. Where the ticket is silent, decide \
and write the decision as a criterion. When the file is written, reply with a one-line summary and no tool call."""
KIRO_DESIGN = """You are running the design phase of a spec-driven workflow (Kiro style). Read SPEC.md, the examples in \
tests/ and {dir}/requirements.md. Do not write product code.

Write {dir}/design.md with these sections: Overview; Architecture; Components and Interfaces; Data Models; Error \
Handling; Testing Strategy. Technical context: {tech} Refer to requirement numbers where a design element serves them. \
When the file is written, reply with a one-line summary and no tool call."""
KIRO_TASKS = """You are running the task-planning phase of a spec-driven workflow (Kiro style). Read {dir}/requirements.md \
and {dir}/design.md. Do not write product code.

Write {dir}/tasks.md: an implementation plan as a numbered checkbox list ("- [ ] 1. ...", sub-tasks "- [ ] 1.1 ..."). Each \
task is one concrete coding step that a developer can do and check, builds on the tasks before it, and ends with a line \
"_Requirements: <numbers>_" naming the acceptance criteria it covers. Include writing and running tests. Every acceptance \
criterion must be covered by at least one task. When the file is written, reply with a one-line summary and no tool call."""
KIRO_IMPLEMENT = """You are running the implementation phase of a spec-driven workflow (Kiro style). The spec is in \
{dir}/: requirements.md, design.md, tasks.md. Execute the tasks in tasks.md in order. After finishing a task, mark it \
done ("- [x]") in tasks.md. Run `python -m pytest tests -q` as you go. When every task is done and the tests pass, reply \
with a short summary and no tool call."""


EXPECT = {  # documents each document phase must leave behind (glob patterns relative to the workspace)
    ("plan", "planner"): ["plan.md"],
    ("speckit", "specify"): ["specs/*/spec.md"], ("speckit", "plan"): ["specs/*/plan.md"], ("speckit", "tasks"): ["specs/*/tasks.md"],
    ("kiro", "requirements"): [".kiro/specs/*/requirements.md"], ("kiro", "design"): [".kiro/specs/*/design.md"],
    ("kiro", "tasks"): [".kiro/specs/*/tasks.md"],
}
CODE_PHASES = {"coder", "implement"}


def phase(ctx, stage, message):
    msgs = [{"role": "user", "content": message}]
    code = stage in CODE_PHASES
    if code:
        ctx.protocol["product_before_code"] = ctx.sandbox.exec(f"test -f {ctx.entry}")[0] == 0
    info = df.agent.agent_loop(ctx.backend, ctx.meter, stage, ctx.sandbox, ctx.system_code if code else ctx.system_docs,
                               msgs, TOOLS, MAX_TURNS, ctx.transcript, ctx.cost_cap)
    ctx.stages[stage] = info
    for pat in EXPECT.get((ctx.condition, stage), []):
        ok = ctx.sandbox.exec(f"ls {pat} >/dev/null 2>&1")[0] == 0
        ctx.protocol["missing"] += [] if ok else [f"{stage}:{pat}"]
    return info


def run_condition(ctx, condition, spec):
    entry = df.ENTRY[spec]
    if condition == "none":
        phase(ctx, "coder", KICKOFF)
    elif condition == "plan":
        phase(ctx, "planner", PLAN_WRITER)
        phase(ctx, "coder", PLAN_CODER)
    elif condition == "speckit":
        brief = (df.config.SPECS / spec / "BRIEF.md").read_text()
        phase(ctx, "specify", speckit.render("specify", brief + "\n\nThe customer's acceptance examples are in tests/."))
        phase(ctx, "plan", speckit.render("plan", TECH.format(entry=entry)))
        phase(ctx, "tasks", speckit.render("tasks", ""))
        phase(ctx, "implement", speckit.render("implement", ""))
    elif condition == "kiro":
        d = KIRO_DIR.format(name=spec)
        phase(ctx, "requirements", KIRO_REQUIREMENTS.format(dir=d))
        phase(ctx, "design", KIRO_DESIGN.format(dir=d, tech=TECH.format(entry=entry)))
        phase(ctx, "tasks", KIRO_TASKS.format(dir=d))
        phase(ctx, "implement", KIRO_IMPLEMENT.format(dir=d))
    else:
        raise ValueError(condition)


SKIP_DOC_DIRS = {"tests", ".specify", "__pycache__", ".pytest_cache", "eval"}


def spec_documents(workspace):
    """Documents a human reviewer would have to read: every non-code text file the run created, except SPEC.md (the
    ticket, given) and the Spec Kit scripts and templates (installed tooling, not output)."""
    files = []
    ws = Path(workspace)
    for p in sorted(ws.rglob("*")):
        rel = p.relative_to(ws)
        if not p.is_file() or rel.parts[0] in SKIP_DOC_DIRS or str(rel) == "SPEC.md" or p.suffix in (".py", ".pyc"):
            continue
        if any(part in ("__pycache__", ".pytest_cache") for part in rel.parts):
            continue
        text = p.read_text(errors="replace")
        files.append({"path": str(rel), "lines": len([x for x in text.splitlines() if x.strip()]), "words": len(text.split()),
                      "needs_clarification": text.count("NEEDS CLARIFICATION")})
    return {"files": len(files), "lines": sum(f["lines"] for f in files), "words": sum(f["words"] for f in files),
            "needs_clarification": sum(f["needs_clarification"] for f in files), "list": files}


def prepare(run_id, spec, condition):
    root = paths.RUNTIME / "ws" / run_id
    if root.exists():
        shutil.rmtree(root)
    (root / "tests").mkdir(parents=True)
    shutil.copy(df.config.SPECS / spec / "BRIEF.md", root / "SPEC.md")
    for f in (df.config.SPECS / spec / "public").glob("*.py"):
        shutil.copy(f, root / "tests" / f.name)
    if condition == "speckit":
        speckit.install(root)
    for p in [root, *root.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else (0o777 if p.suffix == ".sh" else 0o666))
    return root


def is_test_file(path):
    p = Path(path)
    return p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py" \
        or any("test" in part.lower() for part in p.parts[:-1])


def own_tests_are_not_gaming(g):
    """The vendored check only exempts tests/ and eval/. Spec workflows write their own tests elsewhere (dev_tests/ ...),
    which naturally contain example values and import pytest. Those are the agent's tests, not the product: list them
    separately and recompute `gamed` on product files only."""
    g["own_test_files"] = sorted({h["file"] for h in g["hardcoded_literals"] if is_test_file(h["file"])}
                                 | {f for f in g["test_sniffing"] if is_test_file(f)})
    g["hardcoded_literals"] = [h for h in g["hardcoded_literals"] if not is_test_file(h["file"])]
    g["test_sniffing"] = [f for f in g["test_sniffing"] if not is_test_file(f)]
    g["gamed"] = bool(g["tests_modified"] or g["tests_deleted"] or g["hardcoded_literals"] or g["test_sniffing"])
    return g


def load_runs():
    return [json.loads(x) for x in open(RUNS) if x.strip()] if RUNS.exists() else []


def one_run(tag, spec, condition, model, trial, budget, backend=None, run_cap=None):
    short = model.split("-")[1] if model.startswith("claude") else model.replace(".", "")
    run_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{spec}-{condition}-{short}-t{trial}"
    raw = RAW / run_id
    raw.mkdir(parents=True, exist_ok=True)
    ws = prepare(run_id, spec, condition)
    meter = llm.Meter(tag, run_id, budget)
    transcript = df.agent.Transcript(raw / "transcript.jsonl")
    rec = {"run_id": run_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "tag": tag, "spec": spec,
           "spec_level": "brief", "condition": condition, "model": model, "trial": trial, "error": None}
    t0 = time.time()
    ctx = None
    try:
        with df.Sandbox(ws) as sb:
            ctx = SimpleNamespace(backend=backend or llm.backend_for(model, budget), meter=meter, sandbox=sb,
                                  transcript=transcript, cost_cap=run_cap or RUN_CAP.get(model, 1.0), stages={},
                                  system_code=SYSTEM_CODE.format(entry=df.ENTRY[spec]),
                                  system_docs=SYSTEM_DOCS.format(entry=df.ENTRY[spec]), condition=condition,
                                  entry=df.ENTRY[spec], protocol={"missing": [], "product_before_code": False})
            run_condition(ctx, condition, spec)
    except Exception as e:  # API failure, cap reached, Docker failure: keep the record, the cell stays open for --resume
        rec["error"] = f"{type(e).__name__}: {e}"
        transcript.log(stage="harness", role="error", error=traceback.format_exc())
    rec["elapsed_s"] = round(time.time() - t0, 1)
    rec["phases"] = {k: {x: v[x] for x in ("turns", "tool_calls", "stopped_by", "last_pytest")}
                     for k, v in (ctx.stages if ctx else {}).items()}
    if ctx:
        rec["protocol"] = {**ctx.protocol, "followed": not ctx.protocol["missing"] and not ctx.protocol["product_before_code"]}
    rec["usage"] = {"model_calls": meter.calls, "cost_usd": round(meter.cost, 6), "stages": meter.stages}
    shipped = raw / "workspace"
    if shipped.exists():
        shutil.rmtree(shipped)
    shutil.copytree(ws, shipped, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.rmtree(ws, ignore_errors=True)
    rec["spec_docs"] = spec_documents(shipped)
    if rec["error"] is None:
        t1 = time.time()
        rec["grade"] = df.grade.grade(spec, shipped)
        rec["gaming"] = own_tests_are_not_gaming(df.gaming.check(spec, shipped, False, raw / "transcript.jsonl"))
        rec["code"] = df.gaming.loc(shipped)
        rec["grade_s"] = round(time.time() - t1, 1)
        rec["product_present"] = (shipped / df.ENTRY[spec]).exists()
        export_for_c3(rec, shipped)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(RUNS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def export_for_c3(rec, shipped):
    """Copy the shipped product file into corpus/c1/ so C3 analyses C1 output with the same metrics."""
    src = shipped / df.ENTRY[rec["spec"]]
    if not src.exists():
        return
    dst = CORPUS_C1 / rec["run_id"] / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    g = rec["grade"]
    row = {"id": rec["run_id"], "source": f"c1:{rec['tag']}", "spec": rec["spec"], "spec_level": "brief",
           "design": rec["condition"], "model": rec["model"], "trial": rec["trial"], "round": 1, "parent": None,
           "file": str(dst.relative_to(paths.ROOT)) if dst.is_relative_to(paths.ROOT) else str(dst), "holdout_passed": g["holdout"]["passed"],
           "holdout_total": g["holdout"]["total"], "holdout_rate": g["holdout"]["rate"], "by_class": g["holdout"]["by_class"],
           "cost_usd": rec["usage"]["cost_usd"]}
    idx = CORPUS_C1 / "index.jsonl"
    rows = [json.loads(x) for x in open(idx)] if idx.exists() else []
    rows = [r for r in rows if r["id"] != row["id"]] + [row]
    idx.write_text("".join(json.dumps(r) + "\n" for r in rows))


def summary(r):
    if r["error"]:
        return f"  ERROR {r['error'][:200]}"
    g = r["grade"]
    bc = " ".join(f"{k} {v['passed']}/{v['total']}" for k, v in sorted(g["holdout"]["by_class"].items(), reverse=True))
    return (f"  public {g['public']['passed']}/{g['public']['total']} holdout {g['holdout']['passed']}/{g['holdout']['total']} "
            f"[{bc}] docs {r['spec_docs']['files']}f/{r['spec_docs']['words']}w gamed={r['gaming']['gamed']} "
            f"${r['usage']['cost_usd']:.3f} {r['elapsed_s']:.0f}s calls={r['usage']['model_calls']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--specs", default=",".join(df.SPEC_NAMES))
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--models", default="haiku")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--first-trial", type=int, default=1)
    ap.add_argument("--cap", type=float, required=True, help="hard spend cap in USD over --cap-tags (default: this tag)")
    ap.add_argument("--cap-tags", default=None, help="comma-separated tags that share the cap")
    ap.add_argument("--run-cap", type=float)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    tags = a.cap_tags.split(",") if a.cap_tags else [a.tag]
    budget = llm.Budget(a.cap, tags)
    models = [llm.ALIASES.get(m, m) for m in a.models.split(",")]
    done = {(r["tag"], r["spec"], r["condition"], r["model"], r["trial"]) for r in load_runs() if r["error"] is None} if a.resume else set()
    cells = [(s, c, m, t) for t in range(a.first_trial, a.first_trial + a.trials) for m in models
             for s in a.specs.split(",") for c in a.conditions.split(",")]
    for spec, cond, model, trial in cells:
        if (a.tag, spec, cond, model, trial) in done:
            print(f"skip  {spec} {cond} {model} t{trial} (done)")
            continue
        s = llm.spent(tags)
        rc = a.run_cap or RUN_CAP.get(model, 1.0)
        if s + rc > a.cap:
            print(f"STOP: spent ${s:.2f} of ${a.cap:.2f}; the next run (cap ${rc:.2f}) could exceed it. --resume continues.")
            return
        print(f"run   {spec} {cond} {model} t{trial}  (spent ${s:.2f})", flush=True)
        rec = one_run(a.tag, spec, cond, model, trial, budget, run_cap=a.run_cap)
        print(summary(rec), flush=True)
    print(f"finished. spent ${llm.spent(tags):.2f} on {tags}")


if __name__ == "__main__":
    main()
