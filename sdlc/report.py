"""Reports for C1 and C4. Every number in them is computed from results/c1/runs.jsonl, results/c4/runs.jsonl and
results/c4/scores.jsonl.

  uv run python -m sdlc.report c1 --tag c1-pilot
  uv run python -m sdlc.report c4 --tag c4-pilot
"""
import argparse
import json
import statistics as st
from collections import defaultdict

from . import paths

CLASSES = ("stated", "implied", "arbitrary")


def load(p):
    return [json.loads(x) for x in open(p) if x.strip()] if p.exists() else []


def pct(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "-"


def med(xs):
    return st.median(xs) if xs else float("nan")


def c1_report(tag):
    runs = [r for r in load(paths.RESULTS / "c1" / "runs.jsonl") if r["tag"] == tag]
    ok = [r for r in runs if r["error"] is None]
    err = [r for r in runs if r["error"]]
    spend = sum(r["usage"]["cost_usd"] for r in runs)
    L = [f"# C1 report for tag `{tag}`", "",
         f"{len(ok)} finished runs, {len(err)} errored (excluded below; listed at the end), spend ${spend:.2f} at list price "
         "(errored runs included). Records: `results/c1/runs.jsonl`. Spec level: the brief ticket for every run.", "",
         "Time per run is wall time while other lab sessions shared the machine and the API limits; it is indicative only.", ""]

    def table(keys, title):
        g = defaultdict(list)
        for r in ok:
            g[tuple(r[k] for k in keys)].append(r)
        out = [f"## {title}", "",
               "| " + " | ".join(keys) + " | runs | public | holdout | holdout range | stated | implied | arbitrary | "
               "spec docs: files | words | lines | cost/run | time/run | calls/run | gamed |",
               "|" + "---|" * (len(keys) + 15)]
        for k in sorted(g):
            rs = g[k]
            pp, pt = sum(r["grade"]["public"]["passed"] for r in rs), sum(r["grade"]["public"]["total"] for r in rs)
            hp, ht = sum(r["grade"]["holdout"]["passed"] for r in rs), sum(r["grade"]["holdout"]["total"] for r in rs)
            rates = [r["grade"]["holdout"]["rate"] for r in rs]
            cls = []
            for c in CLASSES:
                a = sum(r["grade"]["holdout"]["by_class"][c]["passed"] for r in rs)
                b = sum(r["grade"]["holdout"]["by_class"][c]["total"] for r in rs)
                cls.append(pct(a, b))
            d = [r["spec_docs"] for r in rs]
            out.append("| " + " | ".join(str(x) for x in k) + f" | {len(rs)} | {pct(pp, pt)} | {pct(hp, ht)} | "
                       f"{100 * min(rates):.0f}-{100 * max(rates):.0f}% | " + " | ".join(cls) +
                       f" | {med([x['files'] for x in d]):.0f} | {med([x['words'] for x in d]):,.0f} | {med([x['lines'] for x in d]):,.0f} | "
                       f"${st.fmean(r['usage']['cost_usd'] for r in rs):.3f} | {st.fmean(r['elapsed_s'] for r in rs):.0f}s | "
                       f"{st.fmean(r['usage']['model_calls'] for r in rs):.1f} | {sum(r['gaming']['gamed'] for r in rs)} |")
        return out + [""]

    L += table(["model", "condition"], "By model and condition (specs pooled)")
    L += table(["model", "spec", "condition"], "By model, spec and condition")
    L += ["Spec docs: medians over runs of every non-code file the run created (SPEC.md and the installed Spec Kit tooling "
          "excluded). Words are whitespace-separated tokens.", "", "## Per run", "",
          "| run | public | holdout | failed held-out scenarios | docs | phases (turns, stopped by) |", "|---|---|---|---|---|---|"]
    for r in ok:
        g = r["grade"]
        ph = "; ".join(f"{k} {v['turns']} {v['stopped_by']}" for k, v in r["phases"].items())
        L.append(f"| `{r['run_id']}` | {g['public']['passed']}/{g['public']['total']} | {g['holdout']['passed']}/{g['holdout']['total']} | "
                 f"{', '.join(g['holdout']['failed']) or 'none'} | {r['spec_docs']['files']} files, {r['spec_docs']['words']} words | {ph} |")
    if err:
        L += ["", "## Errored runs", ""] + [f"- `{r['run_id']}`: {r['error'][:300]}" for r in err]
    cl = [r for r in ok if r.get("clarify")]
    if cl:
        L += ["", "## Spec Kit clarify: questions answered by the owner model", "",
              "| run | questions | owner said 'no preference' | Q/A lines recorded in spec.md |", "|---|---|---|---|"]
        L += [f"| `{r['run_id']}` | {len(r['clarify']['exchanges'])} | {r['clarify']['no_preference']} | {r['clarify']['recorded_in_spec']} |" for r in cl]
    nf = [r for r in ok if r.get("protocol") and not r["protocol"]["followed"]]
    L += ["", "## Protocol", ""]
    L += [f"- `{r['run_id']}`: missing={r['protocol']['missing']} product_before_code={r['protocol']['product_before_code']}" for r in nf] \
        or ["- every finished run left each phase's document and wrote no product code before its coding phase"]
    gam = [r for r in ok if r["gaming"]["gamed"] or r["gaming"]["tests_added"]]
    L += ["", "## Gaming checks and added tests", ""]
    L += [f"- `{r['run_id']}`: gamed={r['gaming']['gamed']} modified={r['gaming']['tests_modified']} deleted={r['gaming']['tests_deleted']} "
          f"literals={r['gaming']['hardcoded_literals']} sniffing={r['gaming']['test_sniffing']} added={r['gaming']['tests_added']}" for r in gam] or ["- none"]
    out = paths.RESULTS / f"c1-{tag}-report.md"
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out}")


def label(s):
    return s.get("model", "-") + (" (black-box)" if s.get("variant") == "blackbox" else "")


def c4_report(tag):
    scores = load(paths.RESULTS / "c4" / "scores.jsonl")
    runs = [r for r in load(paths.RESULTS / "c4" / "runs.jsonl") if r["tag"] == tag]
    base = {s["suite_id"]: s for s in scores if s["kind"] in ("public", "holdout")}
    agent = {s["suite_id"]: s for s in scores if s["kind"] == "agent" and s.get("tag") == tag}
    L = [f"# C4 report for tag `{tag}`", "",
         f"{len(runs)} test-writing runs ({sum(1 for r in runs if r['error'])} errored), {len(agent)} agent suites scored. "
         f"Writer spend ${sum(r['usage']['cost_usd'] for r in runs):.2f} at list price (Gemini ran on the free tier). "
         "Mutants: every mutmut 3.8.1 mutant of the v1 reference (`results/c4/mutants.json`). Records: `results/c4/runs.jsonl`, "
         "`results/c4/scores.jsonl`; agent suites: `suites/c4/`.", "",
         "Caveat: the public and held-out suites were written by the dark factory lab session (a Claude model, working from the "
         "full spec, iterating against the reference), not by human developers. 'Killable' = killed by at least one suite in "
         "this table (baselines and every agent suite of every tag scored so far); the rest are survivors of every suite, which "
         "includes equivalent mutants and mutants in v2-only code (the reference carries the change request behind a flag).", ""]
    for spec in sorted({s["spec"] for s in scores}):
        allspec = [s for s in scores if s["spec"] == spec and s["mutation"].get("valid") and s.get("status")]
        killable = set()
        for s in allspec:
            killable |= {m for m, v in s["status"].items() if v != "survived"}
        total = next(iter(allspec))["mutation"]["total"] if allspec else 0
        L += [f"## {spec}", "", f"{total} mutants, {len(killable)} killable.", "",
              "| suite | model | tests (valid/total) | killed | score | score on killable | pytest time on reference | writer cost |",
              "|---|---|---|---|---|---|---|---|"]
        rows = [base.get(f"public-{spec}"), base.get(f"holdout-{spec}")] + sorted(
            [s for s in agent.values() if s["spec"] == spec], key=lambda s: (label(s), s["trial"]))
        for s in rows:
            if not s:
                continue
            m = s["mutation"]
            if not m.get("valid"):
                L.append(f"| {s['suite_id']} | {label(s)} | {s['valid_tests']}/{s['tests']} | invalid baseline | | | | |")
                continue
            kk = sum(1 for k, v in (s.get("status") or {}).items() if v != "survived" and k in killable)
            L.append(f"| {s['kind'] if s['kind'] != 'agent' else '`' + s['suite_id'] + '`'} | {label(s)} | "
                     f"{s['valid_tests']}/{s['tests']} | {m['killed']}/{m['total']} | {100 * m['score']:.1f}% | "
                     f"{100 * kk / len(killable):.1f}% | {s['pytest_s'] if s['pytest_s'] is not None else '-'}s | "
                     f"{'$%.3f' % s['cost_usd'] if 'cost_usd' in s else '-'} |")
        hold = base.get(f"holdout-{spec}")
        if hold and hold.get("status"):
            hk = {m for m, v in hold["status"].items() if v != "survived"}
            L += ["", "Overlap with the held-out suite (mutants killed by the agent suite but not the held-out suite, and the reverse):", ""]
            for s in sorted([s for s in agent.values() if s["spec"] == spec and s.get("status")], key=lambda s: (s["model"], s["trial"])):
                ak = {m for m, v in s["status"].items() if v != "survived"}
                L.append(f"- `{s['suite_id']}`: agent only {len(ak - hk)}, held-out only {len(hk - ak)}, both {len(ak & hk)}")
        L.append("")
    L += ["## By model (specs pooled)", "", "| model | suites | valid tests (median) | invalid tests (total) | mutation score (mean) | writer cost (mean) |",
          "|---|---|---|---|---|---|"]
    by = defaultdict(list)
    for s in agent.values():
        by[label(s)].append(s)
    for k, ss in sorted(by.items()):
        L.append(f"| {k} | {len(ss)} | {med([s['valid_tests'] for s in ss]):.0f} | {sum(len(s['invalid_tests']) for s in ss)} | "
                 f"{100 * st.fmean(s['mutation']['score'] for s in ss):.1f}% | ${st.fmean(s['cost_usd'] for s in ss):.3f} |")
    for kind in ("public", "holdout"):
        ss = [s for s in base.values() if s["kind"] == kind and s["mutation"].get("valid")]
        if ss:
            L.append(f"| {kind} suite | {len(ss)} | {med([s['valid_tests'] for s in ss]):.0f} | 0 | "
                     f"{100 * st.fmean(s['mutation']['score'] for s in ss):.1f}% | - |")
    rf = {r["suite_id"]: r for r in load(paths.RESULTS / "c4" / "realfaults.jsonl")}
    if rf:
        L += ["", "## Real faults: the suites against the dark factory's shipped codebases", "",
              "Faulty = brief-ticket builds that fail at least one held-out scenario; holdout-clean = full-spec builds that pass all "
              "of them. A suite catches a faulty build if it fails on it. A failure on a holdout-clean build is either a false "
              "alarm (the test pins behaviour the spec leaves open) or a real bug the held-out suite misses; these need reading "
              "by hand before either word is used: see `results/c4-falsealarms-report.md` for the sorting. "
              "The held-out suite defines 'faulty', so it catches all of them by construction and is shown only as a check; "
              "the public examples pass on every build by construction.", "",
              "| spec | suite | model | faulty caught | fails on holdout-clean builds |", "|---|---|---|---|---|"]
        for sid, r in sorted(rf.items(), key=lambda kv: (kv[1]["spec"], kv[0])):
            s_ = base.get(sid) or agent.get(sid)
            if s_ is None:
                continue
            name = s_["kind"] if s_["kind"] != "agent" else f"`{sid}`"
            L.append(f"| {r['spec']} | {name} | {label(s_)} | {pct(r['faulty_caught'], r['faulty'])} | "
                     f"{pct(r['false_alarms'], r['correct'])} |")
    tamper = [r for r in runs if r.get("helpers_modified") or r.get("product_modified")]
    peek = [r for r in runs if r.get("peeked")]
    if any(r.get("variant") == "blackbox" for r in runs):
        L += ["", "## Black-box writers: commands that look like decompiling the product", ""]
        L += [f"- `{r['run_id']}`: {'; '.join(c[:120] for c in r['peeked'])}" for r in peek] or ["- none"]
    L += ["", "## Tampering and errors", ""]
    L += [f"- `{r['run_id']}`: helpers modified={r.get('helpers_modified')} product modified={r.get('product_modified')} "
          "(scoring uses pristine helpers and the pristine reference regardless)" for r in tamper]
    L += [f"- `{r['run_id']}` ERROR: {r['error'][:300]}" for r in runs if r["error"]]
    L += [f"- `{r['run_id']}`: {r['error_kind']}" for r in runs if r.get("error_kind")]
    if not tamper and not any(r["error"] or r.get("error_kind") for r in runs):
        L.append("- none")
    out = paths.RESULTS / f"c4-{tag}-report.md"
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["c1", "c4"])
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()
    (c1_report if a.which == "c1" else c4_report)(a.tag)


if __name__ == "__main__":
    main()
