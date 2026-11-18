"""C4 summary by writer: real faults caught, failures on holdout-clean builds, and how those failures sort
(results/c4-falsealarms-report.md verdicts). Writes results/c4-by-writer.md."""
import collections, json, re
from . import paths
from .c4 import load, OUT
from . import fasort

def main():
    sc = {r["suite_id"]: r for r in load(OUT / "scores.jsonl") if r["kind"] == "agent" and r.get("tag") == "c4-main"}
    rf = {r["suite_id"]: r for r in load(OUT / "realfaults.jsonl") if r["suite_id"] in sc}
    fa = {r["suite_id"]: r for r in load(OUT / "falsealarms.jsonl")}
    rank = {"real-bug": 4, "ref-deviates": 3, "open": 2, "false-alarm": 1}
    W = collections.defaultdict(lambda: collections.Counter())
    for sid, r in rf.items():
        m = sc[sid]["model"] + (" (black-box)" if "-tests-bb-" in sid else "")
        w = W[m]; w["suites"] += 1; w["caught"] += r["faulty_caught"]; w["faulty"] += r["faulty"]
        w["clean"] += r["correct"]; w["clean_fail"] += r["false_alarms"]
        w["tests"] += sc[sid]["valid_tests"] if "valid_tests" in sc[sid] else 0
        w["killed"] += sc[sid]["mutation"]["killed"]; w["mutants"] += sc[sid]["mutation"]["total"]
        w["all_faulty"] += r["faulty_caught"] == r["faulty"]
        for b, tests in fa.get(sid, {"failed": {}})["failed"].items():
            vs = {c[0] for t in tests if (c := fasort.classify(r["spec"], f"{sid}::{t.split('::')[1]}"))}
            if vs:
                w[max(vs, key=rank.get)] += 1
    L = ["# C4 by writer (tag c4-main)", "", "Pooled over specs. Faulty: brief-ticket dark factory builds that fail a held-out scenario. "
         "Clean: full-spec builds that pass all of them. Verdicts from `results/c4-falsealarms-report.md`; a clean-build failure "
         "that did not reproduce on a re-run is not in any verdict column. Code: `sdlc/c4writers.py`.", "",
         "| writer | suites | mutants killed | faulty builds caught | suites catching every faulty build | clean-build failures | real bug | ref deviates | open | false alarm |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for m in sorted(W):
        w = W[m]
        L.append(f"| {m} | {w['suites']} | {w['killed']}/{w['mutants']} ({w['killed']/w['mutants']:.1%}) | {w['caught']}/{w['faulty']} ({w['caught']/w['faulty']:.1%}) | "
                 f"{w['all_faulty']}/{w['suites']} | {w['clean_fail']}/{w['clean']} ({w['clean_fail']/w['clean']:.0%}) | {w['real-bug']} | {w['ref-deviates']} | {w['open']} | {w['false-alarm']} |")
    (paths.RESULTS / "c4-by-writer.md").write_text("\n".join(L) + "\n"); print("\n".join(L))

if __name__ == "__main__":
    main()
