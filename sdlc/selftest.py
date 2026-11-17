"""Self-test: proves the graders and metrics measure what they claim. No model calls (SDLC_OFFLINE is set).

  uv run python -m sdlc.selftest

Needs Docker images dfl-sandbox (vendor/darkfactory/sandbox) and sdlc-mut (sandbox/Dockerfile.mut), and the mutants from
`uv run python -m sdlc.mutate generate`. Writes only to a temporary directory.
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

os.environ["SDLC_OFFLINE"] = "1"

from . import c1, c3, c4, df, dup, llm, metrics, mutate, paths, speckit  # noqa: E402
from factory.llm import ScriptedBackend  # noqa: E402  (vendored dark factory)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  (' + str(detail) + ')' if detail and not cond else ''}")


CLONE_SRC = '''
def total_a(rows):
    out = 0
    for row in rows:
        if row["kind"] == "payment" and row["amount"] > 0:
            out += row["amount"] * 100
        elif row["kind"] == "refund":
            out -= row["amount"] * 100
        else:
            raise ValueError("bad kind " + row["kind"])
    return out // 100


def total_b(items):
    acc = 0
    for it in items:
        if it["type"] == "charge" and it["value"] > 0:
            acc += it["value"] * 100
        elif it["type"] == "credit":
            acc -= it["value"] * 100
        else:
            raise ValueError("bad type " + it["type"])
    return acc // 100
'''
DISTINCT_SRC = '''
def mean(xs):
    return sum(xs) / len(xs)


def parse(line):
    name, _, value = line.partition("=")
    return name.strip(), int(value)


class Box:
    def __init__(self, w, h):
        self.w, self.h = w, h

    def area(self):
        return self.w * self.h

    def perimeter(self):
        return 2 * (self.w + self.h)
'''
SMELLY_SRC = '''import os


def f(a, b, unused):
    try:
        x = int(a)
    except Exception:
        x = 0
    if x == 1:
        return 1
    elif x == 2:
        return 2
    elif x == 3:
        return 3
    elif x == 4:
        return 4
    elif x == 5:
        return 5
    elif x == 6:
        return 6
    elif x == 7:
        return 7
    elif x == 8:
        return 8
    elif x == 9:
        return 9
    elif x == 10:
        return b
    return x
'''


def test_c3(tmp):
    d = dup.duplication(CLONE_SRC)
    check("dup: a renamed copy-pasted function is found", d["clones"] >= 1 and d["dup_ratio"] > 0.8, d)
    d = dup.duplication(DISTINCT_SRC)
    check("dup: distinct functions are not reported", d["clones"] == 0 and d["dup_lines"] == 0, d)
    f = tmp / "smelly.py"
    f.write_text(SMELLY_SRC)
    m = metrics.analyze(f)
    check("ruff: unused import, unused argument, blind except, too complex are found",
          {"F401", "ARG001", "BLE001", "C901"} <= set(m["ruff"]), m["ruff"])
    check("pylint: broad-exception-caught and too-many-return-statements are found",
          {"broad-exception-caught", "too-many-return-statements"} <= set(m["pylint"]), m["pylint"])
    check("radon: cyclomatic complexity of the branchy function is 12 (1 + try handler + 10 elifs)", m["cc_max"] == 12, m["cc_max"])
    f2 = tmp / "clean.py"
    f2.write_text(DISTINCT_SRC)
    m2 = metrics.analyze(f2)
    check("clean file has no ruff or pylint findings and lower complexity",
          m2["ruff_total"] == 0 and m2["pylint_total"] == 0 and m2["cc_max"] < m["cc_max"], (m2["ruff"], m2["pylint"]))
    rho, _ = c3.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50], perms=0)
    rho2, _ = c3.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1], perms=0)
    rho3, p3 = c3.spearman(list(range(30)), [(i * 7) % 30 for i in range(30)], perms=2000)
    check("spearman: +1 for monotone, -1 for reversed, near 0 / large p for a scramble",
          abs(rho - 1) < 1e-9 and abs(rho2 + 1) < 1e-9 and abs(rho3) < 0.4 and p3 > 0.05, (rho, rho2, rho3, p3))
    check("spearman: ties get average ranks", c3.ranks([3, 1, 3, 2]) == [3.5, 1.0, 3.5, 2.0], c3.ranks([3, 1, 3, 2]))


def test_graders():
    for spec in df.SPEC_NAMES:
        g = df.grade.grade(spec, df.config.REFERENCE / spec / "v1")
        check(f"grader: reference {spec} passes every public and held-out scenario",
              g["public"]["rate"] == 1.0 and g["holdout"]["rate"] == 1.0, (g["public"]["passed"], g["holdout"]["passed"]))
    g = df.grade.grade("splitr", df.config.ROOT / "selftest_fixtures" / "splitr_cheat")
    check("grader: hard-coded splitr passes the public examples and fails held-out scenarios",
          g["public"]["rate"] == 1.0 and g["holdout"]["rate"] < 0.5, (g["public"], g["holdout"]["passed"]))
    tmp = Path(tempfile.mkdtemp(dir=paths.RUNTIME))
    (tmp / "splitr.py").write_text("import sys\nsys.exit(0)\n")
    g = df.grade.grade("splitr", tmp)
    check("grader: an empty product fails", g["holdout"]["passed"] < g["holdout"]["total"] / 2, g["holdout"]["passed"])
    shutil.rmtree(tmp)


def test_speckit():
    bad = []
    for cmd in ("specify", "plan", "tasks", "implement", "clarify", "analyze"):
        t = speckit.render(cmd, "ARGS-SENTINEL")
        if re.search(r"\{SCRIPT\}|\{ARGS\}|\$ARGUMENTS|__SPECKIT_COMMAND_|^scripts:", t, re.M) or ".specify/.specify" in t:
            bad.append(cmd)
        if cmd in ("specify", "plan") and "ARGS-SENTINEL" not in t:
            bad.append(cmd + ":args")
    check("speckit: commands render with no placeholders left and arguments inserted", not bad, bad)
    ws = c1.prepare("selftest-speckit", "splitr", "speckit")
    with df.Sandbox(ws) as sb:
        sb.exec("mkdir -p specs/001-x && echo '{\"feature_directory\":\"specs/001-x\"}' > .specify/feature.json && echo s > specs/001-x/spec.md")
        code, out = sb.exec(".specify/scripts/bash/setup-plan.sh --json")
        code2, out2 = sb.exec(".specify/scripts/bash/resolve-template.sh spec-template --json")
    ok = code == 0 and '"IMPL_PLAN"' in out and code2 == 0 and "Feature Specification" in out2
    check("speckit: its scripts run in the sandbox without git", ok, (code, out[-200:], code2))
    shutil.rmtree(ws, ignore_errors=True)


def test_llm(tmp):
    hist = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                              {"type": "tool_use", "id": "c1", "name": "run", "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py"}]}]
    o = llm.to_openai_messages("sys", hist)
    ok = (o[0] == {"role": "system", "content": "sys"} and o[2]["tool_calls"][0]["function"]["name"] == "run"
          and json.loads(o[2]["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}
          and o[3] == {"role": "tool", "tool_call_id": "c1", "content": "a.py"})
    check("llm: Anthropic-style history translates to chat-completions messages", ok, o)
    r = llm.responses_items(hist[2])
    check("llm: a tool result becomes a Responses function_call_output", r == [{"type": "function_call_output", "call_id": "c1", "output": "a.py"}], r)
    old = llm.SPEND
    llm.SPEND = tmp / "spend.jsonl"
    try:
        m = llm.Meter("t1", "r", None)
        m.add("s", "claude-haiku-5-5", {"input_tokens": 50_000, "output_tokens": 10_000})
        check("llm: cost uses list price (50k in + 10k out on Haiku = $0.010)", abs(llm.spent("t1") - 0.010) < 1e-9, llm.spent("t1"))
        m.add("s", "claude-haiku-5-5", {"input_tokens": 50_000, "output_tokens": 0, "cache_read_input_tokens": 60_000})
        check("llm: Haiku prompts above 100k tokens bill at 5x", abs(m.stages["s"]["cost_usd"] - (0.010 + 5 * (0.005 + 0.0006))) < 1e-9,
              m.stages["s"]["cost_usd"])
        try:
            llm.Budget(0.03, ["t1"]).check()
            raised = False
        except llm.CapReached:
            raised = True
        check("llm: the budget refuses a call once the cap is reached", raised)
        check("llm: other tags do not count against a budget", llm.Budget(0.03, ["other"]).check() == 0.0)
    finally:
        llm.SPEND = old
    try:
        llm.backend_for("haiku")
        offline = False
    except RuntimeError:
        offline = True
    check("llm: offline mode blocks every real backend", offline)


def scripted_product(spec, files):
    """A scripted 'model' that writes the given files then stops."""
    return ScriptedBackend([[("tool", "write_file", {"path": p, "content": c}) for p, c in files.items()], [("text", "done")]])


def test_c1_pipeline(tmp):
    c1.OUT, c1.RUNS, c1.RAW, c1.CORPUS_C1 = tmp / "c1", tmp / "c1" / "runs.jsonl", tmp / "raw", tmp / "corpus"
    ref = (df.config.REFERENCE / "splitr" / "v1" / "splitr.py").read_text()
    budget = llm.Budget(1.0, ["selftest"])
    turns = {
        "none": [[("tool", "write_file", {"path": "splitr.py", "content": ref})], [("text", "done")]],
        "plan": [[("tool", "write_file", {"path": "plan.md", "content": "one two three four five"})], [("text", "done")],
                 [("tool", "write_file", {"path": "splitr.py", "content": ref})], [("text", "done")]],
        "speckit": [[("tool", "run", {"command": "mkdir -p specs/001-splitr"}),
                     ("tool", "write_file", {"path": "specs/001-splitr/spec.md", "content": "a b c\nd e"})], [("text", "done")],
                    [("tool", "write_file", {"path": "specs/001-splitr/plan.md", "content": "f g"})], [("text", "done")],
                    [("tool", "write_file", {"path": "specs/001-splitr/tasks.md", "content": "- [ ] h"})], [("text", "done")],
                    [("tool", "write_file", {"path": "splitr.py", "content": ref})], [("text", "done")]],
        "kiro": [[("tool", "write_file", {"path": ".kiro/specs/splitr/requirements.md", "content": "WHEN x THE SYSTEM SHALL y"})],
                 [("text", "done")], [("text", "skip design")], [("text", "skip tasks")],
                 [("tool", "write_file", {"path": "splitr.py", "content": "print('broken')\n"})], [("text", "done")]],
    }
    words = {"none": 0, "plan": 5, "speckit": 11, "kiro": 6}   # whitespace-separated tokens, "- [ ] h" is 4
    for cond, t in turns.items():
        rec = c1.one_run("selftest", "splitr", cond, "scripted", 1, budget, backend=ScriptedBackend(t))
        good = cond != "kiro"
        ok = (rec["error"] is None and rec["spec_docs"]["words"] == words[cond]
              and (rec["grade"]["holdout"]["rate"] == 1.0) == good and set(rec["phases"]) == set(
                  {"none": ["coder"], "plan": ["planner", "coder"], "speckit": ["specify", "plan", "tasks", "implement"],
                   "kiro": ["requirements", "design", "tasks", "implement"]}[cond]))
        check(f"c1: '{cond}' runs every phase, counts {words[cond]} spec words, grades {'pass' if good else 'fail'}", ok,
              (rec["error"], rec["spec_docs"]["words"], rec["grade"]["holdout"]["rate"] if not rec["error"] else None, list(rec["phases"])))
    recs = [json.loads(x) for x in open(c1.RUNS)]
    check("c1: protocol check passes when every phase leaves its document, fails when kiro skips design and tasks",
          [r["protocol"]["followed"] for r in recs] == [True, True, True, False]
          and recs[3]["protocol"]["missing"] == ["design:.kiro/specs/*/design.md", "tasks:.kiro/specs/*/tasks.md"],
          [r["protocol"] for r in recs])
    rec = recs[2]
    check("c1: Spec Kit tooling under .specify/ is not counted as a spec document",
          all(not f["path"].startswith(".specify") for f in rec["spec_docs"]["list"]) and rec["spec_docs"]["files"] == 3,
          rec["spec_docs"]["list"])
    rec = c1.one_run("selftest", "splitr", "speckit", "scripted", 2, budget, backend=ScriptedBackend(
        [[("tool", "write_file", {"path": "splitr.py", "content": ref})], [("text", "skipped the spec")]] + [[("text", "x")]] * 6))
    check("c1: writing the product during a document phase is caught", rec["protocol"]["product_before_code"]
          and not rec["protocol"]["followed"] and rec["spec_docs"]["files"] == 0, rec.get("protocol"))
    g = c1.own_tests_are_not_gaming({"tests_modified": [], "tests_deleted": [], "test_sniffing": ["dev_tests/test_cli.py", "splitr.py"],
                                     "hardcoded_literals": [{"file": "dev_tests/test_x.py", "literals": ["Ann:1"]}]})
    check("c1: the agent's own test files are not gaming, product files still are",
          g["gamed"] and g["test_sniffing"] == ["splitr.py"] and not g["hardcoded_literals"]
          and g["own_test_files"] == ["dev_tests/test_cli.py", "dev_tests/test_x.py"], g)
    class Capture(ScriptedBackend):
        def complete(self, system, messages, tools):
            self.systems = getattr(self, "systems", []) + [system]
            self.seen = getattr(self, "seen", []) + [messages[-1]["content"]]
            return super().complete(system, messages, tools)
    owner = Capture([[("text", "B")], [("text", "No preference; use your recommendation.")]])
    coder = ScriptedBackend([
        [("tool", "run", {"command": "mkdir -p specs/001-splitr"}),
         ("tool", "write_file", {"path": "specs/001-splitr/spec.md", "content": "spec"})], [("text", "done")],
        [("tool", "read_file", {"path": "specs/001-splitr/spec.md"})],
        [("text", "**Question:** Are names case-insensitive? Why it matters: grouping.")],
        [("text", "**Question:** How are ties ordered?")],
        [("tool", "write_file", {"path": "specs/001-splitr/spec.md",
                                 "content": "spec\n## Clarifications\n- Q: case? -> A: B\n- Q: ties? -> A: default\n"})],
        [("text", "Clarified 2 questions. Next: /speckit.plan")],
        [("tool", "write_file", {"path": "specs/001-splitr/plan.md", "content": "p"})], [("text", "done")],
        [("tool", "write_file", {"path": "specs/001-splitr/tasks.md", "content": "t"})], [("text", "done")],
        [("tool", "write_file", {"path": "splitr.py", "content": ref})], [("text", "done")]])
    rec = c1.one_run("selftest", "splitr", "speckit-clarify", "scripted", 1, budget, backend=coder, owner=owner)
    full = (df.config.SPECS / "splitr" / "SPEC.md").read_text()
    check("c1 clarify: each question goes to the owner (holding the full spec), each answer comes back, the loop ends on a non-question",
          rec["error"] is None and list(rec["phases"]) == ["specify", "clarify", "plan", "tasks", "implement"]
          and [x["answer"] for x in rec["clarify"]["exchanges"]] == ["B", "No preference; use your recommendation."]
          and rec["clarify"]["no_preference"] == 1 and rec["clarify"]["recorded_in_spec"] == 2
          and all(full[:200] in sy for sy in owner.systems) and "How are ties ordered?" in owner.seen[1]
          and rec["protocol"]["followed"] and rec["grade"]["holdout"]["rate"] == 1.0,
          (rec["error"], list(rec.get("phases", {})), rec.get("clarify"), rec.get("protocol")))
    idx = [json.loads(x) for x in open(c1.CORPUS_C1 / "index.jsonl")]
    check("c1: each shipped product is exported to the C3 corpus with its grade", len(idx) == 6 and all("holdout_rate" in r for r in idx))


def test_mutation(tmp):
    spec = "splitr"
    src = (df.config.REFERENCE / spec / "v1" / "splitr.py").read_text()
    mdir = tmp / "mut"
    mdir.mkdir()
    mdir.chmod(0o777)
    # a seeded mutant the held-out suite must catch: names sorted case-sensitively instead of case-insensitively
    seeded = src.replace("for k in sorted(bal)]", "for k in sorted(bal, key=lambda k: names[k])]")
    (mdir / "seeded.py").write_text(seeded)
    (mdir / "equivalent.py").write_text(src + "\n# a comment changes nothing\n")
    (mdir / "crash.py").write_text(src.replace("def main(", "def main_renamed("))
    for p in mdir.iterdir():
        p.chmod(0o666)
    check("mutation: the seeded mutant really changes the code", seeded != src)
    hold = df.config.HOLDOUT / spec / "v1"
    res = mutate.run_suite_on_mutants(spec, [hold / "helpers.py", hold / "test_holdout.py"], ["seeded", "equivalent", "crash"],
                                      mutant_dir=mdir)
    check("mutation: original passes, seeded mutant killed, equivalent survives, crashing mutant killed",
          res["original"]["status"] == "survived" and res["seeded"]["status"] == "killed"
          and res["equivalent"]["status"] == "survived" and res["crash"]["status"] == "killed", res)
    s = mutate.score(res, ["seeded", "equivalent", "crash"])
    check("mutation: score = killed / total", s["valid"] and s["killed"] == 2 and s["total"] == 3 and s["score"] == 0.6667, s)
    man = mutate.load_manifest()
    check("mutation: mutmut produced mutants for every spec", all(man[s]["kept"] > 100 for s in df.SPEC_NAMES),
          {s: man[s]["kept"] for s in man})
    check("mutation: the fixed sample is reproducible", mutate.sample("splitr", 20) == mutate.sample("splitr", 20))


def test_c4_pipeline(tmp):
    c4.OUT, c4.RUNS, c4.SCORES, c4.RAW, c4.SUITES = tmp / "c4", tmp / "c4" / "runs.jsonl", tmp / "c4" / "scores.jsonl", tmp / "raw4", tmp / "suites"
    (tmp / "c4").mkdir()
    good = ('from helpers import run, lines\n\n'
            'def test_sorted_case_insensitive():\n'
            '    code, out, err = run("date,payer,amount,participants,note\\n2026-03-01,bob,30.00,bob;Zoe;adam,x\\n", "balances", "{f}")\n'
            '    assert code == 0\n    assert [l.split()[0] for l in lines(out)] == ["adam", "bob", "Zoe"]\n')
    rec = c4.write_one("selftest", "splitr", "scripted", 1, llm.Budget(1, ["selftest"]),
                       backend=scripted_product("splitr", {"tests/test_agent.py": good}))
    check("c4: the writer pipeline keeps the suite and flags no tampering",
          rec["error"] is None and rec["test_file"] and not rec["helpers_modified"] and not rec["product_modified"]
          and Path(rec["suite_path"]).exists(), rec)
    bb = c4.write_one("selftest", "splitr", "scripted", 1, llm.Budget(1, ["selftest"]), variant="blackbox", backend=ScriptedBackend([
        [("tool", "run", {"command": "grep -c 'def ' splitr.py; python -m pytest tests -q"})],
        [("tool", "run", {"command": "python -c 'import marshal'"})],
        [("tool", "write_file", {"path": "tests/test_agent.py", "content": good})], [("text", "done")]]))
    shipped = c4.RAW / bb["run_id"] / "workspace" / "splitr.py"
    check("c4 black-box: the writer gets a compiled product that passes the public examples, without the source; peeking is flagged",
          bb["error"] is None and "-tests-bb-" in bb["run_id"] and "def " not in shipped.read_text()
          and "passed" in (bb["writer"]["last_pytest"] or "") and "failed" not in bb["writer"]["last_pytest"]
          and bb["peeked"] == ["python -c 'import marshal'"] and not bb["product_modified"] and bb["variant"] == "blackbox",
          (bb.get("error"), bb.get("writer"), bb.get("peeked")))
    pub = df.config.SPECS / "splitr" / "public" / "helpers.py"
    tf = tmp / "t" / "test_agent.py"
    tf.parent.mkdir()
    # a suite with one wrong test (fails on the correct reference) and one vacuous test
    tf.write_text('def test_wrong():\n    assert 1 == 2\n\ndef test_vacuous():\n    assert True\n')
    r = c4.score_suite("splitr", "selftest-vacuous", [pub, tf], "agent")
    check("c4: a test failing on the reference is counted invalid and deselected; a vacuous suite kills nothing",
          r["invalid_tests"] == ["test_agent.py::test_wrong"] and r["valid_tests"] == 1 and r["mutation"]["valid"]
          and r["mutation"]["killed"] == 0, (r["invalid_tests"], r["mutation"]))
    tf.write_text('import pytest\n\nclass TestX:\n    @pytest.mark.parametrize("v", [1, 2])\n    def test_p(self, v):\n        assert v == 1\n')
    r = c4.score_suite("splitr", "selftest-class", [pub, tf], "agent")
    check("c4: node ids of class-based and parametrized tests deselect correctly",
          r["invalid_tests"] == ["test_agent.py::TestX::test_p[2]"] and r["mutation"]["valid"], (r["invalid_tests"], r["mutation"]))
    c4.REALFAULTS = tmp / "c4" / "rf.jsonl"
    hold = df.config.HOLDOUT / "splitr" / "v1"
    rf = c4.score_realfaults("splitr", "selftest-rf", [hold / "helpers.py", hold / "test_holdout.py"])
    rp = c4.score_realfaults("splitr", "selftest-rfp", [pub, df.config.SPECS / "splitr" / "public" / "test_public.py"])
    check("c4 real faults: the held-out suite catches every faulty build and no correct one; the public examples catch none",
          rf["original_ok"] and rf["faulty"] > 10 and rf["faulty_caught"] == rf["faulty"] and rf["false_alarms"] == 0
          and rp["faulty_caught"] == 0 and rp["false_alarms"] == 0, (rf["faulty_caught"], rf["faulty"], rf["false_alarms"], rp["faulty_caught"]))
    ids = mutate.sample("splitr", 40)
    rh = mutate.score(mutate.run_suite_on_mutants("splitr", [hold / "helpers.py", hold / "test_holdout.py"], ids), ids)
    rg = c4.score_suite("splitr", "selftest-good", [pub, Path(rec["suite_path"])], "agent")
    check("c4: a one-test suite kills fewer mutants than the 26-scenario held-out suite (40-mutant sample)",
          0 < rg["mutation"]["killed"] and rh["killed"] > 0, (rg["mutation"]["killed"], rh["killed"]))


def main():
    paths.RUNTIME.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=paths.RUNTIME, prefix="selftest-"))
    tmp.chmod(0o777)
    try:
        test_c3(tmp)
        test_llm(tmp)
        test_graders()
        test_speckit()
        test_c1_pipeline(tmp)
        test_mutation(tmp)
        test_c4_pipeline(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
